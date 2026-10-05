"""Loop prevention before a session starts (guide/two_way_plan.md §4.3).

validate_routing: instant name-based checks, run on every Start.
run_loop_test:    plays a test clip on every playback device and listens for it on
                  every capture device. Blocking (several seconds), so run it on a
                  worker thread. Start requires a pass for the current devices.

Both take a list of DirectionConfig (one today, two for two-way) and return
Issues, which the UI shows in the same panel as runtime health issues.
"""
import logging
import re
import time
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd
import soxr

from audio.devices import DeviceInfo, DeviceNotFoundError, find_input_device, find_output_device
from audio.health import Issue, IssueCode
from config.audio_config import AudioDeviceConfig
from config.health_config import LoopTestConfig
from config.session_config import DirectionConfig

logger = logging.getLogger(__name__)

ANALYSIS_RATE = 16000
_TAIL_S = 0.3  # extra recording after the clip + max lag

# "In 16ch" is VB-Cable's 16-channel playback end ("CABLE In 16ch (VB-Audio Virtual Cable)").
_ENDPOINT_WORDS = re.compile(r"\b(input|output)\b|\bin \d+ch\b", re.IGNORECASE)


def cable_key(name: str) -> str:
    """'CABLE Input (VB-Audio Virtual Cable)' and 'CABLE Output (VB-Audio Virtual Cable)'
    are two ends of one wire (as is 'CABLE In 16ch'). Strip the endpoint words to pair them.
    Heuristic only: Voicemeeter ('Voicemeeter AUX Input' -> 'Voicemeeter Out B2')
    routes internally and is only caught by run_loop_test."""
    return " ".join(_ENDPOINT_WORDS.sub("", name).casefold().split())


def routing_fingerprint(expected_names: dict[str, str]) -> frozenset:
    """Cache key for a passing loop test: the device names in each role. Changing
    any device gives a new fingerprint, which forces a re-test."""
    return frozenset(expected_names.items())


# ---------------------------------------------------------------------------
# Static validation
# ---------------------------------------------------------------------------

def validate_routing(directions: list[DirectionConfig], expected_names: dict[str, str]) -> list[Issue]:
    """Returns the problems found (empty = OK). expected_names maps
    "<label>.capture" / "<label>.playback" to the name the combo showed."""
    issues: list[Issue] = []
    resolved: dict[str, dict[str, DeviceInfo]] = {}

    for d in directions:
        devices = {}
        for role, config, find in (("capture", d.capture, find_input_device),
                                   ("playback", d.playback, find_output_device)):
            expected = expected_names.get(f"{d.label}.{role}")
            shown = expected or _describe(config, role)
            if config.device_index is None and config.exact_name is None:
                # Nothing chosen. No fallback to the "CABLE" substring default: it's
                # it matches CABLE Output, CABLE Input and CABLE In 16ch alike.
                issues.append(Issue(d.label, IssueCode.DEVICE_NOT_SELECTED,
                                    fields={"role": "input" if role == "capture" else "output"}))
                continue
            try:
                device = find(config)
            except DeviceNotFoundError:
                issues.append(Issue(d.label, IssueCode.DEVICE_OPEN_FAILED, fields={"device": shown}))
                continue
            if expected and device.name != expected:
                issues.append(Issue(d.label, IssueCode.DEVICE_LIST_CHANGED, fields={"device": shown}))
            devices[role] = device
        if len(devices) == 2:
            resolved[d.label] = devices

    # Every playback against every capture, including its own direction's:
    # translated output must never be able to reach any capture device.
    for src in resolved.values():
        for dst_label, dst in resolved.items():
            if cable_key(src["playback"].name) == cable_key(dst["capture"].name):
                issues.append(Issue(dst_label, IssueCode.ROUTING_LOOP, fields={
                    "playback": src["playback"].name, "capture": dst["capture"].name}))

    for role in ("capture", "playback"):
        seen: dict[int, str] = {}
        for label, devices in resolved.items():
            index = devices[role].index
            if index in seen:
                issues.append(Issue(label, IssueCode.DEVICE_SHARED, fields={"device": devices[role].name}))
            seen[index] = label
    return issues


# ---------------------------------------------------------------------------
# Loop test
# ---------------------------------------------------------------------------

