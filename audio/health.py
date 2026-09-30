"""Runtime audio health: device watchdogs, signal checks and echo detection.

Everything the user can fix is reported as an Issue whose text comes from
CATALOGUE, so the UI has one place to show problems (guide/two_way_plan.md §4.3c).
Pre-start checks in audio/routing_check.py report through the same Issue type.
"""
import asyncio
import enum
import math
import time
import unicodedata
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from config.health_config import HealthConfig
from config.session_config import CANDIDATE_TO_STAFF, STAFF_TO_CANDIDATE, DirectionConfig


class Severity(enum.IntEnum):
    OK = 0
    WARNING = 1
    PROBLEM = 2


class IssueCode(enum.Enum):
    # Before Start (audio/routing_check.py)
    ROUTING_LOOP = "routing_loop"
    DEVICE_SHARED = "device_shared"
    DEVICE_LIST_CHANGED = "device_list_changed"
    AUDIO_TEST_FAILED = "audio_test_failed"
    DEVICE_OPEN_FAILED = "device_open_failed"
    # During a session (HealthMonitor)
    INPUT_DISCONNECTED = "input_disconnected"
    OUTPUT_DISCONNECTED = "output_disconnected"
    INPUT_NO_SIGNAL = "input_no_signal"
    INPUT_TOO_LOUD = "input_too_loud"
    AUDIO_DROPPED = "audio_dropped"
    OUTPUT_STUTTER = "output_stutter"
    OUTPUT_LAGGING = "output_lagging"
    ECHO_SAME_DIRECTION = "echo_same_direction"
    ECHO_LOCAL = "echo_local"
    ECHO_REMOTE = "echo_remote"
    SESSION_FAILED = "session_failed"


ECHO_CODES = frozenset({IssueCode.ECHO_SAME_DIRECTION, IssueCode.ECHO_LOCAL, IssueCode.ECHO_REMOTE})
PRE_START_CODES = frozenset({
    IssueCode.ROUTING_LOOP, IssueCode.DEVICE_SHARED, IssueCode.DEVICE_LIST_CHANGED,
    IssueCode.AUDIO_TEST_FAILED, IssueCode.DEVICE_OPEN_FAILED,
})
# Still shown after the session ends, so the user can see why it went wrong.
STICKY_CODES = ECHO_CODES | {IssueCode.INPUT_DISCONNECTED, IssueCode.OUTPUT_DISCONNECTED, IssueCode.SESSION_FAILED}


@dataclass(frozen=True)
class IssueText:
    severity: Severity
    message: str
    action: str


_ECHO_RESUME = " Then click Resume audio."
_ECHO_MUTED = " Audio was muted to stop the loop."

