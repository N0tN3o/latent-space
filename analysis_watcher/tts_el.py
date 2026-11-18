from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config_manager import atomic_write_bytes


def run_el_tts(
    client,
    text: str,
    voice_id="R8MYc5Q5y0TurlOQoX88",
    output="output.mp3",
    speed=1.0,
    model_id="eleven_multilingual_v2",
):
    """Convert text to speech via ElevenLabs and write atomically."""
    voice_settings = {"speed": speed}
    audio = client.text_to_speech.convert(
        text=text,
        voice_id=voice_id,
        model_id=model_id,
        output_format="mp3_44100_128",
        voice_settings=voice_settings,
    )
    data = b"".join(chunk for chunk in audio if chunk)
    atomic_write_bytes(Path(output), data)
    return str(output)