class _DeviceFailure(Exception):
    def __init__(self, label: str, name: str):
        super().__init__(name)
        self.label = label
        self.name = name


def load_test_clip(config: LoopTestConfig) -> np.ndarray:
    """Mono float32 at ANALYSIS_RATE, normalised to config.play_peak. Uses the
    WAV at config.clip_path, or synthetic chirps if it doesn't exist yet."""
    path = Path(config.clip_path)
    if path.exists():
        clip = _read_wav_16k(path)
    else:
        logger.warning("Loop test clip %s not found; using synthetic chirps", path)
        clip = _synthetic_clip()
    peak = float(np.abs(clip).max()) or 1.0
    return (clip / peak * config.play_peak).astype("float32")


def _read_wav_16k(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as wav:
        if wav.getsampwidth() != 2:
            raise ValueError(f"{path}: expected 16-bit PCM WAV")
        channels, rate = wav.getnchannels(), wav.getframerate()
        data = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
    samples = data.reshape(-1, channels).mean(axis=1).astype("float32") / 32768.0
    return soxr.resample(samples, rate, ANALYSIS_RATE) if rate != ANALYSIS_RATE else samples


def _synthetic_clip() -> np.ndarray:
    """Five 0.3 s rising chirps (300 Hz -> 3.4 kHz) with 0.1 s gaps. Sweeping, so
    noise suppression doesn't treat it as a steady tone."""
    rate, length = ANALYSIS_RATE, 0.3
    t = np.arange(int(rate * length)) / rate
    f0, f1 = 300.0, 3400.0
    chirp = np.sin(2 * np.pi * (f0 * t + (f1 - f0) / (2 * length) * t ** 2))
    chirp *= np.hanning(len(chirp))
    gap = np.zeros(int(rate * 0.1))
    return np.concatenate([np.concatenate([chirp, gap]) for _ in range(5)]).astype("float32")


def peak_ncc(recorded: np.ndarray, clip: np.ndarray, min_rms: float = 0.0) -> float:
    """Max |normalised cross-correlation| of `clip` at every position inside
    `recorded`. Positions where the recording is quieter than min_rms are skipped.
    FFT-based, so a few ms even for several seconds at 16 kHz."""
    m, n = len(clip), len(recorded)
    if m == 0 or n < m:
        return 0.0
    size = 1 << (n + m - 1).bit_length()
    corr = np.fft.irfft(np.fft.rfft(recorded, size) * np.conj(np.fft.rfft(clip, size)), size)[: n - m + 1]
    energy = np.concatenate(([0.0], np.cumsum(recorded.astype("float64") ** 2)))
    window = np.maximum(energy[m:] - energy[:-m], 0.0)
    denom = np.linalg.norm(clip) * np.sqrt(window)
    usable = (np.sqrt(window / m) >= min_rms) & (denom > 0)
    if not usable.any():
        return 0.0
    return float(np.max(np.abs(corr[usable] / denom[usable])))


def run_loop_test(directions: list[DirectionConfig], config: LoopTestConfig | None = None,
                  skip_playbacks: frozenset[str] = frozenset()) -> list[Issue]:
    """Spec §6.3. Records every capture device once in silence (baseline), then
    once per playback device while the clip plays on it. A capture whose
    correlation with the clip is high, and well above its baseline, hears that
    playback: a loop or bleed. Blocking; never call it on the GUI thread.

    skip_playbacks: direction labels whose playback device must NOT play the
    clip. Used when Start runs the test during a call, where playing into the
    WhatsApp outgoing route would play the test sound to the candidate. Those
    devices go untested; the caller is responsible for saying so."""
    config = config or LoopTestConfig()
    clip = load_test_clip(config)
    issues: list[Issue] = []
    captures: dict[str, DeviceInfo] = {}
    playbacks: dict[str, DeviceInfo] = {}
    for d in directions:
        for role, device_config, find, target in (("capture", d.capture, find_input_device, captures),
                                                  ("playback", d.playback, find_output_device, playbacks)):
            try:
                target[d.label] = find(device_config)
            except DeviceNotFoundError:
                issues.append(Issue(d.label, IssueCode.DEVICE_OPEN_FAILED,
                                    fields={"device": _describe(device_config, role)}))
    if issues:
        return issues

    seconds = len(clip) / ANALYSIS_RATE + config.max_lag_s + _TAIL_S
    try:
        baseline = _record(captures, seconds, None, clip)
        for play_label, play_device in playbacks.items():
            if play_label in skip_playbacks:
                logger.info("Loop test: skipped playback %r (not played into a live call)", play_device.name)
                continue
            heard = _record(captures, seconds, (play_label, play_device), clip)
            for cap_label, cap_device in captures.items():
                base = peak_ncc(baseline[cap_label], clip, config.min_rms)
                peak = peak_ncc(heard[cap_label], clip, config.min_rms)
                # Levels tell "nothing arrived" (silent) apart from "sound, but not the clip".
                logger.info("Loop test: %r -> %r: NCC %.3f (baseline %.3f), level %s (baseline %s)",
                            play_device.name, cap_device.name, peak, base,
                            _level(heard[cap_label]), _level(baseline[cap_label]))
                if peak >= config.min_ncc and peak >= config.min_ratio * base:
                    issues.append(Issue(cap_label, IssueCode.AUDIO_TEST_FAILED, fields={
                        "playback": play_device.name, "capture": cap_device.name}))
    except _DeviceFailure as failure:
        issues.append(Issue(failure.label, IssueCode.DEVICE_OPEN_FAILED, fields={"device": failure.name}))
    return issues


def _level(samples: np.ndarray) -> str:
    """RMS level in dBFS, or 'silent' for pure digital silence."""
    rms = float(np.sqrt(np.mean(samples.astype("float64") ** 2))) if len(samples) else 0.0
    return f"{20 * np.log10(rms):.1f} dBFS" if rms > 0 else "silent"


def _describe(config: AudioDeviceConfig, role: str) -> str:
    if config.exact_name:
        return config.exact_name
    if config.device_index is not None:
        return f"{role} device #{config.device_index}"
    return f"(no {role} device selected)"


def _record(captures: dict[str, DeviceInfo], seconds: float,
            play: tuple[str, DeviceInfo] | None, clip: np.ndarray) -> dict[str, np.ndarray]:
    """Record every capture device for `seconds`, optionally playing the clip on
    one output. Returns mono float32 at ANALYSIS_RATE per capture label.
    sd.play()/sd.rec() share one global stream, so explicit streams are used."""
    chunks: dict[str, list[np.ndarray]] = {label: [] for label in captures}
    streams = []
    try:
        for label, device in captures.items():
            buffer = chunks[label]
            try:
                stream = sd.InputStream(
                    device=device.index, samplerate=int(device.default_samplerate),
                    channels=min(device.max_input_channels, 2), dtype="float32",
                    callback=lambda indata, frames, time_info, status, buffer=buffer: buffer.append(indata.copy()))
                stream.start()
            except sd.PortAudioError as exc:
                raise _DeviceFailure(label, device.name) from exc
            streams.append(stream)

        if play is not None:
            play_label, device = play
            rate = int(device.default_samplerate)
            samples = soxr.resample(clip, ANALYSIS_RATE, rate).astype("float32")
            position = 0

            def feed(outdata, frames, time_info, status):
                nonlocal position
                chunk = samples[position:position + frames]
                outdata[: len(chunk), 0] = chunk
                outdata[len(chunk):, 0] = 0
                position += len(chunk)

            try:
                stream = sd.OutputStream(device=device.index, samplerate=rate, channels=1,
                                         dtype="float32", callback=feed)
                stream.start()
            except sd.PortAudioError as exc:
                raise _DeviceFailure(play_label, device.name) from exc
            streams.append(stream)

        time.sleep(seconds)
    finally:
        for stream in streams:
            stream.stop()
            stream.close()

    recorded = {}
    for label, device in captures.items():
        rate = int(device.default_samplerate)
        data = np.concatenate(chunks[label]) if chunks[label] else np.zeros((0, 1), dtype="float32")
        if len(data) < 0.5 * seconds * rate:
            raise _DeviceFailure(label, device.name)  # opened, but delivered (almost) no audio
        mono = data.mean(axis=1).astype("float32")
        recorded[label] = soxr.resample(mono, rate, ANALYSIS_RATE)
    return recorded
