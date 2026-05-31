#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

DISCOVER_MESSAGE = b"SLIME_AUDIO_DISCOVER_V1"
SHARED_STREAM_START_MESSAGE = b"SLIME_AUDIO_SHARED_STREAM_START_V1"
SHARED_STREAM_STOP_MESSAGE = b"SLIME_AUDIO_SHARED_STREAM_STOP_V1"
RESET_AUDIO_MESSAGE = b"SLIME_AUDIO_RESET_AUDIO_V1"
EFFECT_MESSAGE_PREFIX = b"SLIME_AUDIO_EFFECT_V1 "
DEFAULT_PORT = 47777
DEFAULT_LIVE_DELAY_MS = 7000
DEFAULT_PREBUFFER_MS = 15000


@dataclass(frozen=True)
class Receiver:
    endpoint: str
    host: str
    port: int
    machine_name: str
    user_name: str
    version: str
    rtt_ms: float = 0.0
    clock_offset_ms: float = 0.0
    stream_muted: bool = False
    diagnostics: dict | None = None


def format_diagnostics(
    diagnostics: dict | None,
    now_ms: float | None = None,
    clock_offset_ms: float = 0.0,
) -> str:
    if not diagnostics:
        return "diag=none"
    now_ms = now_ms if now_ms is not None else time.time() * 1000
    last_packet_ms = float(diagnostics.get("LastPacketUnixTimeMs") or 0)
    receiver_now_ms = now_ms + clock_offset_ms
    last_packet_age_ms = max(0.0, receiver_now_ms - last_packet_ms) if last_packet_ms else 0.0
    return (
        f"diag_sessions={diagnostics.get('ActiveSessions', 0)}"
        f"\tdiag_packets={diagnostics.get('ReceivedPackets', 0)}"
        f"\tdiag_missing_frames={diagnostics.get('MissingFrames', 0)}"
        f"\tdiag_reads={diagnostics.get('ReadCalls', 0)}"
        f"\tdiag_buffered_packets={diagnostics.get('MaxBufferedPackets', 0)}"
        f"\tdiag_packet_span={diagnostics.get('MaxBufferedPacketSpan', 0)}"
        f"\tdiag_latest_seq={diagnostics.get('LatestSequence', -1)}"
        f"\tdiag_last_packet_age_ms={last_packet_age_ms:.0f}"
        f"\tdiag_resets={diagnostics.get('ResetCount', 0)}"
        f"\tdiag_decode_failures={diagnostics.get('DecodeFailures', 0)}"
        f"\tshared_stream_listening={str(bool(diagnostics.get('SharedStreamListening'))).lower()}"
        f"\tshared_stream_exit_code={diagnostics.get('SharedStreamExitCode')}"
        f"\tshared_stream_status={diagnostics.get('SharedStreamStatus') or ''}"
        f"\tshared_stream_host={diagnostics.get('SharedStreamServerHost') or ''}"
        f"\tshared_stream_pid={diagnostics.get('SharedStreamProcessId')}"
        f"\tshared_stream_started_ms={diagnostics.get('SharedStreamStartedUnixTimeMs', 0)}"
        f"\tshared_stream_last_exit_ms={diagnostics.get('SharedStreamLastExitUnixTimeMs', 0)}"
        f"\tshared_stream_exits={diagnostics.get('SharedStreamExitCount', 0)}"
        f"\tshared_stream_last_stderr_ms={diagnostics.get('SharedStreamLastStderrUnixTimeMs', 0)}"
        f"\ttelemetry_path={diagnostics.get('SharedStreamTelemetryPath') or ''}"
    )


def discover_receivers(port: int = DEFAULT_PORT, timeout_ms: int = 2500) -> list[Receiver]:
    found: dict[str, Receiver] = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.5)
        sent_ms = time.time() * 1000
        sock.sendto(DISCOVER_MESSAGE, ("255.255.255.255", port))
        stop_at = time.monotonic() + (timeout_ms / 1000)
        while time.monotonic() < stop_at:
            try:
                payload, address = sock.recvfrom(4096)
                received_ms = time.time() * 1000
            except TimeoutError:
                continue
            except socket.timeout:
                continue
            response = parse_discovery_response(payload, address[0], sent_ms, received_ms)
            if response is not None:
                found[response.endpoint] = response

    return sorted(found.values(), key=lambda receiver: receiver.machine_name.casefold())


