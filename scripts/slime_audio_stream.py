#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
import pwd
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

DISCOVER_MESSAGE = b"SLIME_AUDIO_DISCOVER_V1"
SHARED_STREAM_START_MESSAGE = b"SLIME_AUDIO_SHARED_STREAM_START_V1"
SHARED_STREAM_STOP_MESSAGE = b"SLIME_AUDIO_SHARED_STREAM_STOP_V1"
RESET_AUDIO_MESSAGE = b"SLIME_AUDIO_RESET_AUDIO_V1"
EFFECT_MESSAGE_PREFIX = b"SLIME_AUDIO_EFFECT_V1 "
OUTPUT_DEVICE_MESSAGE_PREFIX = b"SLIME_AUDIO_OUTPUT_DEVICE_V1 "
DEFAULT_PORT = 47777
DEFAULT_LIVE_DELAY_MS = 7000
DEFAULT_PREBUFFER_MS = 15000
# Time between the snapserver reading a PCM chunk from the FIFO and the
# snapclients playing it. This is the snapserver --buffer setting and is not
# reported by Server.GetStatus, so we treat it as the baseline end-to-end
# latency and add any client-configured latency on top.
DEFAULT_SNAPCAST_BUFFER_MS = 1000
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ACTIVE_SET = REPO_ROOT / "runtime" / "active-set.json"
DEFAULT_ACTIVE_STREAM_SESSION = REPO_ROOT / "runtime" / "active-stream-session.json"
DEFAULT_ACTIVE_STREAM_STATE = REPO_ROOT / "runtime" / "active-stream-state.json"
DEFAULT_DJ_PAUSE_FILE = REPO_ROOT / "runtime" / "dj-watchdog.paused"


def system_snapcast_fifo_path() -> Path:
    return Path("/tmp/snapfifo")


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
    def format_bool_field(name: str) -> str:
        if name not in diagnostics:
            return "unknown"
        return str(bool(diagnostics.get(name))).lower()

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
        f"\tshared_stream_uptime_ms={diagnostics.get('SharedStreamUptimeMs', 0)}"
        f"\tshared_stream_reconnect_attempts={diagnostics.get('SharedStreamReconnectAttempts', 0)}"
        f"\tshared_stream_snapserver_ok={format_bool_field('SharedStreamSnapserverOk')}"
        f"\tshared_stream_snapserver_error={diagnostics.get('SharedStreamSnapserverError') or ''}"
        f"\tshared_stream_client_connected={format_bool_field('SharedStreamSnapserverClientConnected')}"
        f"\tshared_stream_client_stream={diagnostics.get('SharedStreamSnapserverClientStream') or ''}"
        f"\tshared_stream_server_stream_status={diagnostics.get('SharedStreamSnapserverStreamStatus') or ''}"
        f"\ttelemetry_path={diagnostics.get('SharedStreamTelemetryPath') or ''}"
        f"\toutput_device={diagnostics.get('SharedStreamOutputDevice') or 'default'}"
        f"\toutput_devices={','.join(diagnostics.get('SharedStreamOutputDevices') or [])}"
        f"\tlast_exit_status={diagnostics.get('SharedStreamLastExitStatus') or ''}"
        f"\tlast_stderr={diagnostics.get('SharedStreamLastStderr') or ''}"
        f"\tstart_command={diagnostics.get('SharedStreamStartCommand') or ''}"
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
                payload, address = sock.recvfrom(16384)
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


def snapserver_status(control_port: int = 1705, timeout_s: float = 1.0) -> dict:
    with socket.create_connection(("127.0.0.1", control_port), timeout=timeout_s) as sock:
        sock.sendall(b'{"id":1,"jsonrpc":"2.0","method":"Server.GetStatus"}\n')
        sock.settimeout(timeout_s)
        raw = b""
        while b"\n" not in raw:
            chunk = sock.recv(65536)
            if not chunk:
                break
            raw += chunk
    return json.loads(raw.decode("utf-8"))


def snapserver_stream_ids(status: dict) -> set[str]:
    server = (status.get("result") or {}).get("server") or {}
    stream_ids: set[str] = set()
    for stream in server.get("streams") or []:
        stream_id = stream.get("id")
        if stream_id:
            stream_ids.add(str(stream_id))
    return stream_ids


