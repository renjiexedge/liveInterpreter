import numpy as np
import soxr


class Resampler:
    """Converts native-format audio into mono 16-bit little-endian PCM at a
    target rate. Stateful across calls to process() so chunk boundaries stay
    click-free — only ever call it from a single thread.
    """

    def __init__(self, in_rate: int, in_channels: int, out_rate: int = 16000, quality: str = "HQ"):
        self.in_rate = in_rate
        self.in_channels = in_channels
        self.out_rate = out_rate
        self._stream = soxr.ResampleStream(in_rate, out_rate, 1, dtype="float32", quality=quality)

    def process(self, frames: np.ndarray) -> bytes:
        mono = self._downmix(frames)
        resampled = self._stream.resample_chunk(mono, last=False)
        return self._to_pcm_bytes(resampled)

    def flush(self) -> bytes:
        resampled = self._stream.resample_chunk(np.empty(0, dtype="float32"), last=True)
        return self._to_pcm_bytes(resampled)

    def _downmix(self, frames: np.ndarray) -> np.ndarray:
        if frames.ndim == 1 or frames.shape[1] == 1:
            return frames.reshape(-1).astype("float32", copy=False)
        return frames.mean(axis=1).astype("float32", copy=False)

    def _to_pcm_bytes(self, samples: np.ndarray) -> bytes:
        clipped = np.clip(samples, -1.0, 1.0)
        pcm16 = (clipped * 32767.0).astype("<i2")
        return pcm16.tobytes()
