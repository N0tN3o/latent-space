import os
import platform
import subprocess
import sys
from pathlib import Path
from elevenlabs.client import ElevenLabs

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config_manager import load_env_vars


def el_tts_setup(location=None):
    """Initialize ElevenLabs client using config_manager or .env file."""
    env = load_env_vars()
    api_key = env.get("API_KEY") or env.get("ELEVENLABS_API_KEY") or ""
    if not api_key and location and Path(location).exists():
        for line in Path(location).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                if k.strip() in ("API_KEY", "ELEVENLABS_API_KEY"):
                    api_key = v.strip().strip('"').strip("'")
                    break
    return ElevenLabs(api_key=api_key)


def run_el_tts(client, text: str, voice_id='R8MYc5Q5y0TurlOQoX88', output='output.mp3', speed=1.0):
    """Convert text to speech via ElevenLabs."""
    voice_settings = {'speed': speed}
    audio = client.text_to_speech.convert(
        text=text,
        voice_id=voice_id,
        model_id="eleven_v3",
        output_format="mp3_44100_128",
        voice_settings=voice_settings,
    )
    with open(str(output), 'wb') as file:
        for chunk in audio:
            file.write(chunk)
    return str(output)


def open_audio_file(filename: str):
    """Open audio file in the system default audio player across Windows, macOS, and Linux."""
    if platform.system() == 'Windows':
        os.startfile(str(filename))
    elif platform.system() == 'Darwin':
        subprocess.run(['open', str(filename)], check=False)
    else:
        subprocess.run(['xdg-open', str(filename)], check=False)