def snapserver_buffer_ms(status: dict, default: int = DEFAULT_SNAPCAST_BUFFER_MS) -> int:
    """Estimate end-to-end snapcast latency from a Server.GetStatus payload.

    The server --buffer is not exposed by GetStatus, so we use ``default`` as the
    baseline and add the largest connected client's configured added latency.
    """
    server = (status.get("result") or {}).get("server") or {}
    client_latency = 0
    for group in server.get("groups") or []:
        for client in group.get("clients") or []:
            if not client.get("connected"):
                continue
            latency = (client.get("config") or {}).get("latency")
            if isinstance(latency, (int, float)):
                client_latency = max(client_latency, int(latency))
    return max(0, default) + max(0, client_latency)


def resolve_snapcast_latency_ms(
    control_port: int = 1705,
    override_ms: int | None = None,
    default: int = DEFAULT_SNAPCAST_BUFFER_MS,
) -> int:
    """Latency from "ffmpeg starts feeding the FIFO" to "listener hears it"."""
    if override_ms is not None and override_ms >= 0:
        return override_ms
    try:
        status = snapserver_status(control_port=control_port)
    except Exception:
        return default
    return snapserver_buffer_ms(status, default)


def connected_snapclient_ids(status: dict) -> set[str]:
    server = (status.get("result") or {}).get("server") or {}
    connected: set[str] = set()
    for group in server.get("groups") or []:
        for client in group.get("clients") or []:
            if client.get("connected"):
                client_id = client.get("id")
                if client_id:
                    connected.add(str(client_id))
    return connected


def wait_for_snapclients(targets: list[Receiver], *, timeout_s: float = 10.0, control_port: int = 1705) -> None:
    expected = {target.machine_name for target in targets if target.machine_name and target.machine_name != target.host}
    if not expected:
        time.sleep(min(timeout_s, 1.0))
        return
    deadline = time.monotonic() + timeout_s
    last_connected: set[str] = set()
    while time.monotonic() < deadline:
        try:
            last_connected = connected_snapclient_ids(snapserver_status(control_port=control_port))
        except Exception:
            last_connected = set()
        if expected.issubset(last_connected):
            return
        time.sleep(0.25)
    missing = ", ".join(sorted(expected - last_connected)) or "unknown"
    raise RuntimeError(f"snapclients did not connect before stream start: {missing}")


def iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def iso_from_unix_ms(unix_ms: float) -> str:
    return datetime.fromtimestamp(unix_ms / 1000).astimezone().isoformat(timespec="milliseconds")