def parse_discovery_response(
    payload: bytes,
    host: str,
    sent_ms: float | None = None,
    received_ms: float | None = None,
) -> Receiver | None:
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if data.get("App") != "slime-audio":
        return None

    port = int(data.get("Port") or DEFAULT_PORT)
    rtt_ms = max(0.0, (received_ms - sent_ms)) if sent_ms is not None and received_ms is not None else 0.0
    receiver_time = float(data.get("UnixTimeMs") or 0)
    midpoint_ms = ((sent_ms + received_ms) / 2) if sent_ms is not None and received_ms is not None else 0.0
    clock_offset_ms = receiver_time - midpoint_ms if receiver_time and midpoint_ms else 0.0
    return Receiver(
        endpoint=f"{host}:{port}",
        host=host,
        port=port,
        machine_name=str(data.get("MachineName") or host),
        user_name=str(data.get("UserName") or ""),
        version=str(data.get("Version") or ""),
        rtt_ms=rtt_ms,
        clock_offset_ms=clock_offset_ms,
        stream_muted=bool(data.get("StreamMuted") or False),
        diagnostics=data.get("Diagnostics") if isinstance(data.get("Diagnostics"), dict) else None,
    )


def resolve_targets(
    values: Iterable[str],
    discovered: list[Receiver],
    default_port: int = DEFAULT_PORT,
    include_muted: bool = False,
) -> list[Receiver]:
    requested = list(values)
    if not requested:
        raise ValueError("at least one --target is required; use --target all for every discovered receiver")

    by_name = {receiver.machine_name.casefold(): receiver for receiver in discovered}
    by_endpoint = {receiver.endpoint.casefold(): receiver for receiver in discovered}
    targets: list[Receiver] = []

    for value in requested:
        normalized = value.casefold()
        if normalized == "all":
            targets.extend(receiver for receiver in discovered if include_muted or not receiver.stream_muted)
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


def require_ffmpeg() -> str:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise FileNotFoundError("ffmpeg is not installed")
    return ffmpeg


def require_ffplay() -> str:
    ffplay = shutil.which("ffplay")
    if ffplay is None:
        raise FileNotFoundError("ffplay is not installed")
    return ffplay


def require_snapserver() -> str:
    snapserver = shutil.which("snapserver")
    if snapserver is None:
        raise FileNotFoundError("snapserver is not installed")
    return snapserver


