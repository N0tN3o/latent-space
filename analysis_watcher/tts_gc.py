from pathlib import Path
import sys
from google.cloud import texttospeech

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config_manager import atomic_write_bytes


def _infer_gc_language_code(voice_id: str) -> str:
    """Infer BCP-47 language_code prefix (e.g. nl-NL, en-GB) from a Google Cloud voice name."""
    parts = (voice_id or "").strip().split("-")
    if (
        len(parts) >= 3
        and 2 <= len(parts[0]) <= 3 and parts[0].isalpha()
        and 2 <= len(parts[1]) <= 4 and parts[1].isalpha()
    ):
        return f"{parts[0].lower()}-{parts[1].upper()}"
    return "en-US"


def gc_tts_setup(voice_id="en-US-Chirp3-HD-Zephyr", speaking_rate=1.0, pitch=0.0):
    """
    Configure Google Cloud credentials and TTS properties.

    Returns:
        client: the Google Cloud client used to execute commands
        voice: the configured voice
        audio_config: properties of the audio
    """
    client = texttospeech.TextToSpeechClient()
    lang_code = _infer_gc_language_code(voice_id)
    voice = texttospeech.VoiceSelectionParams(
        language_code=lang_code,
        name=f"{voice_id}",
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
    atomic_write_bytes(Path(filename), response.audio_content)
    return filename