CATALOGUE: dict[IssueCode, IssueText] = {
    IssueCode.ROUTING_LOOP: IssueText(
        Severity.PROBLEM,
        "Audio loop: '{playback}' feeds straight into '{capture}'.",
        "Choose a different output or input device."),
    IssueCode.DEVICE_SHARED: IssueText(
        Severity.PROBLEM,
        "'{device}' is chosen for two directions.",
        "Give each direction its own device."),
    IssueCode.DEVICE_LIST_CHANGED: IssueText(
        Severity.WARNING,
        "The list of audio devices changed since you chose '{device}'.",
        "Re-select the input and output devices."),
    IssueCode.AUDIO_TEST_FAILED: IssueText(
        Severity.PROBLEM,
        "The test sound played on '{playback}' was picked up by '{capture}'. "
        "Translations would repeat in a loop.",
        "Check Windows 'Listen to this device', Voicemeeter routing, and that speakers "
        "aren't near the microphone. Use a headset."),
    IssueCode.DEVICE_OPEN_FAILED: IssueText(
        Severity.PROBLEM,
        "Couldn't open '{device}'. It may be unplugged or in use by another app in exclusive mode.",
        "Plug it in, or close the other app, then try again."),
    IssueCode.INPUT_DISCONNECTED: IssueText(
        Severity.PROBLEM,
        "Warning: input device '{capture}' disconnected or stopped sending audio.",
        "Reconnect the microphone/cable, then press Stop and Start."),
    IssueCode.OUTPUT_DISCONNECTED: IssueText(
        Severity.PROBLEM,
        "Warning: output device '{playback}' disconnected. Translated audio can't be played.",
        "Reconnect the headset/cable, then press Stop and Start."),
    IssueCode.INPUT_NO_SIGNAL: IssueText(
        Severity.WARNING,
        "No sound is coming from '{capture}'.",
        "If it's a microphone, check it isn't muted (Windows or headset switch). If it's the "
        "WhatsApp cable, check the call has started and WhatsApp's speaker is set to this cable."),
    IssueCode.INPUT_TOO_LOUD: IssueText(
        Severity.WARNING,
        "'{capture}' is too loud (clipping). Translation quality may drop.",
        "Lower the microphone level in Windows sound settings."),
    IssueCode.AUDIO_DROPPED: IssueText(
        Severity.WARNING,
        "Some audio is being dropped before translation.",
        "Close other heavy apps and check your internet connection."),
    IssueCode.OUTPUT_STUTTER: IssueText(
        Severity.WARNING,
        "Translated audio is stuttering.",
        "Check your internet connection; close other heavy apps."),
    IssueCode.OUTPUT_LAGGING: IssueText(
        Severity.WARNING,
        "Translated audio is running {seconds} s behind.",
        "It will catch up when speakers pause. If it keeps growing, press Stop and Start."),
    IssueCode.ECHO_SAME_DIRECTION: IssueText(
        Severity.WARNING,
        "Echo detected: the translation played on '{playback}' is being picked up again by '{capture}'.",
        "Check Windows 'Listen to this device' and Voicemeeter routing, and move speakers "
        "away from the microphone."),
    IssueCode.ECHO_LOCAL: IssueText(
        Severity.WARNING,
        "Your microphone is picking up the translation you are hearing.",
        "Use a headset (not speakers) or lower the headset volume."),
    IssueCode.ECHO_REMOTE: IssueText(
        Severity.WARNING,
        "The other person's audio contains your own translated speech. Their speaker is being "
        "picked up by their microphone.",
        "Ask them to use headphones or turn their speaker down."),
    IssueCode.SESSION_FAILED: IssueText(
        Severity.PROBLEM,
        "The translation session stopped unexpectedly: {detail}",
        "Press Start to try again. If it keeps happening, check your internet connection."),
}


class _Blank(dict):
    def __missing__(self, key):
        return "?"


@dataclass
class Issue:
    """One (direction, code) problem. active=False means it has cleared."""

    label: str
    code: IssueCode
    active: bool = True
    severity: Severity | None = None    # overrides the catalogue's (echo strikes)
    fields: dict = field(default_factory=dict)

    @property
    def level(self) -> Severity:
        return self.severity if self.severity is not None else CATALOGUE[self.code].severity

    @property
    def message(self) -> str:
        text = CATALOGUE[self.code].message.format_map(_Blank(self.fields))
        return text + _ECHO_MUTED if self.muted else text

    @property
    def action(self) -> str:
        text = CATALOGUE[self.code].action.format_map(_Blank(self.fields))
        return text + _ECHO_RESUME if self.muted else text

    @property
    def muted(self) -> bool:
        return bool(self.fields.get("muted"))


# ---------------------------------------------------------------------------
# Echo detection (transcript-based)
# ---------------------------------------------------------------------------

def normalize_text(text: str) -> str:
    """Casefolded letters, marks and digits only. Keeps combining marks (Burmese,
    Thai vowel signs) and works for scripts without spaces."""
    return "".join(c for c in text.casefold() if unicodedata.category(c)[0] in "LMN")


def match_coverage(probe: str, text: str) -> float:
    """Share of `probe` found as one contiguous run inside `text`."""
    if not probe or not text:
        return 0.0
    match = SequenceMatcher(None, probe, text, autojunk=False).find_longest_match(
        0, len(probe), 0, len(text))
    return match.size / len(probe)


def echo_code(src_label: str, dst_label: str) -> IssueCode:
    """src produced the audio, dst's input picked it up again."""
    if src_label == dst_label:
        return IssueCode.ECHO_SAME_DIRECTION
    if src_label == STAFF_TO_CANDIDATE and dst_label == CANDIDATE_TO_STAFF:
        return IssueCode.ECHO_REMOTE
    return IssueCode.ECHO_LOCAL


@dataclass(frozen=True)
class EchoHit:
    src_label: str
    dst_label: str
    code: IssueCode


