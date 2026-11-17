def run_el_tts(client, text: str, voice_id="R8MYc5Q5y0TurlOQoX88", output="output.mp3", speed=1.0):
    """Convert text to speech via ElevenLabs."""
    voice_settings = {"speed": speed}
    audio = client.text_to_speech.convert(
        text=text,
        voice_id=voice_id,
        model_id="eleven_v3",
        output_format="mp3_44100_128",
        voice_settings=voice_settings,
    )
    with open(str(output), "wb") as file:
        for chunk in audio:
            file.write(chunk)
    return str(output)
