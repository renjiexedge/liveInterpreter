"""SessionManager: runs every translation direction of a session on ONE background
thread with ONE asyncio loop (Qt's event loop isn't asyncio, see CLAUDE.md).

Qt-agnostic: it reports through plain callbacks, which ui/live_interpreter.py points at Qt
Signal.emit (safe to call from this thread). Works on a list of DirectionConfig,
so the one-way app passes one direction and two-way passes two.

State flow (guide/two_way_plan.md §4.4, spec §7):
    IDLE -> CONNECTING -> TRANSLATING [<-> RECONNECTING] [<-> DEGRADED] -> STOPPING -> IDLE
Start is atomic: TRANSLATING only once every direction has connected; if any
direction fails, Stop is pressed, or CONNECT_TIMEOUT_S passes first, all of them
are torn down. Until then no direction sends audio (see GatedAudio), so a direction
that connects first can't translate or play anything that a failed start would
then cut off. Once translating, a dropped connection is re-opened by run_session
(RECONNECTING until it's back, Phase 6), and a direction that still ends doesn't
cancel the others (DEGRADED; retry(label) restarts it), except a sign-in or quota
failure, which stops the whole session (spec §8.2/F10: no silent fallback). ERROR
is reported just before the final IDLE when the session ended because of a
failure; its message says why.
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
from engines.gemini_live import is_connection_drop, run_session

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_S = 15  # covers opening the capture device and the Live connection
STOP_GRACE_S = 5        # how long a direction gets to drain after Stop before it's cancelled


class State(enum.Enum):
    IDLE = "idle"
    CONNECTING = "connecting"
    TRANSLATING = "translating"
    RECONNECTING = "reconnecting"
    DEGRADED = "degraded"
    STOPPING = "stopping"
    ERROR = "error"
    # READY (spec §7: audio checked, not yet translating) isn't used: Start goes
    # straight from the loop test to CONNECTING.


class DirectionState(enum.Enum):
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
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


def failure_code(exc: BaseException) -> IssueCode:
    """Which spec §8.2 issue a direction's failure is. The Live API reports most
    service errors as google.genai.errors.APIError carrying a WebSocket close code
    and the server's reason text (an invalid key is code 1007, "API key not
    valid..."), so they're told apart by status code and text. A dropped
    connection raises websockets' ConnectionClosed (on send), APIError 1006
    (abnormal closure, on receive), an OSError while connecting, or TimeoutError
    when a reconnect attempt hangs."""
    if isinstance(exc, (DeviceNotFoundError, sd.PortAudioError)):
        return IssueCode.SESSION_FAILED
    code = getattr(exc, "code", None)
    text = str(exc).casefold()
    if code in (401, 403) or any(word in text for word in ("api key", "permission_denied", "unauthenticated")):
        return IssueCode.SERVICE_AUTH_FAILED
    if code == 429 or any(word in text for word in ("quota", "resource_exhausted", "rate limit")):
        return IssueCode.QUOTA_EXCEEDED
    if isinstance(exc, (OSError, ConnectionClosed, InvalidHandshake, TimeoutError)) or code == 1006:
        return IssueCode.NETWORK_LOST
    return IssueCode.SESSION_FAILED


def classify_failure(label: str, exc: BaseException) -> Issue:
    """The spec §8.2 issue for a direction's failure (see failure_code)."""
    code = failure_code(exc)
    if isinstance(exc, (DeviceNotFoundError, sd.PortAudioError)):
        return Issue(label, code, fields={
            "detail": "an audio device couldn't be opened. It may be unplugged or in use."})
    if code is IssueCode.SERVICE_AUTH_FAILED:
        diagnostic_id = uuid.uuid4().hex[:8].upper()
        logger.error("[%s] Translation service sign-in failed (diagnostic ID %s): %s", label, diagnostic_id, exc)
        return Issue(label, code, fields={"id": diagnostic_id})
    if code is IssueCode.SESSION_FAILED:
        return Issue(label, code, fields={"detail": str(exc) or type(exc).__name__})
    return Issue(label, code)


