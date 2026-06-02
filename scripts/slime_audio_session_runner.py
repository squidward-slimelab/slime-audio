#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from slime_audio_session import Clip, MixSession, load_session, parse_ms, playhead_ms_from_state
from slime_audio_session_mixdown import session_duration_ms
from slime_audio_stream import (
    SHARED_STREAM_START_MESSAGE,
    Receiver,
    discover_receivers,
    require_ffmpeg,
    require_snapserver,
    resolve_targets,
    send_control,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SESSION = REPO_ROOT / "runtime" / "mix-session.json"
DEFAULT_STATE = REPO_ROOT / "runtime" / "mix-session-state.json"
DEFAULT_HISTORY = REPO_ROOT / "runtime" / "play-history.jsonl"
_active_stream: subprocess.Popen[bytes] | None = None


class PreparedWindow:
    def __init__(
        self,
        *,
        temp: tempfile.TemporaryDirectory[str],
        output: Path,
        start_ms: int,
        end_ms: int,
        active_clips: list[Clip],
        render_command: list[str],
    ) -> None:
        self.temp = temp
        self.output = output
        self.start_ms = start_ms
        self.end_ms = end_ms
        self.active_clips = active_clips
        self.render_command = render_command

    def cleanup(self) -> None:
        self.temp.cleanup()


class RunningWindow:
    def __init__(self, process: subprocess.Popen[bytes], handle: object | None = None) -> None:
        self.process = process
        self.handle = handle

    def close(self) -> None:
        if self.handle is not None:
            close = getattr(self.handle, "close", None)
            if close is not None:
                close()
            self.handle = None


class PersistentSnapcast:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.targets: list[Receiver] = []
        self.server: subprocess.Popen[bytes] | None = None
        self.fifo_handle: object | None = None

    def start(self) -> None:
        try:
            self.args.snapcast_fifo.unlink()
        except FileNotFoundError:
            pass
        os.mkfifo(self.args.snapcast_fifo)
        self.server = subprocess.Popen(
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
                str(self.args.snapcast_port),
                "--stream.buffer",
                str(self.args.snapcast_buffer_ms),
                "--stream.source",
                (
                    f"pipe://{self.args.snapcast_fifo}?name=slime-audio"
                    f"&sampleformat={self.args.sample_rate}:16:{self.args.channels}&codec=flac"
                ),
                "--logging.sink",
                "stderr",
                "--logging.filter",
                "*:warning",
            ],
        )
        time.sleep(0.8)
        if self.server.poll() is not None:
            raise subprocess.CalledProcessError(self.server.returncode or 1, "snapserver")
        self.fifo_handle = self.args.snapcast_fifo.open("wb")
        discovered = discover_receivers(47777, self.args.discover_timeout_ms)
        self.targets = resolve_targets(self.args.target, discovered, 47777, include_muted=False)
        if not self.targets:
            raise SystemExit("no targets resolved")
        send_control(self.targets, SHARED_STREAM_START_MESSAGE, "started snapclient")

    def start_window(self, audio_path: Path) -> RunningWindow:
        if self.fifo_handle is None:
            raise RuntimeError("snapcast fifo writer is not open")
        command = [
            require_ffmpeg(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-re",
            "-i",
            str(audio_path),
            "-vn",
            "-f",
            "s16le",
            "-acodec",
            "pcm_s16le",
            "-ac",
            str(self.args.channels),
            "-ar",
            str(self.args.sample_rate),
            "pipe:1",
        ]
        process = subprocess.Popen(command, stdout=self.fifo_handle, start_new_session=True)
        return RunningWindow(process)

    def stop(self) -> None:
        if self.fifo_handle is not None:
            close = getattr(self.fifo_handle, "close", None)
            if close is not None:
                close()
            self.fifo_handle = None
        if self.server is not None and self.server.poll() is None:
            self.server.terminate()
            try:
                self.server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.server.kill()
                self.server.wait(timeout=5)
        try:
            self.args.snapcast_fifo.unlink()
        except FileNotFoundError:
            pass


def iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def append_history(path: Path | None, event: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


def clip_overlaps_window(clip: Clip, start_ms: int, end_ms: int) -> bool:
    if clip.duration_ms is None:
        return clip.start_ms >= start_ms and clip.start_ms < end_ms
    return clip.start_ms < end_ms and clip.end_ms is not None and clip.end_ms > start_ms


def clips_in_window(session: MixSession, start_ms: int, end_ms: int) -> list[Clip]:
    return sorted(
        [clip for clip in session.clips if clip_overlaps_window(clip, start_ms, end_ms)],
        key=lambda clip: (clip.start_ms, clip.deck, clip.id),
    )


def next_event_start_ms(session: MixSession, playhead_ms: int) -> int | None:
    starts = [clip.start_ms for clip in session.clips if clip.start_ms >= playhead_ms]
    starts.extend(lean.start_ms for lean in session.mic_lean_ins if lean.start_ms >= playhead_ms)
    return min(starts) if starts else None


def stream_command(args: argparse.Namespace, audio_path: Path) -> list[str]:
    command = [
        "python3",
        str(REPO_ROOT / "scripts" / "slime_audio_stream.py"),
        str(audio_path),
    ]
    for target in args.target:
        command.extend(["--target", target])
    command.extend(
        [
            "--mode",
            args.mode,
            "--backend",
            args.backend,
            "--discover-timeout-ms",
            str(args.discover_timeout_ms),
            "--delay-ms",
            str(args.delay_ms),
        ]
    )
    if args.mode == "snapcast":
        command.extend(["--snapcast-port", str(args.snapcast_port)])
        command.extend(["--snapcast-buffer-ms", str(args.snapcast_buffer_ms)])
        command.extend(["--snapcast-fifo", str(args.snapcast_fifo)])
    if args.mode == "multicast":
        command.extend(["--multicast-group", args.multicast_group])
        command.extend(["--multicast-port", str(args.multicast_port)])
        if args.no_auto_listeners:
            command.append("--no-auto-listeners")
        if args.stop_listeners_when_done:
            command.append("--stop-listeners-when-done")
    return command


def mixdown_command(args: argparse.Namespace, start_ms: int, duration_ms: int, output: Path) -> list[str]:
    command = [
        "python3",
        str(REPO_ROOT / "scripts" / "slime_audio_session_mixdown.py"),
        str(args.session),
        "--from",
        str(start_ms),
        "--duration",
        str(duration_ms),
        "--output",
        str(output),
        "--kokoro-url",
        args.kokoro_url,
        "--voice",
        args.voice,
        "--sample-rate",
        str(args.sample_rate),
        "--channels",
        str(args.channels),
    ]
    if args.skip_tts:
        command.append("--skip-tts")
    return command


def stop_active_stream() -> None:
    global _active_stream
    process = _active_stream
    if process is None or process.poll() is not None:
        _active_stream = None
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        _active_stream = None
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
    finally:
        _active_stream = None


def run_stream(command: list[str]) -> int:
    global _active_stream
    process = subprocess.Popen(command, cwd=REPO_ROOT, start_new_session=True)
    _active_stream = process
    try:
        return process.wait()
    finally:
        if _active_stream is process:
            stop_active_stream()


def start_stream(command: list[str]) -> subprocess.Popen[bytes]:
    global _active_stream
    process = subprocess.Popen(command, cwd=REPO_ROOT, start_new_session=True)
    _active_stream = process
    return process


def wait_stream(process: subprocess.Popen[bytes]) -> int:
    try:
        return process.wait()
    finally:
        global _active_stream
        if _active_stream is process:
            _active_stream = None


def start_window_stream(args: argparse.Namespace, window: PreparedWindow, snapcast: PersistentSnapcast | None) -> tuple[RunningWindow, list[str]]:
    global _active_stream
    if snapcast is not None:
        running = snapcast.start_window(window.output)
        _active_stream = running.process
        return running, [
            "persistent-snapcast",
            str(window.output),
            "--targets",
            ",".join(target.machine_name for target in snapcast.targets),
        ]
    stream = stream_command(args, window.output)
    process = start_stream(stream)
    return RunningWindow(process), stream


def wait_window_stream(running: RunningWindow) -> int:
    try:
        return wait_stream(running.process)
    finally:
        running.close()


def install_signal_handlers() -> None:
    def handle_stop(signum: int, _frame: object) -> None:
        stop_active_stream()
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, handle_stop)
    signal.signal(signal.SIGINT, handle_stop)


def write_window_state(
    args: argparse.Namespace,
    state: dict[str, Any],
    *,
    session: MixSession,
    start_ms: int,
    end_ms: int,
    active_clips: list[Clip],
) -> dict[str, Any]:
    now = iso_now()
    if not state.get("mix_started_at"):
        state["mix_started_at"] = now
    state.update(
        {
            "session": str(args.session),
            "timeline_mode": "native-session-runner",
            "started_at": now,
            "window_started_at": now,
            "window_start_ms": start_ms,
            "window_end_ms": end_ms,
            "current": active_clips[0].path if active_clips else None,
            "resolved_current": active_clips[0].path if active_clips else None,
            "current_clips": [
                {
                    "id": clip.id,
                    "deck": clip.deck,
                    "path": clip.path,
                    "start_ms": clip.start_ms,
                    "end_ms": clip.end_ms,
                    "trim_start_ms": clip.trim_start_ms,
                }
                for clip in active_clips
            ],
            "duration_ms": session_duration_ms(session),
            "updated_at": now,
        }
    )
    write_json(args.state, state)
    return state


def render_window(args: argparse.Namespace, start_ms: int, duration_ms: int, output: Path) -> list[str]:
    command = mixdown_command(args, start_ms, duration_ms, output)
    if not args.dry_run:
        subprocess.run(command, cwd=REPO_ROOT, check=True)
    return command


def prepare_window(args: argparse.Namespace, session: MixSession, start_ms: int, end_ms: int) -> PreparedWindow:
    temp_root = args.temp_dir
    if temp_root is not None:
        temp_root.mkdir(parents=True, exist_ok=True)
    temp = tempfile.TemporaryDirectory(prefix="slime-session-runner-", dir=temp_root)
    output = Path(temp.name) / f"window-{start_ms}-{end_ms}.wav"
    active_clips = clips_in_window(session, start_ms, end_ms)
    try:
        render_command = render_window(args, start_ms, end_ms - start_ms, output)
    except Exception:
        temp.cleanup()
        raise
    return PreparedWindow(
        temp=temp,
        output=output,
        start_ms=start_ms,
        end_ms=end_ms,
        active_clips=active_clips,
        render_command=render_command,
    )


def run_session(args: argparse.Namespace) -> int:
    install_signal_handlers()
    state = load_json(args.state)
    has_playhead = any(key in state for key in ("playhead_ms", "mix_playhead_ms", "window_started_at", "mix_started_at"))
    if args.reset_state or not has_playhead:
        state = {"mix_started_at": iso_now()}
        write_json(args.state, state)

    prepared: PreparedWindow | None = None
    snapcast = PersistentSnapcast(args) if args.mode == "snapcast" and not args.dry_run else None
    if snapcast is not None:
        snapcast.start()
    try:
        while True:
            session = load_session(args.session)
            total_ms = session_duration_ms(session)
            playhead_ms = min(playhead_ms_from_state(args.state), total_ms)
            if playhead_ms >= total_ms:
                state["current"] = None
                state["completed_at"] = iso_now()
                write_json(args.state, state)
                print("session done", flush=True)
                return 0

            if prepared is not None:
                window = prepared
                prepared = None
                start_ms = window.start_ms
                end_ms = window.end_ms
                active_clips = window.active_clips
            else:
                next_start = next_event_start_ms(session, playhead_ms)
                if next_start is not None and next_start > playhead_ms and not clips_in_window(session, playhead_ms, min(next_start, total_ms)):
                    wait_ms = min(next_start - playhead_ms, args.idle_poll_ms)
                    if args.dry_run:
                        print(f"sleep {wait_ms}ms until next event at {next_start}ms")
                        return 0
                    time.sleep(wait_ms / 1000)
                    continue

                start_ms = playhead_ms
                end_ms = min(total_ms, start_ms + args.window_ms)
                window = prepare_window(args, session, start_ms, end_ms)
                active_clips = window.active_clips

            state = write_window_state(args, state, session=session, start_ms=start_ms, end_ms=end_ms, active_clips=active_clips)
            append_history(
                args.history_log,
                {
                    "event": "session_window_started",
                    "session": str(args.session),
                    "state": str(args.state),
                    "timestamp": state["window_started_at"],
                    "window_start_ms": start_ms,
                    "window_end_ms": end_ms,
                    "clips": [clip.id for clip in active_clips],
                },
            )

            if args.dry_run:
                stream = stream_command(args, window.output)
                print(json.dumps({"render": window.render_command, "stream": stream, "clips": [clip.id for clip in active_clips]}, indent=2))
                window.cleanup()
                return 0

            running, stream = start_window_stream(args, window, snapcast)
            started_monotonic = time.monotonic()
            returncode = 0
            while True:
                polled = running.process.poll()
                if polled is not None:
                    returncode = wait_window_stream(running)
                    break
                remaining_ms = (end_ms - start_ms) - int((time.monotonic() - started_monotonic) * 1000)
                if (
                    args.prerender_next
                    and prepared is None
                    and end_ms < total_ms
                    and remaining_ms <= args.prerender_lead_ms
                ):
                    next_session = load_session(args.session)
                    next_total_ms = session_duration_ms(next_session)
                    next_start_ms = end_ms
                    next_end_ms = min(next_total_ms, next_start_ms + args.window_ms)
                    prepared = prepare_window(args, next_session, next_start_ms, next_end_ms)
                    append_history(
                        args.history_log,
                        {
                            "event": "session_window_prerendered",
                            "session": str(args.session),
                            "state": str(args.state),
                            "timestamp": iso_now(),
                            "window_start_ms": next_start_ms,
                            "window_end_ms": next_end_ms,
                            "clips": [clip.id for clip in prepared.active_clips],
                        },
                    )
                time.sleep(0.2)

            window.cleanup()
            if returncode != 0:
                append_history(
                    args.history_log,
                    {
                        "event": "session_window_failed",
                        "returncode": returncode,
                        "session": str(args.session),
                        "state": str(args.state),
                        "timestamp": iso_now(),
                        "window_start_ms": start_ms,
                        "window_end_ms": end_ms,
                    },
                )
                if prepared is not None:
                    prepared.cleanup()
                    prepared = None
                time.sleep(args.retry_seconds)
                continue

            append_history(
                args.history_log,
                {
                    "event": "session_window_completed",
                    "session": str(args.session),
                    "state": str(args.state),
                    "timestamp": iso_now(),
                    "window_start_ms": start_ms,
                    "window_end_ms": end_ms,
                    "clips": [clip.id for clip in active_clips],
                },
            )
    finally:
        if prepared is not None:
            prepared.cleanup()
        if snapcast is not None:
            snapcast.stop()


def parse_args_from(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a live-editable timestamped SlimeAudio mix session.")
    parser.add_argument("--session", type=Path, default=DEFAULT_SESSION)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--history-log", type=Path, default=DEFAULT_HISTORY)
    parser.add_argument("--target", action="append", default=None)
    parser.add_argument("--mode", choices=["snapcast", "multicast"], default="snapcast")
    parser.add_argument("--backend", choices=["auto", "ffmpeg"], default="ffmpeg")
    parser.add_argument("--window-ms", type=int, default=180_000)
    parser.add_argument("--prerender-next", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--prerender-lead-ms", type=int, default=60_000)
    parser.add_argument("--idle-poll-ms", type=int, default=1000)
    parser.add_argument("--retry-seconds", type=int, default=5)
    parser.add_argument("--discover-timeout-ms", type=int, default=4000)
    parser.add_argument("--delay-ms", type=int, default=0)
    parser.add_argument("--snapcast-port", type=int, default=1704)
    parser.add_argument("--snapcast-buffer-ms", type=int, default=1000)
    parser.add_argument("--snapcast-fifo", type=Path, default=Path("/tmp/slime-audio-snapfifo"))
    parser.add_argument("--multicast-group", default="239.77.77.77")
    parser.add_argument("--multicast-port", type=int, default=47778)
    parser.add_argument("--no-auto-listeners", action="store_true")
    parser.add_argument("--stop-listeners-when-done", action="store_true")
    parser.add_argument("--kokoro-url", default="http://robokrabs:7862")
    parser.add_argument("--voice", default="am_eric")
    parser.add_argument("--sample-rate", type=int, default=48_000)
    parser.add_argument("--channels", type=int, default=2)
    parser.add_argument("--skip-tts", action="store_true")
    parser.add_argument("--reset-state", action="store_true")
    parser.add_argument(
        "--temp-dir",
        type=Path,
        default=None,
        help="Directory for prerendered runner windows. Defaults to the system temp directory.",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.target is None:
        args.target = ["all"]
    if args.window_ms <= 0:
        raise SystemExit("--window-ms must be positive")
    return args


def main() -> int:
    return run_session(parse_args_from())


if __name__ == "__main__":
    raise SystemExit(main())
