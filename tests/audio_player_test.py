"""Feeds a .wav file straight into AudioPlayer, bypassing Gemini and the
live session entirely.

Isolates AudioPlayer's resample/device-output path from the rest of the
pipeline: if a wav file that sounds clean externally (e.g. in Audacity or
VLC) comes out distorted here, the bug is confirmed to live in AudioPlayer's
resample step or its sd.OutputStream setup -- not upstream in Gemini's
audio or session timing.

Usage (from the project root):
    env/Scripts/python.exe tests/audio_player_test.py
    env/Scripts/python.exe tests/audio_player_test.py path/to/file.wav
    env/Scripts/python.exe tests/audio_player_test.py --device "Voicemeeter Input"

With no wav path given, plays the first .wav file found in
tests/sample_audio/ -- drop a debug_dumps/received_*.wav from a live
session in there (or any mono 16-bit PCM wav) to test with it.

--device selects the output device the same way AudioDeviceConfig always
does elsewhere in the project (case-insensitive name substring match);
default is "CABLE" to match AudioDeviceConfig's own default. Pass whatever
device you're actually debugging (e.g. "Voicemeeter Input") to reproduce
the same conditions you heard the distortion under.

--dump-resampled PATH additionally writes the exact post-resample samples
to a wav file, bypassing the device entirely, so you can compare it
against the pre-resample dump (RawAudioDumper / debug_dumps/) and see
exactly which stage introduces the distortion.

--use-play skips AudioPlayer/sd.OutputStream entirely and plays the wav via
sd.play() instead -- a different, higher-level sounddevice code path with
its own buffering. Point it at an already-native-rate file (e.g. a
--dump-resampled output) so no resampling is needed either way. If this
sounds clean while the normal path doesn't, the bug is confirmed to be in
how player.py configures its OutputStream, not in outputting PCM to this
device/driver in general.
"""

import argparse
import logging
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import sounddevice as sd

from audio.devices import find_output_device
from audio.player import AudioPlayer
from config.audio_config import AudioDeviceConfig

SAMPLE_AUDIO_DIR = Path(__file__).resolve().parent / "sample_audio"
CHUNK_MS = 20  # matches AudioPipelineConfig's default chunk size elsewhere in the project


def _find_wav() -> Path:
    candidates = sorted(SAMPLE_AUDIO_DIR.glob("*.wav"))
    if not candidates:
        raise FileNotFoundError(
            f"No .wav files found in {SAMPLE_AUDIO_DIR}. Drop one there "
            f"(e.g. a debug_dumps/received_*.wav from a live session) or "
            f"pass a path explicitly."
        )
    return candidates[0]


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "wav_path",
        nargs="?",
        type=Path,
        help="Wav file to play (defaults to the first .wav in tests/sample_audio/)",
    )
    parser.add_argument(
        "--device",
        default="CABLE",
        help='Output device name substring, matching AudioDeviceConfig (default: "CABLE")',
    )
    parser.add_argument(
        "--dump-resampled",
        type=Path,
        default=None,
        help=(
            "Also write the exact post-resample samples (what's handed to the "
            "device) to this .wav path, so it can be played back independent "
            "of any device/driver to see whether resample_chunk() itself is "
            "where the corruption starts."
        ),
    )
    parser.add_argument(
        "--use-play",
        action="store_true",
        help=(
            "Bypass AudioPlayer/sd.OutputStream and play via sd.play() "
            "instead, to isolate whether OutputStream's own configuration "
            "is the source of distortion. Point this at an already "
            "native-rate wav (e.g. a --dump-resampled file)."
        ),
    )
    parser.add_argument(
        "--chunk-ms",
        type=int,
        default=CHUNK_MS,
        help=(
            "Size of each write() call in ms of source audio (default: 20, "
            "matching AudioPipelineConfig elsewhere). Try a much larger "
            "value (e.g. 1000, or larger than the file's own duration) to "
            "see whether fewer/bigger write() calls reduce static -- "
            "indicating write-frequency/Python-loop overhead rather than "
            "the stream's buffer size."
        ),
    )
    args = parser.parse_args()

    wav_path = args.wav_path or _find_wav()

    with wave.open(str(wav_path), "rb") as wav_file:
        channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()
        frame_rate = wav_file.getframerate()
        frames = wav_file.readframes(wav_file.getnframes())

    if channels != 1 or sample_width != 2:
        raise ValueError(
            f"{wav_path} is {channels}-channel {sample_width * 8}-bit; "
            f"AudioPlayer expects mono 16-bit PCM, same as Gemini's output."
        )

    print(f"Source: {frame_rate} Hz mono 16-bit, {len(frames)} bytes")

    if args.use_play:
        device = find_output_device(AudioDeviceConfig(name_substring=args.device))
        print(f"Playing {wav_path} via sd.play() on {device.name!r} (no AudioPlayer involved)...")
        samples = np.frombuffer(frames, dtype="<i2")
        sd.play(samples, samplerate=frame_rate, device=device.index)
        sd.wait()
        print("Done (sd.play).")
        return

    print(f"Playing {wav_path} through AudioPlayer...")

    player = AudioPlayer(
        device_config=AudioDeviceConfig(name_substring=args.device),
        source_rate=frame_rate,
        debug_dump_path=args.dump_resampled,
    )
    player.start()

    # AudioPlayer.write() no longer blocks -- it just resamples and appends
    # to an internal buffer that a real-time callback drains on its own
    # schedule. No manual pacing needed here either way: the chunk size only
    # affects how many Python-side write() calls happen, not playback timing.
    chunk_bytes = int(frame_rate * args.chunk_ms / 1000) * sample_width
    start = time.perf_counter()
    try:
        for offset in range(0, len(frames), chunk_bytes):
            player.write(frames[offset : offset + chunk_bytes])
    finally:
        player.stop()  # blocks until the callback has drained the buffer
    elapsed = time.perf_counter() - start

    # Resampling preserves duration, so the source's own duration is what
    # actual playback should take. Timed across the write loop AND stop()'s
    # drain wait (stop() is what actually blocks until playback finishes
    # now), so this still catches a real rate-negotiation mismatch at the
    # device layer, distinct from anything in resample_chunk().
    expected_duration = len(frames) / sample_width / frame_rate
    print(
        f"Done. {player.underrun_count} underrun(s) reported. "
        f"Expected duration: {expected_duration:.3f}s, actual wall time: {elapsed:.3f}s "
        f"(ratio: {elapsed / expected_duration:.3f})"
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        main()
    except KeyboardInterrupt:
        print("Stopped.")
