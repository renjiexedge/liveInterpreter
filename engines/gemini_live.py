import asyncio
import os

from google import genai
from google.genai import types

API_KEY = os.environ.get("GEMINI_API_KEY")
MODEL = "gemini-3.5-live-translate-preview"
TARGET_LANGUAGE_CODE = "en"  # Set your desired target language code here

client = genai.Client(api_key=API_KEY)

config = types.LiveConnectConfig(
    response_modalities=["AUDIO"],
    input_audio_transcription=types.AudioTranscriptionConfig(),
    output_audio_transcription=types.AudioTranscriptionConfig(),
    translation_config=types.TranslationConfig(
        target_language_code=TARGET_LANGUAGE_CODE,
        echo_target_language=True,
    ),
)


async def send_audio(session, audio_queue):
    """Stream PCM chunks from audio_queue to Gemini until a None sentinel arrives."""
    while True:
        chunk = await audio_queue.get()
        if chunk is None:
            break
        await session.send_realtime_input(
            audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000")
        )


async def receive_responses(session):
    """Print transcripts and report translated audio as it streams back."""
    async for response in session.receive():
        server_content = response.server_content
        if not server_content:
            continue
        if server_content.input_transcription:
            print(f"Input transcript: {server_content.input_transcription.text}")
        if server_content.output_transcription:
            print(f"Output transcript: {server_content.output_transcription.text}")
        if server_content.model_turn:
            for part in server_content.model_turn.parts:
                if part.inline_data:
                    audio_data = part.inline_data.data
                    print(f"Received audio chunk ({len(audio_data)} bytes)")


async def run_session(audio_queue):
    """Open a Live translation session and run sending/receiving concurrently.

    Stops as soon as either side finishes (e.g. the caller stops feeding
    audio, or the server closes the stream), cancelling the other.
    """
    async with client.aio.live.connect(model=MODEL, config=config) as session:
        print("Session started with translation")
        send_task = asyncio.create_task(send_audio(session, audio_queue))
        receive_task = asyncio.create_task(receive_responses(session))

        done, pending = await asyncio.wait(
            {send_task, receive_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            task.result()  # re-raise any exception the task hit


async def main():
    audio_queue = asyncio.Queue()

    # Demo: feed one placeholder PCM chunk, then signal end of stream.
    await audio_queue.put(b"\x00\x00\x00\x00\x00\x00\x00\x00")
    await audio_queue.put(None)

    await run_session(audio_queue)


if __name__ == "__main__":
    asyncio.run(main())
