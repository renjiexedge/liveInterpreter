import asyncio
import contextlib
import functools
import re
import sys
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime

from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from websockets.exceptions import ConnectionClosed, InvalidHandshake

from app_logging.audio_log import RawAudioDumper, log_interrupted, log_mime_type
from app_logging.gemini_log import ChunkStats, UsageStats, log_transcript
from app_logging.gemini_log import logger as gemini_logger
from audio.player import AudioPlayer, is_silent
from config import credentials
from config.audio_config import AudioDeviceConfig
from config.model_config import DEFAULT_MODEL, ResilienceConfig
from config.paths import debug_dump_dir, resource_path
from version import RELEASE

if not RELEASE and not getattr(sys, "frozen", False):
    # Dev only: reads .env in the project root into the process environment.
    # Never in a frozen build, even with --dev: it has no .env and no python-dotenv.
    # Imported here so a release build can leave python-dotenv out.
    # An explicit path: with no __main__ file (python -c, a REPL), find_dotenv
    # searches from the working directory instead of from this file.
    from dotenv import load_dotenv
    load_dotenv(resource_path(".env"))

DEFAULT_TARGET_LANGUAGE_CODE = "en"  # used when the caller doesn't pick a language
RECEIVE_SAMPLE_RATE = 24000  # PCM16 rate Gemini streams translated audio back at
SEND_BYTES_PER_MS = 32  # captured audio is 16 kHz mono PCM16
# WebSocket close codes after which re-opening the connection is worth trying:
# normal/going away (server-side rotation), abnormal (network), and the server's
# transient ones. 1007/1008 (bad key, invalid argument, policy) aren't retried.
RECONNECT_CLOSE_CODES = frozenset({1000, 1001, 1006, 1011, 1012, 1013, 1014})
# ChunkStats summary interval: 15 s in release keeps ~9 hours in the 3 x 1 MB log.
CHUNK_STATS_INTERVAL_S = 15.0 if RELEASE else 5.0


def api_key_present() -> bool:
    """True if a key is available (config/credentials.py: the environment, then
    Windows Credential Manager). Callers check this before Start, so a missing
    key gets a friendly message, not a crash."""
    return bool(credentials.get_api_key())


@functools.cache
def get_client() -> genai.Client:
    """The shared client, built on first use rather than at import: genai.Client
    raises without a key, and importing this module shouldn't. Cached, so call
    reset_client() after the key changes."""
    return genai.Client(api_key=credentials.get_api_key())


def reset_client() -> None:
    """Drop the cached client, so the next get_client() uses the current key."""
    get_client.cache_clear()


def validate_key(key: str, model: str = DEFAULT_MODEL) -> None:
    """Raises if `key` can't use `model` (setup wizard, before the key is saved).
    One small authenticated call: models.get takes ~0.2 s, and an unknown model
    is a 404 even for a Live-only one, so it also checks the key can see the
    default model (checked with the dev key, 2026-10-13). A bad key raises
    genai_errors.ClientError 400 "API key not valid"."""
    client = genai.Client(api_key=key.strip(), http_options=types.HttpOptions(timeout=15_000))  # ms
    client.models.get(model=model)


def build_live_config(
    target_language_code: str,
    echo_target_language: bool = True,
    session_resumption: bool = False,
    resumption_handle: str | None = None,
    context_compression: bool = False,
) -> types.LiveConnectConfig:
    """LiveConnectConfig for a translation session into target_language_code (BCP-47).

    echo_target_language=True makes the model voice speech that's already in the
    target language too ("parrot"); False keeps it silent for such speech, which
    also stops a re-captured translation from being played again (see DirectionConfig).

    session_resumption asks the server for resumption handles; resumption_handle
    resumes the session that handle came from. context_compression enables the
    sliding window, which lifts the ~15 min audio session limit (§7.1).
    """
    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        translation_config=types.TranslationConfig(
            target_language_code=target_language_code,
            echo_target_language=echo_target_language,
        ),
    )
    if session_resumption:
        config.session_resumption = types.SessionResumptionConfig(handle=resumption_handle)
    if context_compression:
        config.context_window_compression = types.ContextWindowCompressionConfig(
            sliding_window=types.SlidingWindow())
    return config