def write_audio_anchor(anchor_file: Path, *, latency_ms: int) -> None:
    """Record the wall-clock instant the first streamed sample becomes audible.

    Called the moment ffmpeg begins feeding the FIFO; the listener hears that
    sample ``latency_ms`` later, so the anchor is offset into the future. The
    runner reads this to align the dashboard playhead with the actual audio.
    """
    anchor_ms = time.time() * 1000 + max(0, latency_ms)
    write_json(
        anchor_file,
        {
            "audio_started_at": iso_from_unix_ms(anchor_ms),
            "audio_anchor_ms": int(round(anchor_ms)),
            "latency_ms": int(latency_ms),
        },
    )


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def probe_duration_ms(path: Path) -> int | None:
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return None
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        seconds = float(result.stdout.strip())
    except (OSError, ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return max(1, int(round(seconds * 1000)))


def display_title(path: Path) -> str:
    return path.stem.replace("_", " ").replace("-", " ").strip() or path.name


def publish_active_stream(
    *,
    input_path: Path,
    targets: list[Receiver],
    mode: str,
    backend: str,
    active_pointer: Path,
    active_session: Path,
    active_state: Path,
    source_session: Path | None,
    dashboard_title: str | None,
    dashboard_slug: str | None,
    start_offset_ms: int,
    dry_run: bool,
    start_latency_ms: int = 0,
) -> None:
    if dry_run:
        return

    resolved_input = input_path.resolve()
    if source_session is not None and not source_session.exists():
        raise FileNotFoundError(f"source session not found: {source_session}")

    session_path = source_session.resolve() if source_session is not None else active_session.resolve()
    state_path = active_state.resolve()
    title = dashboard_title or display_title(resolved_input)
    slug = dashboard_slug or "active-stream"
    now = iso_now()
    # The playhead anchor is when the audio becomes audible, which is the
    # snapcast latency after this call (publish fires just before ffmpeg feeds
    # the FIFO). started_at/updated_at stay "now" as plain bookkeeping.
    window_started_at = iso_from_unix_ms(time.time() * 1000 + max(0, start_latency_ms))
    duration_ms = probe_duration_ms(resolved_input)
    receivers = [
        {
            "endpoint": target.endpoint,
            "machine_name": target.machine_name,
            "host": target.host,
            "port": target.port,
            "version": target.version,
        }
        for target in targets
    ]

    if source_session is None:
        clip = {
            "id": "active-stream",
            "deck": "deck-1",
            "path": str(resolved_input),
            "start_ms": 0,
            "trim_start_ms": 0,
            "kind": "song",
        }
        if duration_ms is not None:
            clip["duration_ms"] = duration_ms
        write_json(
            session_path,
            {
                "version": 1,
                "source": "direct-stream",
                "timeline_mode": "direct-stream",
                "decks": ["deck-1"],
                "clips": [clip],
            },
        )

    write_json(
        state_path,
        {
            "current": str(resolved_input),
            "resolved_current": str(resolved_input),
            "current_clips": [
                {
                    "id": "active-stream",
                    "deck": "deck-1",
                    "path": str(resolved_input),
                    "start_ms": 0,
                    "trim_start_ms": 0,
                    "duration_ms": duration_ms,
                }
            ],
            "started_at": now,
            "updated_at": now,
            "window_started_at": window_started_at,
            "window_start_ms": max(0, start_offset_ms),
            "window_end_ms": duration_ms,
            "duration_ms": duration_ms,
            "timeline_mode": "direct-stream",
            "runner_status": "streaming",
            "stream_pid": os.getpid(),
            "stream_mode": mode,
            "stream_backend": backend,
            "stream_input": str(resolved_input),
            "receivers": receivers,
        },
    )
    write_json(
        active_pointer,
        {
            "active_session_path": str(session_path),
            "active_state_path": str(state_path),
            "archive_session_path": str(session_path),
            "loaded_at": now,
            "slug": slug,
            "title": title,
            "playback_mode": "direct-stream",
            "stream_input": str(resolved_input),
            "targets": [target.machine_name for target in targets],
        },
    )


def mark_active_stream_finished(active_state: Path, *, dry_run: bool, status: str, reason: str | None = None) -> None:
    if dry_run or not active_state.exists():
        return
    try:
        payload = json.loads(active_state.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if payload.get("stream_pid") != os.getpid():
        return
    now = iso_now()
    payload["updated_at"] = now
    payload["runner_status"] = status
    if status == "completed":
        payload["completed_at"] = now
    else:
        payload["failed_at"] = now
        if reason:
            payload["runner_exit_reason"] = reason
    write_json(active_state, payload)


def mark_active_stream_completed(active_state: Path, *, dry_run: bool) -> None:
    mark_active_stream_finished(active_state, dry_run=dry_run, status="completed")


def playback_start_paused(pause_file: Path | None, *, ignore_pause: bool) -> bool:
    return bool(pause_file and pause_file.exists() and not ignore_pause)


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
    start_offset_ms: int = 0,
) -> None:
    seek_args = ["-ss", f"{start_offset_ms / 1000:.3f}"] if start_offset_ms > 0 else []
    subprocess.run(
        [
            require_ffmpeg(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-re",
            *seek_args,
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
    sample_rate: int,
    channels: int,
    delay_ms: int,
    start_offset_ms: int = 0,
    on_ready: Callable[[], None] | None = None,
    control_port: int = 1705,
    stream_id: str = "default",
    anchor_file: Path | None = None,
    latency_ms: int = 0,
) -> None:
    status = snapserver_status(control_port=control_port)
    stream_ids = snapserver_stream_ids(status)
    if stream_id not in stream_ids:
        raise RuntimeError(f"system snapserver missing stream {stream_id!r}; streams={sorted(stream_ids)}")
    if not fifo_path.exists():
        raise FileNotFoundError(f"system snapserver fifo does not exist: {fifo_path}")
    if not stat_is_fifo(fifo_path):
        raise RuntimeError(f"system snapserver fifo is not a fifo: {fifo_path}")

    send_control(targets, SHARED_STREAM_START_MESSAGE, "started snapclient")
    wait_for_snapclients(targets, timeout_s=max(10.0, delay_ms / 1000), control_port=control_port)
    time.sleep(max(delay_ms, 0) / 1000)
    # ffmpeg is about to start feeding the FIFO: anchor the playhead here, before
    # the audible sample, so the dashboard tracks the audio instead of leading it.
    if anchor_file is not None:
        write_audio_anchor(anchor_file, latency_ms=latency_ms)
    if on_ready is not None:
        on_ready()

    seek_args = ["-ss", f"{start_offset_ms / 1000:.3f}"] if start_offset_ms > 0 else []
    ffmpeg_cmd = [
        require_ffmpeg(),
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-re",
        *seek_args,
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
        str(fifo_path),
    ]
    fifo_stat = os.stat(fifo_path)
    if fifo_stat.st_uid != os.geteuid():
        fifo_owner = pwd.getpwuid(fifo_stat.st_uid).pw_name
        ffmpeg_cmd = ["sudo", "-n", "-u", fifo_owner, *ffmpeg_cmd]
    subprocess.run(ffmpeg_cmd, check=True)


def stat_is_fifo(path: Path) -> bool:
    return path.exists() and os.stat(path).st_mode & 0o170000 == 0o010000


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


def send_output_device_control(targets: list[Receiver], soundcard: str | None) -> None:
    payload = OUTPUT_DEVICE_MESSAGE_PREFIX + json.dumps(
        {"Soundcard": soundcard},
        separators=(",", ":"),
    ).encode("utf-8")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        for target in targets:
            sock.sendto(payload, (target.host, target.port))
            print(f"output device {target.endpoint}\t{target.machine_name}\t{soundcard or 'default'}")


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
    parser.add_argument("--snapcast-fifo", type=Path, default=None)
    parser.add_argument("--delay-ms", type=int, default=DEFAULT_LIVE_DELAY_MS)
    parser.add_argument("--sample-rate", type=int, default=48000)
    parser.add_argument("--channels", type=int, default=2)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--start-listeners", action="store_true", help="Start shared stream listeners on the selected targets and exit.")
    parser.add_argument("--stop-listeners", action="store_true", help="Stop shared stream listeners on the selected targets and exit.")
    parser.add_argument("--reset-audio", action="store_true", help="Reset active playback sessions on the selected targets and exit.")
    parser.add_argument("--output-device", help="Set the target snapclient soundcard name or index and exit.")
    parser.add_argument("--default-output-device", action="store_true", help="Reset the target snapclient soundcard to the system default and exit.")
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
    parser.add_argument("--active-pointer", type=Path, default=DEFAULT_ACTIVE_SET)
    parser.add_argument("--active-session", type=Path, default=DEFAULT_ACTIVE_STREAM_SESSION)
    parser.add_argument("--active-state", type=Path, default=DEFAULT_ACTIVE_STREAM_STATE)
    parser.add_argument(
        "--pause-file",
        type=Path,
        default=DEFAULT_DJ_PAUSE_FILE,
        help="If present, refuse to start a file stream unless --ignore-pause is set.",
    )
    parser.add_argument("--ignore-pause", action="store_true", help="Start a file stream even when the DJ pause file exists.")
    parser.add_argument("--source-session", type=Path, help="Existing session JSON that describes this rendered stream.")
    parser.add_argument("--dashboard-title", help="Title shown by the dashboard for this stream.")
    parser.add_argument("--dashboard-slug", help="Slug shown by the dashboard for this stream.")
    parser.add_argument("--start-offset-ms", type=int, default=0, help="Start streaming this far into the file and publish that playhead to the dashboard.")
    parser.add_argument("--no-active-pointer", action="store_true", help="Do not publish this playback to the dashboard active pointer.")
    parser.add_argument("--anchor-file", type=Path, help="Write the buffer-adjusted audio-start anchor here once the stream becomes audible.")
    parser.add_argument("--snapcast-buffer-ms", type=int, default=-1, help="Override the snapcast end-to-end latency (ms). -1 queries the snapserver.")
    args = parser.parse_args()
    if args.snapcast_fifo is None:
        args.snapcast_fifo = system_snapcast_fifo_path()

    control_count = sum(
        1
        for enabled in (
            args.start_listeners,
            args.stop_listeners,
            args.reset_audio,
            args.output_device is not None,
            args.default_output_device,
        )
        if enabled
    )
    if control_count > 1:
        raise SystemExit(
            "--start-listeners, --stop-listeners, --reset-audio, --output-device, and "
            "--default-output-device are mutually exclusive"
        )

    discovered = discover_receivers(args.port, args.discover_timeout_ms)
    include_muted = (
        args.include_muted
        or args.start_listeners
        or args.stop_listeners
        or args.reset_audio
        or args.output_device is not None
        or args.default_output_device
        or args.dry_run
    )
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
            print(f"snapcast fifo={args.snapcast_fifo}")
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

    if args.output_device is not None or args.default_output_device:
        send_output_device_control(targets, None if args.default_output_device else args.output_device)
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

    if playback_start_paused(args.pause_file, ignore_pause=args.ignore_pause):
        print(
            json.dumps(
                {
                    "status": "paused",
                    "component": "stream",
                    "pause_file": str(args.pause_file),
                    "file": str(args.file) if args.file is not None else None,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        return 0

    if args.file is None:
        raise SystemExit("file is required unless --start-listeners or --stop-listeners is set")

    if not args.file.exists():
        raise SystemExit(f"file not found: {args.file}")

    override_buffer_ms = None if args.snapcast_buffer_ms < 0 else args.snapcast_buffer_ms
    stream_latency_ms = (
        resolve_snapcast_latency_ms(override_ms=override_buffer_ms)
        if args.mode == "snapcast"
        else max(0, args.snapcast_buffer_ms)
    )

    def publish_ready_stream() -> None:
        if args.no_active_pointer:
            return
        publish_active_stream(
            input_path=args.file,
            targets=targets,
            mode=args.mode,
            backend=args.backend,
            active_pointer=args.active_pointer,
            active_session=args.active_session,
            active_state=args.active_state,
            source_session=args.source_session,
            dashboard_title=args.dashboard_title,
            dashboard_slug=args.dashboard_slug,
            start_offset_ms=args.start_offset_ms,
            dry_run=args.dry_run,
            start_latency_ms=stream_latency_ms,
        )

    if args.mode == "multicast":
        publish_ready_stream()
        if not args.no_auto_listeners:
            send_control(targets, SHARED_STREAM_START_MESSAGE, "started listener")
            time.sleep(max(args.delay_ms, 0) / 1000)
        if args.anchor_file is not None:
            write_audio_anchor(args.anchor_file, latency_ms=stream_latency_ms)
        print(
            f"multicast backend={args.backend} file={args.file} "
            f"group={args.multicast_group}:{args.multicast_port} targets={len(targets)}",
            flush=True,
        )
        try:
            run_multicast_stream(
                args.file,
                args.multicast_group,
                args.multicast_port,
                args.backend,
                args.sample_rate,
                args.channels,
                args.start_offset_ms,
            )
        except Exception as exc:
            if not args.no_active_pointer:
                mark_active_stream_finished(
                    args.active_state,
                    dry_run=args.dry_run,
                    status="failed",
                    reason=f"{exc.__class__.__name__}: {exc}",
                )
            raise
        else:
            if not args.no_active_pointer:
                mark_active_stream_completed(args.active_state, dry_run=args.dry_run)
        finally:
            if args.stop_listeners_when_done:
                send_control(targets, SHARED_STREAM_STOP_MESSAGE, "stopped listener")
        return 0

    if args.mode == "snapcast":
        print(
            f"snapcast backend={args.backend} file={args.file} "
            f"fifo={args.snapcast_fifo} targets={len(targets)}",
            flush=True,
        )
        try:
            run_snapcast_stream(
                args.file,
                targets,
                args.snapcast_fifo,
                args.sample_rate,
                args.channels,
                args.delay_ms,
                args.start_offset_ms,
                on_ready=publish_ready_stream,
                anchor_file=args.anchor_file,
                latency_ms=stream_latency_ms,
            )
        except Exception as exc:
            if not args.no_active_pointer:
                mark_active_stream_finished(
                    args.active_state,
                    dry_run=args.dry_run,
                    status="failed",
                    reason=f"{exc.__class__.__name__}: {exc}",
                )
            raise
        else:
            if not args.no_active_pointer:
                mark_active_stream_completed(args.active_state, dry_run=args.dry_run)
        return 0

    raise SystemExit(f"unsupported stream mode: {args.mode}")


if __name__ == "__main__":
    raise SystemExit(main())