class TextEchoDetector:
    """Flags an input transcript that repeats a recent output transcript of any
    direction. Order rule: the output must have arrived before the probed input
    started. A real echo is output first, re-captured after. With
    echo_target_language=True, parroting goes the other way round (input first),
    so it isn't flagged."""

    def __init__(self, config: HealthConfig):
        self._cfg = config
        self._text: dict[tuple[str, str], deque[tuple[float, str]]] = defaultdict(deque)
        self._pending: dict[str, int] = defaultdict(int)

    def feed(self, label: str, kind: str, text: str, now: float) -> EchoHit | None:
        norm = normalize_text(text)
        if not norm:
            return None
        buf = self._text[(label, kind)]
        buf.append((now, norm))
        self._prune(buf, now)
        if kind != "input":
            return None

        self._pending[label] += len(norm)
        if self._pending[label] < self._cfg.echo_min_new_chars:
            return None
        self._pending[label] = 0

        probe, probe_start = self._probe(buf)
        if len(probe) < self._cfg.echo_min_probe_chars:
            return None
        for (src_label, src_kind), out_buf in self._text.items():
            if src_kind != "output":
                continue
            self._prune(out_buf, now)
            earlier = "".join(t for ts, t in out_buf if ts < probe_start)
            if match_coverage(probe, earlier) >= self._cfg.echo_match_ratio:
                buf.clear()  # needs fresh input before it can report again
                return EchoHit(src_label, label, echo_code(src_label, label))
        return None

    def reset(self) -> None:
        self._text.clear()
        self._pending.clear()

    def _probe(self, buf) -> tuple[str, float]:
        parts, chars, start = [], 0, 0.0
        for ts, text in reversed(buf):
            parts.append(text)
            chars += len(text)
            start = ts
            if chars >= self._cfg.echo_probe_chars:
                break
        return "".join(reversed(parts))[-self._cfg.echo_probe_chars:], start

    def _prune(self, buf, now: float) -> None:
        while buf and now - buf[0][0] > self._cfg.echo_window_s:
            buf.popleft()


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------

class _CounterWatch:
    """Active when a growing counter rises by >= threshold within window_s;
    clears once it hasn't moved for clear_s."""

    def __init__(self, threshold: float, window_s: float, clear_s: float):
        self._threshold = threshold
        self._window_s = window_s
        self._clear_s = clear_s
        self._samples: deque[tuple[float, float]] = deque()
        self._last_value: float | None = None
        self._last_change = 0.0
        self.active = False

    def update(self, value: float, now: float) -> bool:
        if self._last_value is not None and value != self._last_value:
            self._last_change = now
        self._last_value = value
        self._samples.append((now, value))
        while now - self._samples[0][0] > self._window_s:
            self._samples.popleft()
        if value - self._samples[0][1] >= self._threshold:
            self.active = True
        elif self.active and now - self._last_change >= self._clear_s:
            self.active = False
        return self.active


@dataclass
class _Direction:
    config: DirectionConfig
    router: object
    jitter_buffer: object
    clip_watch: _CounterWatch
    drop_watch: _CounterWatch
    stutter_watch: _CounterWatch
    player: object | None = None
    lagging: bool = False


