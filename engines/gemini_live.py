import asyncio
from google import genai
from google.genai import types
from requests import session


client = genai.Client()
model="gemini-3.5-live-translate-preview"
target_language_code = "en"  # Set your desired target language code here
chunk = b'\x00\x00\x00\x00\x00\x00\x00\x00'  # Example raw PCM audio bytes

config = types.LiveConnectConfig(
    response_modalities=["AUDIO"],
    input_audio_transcription=types.AudioTranscriptionConfig(),
    output_audio_transcription=types.AudioTranscriptionConfig(),
    translation_config=types.TranslationConfig(
        target_language_code=target_language_code,
        echo_target_language=True
    )
)


async def main():
    async with client.aio.live.connect(model=model, config=config) as session:
        print("Session started with translation")
        # Start receiving the translated audio stream
        async for response in session.receive():
            if response.server_content:
                if response.server_content.input_transcription:
                    print(f"Input transcript: {response.server_content.input_transcription.text}")
                if response.server_content.output_transcription:
                    print(f"Output transcript: {response.server_content.output_transcription.text}")
                if response.server_content.model_turn:
                    for part in response.server_content.model_turn.parts:
                        if part.inline_data:
                            audio_data = part.inline_data.data
                            # Play or process the translated audio chunk
                            print(f"Received audio chunk ({len(audio_data)} bytes)")

        # Assuming 'chunk' is your raw PCM audio bytes
        await session.send_realtime_input(
            audio=types.Blob(
            data=chunk,
            mime_type="audio/pcm;rate=16000"
        )
)



if __name__ == "__main__":
    asyncio.run(main())