def convert_with_ffmpeg(input_path: Path, output_path: Path, sample_rate: int, channels: int) -> None:
    subprocess.run(
        [
            require_ffmpeg(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(input_path),
            "-vn",
            "-acodec",
            "pcm_s16le",
            "-ac",
            str(channels),
            "-ar",
            str(sample_rate),
            str(output_path),
        ],
        check=True,
    )


def convert_to_stream_wav(input_path: Path, output_path: Path, backend: str, sample_rate: int, channels: int) -> str:
    convert_with_ffmpeg(input_path, output_path, sample_rate, channels)
    return "ffmpeg"


def open_decoder_stdout(
    input_path: Path,
    backend: str,
    sample_rate: int,
    channels: int,
) -> tuple[subprocess.Popen[bytes], str]:
    return (
        subprocess.Popen(
            [
                require_ffmpeg(),
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(input_path),
                "-vn",
                "-f",
                "s16le",
                "-acodec",
                "pcm_s16le",
                "-ac",
                str(channels),
                "-ar",
                str(sample_rate),
                "pipe:1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        ),
        "ffmpeg",
    )


def run_multicast_stream(
    input_path: Path,
    group: str,
    port: int,
    backend: str,
    sample_rate: int,
    channels: int,
) -> None:
    subprocess.run(
        [
            require_ffmpeg(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-re",
            "-i",
            str(input_path),
            "-vn",
            "-ac",
            str(channels),
            "-ar",
            str(sample_rate),
            "-c:a",
            "mp2",
            "-b:a",
            "256k",
            "-f",
            "mpegts",
            f"udp://{group}:{port}?ttl=2&pkt_size=1316",
        ],
        check=True,
    )


def run_snapcast_stream(
    input_path: Path,
    targets: list[Receiver],
    fifo_path: Path,
    port: int,
    sample_rate: int,
    channels: int,
    buffer_ms: int,
    delay_ms: int,
) -> None:
    try:
        fifo_path.unlink()
    except FileNotFoundError:
        pass
    os.mkfifo(fifo_path)
    server = subprocess.Popen(
        [
            require_snapserver(),
            "--config",
            "/dev/null",
            "--server.datadir",
            "/tmp/slime-audio-snapserver",
            "--http.enabled",
            "false",
            "--tcp.enabled",
            "true",
            "--tcp.port",
            "1705",
            "--stream.port",
            str(port),
            "--stream.buffer",
            str(buffer_ms),
            "--stream.source",
            f"pipe://{fifo_path}?name=slime-audio&sampleformat={sample_rate}:16:{channels}&codec=flac",
            "--logging.sink",
            "stderr",
            "--logging.filter",
            "*:warning",
        ],
    )
    time.sleep(0.8)
    if server.poll() is not None:
        raise subprocess.CalledProcessError(server.returncode or 1, "snapserver")

    try:
        send_control(targets, SHARED_STREAM_START_MESSAGE, "started snapclient")
        time.sleep(max(delay_ms, 0) / 1000)
        try:
            with fifo_path.open("wb") as fifo:
                subprocess.run(
                    [
                        require_ffmpeg(),
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-re",
                        "-i",
                        str(input_path),
                        "-vn",
                        "-f",
                        "s16le",
                        "-acodec",
                        "pcm_s16le",
                        "-ac",
                        str(channels),
                        "-ar",
                        str(sample_rate),
                        "pipe:1",
                    ],
                    stdout=fifo,
                    check=True,
                )
        finally:
            try:
                fifo_path.unlink()
            except FileNotFoundError:
                pass
    finally:
        if server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)


def send_control(targets: list[Receiver], payload: bytes, label: str) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        for target in targets:
            sock.sendto(payload, (target.host, target.port))
            print(f"{label} {target.endpoint}\t{target.machine_name}\t{target.version}")


def send_effect_control(
    targets: list[Receiver],
    delay_ms: int,
    fade_in_ms: int,
    hold_ms: int,
    fade_out_ms: int,
    volume: float,
    lowpass_hz: float,
) -> None:
    sender_start_ms = int((time.time() * 1000) + delay_ms)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        for target in targets:
            payload = {
                "StartUnixTimeMs": int(sender_start_ms + target.clock_offset_ms),
                "FadeInMs": fade_in_ms,
                "HoldMs": hold_ms,
                "FadeOutMs": fade_out_ms,
                "Volume": volume,
                "LowPassHz": lowpass_hz,
            }
            data = EFFECT_MESSAGE_PREFIX + json.dumps(payload, separators=(",", ":")).encode("utf-8")
            sock.sendto(data, (target.host, target.port))
            print(
                f"effect {target.endpoint}\t{target.machine_name}\tstart={payload['StartUnixTimeMs']}"
                f"\tvolume={volume}\tlowpass={lowpass_hz}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="Stream a local audio file to SlimeAudio receivers via shared-stream backends.")
    parser.add_argument("file", nargs="?", type=Path)
    parser.add_argument("--target", action="append", required=True, help="Receiver name, host:port, or all")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--discover-timeout-ms", type=int, default=2500)
    parser.add_argument("--include-muted", action="store_true", help="Include receivers that asked the server not to stream to them.")
    parser.add_argument("--backend", choices=["auto", "ffmpeg"], default="auto")
    parser.add_argument("--mode", choices=["multicast", "snapcast"], default="snapcast")
    parser.add_argument("--multicast-group", default="239.77.77.77")
    parser.add_argument("--multicast-port", type=int, default=47778)
    parser.add_argument("--snapcast-port", type=int, default=1704)
    parser.add_argument("--snapcast-buffer-ms", type=int, default=1000)
    parser.add_argument("--snapcast-fifo", type=Path, default=Path("/tmp/slime-audio-snapfifo"))
    parser.add_argument("--delay-ms", type=int, default=DEFAULT_LIVE_DELAY_MS)
    parser.add_argument("--sample-rate", type=int, default=48000)
    parser.add_argument("--channels", type=int, default=2)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--start-listeners", action="store_true", help="Start shared stream listeners on the selected targets and exit.")
    parser.add_argument("--stop-listeners", action="store_true", help="Stop shared stream listeners on the selected targets and exit.")
    parser.add_argument("--reset-audio", action="store_true", help="Reset active playback sessions on the selected targets and exit.")
    parser.add_argument("--no-auto-listeners", action="store_true", help="Do not auto-start shared stream listeners before multicast playback.")
    parser.add_argument("--stop-listeners-when-done", action="store_true", help="Stop shared stream listeners after multicast playback exits.")
    parser.add_argument("--effect", action="store_true", help="Send a synced music effect envelope and exit.")
    parser.add_argument("--effect-volume", type=float, default=0.45)
    parser.add_argument("--effect-lowpass-hz", type=float, default=1400.0)
    parser.add_argument("--effect-fade-in-ms", type=int, default=350)
    parser.add_argument("--effect-hold-ms", type=int, default=1400)
    parser.add_argument("--effect-fade-out-ms", type=int, default=600)
    parser.add_argument("--effect-during-stream", action="store_true", help="Send a synced effect envelope aligned to this stream's start.")
    parser.add_argument("--effect-start-offset-ms", type=int, default=-250)
    args = parser.parse_args()

    control_count = sum(1 for enabled in (args.start_listeners, args.stop_listeners, args.reset_audio) if enabled)
    if control_count > 1:
        raise SystemExit("--start-listeners, --stop-listeners, and --reset-audio are mutually exclusive")

    discovered = discover_receivers(args.port, args.discover_timeout_ms)
    include_muted = args.include_muted or args.start_listeners or args.stop_listeners or args.reset_audio or args.dry_run
    targets = resolve_targets(args.target, discovered, args.port, include_muted=include_muted)
    if not targets:
        raise SystemExit("no targets resolved")

    if args.dry_run:
        now_ms = time.time() * 1000
        for target in targets:
            print(
                f"target {target.endpoint}\t{target.machine_name}\t{target.version}"
                f"\trtt={target.rtt_ms:.1f}ms\toffset={target.clock_offset_ms:.1f}ms"
                f"\tstream_muted={str(target.stream_muted).lower()}"
                f"\t{format_diagnostics(target.diagnostics, now_ms, target.clock_offset_ms)}"
            )
        if args.mode == "multicast":
            print(f"multicast {args.multicast_group}:{args.multicast_port}")
        elif args.mode == "snapcast":
            print(f"snapcast port={args.snapcast_port} fifo={args.snapcast_fifo}")
        return 0

    if args.start_listeners:
        send_control(targets, SHARED_STREAM_START_MESSAGE, "started listener")
        return 0

    if args.stop_listeners:
        send_control(targets, SHARED_STREAM_STOP_MESSAGE, "stopped listener")
        return 0

    if args.reset_audio:
        send_control(targets, RESET_AUDIO_MESSAGE, "reset audio")
        return 0

    if args.effect:
        send_effect_control(
            targets,
            args.delay_ms,
            args.effect_fade_in_ms,
            args.effect_hold_ms,
            args.effect_fade_out_ms,
            args.effect_volume,
            args.effect_lowpass_hz,
        )
        return 0

    if args.file is None:
        raise SystemExit("file is required unless --start-listeners or --stop-listeners is set")

    if not args.file.exists():
        raise SystemExit(f"file not found: {args.file}")

    if args.mode == "multicast":
        if not args.no_auto_listeners:
            send_control(targets, SHARED_STREAM_START_MESSAGE, "started listener")
            time.sleep(max(args.delay_ms, 0) / 1000)
        print(
            f"multicast backend={args.backend} file={args.file} "
            f"group={args.multicast_group}:{args.multicast_port} targets={len(targets)}",
            flush=True,
        )
        try:
            run_multicast_stream(args.file, args.multicast_group, args.multicast_port, args.backend, args.sample_rate, args.channels)
        finally:
            if args.stop_listeners_when_done:
                send_control(targets, SHARED_STREAM_STOP_MESSAGE, "stopped listener")
        return 0

    if args.mode == "snapcast":
        print(
            f"snapcast backend={args.backend} file={args.file} "
            f"port={args.snapcast_port} targets={len(targets)}",
            flush=True,
        )
        run_snapcast_stream(
            args.file,
            targets,
            args.snapcast_fifo,
            args.snapcast_port,
            args.sample_rate,
            args.channels,
            args.snapcast_buffer_ms,
            args.delay_ms,
        )
        return 0

    raise SystemExit(f"unsupported stream mode: {args.mode}")


if __name__ == "__main__":
    raise SystemExit(main())
