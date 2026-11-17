from google.cloud import texttospeech


def gc_tts_setup(voice_id="en-US-Chirp3-HD-Zephyr", speaking_rate=1.0, pitch=0.0):
    """
    Configure Google Cloud credentials and TTS properties.

    Returns:
        client: the Google Cloud client used to execute commands
        voice: the configured voice
        audio_config: properties of the audio
    """
    client = texttospeech.TextToSpeechClient()
    voice = texttospeech.VoiceSelectionParams(
        language_code="en-US",
        name=f"{voice_id}",
        ssml_gender=texttospeech.SsmlVoiceGender.FEMALE,
    )
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.MP3,
        speaking_rate=speaking_rate,
        pitch=pitch,
    )
    return client, voice, audio_config


def run_gc_tts(client, voice, audio_config, text: str, output: str):
    """Synthesize speech from the given text into <output>.mp3."""
    synthesis_input = texttospeech.SynthesisInput(text=text)
    response = client.synthesize_speech(
        input=synthesis_input,
        voice=voice,
        audio_config=audio_config,
    )
    filename = f"{output}.mp3"
    with open(filename, "wb") as out:
        out.write(response.audio_content)
    return filename
