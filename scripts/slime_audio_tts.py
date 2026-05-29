#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import socket
import struct
import subprocess
import tempfile
import time
import uuid
import wave
from pathlib import Path

import edge_tts


async def synthesize(text: str, mp3_path: Path, voice: str, rate: str) -> None:
    communicate = edge_tts.Communicate(text, voice=voice, rate=rate)
    await communicate.save(str(mp3_path))


def convert_to_wav(mp3_path: Path, wav_path: Path, volume: float) -> None:
    subprocess.run(
        [
            "gst-launch-1.0",
            "-q",
            "filesrc",
            f"location={mp3_path}",
            "!",
            "decodebin",
            "!",
            "volume",
            f"volume={volume}",
            "!",
            "audioconvert",
            "!",
            "audioresample",
            "!",
            "audio/x-raw,format=S16LE,channels=1,rate=24000",
            "!",
            "wavenc",
            "!",
            "filesink",
            f"location={wav_path}",
        ],
        check=True,
    )


def send_wav(wav_path: Path, host: str, port: int, delay_ms: int) -> None:
    with wave.open(str(wav_path), "rb") as audio:
        channels = audio.getnchannels()
        rate = audio.getframerate()
        width = audio.getsampwidth()
        frames = audio.readframes(audio.getnframes())

    if width != 2:
        raise SystemExit("expected 16-bit PCM wav")

    session = uuid.uuid4()
    start_ms = int((time.time() * 1000) + delay_ms)
    chunk_frames = max(1, rate // 20)
    chunk_bytes = chunk_frames * channels * width
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    sequence = 0
    for offset in range(0, len(frames), chunk_bytes):
        payload = frames[offset : offset + chunk_bytes]
        header = (
            b"SLA1"
            + bytes([1])
            + session.bytes_le
            + struct.pack("<iqihhh", sequence, start_ms, rate, channels, 16, len(payload))
        )
        sock.sendto(header + payload, (host, port))
        sequence += 1
        time.sleep(0.045)

    header = (
        b"SLA1"
        + bytes([2])
        + session.bytes_le
        + struct.pack("<iqihhh", sequence, start_ms, rate, channels, 16, 0)
    )
    sock.sendto(header, (host, port))
    print(f"sent session={session} bytes={len(frames)} target={host}:{port}")


def main() -> int:
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
