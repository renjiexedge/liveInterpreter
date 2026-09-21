import asyncio
import logging
import os
import sys

from audio.devices import DeviceNotFoundError
from audio.pipeline import build_audio_pipeline
from config.audio_config import AudioDeviceConfig, AudioPipelineConfig


async def main() -> None:
    # Imported here so the friendly key check below runs before
    # engines.gemini_live builds its genai.Client at import time.
    from engines.gemini_live import run_session

    # JitterBuffer grabs the running event loop, so this must be called
    # from inside an async function.
    router, jitter_buffer = build_audio_pipeline(AudioDeviceConfig(), AudioPipelineConfig())

    router.start()
    print("Listening on the virtual cable. Press Ctrl+C to stop.")
    try:
        await run_session(audio_queue=jitter_buffer)
    finally:
        router.stop()
        if jitter_buffer.dropped_count:
            print(f"Dropped {jitter_buffer.dropped_count} audio chunks to stay real-time.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if not os.environ.get("GEMINI_API_KEY"):
        sys.exit("GEMINI_API_KEY environment variable is not set.")

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Stopped.")
    except DeviceNotFoundError as e:
        sys.exit(str(e))