class HealthMonitor:
    """One per session; runs on the session's asyncio loop. Direction-agnostic:
    attach one direction today, two for two-way.

    on_issue(Issue) is called on every change (raised, updated, cleared). It's
    meant to be a Qt Signal's emit, which is safe from this thread."""

    def __init__(self, on_issue: Callable[[Issue], None], config: HealthConfig | None = None):
        self._on_issue = on_issue
        self._cfg = config or HealthConfig()
        self._dirs: dict[str, _Direction] = {}
        self._reported: dict[tuple[str, IssueCode], Issue] = {}
        self._echo = TextEchoDetector(self._cfg)
        self._echo_hits: dict[tuple[str, IssueCode], deque[float]] = defaultdict(deque)
        self._muted: set[str] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopping = False

    # ---- hooks (session loop) ------------------------------------------
    def attach(self, direction: DirectionConfig, router, jitter_buffer) -> None:
        cfg = self._cfg
        blocks_per_window = cfg.window_s * 1000 / direction.pipeline.chunk_ms
        self._dirs[direction.label] = _Direction(
            config=direction, router=router, jitter_buffer=jitter_buffer,
            clip_watch=_CounterWatch(math.ceil(blocks_per_window * cfg.clip_ratio), cfg.window_s, cfg.clear_after_s),
            drop_watch=_CounterWatch(1, cfg.window_s, cfg.clear_after_s),
            stutter_watch=_CounterWatch(cfg.stutter_count, cfg.window_s, cfg.clear_after_s),
        )

    def player_ready(self, label: str, player) -> None:
        self._dirs[label].player = player

    def transcript_delta(self, label: str, kind: str, text: str) -> None:
        hit = self._echo.feed(label, kind, text, time.monotonic())
        if hit is not None:
            self._handle_echo(hit)

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        while True:
            if not self._stopping:
                self._poll(time.monotonic())
            await asyncio.sleep(self._cfg.poll_s)

    # ---- any thread -----------------------------------------------------
    def stop_watching(self) -> None:
        """Call before stopping the devices, so a deliberate Stop isn't reported
        as a disconnect. A plain flag write, safe from the GUI thread."""
        self._stopping = True

    def resume(self, label: str) -> None:
        """'Resume audio' button: unmute the direction's player and clear its echo rows."""
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._resume, label)

    # ---- session end (session loop) --------------------------------------
    def finish(self) -> None:
        """Clear transient issues. Disconnects, echoes and failures stay visible
        until the next Start, so the user can see why the session went wrong."""
        self._stopping = True
        for label, code in list(self._reported):
            if code not in STICKY_CODES:
                self._set(label, code, False)

    # ---- internals -------------------------------------------------------
    def _poll(self, now: float) -> None:
        cfg = self._cfg
        for label, d in self._dirs.items():
            router, player = d.router, d.player
            capture = router.device_name
            input_down = now - router.last_callback_at > cfg.disconnect_after_s
            self._set(label, IssueCode.INPUT_DISCONNECTED, input_down, capture=capture)
            self._set(label, IssueCode.INPUT_NO_SIGNAL,
                      not input_down and router.zero_run_s >= cfg.no_signal_after_s, capture=capture)
            self._set(label, IssueCode.INPUT_TOO_LOUD,
                      d.clip_watch.update(router.clipped_blocks, now), capture=capture)
            self._set(label, IssueCode.AUDIO_DROPPED,
                      d.drop_watch.update(d.jitter_buffer.dropped_count, now))

            if player is not None:
                playback = player.device_name
                self._set(label, IssueCode.OUTPUT_DISCONNECTED,
                          now - player.last_callback_at > cfg.disconnect_after_s, playback=playback)
                self._set(label, IssueCode.OUTPUT_STUTTER,
                          d.stutter_watch.update(player.mid_speech_underruns, now))
                lag_ms = player.buffered_ms
                d.lagging = lag_ms > cfg.lag_warn_ms or (d.lagging and lag_ms >= cfg.lag_clear_ms)
                self._set(label, IssueCode.OUTPUT_LAGGING, d.lagging, seconds=round(lag_ms / 1000))

        # Echo rows expire on their own unless they muted the player.
        for (label, code), hits in self._echo_hits.items():
            if (label, code) in self._reported and label not in self._muted:
                if not hits or now - hits[-1] > cfg.echo_strike_window_s:
                    self._set(label, code, False)

    def _handle_echo(self, hit: EchoHit) -> None:
        now = time.monotonic()
        key = (hit.src_label, hit.code)
        hits = self._echo_hits[key]
        hits.append(now)
        while now - hits[0] > self._cfg.echo_strike_window_s:
            hits.popleft()

        src = self._dirs.get(hit.src_label)
        dst = self._dirs.get(hit.dst_label)
        severity = Severity.WARNING if len(hits) < 2 else Severity.PROBLEM
        if (severity is Severity.PROBLEM and self._cfg.auto_mute_on_echo
                and src is not None and src.player is not None):
            src.player.muted = True
            self._muted.add(hit.src_label)
        self._set(hit.src_label, hit.code, True, severity=severity,
                  playback=src.player.device_name if src and src.player else "?",
                  capture=dst.router.device_name if dst else "?",
                  muted=hit.src_label in self._muted)

    def _resume(self, label: str) -> None:
        d = self._dirs.get(label)
        if d is not None and d.player is not None:
            d.player.muted = False
        self._muted.discard(label)
        for code in ECHO_CODES:
            self._echo_hits.pop((label, code), None)
            self._set(label, code, False)
        self._echo.reset()

    def _set(self, label: str, code: IssueCode, active: bool,
             severity: Severity | None = None, **fields) -> None:
        key = (label, code)
        previous = self._reported.get(key)
        if active:
            issue = Issue(label, code, True, severity, fields)
            if previous is None or (previous.level, previous.message) != (issue.level, issue.message):
                self._reported[key] = issue
                self._on_issue(issue)
        elif previous is not None:
            del self._reported[key]
            self._on_issue(Issue(label, code, False))
