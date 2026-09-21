from audio.buffers import JitterBuffer
from audio.router import AudioRouter
from config.audio_config import AudioDeviceConfig, AudioPipelineConfig


def build_audio_pipeline(
    device_config: AudioDeviceConfig,
    pipeline_config: AudioPipelineConfig,
) -> tuple[AudioRouter, JitterBuffer]:
    """Wires AudioRouter -> Resampler -> JitterBuffer together.

    The returned JitterBuffer satisfies engines.gemini_live.run_session's
    audio_queue contract (async get() -> bytes | None) directly:

        router, jitter_buffer = build_audio_pipeline(device_cfg, pipeline_cfg)
        router.start()
        await run_session(audio_queue=jitter_buffer)
        router.stop()
    """
    jitter_buffer = JitterBuffer(maxsize=pipeline_config.jitter_max_chunks)
    router = AudioRouter(device_config, pipeline_config, jitter_buffer)
    return router, jitter_buffer
