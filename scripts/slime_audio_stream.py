#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import queue
import shutil
import socket
import struct
import subprocess
import threading
import time
import uuid
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

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
            "aac",
            "-b:a",
            "192k",
            "-f",
            "mpegts",
            f"udp://{group}:{port}?ttl=2&pkt_size=1316",
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


def stream_live_synced(
    input_path: Path,
    targets: list[Receiver],
    backend: str,
    sample_rate: int,
    channels: int,
    delay_ms: int,
    chunk_ms: int,
    prebuffer_ms: int,
    packet_redundancy: int,
    effect: tuple[int, int, int, int, float, float] | None = None,
    refresh_targets: Callable[[], list[Receiver]] | None = None,
    refresh_targets_ms: int = 0,
) -> None:
    decoder, selected_backend = open_decoder_stdout(input_path, backend, sample_rate, channels)
    if decoder.stdout is None:
        raise RuntimeError("decoder stdout was not captured")

    session = uuid.uuid4()
    max_rtt = max((target.rtt_ms for target in targets), default=0.0)
    lead_ms = max(delay_ms, int(max_rtt + 1200))
    sender_start_ms: int | None = None
    target_start_ms: dict[str, int] = {}
    chunk_frames = max(1, sample_rate * chunk_ms // 1000)
    packet_redundancy = max(1, packet_redundancy)
    frame_bytes = channels * 2
    chunk_bytes = chunk_frames * frame_bytes
    prebuffer_frames = max(0, sample_rate * prebuffer_ms // 1000)
    max_buffer_chunks = max(32, int(sample_rate * max(prebuffer_ms + 30000, chunk_ms * 32) / 1000 / chunk_frames))
    decoded_chunks: queue.Queue[bytes | None] = queue.Queue(maxsize=max_buffer_chunks)
    decode_errors: list[BaseException] = []
    endpoints = [(target, (target.host, target.port)) for target in targets]
    refresh_every = refresh_targets_ms / 1000 if refresh_targets is not None and refresh_targets_ms > 0 else 0
    next_refresh = time.monotonic() + refresh_every if refresh_every else 0.0
    sequence = 0
    bytes_sent = 0
    buffered_frames = 0
    pending_payloads: list[bytes] = []
    started: float | None = None

    def decode() -> None:
        try:
            while True:
                payload = decoder.stdout.read(chunk_bytes)
                if not payload:
                    break
                if len(payload) % frame_bytes:
                    payload = payload[: len(payload) - (len(payload) % frame_bytes)]
                if payload:
                    decoded_chunks.put(payload)
        except BaseException as ex:
            decode_errors.append(ex)
        finally:
            decoded_chunks.put(None)

    decoder_thread = threading.Thread(target=decode, name="slime-audio-decoder", daemon=True)
    decoder_thread.start()

    print(
        f"live backend={selected_backend} session={session} lead_ms={lead_ms} "
        f"prebuffer_ms={prebuffer_ms} packet_redundancy={packet_redundancy} "
        f"targets={','.join(f'{target.machine_name}(rtt={target.rtt_ms:.1f}ms,offset={target.clock_offset_ms:.1f}ms)' for target in targets)}",
        flush=True,
    )

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            while True:
                payload = decoded_chunks.get()
                if payload is None:
                    if started is None and pending_payloads:
                        payloads = pending_payloads
                        pending_payloads = []
                    else:
                        break
                else:
                    if started is None:
                        pending_payloads.append(payload)
                        buffered_frames += len(payload) // frame_bytes
                        if prebuffer_frames and buffered_frames < prebuffer_frames:
                            continue
                        payloads = pending_payloads
                        pending_payloads = []
                    else:
                        payloads = [payload]
                if started is None:
                    started = time.monotonic()
                    sender_start_ms = int((time.time() * 1000) + lead_ms)
                    target_start_ms = {
                        target.endpoint: int(sender_start_ms + target.clock_offset_ms)
                        for target, _ in endpoints
                    }
                    if effect is not None:
                        effect_offset_ms, fade_in_ms, hold_ms, fade_out_ms, volume, lowpass_hz = effect
                        send_effect_control(
                            [target for target, _ in endpoints],
                            delay_ms=lead_ms + effect_offset_ms,
                            fade_in_ms=fade_in_ms,
                            hold_ms=hold_ms,
                            fade_out_ms=fade_out_ms,
                            volume=volume,
                            lowpass_hz=lowpass_hz,
                        )

                for payload in payloads:
                    if refresh_every and time.monotonic() >= next_refresh:
                        refreshed = refresh_targets()
                        for target in refreshed:
                            if sender_start_ms is not None:
                                target_start_ms.setdefault(target.endpoint, int(sender_start_ms + target.clock_offset_ms))
                        endpoints = [(target, (target.host, target.port)) for target in refreshed]
                        next_refresh = time.monotonic() + refresh_every
                    for target, endpoint in endpoints:
                        packet = encode_audio_packet(
                            session,
                            sequence,
                            target_start_ms[target.endpoint],
                            sample_rate,
                            channels,
                            payload,
                        )
                        for _ in range(packet_redundancy):
                            sock.sendto(packet, endpoint)
                    bytes_sent += len(payload)
                    sequence += 1
                    next_send = started + ((bytes_sent / frame_bytes) / sample_rate)
                    sleep_for = next_send - time.monotonic()
                    if sleep_for > 0:
                        time.sleep(sleep_for)

            if started is not None:
                for target, endpoint in endpoints:
                    end = encode_audio_packet(
                        session,
                        sequence,
                        target_start_ms[target.endpoint],
                        sample_rate,
                        channels,
                        b"",
                        packet_type=2,
                    )
                    for _ in range(packet_redundancy):
                        sock.sendto(end, endpoint)
    finally:
        try:
            decoder.stdout.close()
        except Exception:
            pass
        return_code = decoder.wait(timeout=5)
        if return_code not in (0, None):
            raise subprocess.CalledProcessError(return_code, selected_backend)
        if decode_errors:
            raise decode_errors[0]

    print(
        f"sent live session={session} bytes={bytes_sent} "
        f"targets={','.join(target.endpoint for target in targets)}",
        flush=True,
    )


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
    parser.add_argument("file", nargs="?", type=Path)
    parser.add_argument("--target", action="append", required=True, help="Receiver name, host:port, or all")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--discover-timeout-ms", type=int, default=2500)
    parser.add_argument("--include-muted", action="store_true", help="Include receivers that asked the server not to stream to them.")
    parser.add_argument("--backend", choices=["auto", "ffmpeg"], default="auto")
    parser.add_argument("--mode", choices=["packets", "multicast"], default="packets")
    parser.add_argument("--multicast-group", default="239.77.77.77")
    parser.add_argument("--multicast-port", type=int, default=47778)
    parser.add_argument("--delay-ms", type=int, default=DEFAULT_LIVE_DELAY_MS)
    parser.add_argument("--packet-delay-ms", type=int, default=45)
    parser.add_argument("--chunk-ms", type=int, default=7)
    parser.add_argument("--prebuffer-ms", type=int, default=DEFAULT_PREBUFFER_MS)
    parser.add_argument("--packet-redundancy", type=int, default=2)
    parser.add_argument("--refresh-targets-ms", type=int, default=3000, help="For --target all packet streams, rediscover subscribers while playing.")
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

    effect = None
    if args.effect_during_stream:
        effect = (
            args.effect_start_offset_ms,
            args.effect_fade_in_ms,
            args.effect_hold_ms,
            args.effect_fade_out_ms,
            args.effect_volume,
            args.effect_lowpass_hz,
        )
    refresh_targets = None
    if args.mode == "packets" and any(target.casefold() == "all" for target in args.target) and args.refresh_targets_ms > 0:
        def refresh_targets() -> list[Receiver]:
            discovered_targets = discover_receivers(args.port, args.discover_timeout_ms)
            return resolve_targets(args.target, discovered_targets, args.port, include_muted=args.include_muted)

    stream_live_synced(
        args.file,
        targets,
        args.backend,
        args.sample_rate,
        args.channels,
        args.delay_ms,
        args.chunk_ms,
        args.prebuffer_ms,
        args.packet_redundancy,
        effect,
        refresh_targets=refresh_targets,
        refresh_targets_ms=args.refresh_targets_ms,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
