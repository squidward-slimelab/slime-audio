#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import uuid
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

DISCOVER_MESSAGE = b"SLIME_AUDIO_DISCOVER_V1"
DEFAULT_PORT = 47777


@dataclass(frozen=True)
class Receiver:
    endpoint: str
    host: str
    port: int
    machine_name: str
    user_name: str
    version: str


def discover_receivers(port: int = DEFAULT_PORT, timeout_ms: int = 2500) -> list[Receiver]:
    found: dict[str, Receiver] = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.5)
        sock.sendto(DISCOVER_MESSAGE, ("255.255.255.255", port))
        deadline = socket.getdefaulttimeout()

        import time

        stop_at = time.monotonic() + (timeout_ms / 1000)
        while time.monotonic() < stop_at:
            try:
                payload, address = sock.recvfrom(4096)
            except TimeoutError:
                continue
            except socket.timeout:
                continue
            response = parse_discovery_response(payload, address[0])
            if response is not None:
                found[response.endpoint] = response

    return sorted(found.values(), key=lambda receiver: receiver.machine_name.casefold())


def parse_discovery_response(payload: bytes, host: str) -> Receiver | None:
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if data.get("App") != "slime-audio":
        return None

    port = int(data.get("Port") or DEFAULT_PORT)
    return Receiver(
        endpoint=f"{host}:{port}",
        host=host,
        port=port,
        machine_name=str(data.get("MachineName") or host),
        user_name=str(data.get("UserName") or ""),
        version=str(data.get("Version") or ""),
    )


def resolve_targets(values: Iterable[str], discovered: list[Receiver], default_port: int = DEFAULT_PORT) -> list[Receiver]:
    requested = list(values)
    if not requested:
        raise ValueError("at least one --target is required; use --target all for every discovered receiver")

    by_name = {receiver.machine_name.casefold(): receiver for receiver in discovered}
    by_endpoint = {receiver.endpoint.casefold(): receiver for receiver in discovered}
    targets: list[Receiver] = []

    for value in requested:
        normalized = value.casefold()
        if normalized == "all":
            targets.extend(discovered)
        elif normalized in by_name:
            targets.append(by_name[normalized])
        elif normalized in by_endpoint:
            targets.append(by_endpoint[normalized])
        else:
            host, port = parse_endpoint(value, default_port)
            targets.append(Receiver(f"{host}:{port}", host, port, host, "", "manual"))

    unique: dict[str, Receiver] = {}
    for target in targets:
        unique[target.endpoint] = target
    return list(unique.values())


def parse_endpoint(value: str, default_port: int = DEFAULT_PORT) -> tuple[str, int]:
    if ":" not in value:
        return value, default_port
    host, port_text = value.rsplit(":", 1)
    return host, int(port_text)


def convert_with_vlc(input_path: Path, output_path: Path, sample_rate: int, channels: int) -> None:
    vlc = shutil.which("cvlc") or shutil.which("vlc")
    if vlc is None:
        raise FileNotFoundError("cvlc/vlc is not installed")

    subprocess.run(
        [
            vlc,
            "-I",
            "dummy",
            "--no-video",
            "--play-and-exit",
            str(input_path),
            f"--sout=#transcode{{acodec=s16l,channels={channels},samplerate={sample_rate}}}:std{{access=file,mux=wav,dst={output_path}}}",
        ],
        check=True,
    )


def convert_with_gstreamer(input_path: Path, output_path: Path, sample_rate: int, channels: int) -> None:
    subprocess.run(
        [
            "gst-launch-1.0",
            "-q",
            "filesrc",
            f"location={input_path}",
            "!",
            "decodebin",
            "!",
            "audioconvert",
            "!",
            "audioresample",
            "!",
            f"audio/x-raw,format=S16LE,channels={channels},rate={sample_rate}",
            "!",
            "wavenc",
            "!",
            "filesink",
            f"location={output_path}",
        ],
        check=True,
    )


def convert_to_stream_wav(input_path: Path, output_path: Path, backend: str, sample_rate: int, channels: int) -> str:
    if backend in {"auto", "vlc"}:
        try:
            convert_with_vlc(input_path, output_path, sample_rate, channels)
            return "vlc"
        except (FileNotFoundError, subprocess.CalledProcessError):
            if backend == "vlc":
                raise

    convert_with_gstreamer(input_path, output_path, sample_rate, channels)
    return "gstreamer"


