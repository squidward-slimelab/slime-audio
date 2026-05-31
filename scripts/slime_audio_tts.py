#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import subprocess
import tempfile
from pathlib import Path

import edge_tts


async def synthesize(text: str, mp3_path: Path, voice: str, rate: str) -> None:
    communicate = edge_tts.Communicate(text, voice=voice, rate=rate)
    await communicate.save(str(mp3_path))


def convert_to_wav(mp3_path: Path, wav_path: Path, volume: float) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(mp3_path),
            "-filter:a",
            f"volume={volume}",
            "-acodec",
            "pcm_s16le",
            "-ac",
            "1",
            "-ar",
            "24000",
            str(wav_path),
        ],
        check=True,
    )


def send_wav(wav_path: Path, host: str, port: int, delay_ms: int) -> None:
    raise RuntimeError(
        "direct TTS audio sending has been removed; plan mic lean-ins with "
        "scripts/slime_audio_lean_ins.py and render/stream the mix session"
    )

def main() -> int:
    raise SystemExit(
        "packet audio TTS transport has been removed; plan mic lean-ins with "
        "scripts/slime_audio_lean_ins.py and render/stream the mix session"
    )
    parser = argparse.ArgumentParser()
    parser.add_argument("text")
    parser.add_argument("--host", default="192.168.0.163")
    parser.add_argument("--port", type=int, default=47777)
    parser.add_argument("--voice", default="en-US-GuyNeural")
    parser.add_argument("--rate", default="-8%")
    parser.add_argument("--volume", type=float, default=1.0)
    parser.add_argument("--delay-ms", type=int, default=1800)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        mp3_path = Path(tmp) / "tts.mp3"
        wav_path = Path(tmp) / "tts.wav"
        asyncio.run(synthesize(args.text, mp3_path, args.voice, args.rate))
        convert_to_wav(mp3_path, wav_path, args.volume)
        send_wav(wav_path, args.host, args.port, args.delay_ms)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
