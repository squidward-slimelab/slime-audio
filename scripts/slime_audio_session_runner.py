#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any

from slime_audio_session import Clip, MixSession, load_session, parse_ms, playhead_ms_from_state
from slime_audio_session_mixdown import session_duration_ms
from slime_audio_stream import (
    require_ffmpeg,
    stat_is_fifo,
    system_snapcast_fifo_path,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SESSION = REPO_ROOT / "runtime" / "mix-session.json"
DEFAULT_STATE = REPO_ROOT / "runtime" / "mix-session-state.json"
DEFAULT_HISTORY = REPO_ROOT / "runtime" / "play-history.jsonl"
DEFAULT_ACTIVE_SET = REPO_ROOT / "runtime" / "active-set.json"
DEFAULT_DJ_PAUSE_FILE = REPO_ROOT / "runtime" / "dj-watchdog.paused"
DEFAULT_KOKORO_URL = os.environ.get("SLIME_AUDIO_KOKORO_URL", "http://robokrabs.tail4cb51.ts.net:7862")
_active_stream: subprocess.Popen[bytes] | None = None


class PreparedWindow:
    def __init__(
        self,
        *,
        temp: tempfile.TemporaryDirectory[str],
        output: Path,
        start_ms: int,
        end_ms: int,
        active_clips: list[Any],
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


def slugify(value: str) -> str:
    clean = "".join(character.lower() if character.isalnum() else "-" for character in value)
    return "-".join(part for part in clean.split("-") if part) or "set"


def display_title_from_session(path: Path) -> str:
    return " ".join(part for part in path.stem.replace("_", "-").split("-") if part).title() or "Live Mix"


def write_active_dashboard_pointer(args: argparse.Namespace) -> None:
    if args.dry_run or args.no_active_pointer:
        return
    session_path = args.session.resolve()
    state_path = args.state.resolve()
    pointer = load_json(args.active_pointer)
    previous_session = pointer.get("active_session_path")
    same_session = previous_session and Path(str(previous_session)).expanduser().resolve() == session_path
    title = args.dashboard_title or (str(pointer.get("title")) if same_session and pointer.get("title") else display_title_from_session(args.session))
    slug = args.dashboard_slug or (str(pointer.get("slug")) if same_session and pointer.get("slug") else slugify(args.session.stem))
    write_json(
        args.active_pointer,
        {
            "slug": slug,
            "title": title,
            "archive_session_path": str(pointer.get("archive_session_path") if same_session and pointer.get("archive_session_path") else session_path),
            "active_session_path": str(session_path),
            "active_state_path": str(state_path),
            "loaded_at": iso_now(),
        },
    )


def append_history(path: Path | None, event: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


def write_runner_status(args: argparse.Namespace, status: str, **fields: Any) -> None:
    state = load_json(args.state)
    now = iso_now()
    if status == "running":
        for key in ("runner_exit_at", "runner_exit_reason", "signal", "traceback"):
            state.pop(key, None)
    state.update(
        {
            "runner_pid": os.getpid(),
            "runner_status": status,
            "runner_updated_at": now,
        }
    )
    state.setdefault("runner_started_at", now)
    state.update(fields)
    write_json(args.state, state)


def record_runner_exit(args: argparse.Namespace, *, status: str, reason: str, **fields: Any) -> None:
    now = iso_now()
    payload = {
        "runner_exit_at": now,
        "runner_exit_reason": reason,
        **fields,
    }
    write_runner_status(args, status, **payload)
    append_history(
        args.history_log,
        {
            "event": "session_runner_exit",
            "pid": os.getpid(),
            "reason": reason,
            "session": str(args.session),
            "state": str(args.state),
            "status": status,
            "timestamp": now,
            **fields,
        },
    )


def should_block_playback_start(args: argparse.Namespace) -> bool:
    return bool(args.pause_file and args.pause_file.exists() and not args.ignore_pause)


def record_paused_start_block(args: argparse.Namespace, *, component: str) -> None:
    now = iso_now()
    pause_file = str(args.pause_file)
    append_history(
        args.history_log,
        {
            "event": "playback_start_blocked",
            "component": component,
            "pause_file": pause_file,
            "pid": os.getpid(),
            "session": str(args.session),
            "state": str(args.state),
            "timestamp": now,
        },
    )
    print(
        json.dumps(
            {
                "status": "paused",
                "component": component,
                "pause_file": pause_file,
                "session": str(args.session),
            },
            sort_keys=True,
        ),
        flush=True,
    )


def clip_overlaps_window(clip: Clip, start_ms: int, end_ms: int) -> bool:
    if clip.duration_ms is None:
        return clip.start_ms >= start_ms and clip.start_ms < end_ms
    return clip.start_ms < end_ms and clip.end_ms is not None and clip.end_ms > start_ms


def clips_in_window(session: MixSession, start_ms: int, end_ms: int) -> list[Any]:
    events = [*session.clips, *session.stem_groups]
    return sorted(
        [clip for clip in events if clip_overlaps_window(clip, start_ms, end_ms)],
        key=lambda clip: (clip.start_ms, clip.deck, clip.id),
    )


def next_event_start_ms(session: MixSession, playhead_ms: int) -> int | None:
    starts = [clip.start_ms for clip in session.clips if clip.start_ms >= playhead_ms]
    starts.extend(group.start_ms for group in session.stem_groups if group.start_ms >= playhead_ms)
    starts.extend(lean.start_ms for lean in session.mic_lean_ins if lean.start_ms >= playhead_ms)
    return min(starts) if starts else None


def window_anchor_path(audio_path: Path) -> Path:
    """Sidecar file the stream writes when this window's audio becomes audible."""
    return audio_path.with_suffix(".anchor.json")


class FifoHold:
    """Keep one long-lived write handle on the snapcast FIFO across render windows.

    snapserver treats the FIFO as ended only when every writer has closed it, so a
    parent-held handle lets per-window ffmpeg writers come and go without the
    server emitting EOF and dropping the stream between windows.
    """

    def __init__(self, fifo_path: Path):
        self.fifo_path = fifo_path
        self.fd: int | None = None
        self.holder: subprocess.Popen[bytes] | None = None
        self.lock_fd: int | None = None

    @property
    def active(self) -> bool:
        return self.fd is not None or (self.holder is not None and self.holder.poll() is None)

    def writer_lock_path(self) -> Path:
        return self.fifo_path.parent / (self.fifo_path.name + ".slime-writer-lock")

    def lock_writer(self) -> bool:
        """Exclusive claim on this FIFO's stream — one live runner per FIFO.

        The keepalive handle below is not a mutex: any number of processes can
        open a FIFO for writing, and two runners interleave PCM into audible
        jumps (heard live 2026-07-03 16:54Z). The flock dies with the process,
        so a crashed runner never wedges the next one.
        """
        if self.lock_fd is not None:
            return True
        import fcntl

        try:
            fd = os.open(self.writer_lock_path(), os.O_CREAT | os.O_RDWR, 0o666)
        except OSError:
            # Bookkeeping must never brick playback: no lock file, no exclusion.
            return True
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        self.lock_fd = fd
        return True

    def acquire(self, *, retries: int = 10, retry_delay_s: float = 0.5) -> bool:
        if not stat_is_fifo(self.fifo_path):
            return False
        for _ in range(retries):
            try:
                # Non-blocking so a missing snapserver reader fails fast (ENXIO)
                # instead of hanging the runner before the first window.
                self.fd = os.open(self.fifo_path, os.O_WRONLY | os.O_NONBLOCK)
                return True
            except OSError as error:
                if error.errno == 6:  # ENXIO: no reader yet
                    time.sleep(retry_delay_s)
                    continue
                if isinstance(error, PermissionError):
                    return self._acquire_via_owner()
                return False
        return False

    def _acquire_via_owner(self) -> bool:
        import pwd

        try:
            owner = pwd.getpwuid(os.stat(self.fifo_path).st_uid).pw_name
        except (OSError, KeyError):
            return False
        command = ["sudo", "-n", "-u", owner, "sh", "-c", 'exec sleep infinity > "$0"', str(self.fifo_path)]
        try:
            self.holder = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            return False
        time.sleep(0.2)
        if self.holder.poll() is not None:
            self.holder = None
            return False
        return True

    def release(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
        if self.holder is not None:
            try:
                os.killpg(self.holder.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            self.holder = None
        if self.lock_fd is not None:
            try:
                os.close(self.lock_fd)
            except OSError:
                pass
            self.lock_fd = None


def stream_command(args: argparse.Namespace, audio_path: Path, *, continuation: bool = False) -> list[str]:
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
            "--anchor-file",
            str(window_anchor_path(audio_path)),
            "--no-active-pointer",
        ]
    )
    if args.snapcast_buffer_ms is not None:
        command.extend(["--snapcast-buffer-ms", str(args.snapcast_buffer_ms)])
    if args.mode == "snapcast":
        command.extend(["--snapcast-fifo", str(args.snapcast_fifo)])
    if args.mode == "multicast":
        command.extend(["--multicast-group", args.multicast_group])
        command.extend(["--multicast-port", str(args.multicast_port)])
        if args.no_auto_listeners:
            command.append("--no-auto-listeners")
        if args.stop_listeners_when_done:
            command.append("--stop-listeners-when-done")
    if args.ignore_pause:
        command.append("--ignore-pause")
    if continuation:
        command.append("--continuation")
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
        "--no-verify",
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


def start_window_stream(args: argparse.Namespace, window: PreparedWindow, *, continuation: bool = False) -> tuple[RunningWindow, list[str]]:
    stream = stream_command(args, window.output, continuation=continuation)
    process = start_stream(stream)
    return RunningWindow(process), stream


def wait_window_stream(running: RunningWindow) -> int:
    try:
        return wait_stream(running.process)
    finally:
        running.close()


def install_signal_handlers(args: argparse.Namespace) -> None:
    def handle_stop(signum: int, _frame: object) -> None:
        record_runner_exit(args, status="stopped", reason=f"signal:{signal.Signals(signum).name}", signal=signum)
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
    active_clips: list[Any],
) -> dict[str, Any]:
    now = iso_now()
    current_path = None
    if active_clips:
        current_path = getattr(active_clips[0], "path", None) or getattr(active_clips[0], "source_path", None)
    if not state.get("mix_started_at"):
        state["mix_started_at"] = now
    # A new window is live: drop any frozen completion playhead and the previous
    # window's audio anchor so playhead_ms_from_state extrapolates from this window.
    for key in ("runner_exit_at", "runner_exit_reason", "signal", "traceback", "playhead_ms", "mix_playhead_ms", "completed_at", "window_audio_latency_ms"):
        state.pop(key, None)
    state.update(
        {
            "session": str(args.session),
            "timeline_mode": "native-session-runner",
            "started_at": now,
            "window_started_at": now,
            "window_start_ms": start_ms,
            "window_end_ms": end_ms,
            "current": current_path,
            "resolved_current": current_path,
            "current_clips": [
                {
                    "id": clip.id,
                    "deck": clip.deck,
                    "path": getattr(clip, "path", None) or getattr(clip, "source_path", None),
                    "start_ms": clip.start_ms,
                    "end_ms": clip.end_ms,
                    "trim_start_ms": clip.trim_start_ms,
                }
                for clip in active_clips
            ],
            "duration_ms": session_duration_ms(session),
            "runner_pid": os.getpid(),
            "runner_status": "running",
            "runner_updated_at": now,
            "updated_at": now,
        }
    )
    state.setdefault("runner_started_at", now)
    write_json(args.state, state)
    return state


def apply_audio_anchor(args: argparse.Namespace, state: dict[str, Any], anchor_path: Path) -> bool:
    """Re-anchor the playhead to real audio start once the stream reports it.

    The stream writes ``audio_started_at`` the moment ffmpeg feeds the FIFO,
    offset by the snapcast buffer so it marks when the listener actually hears
    the window's first sample. Replacing the launch-time ``window_started_at``
    with it removes the constant lead between the dashboard and the audio.
    """
    anchor = load_json(anchor_path)
    audio_started_at = anchor.get("audio_started_at")
    if not audio_started_at:
        return False
    state["window_started_at"] = audio_started_at
    state["window_audio_latency_ms"] = anchor.get("latency_ms")
    state["updated_at"] = audio_started_at
    write_json(args.state, state)
    return True


def freeze_completed_window(args: argparse.Namespace, state: dict[str, Any], end_ms: int) -> dict[str, Any]:
    now = iso_now()
    state["playhead_ms"] = max(0, end_ms)
    for key in ("window_started_at", "window_start_ms", "window_end_ms", "window_audio_latency_ms"):
        state.pop(key, None)
    state["runner_status"] = "running"
    state["runner_updated_at"] = now
    state["updated_at"] = now
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
    Path(temp.name).chmod(0o755)
    output = Path(temp.name) / f"window-{start_ms}-{end_ms}.wav"
    active_clips = clips_in_window(session, start_ms, end_ms)
    try:
        render_command = render_window(args, start_ms, end_ms - start_ms, output)
        if output.exists():
            output.chmod(0o644)
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
    if should_block_playback_start(args):
        record_paused_start_block(args, component="session_runner")
        return 0

    install_signal_handlers(args)
    state = load_json(args.state)
    has_playhead = any(key in state for key in ("playhead_ms", "mix_playhead_ms", "window_started_at", "mix_started_at"))
    if args.reset_state or not has_playhead:
        state = {"mix_started_at": iso_now()}
        write_json(args.state, state)
    write_active_dashboard_pointer(args)
    write_runner_status(args, "running")

    prepared: PreparedWindow | None = None
    fifo_hold = FifoHold(args.snapcast_fifo) if args.mode == "snapcast" and not args.dry_run else None
    windows_streamed = 0
    if fifo_hold is not None:
        if not fifo_hold.lock_writer():
            append_history(
                args.history_log,
                {
                    "event": "session_runner_blocked",
                    "reason": "another session runner owns the snapcast stream (writer lock held)",
                    "fifo": str(args.snapcast_fifo),
                    "session": str(args.session),
                    "timestamp": iso_now(),
                },
            )
            write_runner_status(args, "stopped")
            raise SystemExit(
                "another session runner owns the snapcast stream (writer lock held); "
                "refusing to interleave audio — stop the live runner first or use a different FIFO"
            )
        if fifo_hold.acquire():
            append_history(
                args.history_log,
                {
                    "event": "session_fifo_hold_acquired",
                    "fifo": str(args.snapcast_fifo),
                    "session": str(args.session),
                    "timestamp": iso_now(),
                },
            )
        else:
            # Without the hold, per-window writer exits can EOF the snapserver
            # stream; fall back to full re-establishment on every window.
            append_history(
                args.history_log,
                {
                    "event": "session_fifo_hold_unavailable",
                    "fifo": str(args.snapcast_fifo),
                    "session": str(args.session),
                    "timestamp": iso_now(),
                },
            )
    try:
        while True:
            session = load_session(args.session)
            total_ms = session_duration_ms(session)
            playhead_ms = min(playhead_ms_from_state(args.state), total_ms)
            if playhead_ms >= total_ms:
                state["current"] = None
                state["completed_at"] = iso_now()
                state["runner_pid"] = os.getpid()
                state["runner_status"] = "completed"
                state["runner_updated_at"] = state["completed_at"]
                # Freeze the playhead explicitly so playhead_ms_from_state stops
                # extrapolating from the (now stale) window anchor after we exit.
                state["playhead_ms"] = total_ms
                for key in ("window_started_at", "window_start_ms", "window_end_ms", "window_audio_latency_ms"):
                    state.pop(key, None)
                write_json(args.state, state)
                append_history(
                    args.history_log,
                    {
                        "event": "session_runner_completed",
                        "pid": os.getpid(),
                        "session": str(args.session),
                        "state": str(args.state),
                        "timestamp": state["completed_at"],
                    },
                )
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
                end_ms = total_ms if args.single_window else min(total_ms, start_ms + args.window_ms)
                window = prepare_window(args, session, start_ms, end_ms)
                active_clips = window.active_clips

            if args.dry_run:
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
                stream = stream_command(args, window.output)
                print(json.dumps({"render": window.render_command, "stream": stream, "clips": [clip.id for clip in active_clips]}, indent=2))
                window.cleanup()
                return 0

            if fifo_hold is not None and not fifo_hold.active and fifo_hold.acquire(retries=1, retry_delay_s=0.2):
                append_history(
                    args.history_log,
                    {
                        "event": "session_fifo_hold_acquired",
                        "fifo": str(args.snapcast_fifo),
                        "session": str(args.session),
                        "timestamp": iso_now(),
                    },
                )
            continuation = windows_streamed > 0 and fifo_hold is not None and fifo_hold.active
            running, stream = start_window_stream(args, window, continuation=continuation)
            windows_streamed += 1
            started_monotonic = time.monotonic()
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
            returncode = 0
            anchor_path = window_anchor_path(window.output)
            anchor_applied = False
            while True:
                polled = running.process.poll()
                if polled is not None:
                    returncode = wait_window_stream(running)
                    break
                if not anchor_applied and anchor_path.exists() and apply_audio_anchor(args, state, anchor_path):
                    anchor_applied = True
                    # The window's audio just began; measure remaining time from here
                    # so prerender lead is relative to playback, not the launch.
                    started_monotonic = time.monotonic()
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
                # Re-establish snapclient control on the next attempt instead of
                # assuming the failed window left receivers in a good state.
                windows_streamed = 0
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
            state = freeze_completed_window(args, state, end_ms)
    finally:
        if fifo_hold is not None:
            fifo_hold.release()
        if prepared is not None:
            prepared.cleanup()


def parse_args_from(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a live-editable timestamped SlimeAudio mix session.")
    parser.add_argument("--session", type=Path, default=DEFAULT_SESSION)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--history-log", type=Path, default=DEFAULT_HISTORY)
    parser.add_argument("--active-pointer", type=Path, default=DEFAULT_ACTIVE_SET)
    parser.add_argument("--dashboard-title")
    parser.add_argument("--dashboard-slug")
    parser.add_argument("--no-active-pointer", action="store_true")
    parser.add_argument("--target", action="append", default=None)
    parser.add_argument("--mode", choices=["snapcast", "multicast"], default="snapcast")
    parser.add_argument("--backend", choices=["auto", "ffmpeg"], default="ffmpeg")
    parser.add_argument("--window-ms", type=int, default=180_000)
    parser.add_argument(
        "--single-window",
        action="store_true",
        help="Render and stream from the current playhead to session end as one window to avoid FIFO handoff gaps.",
    )
    parser.add_argument("--prerender-next", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--prerender-lead-ms", type=int, default=60_000)
    parser.add_argument("--idle-poll-ms", type=int, default=1000)
    parser.add_argument("--retry-seconds", type=int, default=5)
    parser.add_argument("--discover-timeout-ms", type=int, default=4000)
    parser.add_argument("--delay-ms", type=int, default=0)
    parser.add_argument(
        "--snapcast-buffer-ms",
        type=int,
        default=None,
        help="Override the snapcast end-to-end latency (ms) used to anchor the playhead. Default queries the snapserver.",
    )
    parser.add_argument("--snapcast-fifo", type=Path, default=None)
    parser.add_argument("--multicast-group", default="239.77.77.77")
    parser.add_argument("--multicast-port", type=int, default=47778)
    parser.add_argument("--no-auto-listeners", action="store_true")
    parser.add_argument("--stop-listeners-when-done", action="store_true")
    parser.add_argument("--kokoro-url", default=DEFAULT_KOKORO_URL)
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
    parser.add_argument(
        "--pause-file",
        type=Path,
        default=DEFAULT_DJ_PAUSE_FILE,
        help="If present, refuse to start playback unless --ignore-pause is set.",
    )
    parser.add_argument("--ignore-pause", action="store_true", help="Start playback even when the DJ pause file exists.")
    args = parser.parse_args(argv)
    if args.target is None:
        args.target = ["all"]
    if args.snapcast_fifo is None:
        args.snapcast_fifo = system_snapcast_fifo_path()
    if args.window_ms <= 0:
        raise SystemExit("--window-ms must be positive")
    return args


def main() -> int:
    args = parse_args_from()
    try:
        return run_session(args)
    except SystemExit:
        raise
    except BaseException as exc:
        record_runner_exit(
            args,
            status="fatal",
            reason=f"{exc.__class__.__name__}: {exc}",
            traceback="".join(traceback.format_exception(exc.__class__, exc, exc.__traceback__)),
        )
        raise


if __name__ == "__main__":
    raise SystemExit(main())
