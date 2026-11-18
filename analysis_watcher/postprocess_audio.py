import os
from pathlib import Path
import threading
import pydub


def _atomic_export_mp3(segment, target_path: str):
    out = Path(target_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        segment.export(str(tmp), format="mp3")
        os.replace(str(tmp), str(out))
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except Exception:
                pass


def decrease_volume(audio_path: str, dB: int, output_path: str = None):
    """
    Decrease the volume of an audio file by dB decibels.
    """
    if output_path is None:
        output_path = audio_path

    audio = pydub.AudioSegment.from_file(audio_path)
    silent_audio = audio + dB  # negative dB decreases volume
    _atomic_export_mp3(silent_audio, output_path)
    print(f"Volume decreased by {dB}dB. Saved to {output_path}")


def overlay_audio(audio1="output_ryan.mp3", audio2="output_rae.mp3", output_name="output.mp3"):
    seg1 = pydub.AudioSegment.from_file(audio1)
    seg2 = pydub.AudioSegment.from_file(audio2)
    output = seg2.overlay(seg1)
    _atomic_export_mp3(output, str(output_name))
    return output_name