def should_reconnect(exc: BaseException) -> bool:
    """run_session's reconnect rule: a dropped connection, unless the close
    reason says sign-in or quota (those stop the session instead)."""
    return is_connection_drop(exc) and failure_code(exc) not in FATAL_CODES


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
        # Session-thread state, kept on self so retry() can restart one direction.
        self._configs: dict[str, DirectionConfig] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._routers: dict[str, object] = {}
        self._all_connected: asyncio.Event | None = None
        self._tasks_changed: asyncio.Event | None = None
        self._translating = False
        self._reconnecting: set[str] = set()
        self._live: set[str] = set()  # directions with an open connection right now

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

    def retry(self, label: str) -> None:
        """Retry button for a direction that failed while the other kept running
        (spec §7 Degraded: "retry failed direction"). Thread-safe; ignored unless
        that direction has ended and the session is still translating."""
        loop = self._loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(self._retry, label)
            except RuntimeError:
                pass  # the loop closed in the meantime

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
            self._tasks = {}
            self._routers = {}
            self._translating = False
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
        self._reconnecting.clear()
        self._live.clear()
        self._translating = False

        self._configs = {d.label: d for d in directions}
        self._routers = routers = {}
        connected = {d.label: asyncio.Event() for d in directions}
        self._all_connected = all_connected = asyncio.Event()  # opens every direction's GatedAudio
        self._tasks_changed = asyncio.Event()
        self._tasks = tasks = {
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
            self._translating = True
            self._report_running_state()
            await self._run_until_stopped(tasks)
            self._translating = False
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

    async def _run_until_stopped(self, tasks: dict) -> None:
        """Wait for Stop or for every direction to end. A direction ending while
        another is still running puts the session in DEGRADED. `tasks` is the
        live dict: retry() swaps a direction's task in and sets _tasks_changed."""
        stop_wait = asyncio.ensure_future(self._stop_event.wait())
        try:
            while running := {t for t in tasks.values() if not t.done()}:
                self._tasks_changed.clear()
                changed = asyncio.ensure_future(self._tasks_changed.wait())
                try:
                    done, _ = await asyncio.wait(running | {stop_wait, changed},
                                                 return_when=asyncio.FIRST_COMPLETED)
                finally:
                    changed.cancel()
                if stop_wait in done:
                    return
                self._report_running_state()
        finally:
            stop_wait.cancel()
            await asyncio.gather(stop_wait, return_exceptions=True)

    def _report_running_state(self) -> None:
        """TRANSLATING, RECONNECTING or DEGRADED, from the directions' states.
        Only once the session is translating (not during the atomic start)."""
        if not self._translating:
            return
        if self._reconnecting:
            names = " and ".join(DIRECTION_NAMES.get(label, label) for label in sorted(self._reconnecting))
            self._on_state(State.RECONNECTING, f"Reconnecting {names} translation… "
                                               "Do not continue sensitive questions until it's back.")
        elif len(self._live) == len(self._tasks):
            self._on_state(State.TRANSLATING, "")
        elif self._live:  # with none live, the session is about to end anyway
            self._on_state(State.DEGRADED, self._degraded_message())

    def _retry(self, label: str) -> None:
        """Session loop: restart one ended direction (see retry())."""
        task, d = self._tasks.get(label), self._configs.get(label)
        if (task is None or d is None or not task.done() or not self._translating
                or self._stop_event is None or self._stop_event.is_set()):
            return
        issue = self._failures.get(label)
        if issue is not None and issue.code in FATAL_CODES:
            return  # the session is stopping anyway
        if not task.cancelled():
            task.exception()  # retrieved: no "exception never retrieved" warning
        if issue is not None:
            del self._failures[label]
            self._on_issue(Issue(label, issue.code, active=False))
        logger.info("[%s] Retrying the direction", label)
        self._tasks[label] = asyncio.create_task(self._run_direction(
            d, self._monitor, self._routers, asyncio.Event(), self._all_connected))
        self._tasks_changed.set()

    async def _run_direction(self, d: DirectionConfig, monitor: HealthMonitor,
                             routers: dict, connected: asyncio.Event,
                             all_connected: asyncio.Event) -> None:
        label = d.label
        self._on_direction_state(label, DirectionState.CONNECTING, "")

        def on_connected() -> None:
            connected.set()
            self._live.add(label)
            self._on_direction_state(label, DirectionState.CONNECTED, "")
            if label in self._reconnecting:
                self._reconnecting.discard(label)
                self._on_issue(Issue(label, IssueCode.RECONNECTING, active=False))
            self._report_running_state()

        def on_reconnecting(planned: bool, _reason: str) -> None:  # run_session logs the reason
            self._reconnecting.add(label)
            self._live.discard(label)
            self._on_direction_state(label, DirectionState.RECONNECTING,
                                     "Renewing the connection…" if planned
                                     else "Connection lost. Reconnecting…")
            if not planned:  # a GoAway renewal is a brief planned gap, not a fault
                self._on_issue(Issue(label, IssueCode.RECONNECTING))
            self._report_running_state()

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
                model=d.model,
                on_reconnecting=on_reconnecting,
                should_reconnect=should_reconnect,
                on_stalled=lambda seconds: monitor.output_stalled(label, seconds),
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
            self._live.discard(label)
            if label in self._reconnecting:  # stopped, or reconnecting gave up
                self._reconnecting.discard(label)
                self._on_issue(Issue(label, IssueCode.RECONNECTING, active=False))
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

    def _degraded_message(self) -> str:
        """Spec §8.2: "Candidate-to-staff translation is unavailable. Staff-to-candidate remains active."
        A direction being retried counts as unavailable until it connects."""
        down = [DIRECTION_NAMES.get(label, label) for label in self._tasks if label not in self._live]
        up = [DIRECTION_NAMES.get(label, label) for label in self._tasks if label in self._live]
        return f"{' and '.join(down)} translation is unavailable. {' and '.join(up)} remains active."
