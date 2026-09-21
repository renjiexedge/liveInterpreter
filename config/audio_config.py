from dataclasses import dataclass


@dataclass
class AudioDeviceConfig:
    """Selects which input device AudioRouter should capture from."""

    name_substring: str = "CABLE"
    device_index: int | None = None
    exact_name: str | None = None


@dataclass
class AudioPipelineConfig:
    """Tunables for the resample/chunk/buffer stages."""

    chunk_ms: int = 20
    target_rate: int = 16000
    jitter_max_chunks: int = 10
    resample_quality: str = "HQ"
