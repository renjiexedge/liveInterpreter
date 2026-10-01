"""SessionManager: runs every translation direction of a session on ONE background
thread with ONE asyncio loop (Qt's event loop isn't asyncio, see CLAUDE.md).

Qt-agnostic: it reports through plain callbacks, which app.py points at Qt
Signal.emit (safe to call from this thread). Works on a list of DirectionConfig,
so the one-way app passes one direction and two-way passes two.

State flow (guide/two_way_plan.md §4.4):
    IDLE -> CONNECTING -> TRANSLATING [-> DEGRADED] -> STOPPING -> IDLE
Start is atomic: TRANSLATING only once every direction has connected; if any
direction fails, Stop is pressed, or CONNECT_TIMEOUT_S passes first, all of them
are torn down. Until then no direction sends audio (see GatedAudio), so a direction
that connects first can't translate or play anything that a failed start would
then cut off. Once translating, one direction ending doesn't cancel the others
(DEGRADED), except a sign-in or quota failure, which stops the whole session
(spec §8.2/F10: no silent fallback). ERROR is reported just before the final IDLE
when the session ended because of a failure; its message says why.
"""

import asyncio
import enum
import logging
import threading
import uuid
from collections.abc import Callable

import sounddevice as sd
from websockets.exceptions import ConnectionClosed, InvalidHandshake

from audio.devices import DeviceNotFoundError
from audio.health import HealthMonitor, Issue, IssueCode
from audio.pipeline import build_audio_pipeline
from config.session_config import DIRECTION_NAMES, DirectionConfig
from engines.gemini_live import run_session

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_S = 15  # covers opening the capture device and the Live connection
STOP_GRACE_S = 5        # how long a direction gets to drain after Stop before it's cancelled


class State(enum.Enum):
    IDLE = "idle"
    CONNECTING = "connecting"
    TRANSLATING = "translating"
    DEGRADED = "degraded"
    STOPPING = "stopping"
    ERROR = "error"
    # READY / RECONNECTING are Phase 6.


class DirectionState(enum.Enum):
    CONNECTING = "connecting"
    CONNECTED = "connected"
    STOPPED = "stopped"
    FAILED = "failed"


StateCallback = Callable[[State, str], None]                          # (state, message)
DirectionStateCallback = Callable[[str, DirectionState, str], None]   # (label, state, message)
TranscriptCallback = Callable[[str, dict[str, str]], None]            # (label, {"input", "output"})
IssueCallback = Callable[[Issue], None]


class GatedAudio:
    """Wraps a JitterBuffer for run_session's audio_queue contract, but discards
    captured audio until `gate` is set (every direction connected). Discarding
    rather than buffering: audio from before the start would be stale by then.
    Draining the buffer while closed also keeps it from overflowing, so the
    health monitor doesn't report dropped audio. The None end-of-stream sentinel
    always passes, so Stop still works while the gate is closed."""

    def __init__(self, source, gate: asyncio.Event):
        self._source = source
        self._gate = gate

    async def get(self) -> bytes | None:
        while True:
            chunk = await self._source.get()
            if chunk is None or self._gate.is_set():
                return chunk


# Failures that would hit every direction alike: stop the whole session.
FATAL_CODES = frozenset({IssueCode.SERVICE_AUTH_FAILED, IssueCode.QUOTA_EXCEEDED})


def classify_failure(label: str, exc: BaseException) -> Issue:
    """The spec §8.2 issue for a direction's failure. The Live API reports most
    service errors as google.genai.errors.APIError carrying a WebSocket close code
    and the server's reason text (an invalid key is code 1007, "API key not
    valid..."), so they're told apart by status code and text. A dropped
    connection raises websockets' ConnectionClosed (on send), APIError 1006
    (abnormal closure, on receive), or an OSError while connecting."""
    if isinstance(exc, (DeviceNotFoundError, sd.PortAudioError)):
        return Issue(label, IssueCode.SESSION_FAILED, fields={
            "detail": "an audio device couldn't be opened. It may be unplugged or in use."})
    code = getattr(exc, "code", None)
    text = str(exc).casefold()
    if code in (401, 403) or any(word in text for word in ("api key", "permission_denied", "unauthenticated")):
        diagnostic_id = uuid.uuid4().hex[:8].upper()
        logger.error("[%s] Translation service sign-in failed (diagnostic ID %s): %s", label, diagnostic_id, exc)
        return Issue(label, IssueCode.SERVICE_AUTH_FAILED, fields={"id": diagnostic_id})
    if code == 429 or any(word in text for word in ("quota", "resource_exhausted", "rate limit")):
        return Issue(label, IssueCode.QUOTA_EXCEEDED)
    if isinstance(exc, (OSError, ConnectionClosed, InvalidHandshake)) or code == 1006:
        return Issue(label, IssueCode.NETWORK_LOST)
    return Issue(label, IssueCode.SESSION_FAILED, fields={"detail": str(exc) or type(exc).__name__})


