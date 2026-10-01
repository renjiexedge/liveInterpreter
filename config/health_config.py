from dataclasses import dataclass
from pathlib import Path


@dataclass
class LoopTestConfig:
    """Tunables for audio/routing_check.run_loop_test. The NCC thresholds are
    starting points: calibrate them against a deliberate loop and deliberate
    speaker-to-mic bleed (guide/two_way_plan.md §6, open question 12)."""

    clip_path: Path = Path("audio_test/loop_test.wav")  # 16-bit PCM WAV; synthetic chirps if missing
    max_lag_s: float = 1.0      # device + driver latency searched by the correlation
    min_ncc: float = 0.25       # peak normalised cross-correlation that counts as "heard"
    min_ratio: float = 4.0      # ...and it must be this many times the silent baseline's peak
    min_rms: float = 1e-3       # ignore matches quieter than about -60 dBFS
    play_peak: float = 0.5      # clip is normalised to this peak before playing


@dataclass
class HealthConfig:
    """Tunables for audio/health.HealthMonitor."""

    poll_s: float = 0.25
    disconnect_after_s: float = 1.5     # no device callback for this long -> disconnected
    no_signal_after_s: float = 30.0     # pure digital silence for this long -> no signal
    clip_level: float = 0.99
    clip_ratio: float = 0.05            # fraction of blocks clipping within window_s
    window_s: float = 5.0               # counters are judged over this window...
    clear_after_s: float = 10.0         # ...and clear after this long without growth
    stutter_count: int = 3              # mid-speech underruns within window_s
    # Only reachable when AudioPlayer's queue cap (MAX_OUTPUT_QUEUE_MS, 1200 ms) is
    # off or raised above it. With the cap on, OUTPUT_SKIPPED reports the drops instead.
    lag_warn_ms: float = 3000.0
    lag_clear_ms: float = 1000.0

    # Echo detector (transcript-based).
    echo_window_s: float = 20.0         # how far back output text is kept
    echo_min_new_chars: int = 20        # re-check once an input grows by this much
    echo_probe_chars: int = 30          # latest input characters compared against outputs
    echo_min_probe_chars: int = 20
    echo_match_ratio: float = 0.8       # contiguous match covering this share of the probe
    echo_strike_window_s: float = 30.0  # 2 hits within this window -> red
    # Off: an echo is only reported. On: the 2nd strike also mutes the player that
    # produced the echoed audio until the user clicks Resume.
    auto_mute_on_echo: bool = False