def is_connection_drop(exc: BaseException) -> bool:
    """True for failures that re-opening the connection can fix: the network
    dropping, the server closing or rotating the connection, a timed-out connect.
    run_session's default should_reconnect."""
    if isinstance(exc, (OSError, ConnectionClosed, InvalidHandshake, TimeoutError)):
        return True
    return isinstance(exc, genai_errors.APIError) and exc.code in RECONNECT_CLOSE_CODES


def _parse_duration_s(value: str | None) -> float | None:
    """Protobuf JSON duration ("12s", "0.5s") -> seconds."""
    match = re.fullmatch(r"\s*([\d.]+)s\s*", value or "")
    return float(match.group(1)) if match else None


TranscriptCallback = Callable[[dict[str, str]], None]
# ("input" | "output", text) for every transcription delta, as it arrives.
TranscriptDeltaCallback = Callable[[str, str], None]
# (planned, reason): planned=True is a GoAway renewal, False a lost connection.
ReconnectingCallback = Callable[[bool, str], None]


class PrerollBuffer:
    """run_session's view of the caller's audio_queue. While connected, get()
    reads the queue directly, so its own bounds and drop counting apply as before.
    During a reconnect gap, hold() keeps draining the queue into a buffer that
    keeps only the newest max_bytes, and the next connection sends that first
    (spec §10.2 pre-roll: speech that starts mid-reconnect isn't lost, and audio
    older than the bound is dropped for freshness)."""

    def __init__(self, source, max_bytes: int):
        self._source = source
        self._max_bytes = max_bytes
        self._chunks: deque[bytes] = deque()
        self._bytes = 0
        self._holder: asyncio.Task | None = None
        self._ended = asyncio.Event()
        self.dropped_bytes = 0

    @property
    def ended(self) -> bool:
        """The caller's end-of-stream sentinel arrived during a gap."""
        return self._ended.is_set()

    @property
    def buffered_ms(self) -> float:
        return self._bytes / SEND_BYTES_PER_MS

    async def get(self) -> bytes | None:
        if self._chunks:
            chunk = self._chunks.popleft()
            self._bytes -= len(chunk)
            return chunk
        if self.ended:
            return None
        return await self._source.get()

    def hold(self) -> None:
        if self._holder is None:
            self._holder = asyncio.create_task(self._hold())

    async def release(self) -> None:
        holder, self._holder = self._holder, None
        if holder is not None:
            holder.cancel()
            await asyncio.gather(holder, return_exceptions=True)

    async def wait_ended(self, timeout_s: float) -> None:
        """Sleep for timeout_s, or less if the stream ends meanwhile (Stop)."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._ended.wait(), timeout_s)

    async def _hold(self) -> None:
        while True:
            chunk = await self._source.get()
            if chunk is None:
                self._ended.set()
                return
            self._chunks.append(chunk)
            self._bytes += len(chunk)
            while self._bytes > self._max_bytes and len(self._chunks) > 1:
                old = self._chunks.popleft()
                self._bytes -= len(old)
                self.dropped_bytes += len(old)


class _Link:
    """Connection bookkeeping that outlives a single connection: the latest
    resumption handle, the GoAway renewal request, and the stall watch's view of
    speech heard but not yet answered."""

    def __init__(self, label: str, margin_s: float):
        self._prefix = f"[{label}] " if label else ""
        self._margin_s = margin_s
        self.handle: str | None = None
        self.renew = asyncio.Event()  # set when this connection should be replaced now
        self._renew_pending = False
        self._timer: asyncio.TimerHandle | None = None
        self._unanswered_since: float | None = None
        self._unanswered_chars = 0

    def new_connection(self) -> None:
        self.renew.clear()
        self._renew_pending = False
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self.answered()

    def heard(self, text: str) -> None:
        """An input transcription delta: speech now waiting for a translation."""
        chars = len(text.strip())
        if chars:
            if self._unanswered_since is None:
                self._unanswered_since = time.monotonic()
            self._unanswered_chars += chars

    def answered(self) -> None:
        """Translation came back (audible audio or output text), or the turn ended."""
        self._unanswered_since = None
        self._unanswered_chars = 0

    def stalled(self, after_s: float, min_chars: int) -> bool:
        return (self._unanswered_since is not None and self._unanswered_chars >= min_chars
                and time.monotonic() - self._unanswered_since >= after_s)

    def session_resumption_update(self, update: types.LiveServerSessionResumptionUpdate) -> None:
        if update.resumable and update.new_handle:
            self.handle = update.new_handle

    def go_away(self, time_left: str | None) -> None:
        """Renew at the next turn end, so a translation isn't cut mid-sentence, or
        goaway_margin_s before the deadline, whichever comes first."""
        seconds = _parse_duration_s(time_left)
        delay = max(0.0, seconds - self._margin_s) if seconds is not None else 0.0
        gemini_logger.warning("%sServer GoAway (time left %s): renewing the connection at the next "
                              "turn end, or in %.1f s", self._prefix, time_left, delay)
        self._renew_pending = True
        if self._timer is None:
            self._timer = asyncio.get_running_loop().call_later(delay, self.renew.set)

    def turn_ended(self) -> None:
        self.answered()
        if self._renew_pending:
            self.renew.set()


async def _watch_stall(link: _Link, after_s: float, min_chars: int) -> None:
    """Returns once the connection has stalled (see _Link.stalled)."""
    while not link.stalled(after_s, min_chars):
        await asyncio.sleep(1.0)


async def send_audio(session, audio_queue, chunk_stats: ChunkStats | None = None):
    """Stream PCM chunks from audio_queue to Gemini until a None sentinel arrives."""
    while True:
        chunk = await audio_queue.get()
        if chunk is None:
            break
        await session.send_realtime_input(
            audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000")
        )
        if chunk_stats is not None:
            chunk_stats.sent(chunk)


async def receive_responses(
    session,
    audio_player: AudioPlayer,
    on_transcript: TranscriptCallback | None,
    raw_dumper: RawAudioDumper | None = None,
    on_transcript_delta: TranscriptDeltaCallback | None = None,
    label: str = "",
    chunk_stats: ChunkStats | None = None,
    link: _Link | None = None,
    usage: UsageStats | None = None,
) -> None:
    """Play translated audio through audio_player and report each finished
    input/output transcript pair via on_transcript as {"input": ..., "output": ...}.
    Each individual delta also goes to on_transcript_delta, so the echo detector
    doesn't have to wait for turn_complete. A pair still unfinished when the
    connection ends is reported too, so a reconnect or Stop doesn't lose it.

    Runs until the connection closes, which the SDK raises as APIError (even a
    normal close), or until cancelled. Feeds usage_metadata to usage, and
    resumption handles and GoAway notices to link."""
    input_text = ""
    output_text = ""
    last_mime_type = None
    try:
        # session.receive() stops after each complete turn; keep listening.
        while True:
            async for response in session.receive():
                if response.usage_metadata and usage is not None:
                    usage.record(response.usage_metadata)
                if link is not None:
                    if response.session_resumption_update:
                        link.session_resumption_update(response.session_resumption_update)
                    if response.go_away:
                        link.go_away(response.go_away.time_left)
                server_content = response.server_content
                if not server_content:
                    continue
                if server_content.interrupted:
                    # Spec §10.2: never replay audio from an interrupted response.
                    frames = audio_player.flush()
                    rate = audio_player.sample_rate
                    log_interrupted(1000 * frames / rate if rate else 0.0, label)
                if server_content.input_transcription:
                    # .text can be None on some deltas (e.g. an interim/empty update).
                    delta = server_content.input_transcription.text or ""
                    input_text += delta
                    if link is not None:
                        link.heard(delta)
                    if delta and on_transcript_delta is not None:
                        on_transcript_delta("input", delta)
                if server_content.output_transcription:
                    delta = server_content.output_transcription.text or ""
                    output_text += delta
                    if delta and link is not None:
                        link.answered()
                    if delta and on_transcript_delta is not None:
                        on_transcript_delta("output", delta)
                if server_content.model_turn:
                    for part in server_content.model_turn.parts:
                        if part.inline_data and isinstance(part.inline_data.data, bytes):
                            if part.inline_data.mime_type != last_mime_type:
                                last_mime_type = part.inline_data.mime_type
                                log_mime_type(last_mime_type, label)
                            # Gemini streams real-time silence even while stuck,
                            # so only audible audio counts as an answer.
                            if link is not None and not is_silent(part.inline_data.data):
                                link.answered()
                            if chunk_stats is not None:
                                chunk_stats.received(part.inline_data.data)
                            if raw_dumper is not None:
                                # Disk I/O: keep it off the event loop, which two directions share.
                                await asyncio.to_thread(raw_dumper.write, part.inline_data.data)
                            await asyncio.to_thread(audio_player.write, part.inline_data.data)
                if server_content.turn_complete:
                    if input_text or output_text:
                        transcript = {"input": input_text, "output": output_text}
                        log_transcript(transcript, label)
                        if on_transcript is not None:
                            on_transcript(transcript)
                    input_text = ""
                    output_text = ""
                    if link is not None:
                        link.turn_ended()
    finally:
        if input_text or output_text:
            transcript = {"input": input_text, "output": output_text}
            log_transcript(transcript, label)
            if on_transcript is not None:
                on_transcript(transcript)


async def _run_connection(session, audio: PrerollBuffer, link: _Link, receive_kwargs: dict,
                          chunk_stats: ChunkStats, resilience: ResilienceConfig) -> str:
    """Send and receive on one open connection. Returns "ended" when the caller's
    audio stream ended, "renew" when a GoAway renewal is due, "stalled" when the
    stall watch fired; raises whatever ended the connection otherwise."""
    send_task = asyncio.create_task(send_audio(session, audio, chunk_stats))
    receive_task = asyncio.create_task(receive_responses(session, link=link, **receive_kwargs))
    renew_task = asyncio.create_task(link.renew.wait())
    tasks = (send_task, receive_task, renew_task)
    stall_task = None
    if resilience.stall_after_s > 0:
        stall_task = asyncio.create_task(
            _watch_stall(link, resilience.stall_after_s, resilience.stall_min_input_chars))
        tasks += (stall_task,)
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    for task in (send_task, receive_task):
        if task in done:
            task.result()  # re-raise any exception the task hit
    if send_task in done:
        return "ended"
    if renew_task in done:
        return "renew"
    if stall_task in done:
        return "stalled"
    raise ConnectionError("the server ended the stream")


async def run_session(
    audio_queue,
    output_device_config: AudioDeviceConfig | None = None,
    on_transcript: TranscriptCallback | None = None,
    dump_raw_audio: bool = False,
    target_language_code: str = DEFAULT_TARGET_LANGUAGE_CODE,
    echo_target_language: bool = True,
    on_transcript_delta: TranscriptDeltaCallback | None = None,
    on_player_ready: Callable[[AudioPlayer], None] | None = None,
    label: str = "",
    on_connected: Callable[[], None] | None = None,
    model: str = DEFAULT_MODEL,
    resilience: ResilienceConfig | None = None,
    on_reconnecting: ReconnectingCallback | None = None,
    should_reconnect: Callable[[BaseException], bool] = is_connection_drop,
    on_stalled: Callable[[float], None] | None = None,
) -> None:
    """Open a Live translation session on `model` and run sending/receiving
    concurrently, reconnecting as needed (spec §10.2, tuned by resilience).

    Speech is translated into target_language_code (BCP-47, e.g. "en", "th").
    The language is fixed for the lifetime of the session. echo_target_language
    is passed to build_live_config.

    on_transcript_delta receives every transcription delta, and on_player_ready
    receives the AudioPlayer once it's playing; both feed audio/health.HealthMonitor.

    label names the direction ("A"/"B", see config/session_config.py). It
    prefixes this session's log lines and goes into the dump filename, so two
    directions running at once stay distinguishable. on_connected is called
    (on the event loop) each time a Live connection opens, so a caller can tell
    "connecting"/"reconnecting" from "translating".

    Reconnecting: once the first connection is open, a connection ending with
    an error should_reconnect accepts is re-opened, resuming the session via the
    latest resumption handle when the model sends them. A GoAway renews the
    connection at the next turn end. on_reconnecting(planned, reason) is called
    before each attempt. Audio captured during the gap is kept (newest
    resilience.preroll_ms) and sent first. An unplanned outage longer than
    resilience.reconnect_give_up_s raises its last error. A failure before the
    first connection opens is raised straight away (the caller's atomic start).
    A connection that transcribes speech but returns no translation for
    resilience.stall_after_s is replaced by a fresh (not resumed) session, and
    on_stalled(stall_after_s) is called first.

    Translated audio is played out through the audio/ package's AudioPlayer,
    targeting output_device_config (defaults to the virtual cable, matching
    AudioDeviceConfig's default). Each finished input/output transcript pair
    is reported via on_transcript as it completes.

    When dump_raw_audio is True (dev only: ignored in a release build, since it
    writes speech to disk), every received audio chunk is also written,
    unmodified, to debug_dumps/received_<label>_<timestamp>.wav -- bypassing
    AudioPlayer's resample/device-output step entirely, so the file can be
    checked externally to see whether distortion is already present in the
    bytes Gemini sends.

    Returns once the caller's audio_queue yields None (end of stream).
    """
    resilience = resilience or ResilienceConfig()
    audio_player = AudioPlayer(
        device_config=output_device_config or AudioDeviceConfig(),
        source_rate=RECEIVE_SAMPLE_RATE,
    )
    audio_player.start()
    if on_player_ready is not None:
        on_player_ready(audio_player)

    raw_dumper: RawAudioDumper | None = None
    if dump_raw_audio and RELEASE:
        gemini_logger.warning("dump_raw_audio is ignored in a release build.")
    elif dump_raw_audio:
        dump_dir = debug_dump_dir()
        dump_dir.mkdir(exist_ok=True)
        # The label keeps two directions started in the same second from
        # opening the same file.
        dump_name = f"received_{label}_" if label else "received_"
        dump_path = dump_dir / f"{dump_name}{datetime.now():%Y%m%d_%H%M%S}.wav"
        raw_dumper = RawAudioDumper(dump_path, sample_rate=RECEIVE_SAMPLE_RATE)

    chunk_stats = ChunkStats(label, interval_s=CHUNK_STATS_INTERVAL_S, player=audio_player)
    usage = UsageStats(label, model)
    log_prefix = f"[{label}] " if label else ""
    audio = PrerollBuffer(audio_queue, max_bytes=int(resilience.preroll_ms * SEND_BYTES_PER_MS))
    link = _Link(label, resilience.goaway_margin_s)
    receive_kwargs = dict(audio_player=audio_player, on_transcript=on_transcript, raw_dumper=raw_dumper,
                          on_transcript_delta=on_transcript_delta, label=label,
                          chunk_stats=chunk_stats, usage=usage)
    features = dict(session_resumption=resilience.session_resumption,
                    context_compression=resilience.context_compression)
    session_started = time.monotonic()
    connected_once = False
    outage_since: float | None = None  # start of the current unplanned outage
    gap_started = 0.0
    attempt = 0
    last_error: BaseException | None = None  # what ended the last connection, if unplanned
    try:
        while True:
            config = build_live_config(target_language_code, echo_target_language,
                                       resumption_handle=link.handle, **features)
            used_handle = link.handle
            opened = False
            planned = False
            try:
                async with contextlib.AsyncExitStack() as stack:
                    connect = get_client().aio.live.connect(model=model, config=config)
                    if connected_once:
                        # A hung reconnect attempt is abandoned and retried.
                        async with asyncio.timeout(resilience.connect_timeout_s):
                            session = await stack.enter_async_context(connect)
                    else:
                        session = await stack.enter_async_context(connect)
                    opened = True
                    await audio.release()  # back to reading the caller's queue directly
                    link.new_connection()
                    usage.connection_started()
                    if connected_once:
                        gemini_logger.info(
                            "%sReconnected after %.1f s (%s; resumed: %s). Sending %.0f ms of pre-roll, "
                            "%.0f ms older audio dropped.", log_prefix, time.monotonic() - gap_started, model,
                            "yes" if used_handle else "no", audio.buffered_ms,
                            audio.dropped_bytes / SEND_BYTES_PER_MS)
                        audio.dropped_bytes = 0
                    else:
                        gemini_logger.info(
                            "%sSession started on %s with translation to %r (resumption: %s, "
                            "compression: %s)", log_prefix, model, target_language_code,
                            features["session_resumption"], features["context_compression"])
                    connected_once = True
                    outage_since = None
                    attempt = 0
                    if on_connected is not None:
                        on_connected()
                    try:
                        outcome = await _run_connection(session, audio, link, receive_kwargs, chunk_stats,
                                                        resilience)
                    finally:
                        usage.connection_ended()
                if outcome == "ended":
                    return
                planned = True
                if outcome == "stalled":
                    reason = (f"speech was transcribed but no translation came back for "
                              f"{resilience.stall_after_s:g} s")
                    link.handle = None  # resuming could bring the stuck state back
                    if on_stalled is not None:
                        on_stalled(resilience.stall_after_s)
                else:
                    reason = "the server asked for a new connection (GoAway)"
            except Exception as exc:
                text = str(exc).casefold()
                rejected_argument = (isinstance(exc, genai_errors.APIError) and exc.code == 1007
                                     and "api key" not in text)
                if not connected_once:
                    if rejected_argument and any(features.values()):
                        # The model may not support resumption/compression: try once without.
                        gemini_logger.warning("%s%s rejected the connection config (%s). Retrying without "
                                              "session resumption and context compression.", log_prefix, model, exc)
                        features = dict.fromkeys(features, False)
                        continue
                    raise  # never connected: the caller's start fails
                if used_handle and not opened and rejected_argument:
                    gemini_logger.warning("%sResuming with the saved handle failed (%s). Starting a fresh "
                                          "session instead.", log_prefix, exc)
                    link.handle = None
                elif not should_reconnect(exc):
                    raise
                last_error = exc
                reason = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__

            # ---- reconnect ----
            if audio.ended:
                return  # the stream ended (Stop) while that connection went down
            audio.hold()  # keep the newest audio for the next connection
            now = time.monotonic()
            if attempt == 0:
                gap_started = now
            if not planned:
                if outage_since is None:
                    outage_since = now
                elif now - outage_since > resilience.reconnect_give_up_s:
                    gemini_logger.error("%sGiving up: no connection for %.0f s.", log_prefix, now - outage_since)
                    raise last_error
            delay = 0.0 if planned else resilience.reconnect_backoff_s[
                min(attempt, len(resilience.reconnect_backoff_s) - 1)]
            attempt += 1
            gemini_logger.warning("%s%s: %s. Reconnecting (attempt %d%s).", log_prefix,
                                  "Renewing the connection" if planned else "Connection lost", reason,
                                  attempt, f", in {delay:g} s" if delay else "")
            if on_reconnecting is not None:
                on_reconnecting(planned, reason)
            if delay:
                await audio.wait_ended(delay)
                if audio.ended:
                    return
    finally:
        await audio.release()
        usage.session_ended(time.monotonic() - session_started)
        chunk_stats.flush()
        # AudioPlayer.stop() sleep-polls until playback drains (up to ~1 s past
        # the buffered audio). Run it off the loop: with two directions sharing
        # one loop, a synchronous wait here would stall the other direction's
        # send/receive while this one stops.
        await asyncio.to_thread(audio_player.stop)
        if raw_dumper is not None:
            raw_dumper.close()


async def main():
    audio_queue = asyncio.Queue()

    # Demo: feed one placeholder PCM chunk, then signal end of stream.
    await audio_queue.put(b"\x00\x00\x00\x00\x00\x00\x00\x00")
    await audio_queue.put(None)

    await run_session(audio_queue, on_transcript=print)


if __name__ == "__main__":
    asyncio.run(main())