def describe_error(exc: BaseException) -> str:
    """Staff-friendly text for a failure (the status line)."""
    return classify_failure("", exc).message


class SessionManager:
    def __init__(
        self,
        on_state: StateCallback,
        on_direction_state: DirectionStateCallback,
        on_transcript: TranscriptCallback,
        on_issue: IssueCallback,
    ):
        self._on_state = on_state
        self._on_direction_state = on_direction_state
        self._on_transcript = on_transcript
        self._on_issue = on_issue
        self._thread: threading.Thread | None = None
        # Set on the session thread; read by stop()/resume() from the GUI thread.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._monitor: HealthMonitor | None = None
        self._stop_requested = False
        # Per-direction Mute buttons. The wanted state outlives sessions, so a mute
        # set before Start (or before a player exists) applies once it does.
        self._mute_wanted: dict[str, bool] = {}
        self._players: dict[str, object] = {}
        self._failures: dict[str, Issue] = {}

    # ---- GUI-thread API -------------------------------------------------
    @property
    def running(self) -> bool:
        return self._thread is not None

    def start(self, directions: list[DirectionConfig]) -> bool:
        """Start a session for these directions. Returns False if one is already running."""
        if self._thread is not None:
            return False
        self._stop_requested = False
        self._thread = threading.Thread(target=self._thread_main, args=(list(directions),), daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        """Thread-safe. Routers are only ever stopped by the session thread, so the
        GUI never touches an object that thread owns. A Stop pressed before the
        loop exists is remembered and honoured as soon as it does."""
        self._stop_requested = True
        monitor = self._monitor
        if monitor is not None:
            monitor.stop_watching()  # a deliberate Stop isn't a disconnect
        loop, stop_event = self._loop, self._stop_event
        if loop is not None and stop_event is not None:
            try:
                loop.call_soon_threadsafe(stop_event.set)
            except RuntimeError:
                pass  # the loop closed in the meantime: the session is already ending

    def set_muted(self, label: str, muted: bool) -> None:
        """Mute button for one direction's translated audio (spec §4.2). Thread-safe:
        AudioPlayer.user_muted only flips a flag and clears its buffer under a lock.
        Capture and translation keep running, so transcripts continue."""
        self._mute_wanted[label] = muted
        player = self._players.get(label)
        if player is not None:
            player.user_muted = muted

    def resume(self, label: str) -> None:
        """'Resume audio' on an echo row (only when HealthConfig.auto_mute_on_echo is on)."""
        monitor = self._monitor
        if monitor is not None:
            monitor.resume(label)

    # ---- session thread ------------------------------------------------
    def _thread_main(self, directions: list[DirectionConfig]) -> None:
        error = ""
        try:
            error = asyncio.run(self._main(directions))
        except Exception as exc:  # a bug in the manager itself, not a direction failure
            logger.exception("Session manager crashed")
            error = describe_error(exc)
        finally:
            self._loop = None
            self._stop_event = None
            self._monitor = None
            self._players.clear()
            self._thread = None
            if error:
                self._on_state(State.ERROR, error)
            self._on_state(State.IDLE, "")

    async def _main(self, directions: list[DirectionConfig]) -> str:
        """Runs the whole session. Returns "" on a clean end, else why it failed."""
        self._stop_event = asyncio.Event()
        self._loop = asyncio.get_running_loop()
        if self._stop_requested:
            self._stop_event.set()
        monitor = HealthMonitor(on_issue=self._on_issue)
        self._monitor = monitor
        monitor_task = asyncio.create_task(monitor.run())
        self._on_state(State.CONNECTING, "")
        self._failures.clear()

        routers: dict[str, object] = {}
        connected = {d.label: asyncio.Event() for d in directions}
        all_connected = asyncio.Event()  # opens every direction's GatedAudio
        tasks = {
            d.label: asyncio.create_task(
                self._run_direction(d, monitor, routers, connected[d.label], all_connected))
            for d in directions
        }
        try:
            if not await self._wait_until_connected(connected, tasks):
                if self._stop_event.is_set():
                    self._on_state(State.STOPPING, "")
                    await self._shutdown(monitor, routers, tasks)
                    return self._first_error(tasks)
                # Atomic start: one direction failing, or the timeout, tears down all of them.
                await self._shutdown(monitor, routers, tasks)
                return self._first_error(tasks) or "Couldn't connect to the translation service in time."

            all_connected.set()
            self._on_state(State.TRANSLATING, "")
            await self._run_until_stopped(tasks)
            self._on_state(State.STOPPING, "")
            await self._shutdown(monitor, routers, tasks)
            fatal = any(issue.code in FATAL_CODES for issue in self._failures.values())
            all_failed = len(self._failed(tasks)) == len(tasks)
            return self._first_error(tasks) if fatal or all_failed else ""
        finally:
            monitor_task.cancel()
            await asyncio.gather(monitor_task, return_exceptions=True)
            monitor.finish()

    async def _wait_until_connected(self, connected, tasks) -> bool:
        """True once every direction is connected; False if Stop, the timeout, or a
        direction ending (failure or an early server close) comes first."""
        all_connected = asyncio.ensure_future(asyncio.gather(*(e.wait() for e in connected.values())))
        stop_wait = asyncio.ensure_future(self._stop_event.wait())
        try:
            await asyncio.wait({all_connected, stop_wait, *tasks.values()},
                               timeout=CONNECT_TIMEOUT_S, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in (all_connected, stop_wait):
                waiter.cancel()
            await asyncio.gather(all_connected, stop_wait, return_exceptions=True)
        return (all(e.is_set() for e in connected.values())
                and not self._stop_event.is_set()
                and not any(t.done() for t in tasks.values()))

    async def _run_until_stopped(self, tasks) -> None:
        """Wait for Stop or for every direction to end. A direction ending while
        another is still running puts the session in DEGRADED."""
        stop_wait = asyncio.ensure_future(self._stop_event.wait())
        pending = set(tasks.values())
        try:
            while pending:
                done, pending = await asyncio.wait(pending | {stop_wait}, return_when=asyncio.FIRST_COMPLETED)
                if stop_wait in done:
                    return
                pending.discard(stop_wait)
                if pending:
                    self._on_state(State.DEGRADED, self._degraded_message(tasks))
        finally:
            stop_wait.cancel()
            await asyncio.gather(stop_wait, return_exceptions=True)

    async def _run_direction(self, d: DirectionConfig, monitor: HealthMonitor,
                             routers: dict, connected: asyncio.Event,
                             all_connected: asyncio.Event) -> None:
        label = d.label
        self._on_direction_state(label, DirectionState.CONNECTING, "")

        def on_connected() -> None:
            connected.set()
            self._on_direction_state(label, DirectionState.CONNECTED, "")

        def on_player_ready(player) -> None:
            self._players[label] = player
            player.user_muted = self._mute_wanted.get(label, False)
            monitor.player_ready(label, player)

        # Built here, inside the running loop: JitterBuffer binds to the current loop.
        router, jitter_buffer = build_audio_pipeline(d.capture, d.pipeline)
        try:
            # Opening a device can take a while; keep the loop free for the other direction.
            await asyncio.to_thread(router.start)
            routers[label] = router
            monitor.attach(d, router, jitter_buffer)
            await run_session(
                GatedAudio(jitter_buffer, all_connected),
                output_device_config=d.playback,
                on_transcript=lambda pair: self._on_transcript(label, pair),
                target_language_code=d.target_language_code,
                echo_target_language=d.echo_target_language,
                on_transcript_delta=lambda kind, text: monitor.transcript_delta(label, kind, text),
                on_player_ready=on_player_ready,
                label=label,
                on_connected=on_connected,
            )
            self._on_direction_state(label, DirectionState.STOPPED, "")
        except asyncio.CancelledError:
            self._on_direction_state(label, DirectionState.STOPPED, "")
            raise
        except Exception as exc:
            issue = classify_failure(label, exc)
            self._failures[label] = issue
            logger.warning("[%s] Direction failed: %s", label, exc)
            self._on_direction_state(label, DirectionState.FAILED, issue.message)
            self._on_issue(issue)
            if issue.code in FATAL_CODES:
                self._stop_event.set()  # spec F10: stop translation, no silent fallback
            raise
        finally:
            # Stops capture and feeds the end-of-stream sentinel (AudioRouter.stop is
            # idempotent, so it doesn't matter if _shutdown already did this).
            await asyncio.to_thread(router.stop)

    async def _shutdown(self, monitor: HealthMonitor, routers: dict, tasks: dict) -> None:
        """Graceful: stop capture -> None sentinel -> send_audio ends -> run_session
        drains playback. Anything still running after STOP_GRACE_S (e.g. stuck in
        connect) is cancelled."""
        monitor.stop_watching()
        await asyncio.gather(*(asyncio.to_thread(r.stop) for r in list(routers.values())),
                             return_exceptions=True)
        _, still_running = await asyncio.wait(tasks.values(), timeout=STOP_GRACE_S)
        for task in still_running:
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)

    @staticmethod
    def _failed(tasks: dict) -> list[str]:
        return [label for label, t in tasks.items()
                if t.done() and not t.cancelled() and t.exception() is not None]

    def _first_error(self, tasks: dict) -> str:
        for label in self._failed(tasks):
            issue = self._failures.get(label)
            return issue.message if issue is not None else describe_error(tasks[label].exception())
        return ""

    @staticmethod
    def _degraded_message(tasks: dict) -> str:
        """Spec §8.2: "Candidate-to-staff translation is unavailable. Staff-to-candidate remains active." """
        down = [DIRECTION_NAMES.get(label, label) for label, t in tasks.items() if t.done()]
        up = [DIRECTION_NAMES.get(label, label) for label, t in tasks.items() if not t.done()]
        return f"{' and '.join(down)} translation is unavailable. {' and '.join(up)} remains active."