def run_multicast_stream(
    input_path: Path,
    group: str,
    port: int,
    backend: str,
    sample_rate: int,
    channels: int,
) -> None:
    if backend == "vlc":
        raise SystemExit("multicast mode currently uses gstreamer; use --backend auto or --backend gstreamer")
    subprocess.run(
        [
            "gst-launch-1.0",
            "-q",
            "filesrc",
            f"location={input_path}",
            "!",
            "decodebin",
            "!",
            "audioconvert",
            "!",
            "audioresample",
            "!",
            f"audio/x-raw,format=S16BE,channels={channels},rate={sample_rate}",
            "!",
            "rtpL16pay",
            "pt=96",
            "!",
            "udpsink",
            f"host={group}",
            f"port={port}",
            "auto-multicast=true",
            "ttl-mc=2",
        ],
        check=True,
    )


def stream_wav_synced(wav_path: Path, targets: list[Receiver], delay_ms: int, packet_delay_ms: int) -> None:
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
    endpoints = [(target.host, target.port) for target in targets]

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sequence = 0
        for offset in range(0, len(frames), chunk_bytes):
            payload = frames[offset : offset + chunk_bytes]
            packet = encode_audio_packet(session, sequence, start_ms, rate, channels, payload)
            for endpoint in endpoints:
                sock.sendto(packet, endpoint)
            sequence += 1
            time.sleep(packet_delay_ms / 1000)

        end = encode_audio_packet(session, sequence, start_ms, rate, channels, b"", packet_type=2)
        for endpoint in endpoints:
            sock.sendto(end, endpoint)

    print(
        f"sent session={session} bytes={len(frames)} start={start_ms} "
        f"targets={','.join(target.endpoint for target in targets)}"
    )


def encode_audio_packet(
    session: uuid.UUID,
    sequence: int,
    start_ms: int,
    rate: int,
    channels: int,
    payload: bytes,
    packet_type: int = 1,
) -> bytes:
    header = (
        b"SLA1"
        + bytes([packet_type])
        + session.bytes_le
        + struct.pack("<iqihhh", sequence, start_ms, rate, channels, 16, len(payload))
    )
    return header + payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Stream a local audio file to SlimeAudio receivers with synced start.")
    parser.add_argument("file", type=Path)
    parser.add_argument("--target", action="append", required=True, help="Receiver name, host:port, or all")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--discover-timeout-ms", type=int, default=2500)
    parser.add_argument("--backend", choices=["auto", "vlc", "gstreamer"], default="auto")
    parser.add_argument("--mode", choices=["packets", "multicast"], default="packets")
    parser.add_argument("--multicast-group", default="239.77.77.77")
    parser.add_argument("--multicast-port", type=int, default=47778)
    parser.add_argument("--delay-ms", type=int, default=2500)
    parser.add_argument("--packet-delay-ms", type=int, default=45)
    parser.add_argument("--sample-rate", type=int, default=48000)
    parser.add_argument("--channels", type=int, default=2)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.file.exists():
        raise SystemExit(f"file not found: {args.file}")

    discovered = discover_receivers(args.port, args.discover_timeout_ms)
    targets = resolve_targets(args.target, discovered, args.port)
    if not targets:
        raise SystemExit("no targets resolved")

    if args.dry_run:
        for target in targets:
            print(f"target {target.endpoint}\t{target.machine_name}\t{target.version}")
        if args.mode == "multicast":
            print(f"multicast {args.multicast_group}:{args.multicast_port}")
        return 0

    if args.mode == "multicast":
        print(
            f"multicast backend=gstreamer file={args.file} "
            f"group={args.multicast_group}:{args.multicast_port} targets={len(targets)}",
            flush=True,
        )
        run_multicast_stream(args.file, args.multicast_group, args.multicast_port, args.backend, args.sample_rate, args.channels)
        return 0

    with tempfile.TemporaryDirectory(prefix="slime-audio-stream-") as tmp:
        wav_path = Path(tmp) / "stream.wav"
        backend = convert_to_stream_wav(args.file, wav_path, args.backend, args.sample_rate, args.channels)
        print(f"decoded backend={backend} file={args.file} targets={len(targets)}")
        stream_wav_synced(wav_path, targets, args.delay_ms, args.packet_delay_ms)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
