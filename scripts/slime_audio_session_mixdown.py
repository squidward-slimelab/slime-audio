#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import tempfile
import urllib.request
from pathlib import Path

from slime_audio_session import Automation, MicLeanIn, MixSession, load_session


def seconds(ms: int) -> str:
    return f"{ms / 1000:.3f}"


def shell_escape_filter(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def gain_multiplier(db: float) -> float:
    return 10 ** (db / 20)


def synthesize_kokoro(base_url: str, voice: str, text: str, output_path: Path, timeout: int) -> None:
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/tts",
        data=json.dumps({"text": text, "voice": voice}).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    output_path.write_bytes(base64.b64decode(payload["audio"]))


def normalize_tts(input_path: Path, output_path: Path, sample_rate: int, channels: int) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(input_path),
            "-af",
            "loudnorm=I=-15:TP=-1.5:LRA=8",
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


def automation_window(lean_in: MicLeanIn, param: str, fallback_value: float) -> tuple[int, int, float]:
    effect = next((item for item in lean_in.effects if item.param == param), None)
    if effect is None or not effect.points:
        return max(0, lean_in.start_ms - 250), lean_in.start_ms + 3500, fallback_value
    start = effect.points[0].at_ms
    end = effect.points[-1].at_ms
    value = float(effect.points[0].value)
    return start, end, value


def collect_master_automation(session: MixSession, param: str) -> list[tuple[int, int, float]]:
    windows = []
    for lean_in in session.mic_lean_ins:
        windows.append(automation_window(lean_in, param, 0.45 if param == "duck_volume" else 1400.0))
    for automation in session.automations:
        if automation.target == "master" and automation.param == param:
            windows.append(window_from_automation(automation))
    return sorted(windows, key=lambda item: item[0])


def session_duration_ms(session: MixSession, lean_in_default_ms: int = 5000) -> int:
    ends = [clip.end_ms for clip in session.clips if clip.end_ms is not None]
    ends.extend(lean_in.start_ms + lean_in_default_ms for lean_in in session.mic_lean_ins)
    for param in ("duck_volume", "lowpass_hz"):
        ends.extend(end for _start, end, _value in collect_master_automation(session, param))
    return max(ends, default=1000)


def window_from_automation(automation: Automation) -> tuple[int, int, float]:
    return automation.points[0].at_ms, automation.points[-1].at_ms, float(automation.points[0].value)


def build_filter_complex(session: MixSession, lean_in_audio: dict[str, Path], sample_rate: int, channels: int) -> str:
    filters: list[str] = []
    music_labels: list[str] = []
    for index, clip in enumerate(session.clips):
        duration = f":duration={seconds(clip.duration_ms)}" if clip.duration_ms is not None else ""
        label = f"music{index}"
        volume = gain_multiplier(clip.gain_db)
        filters.append(
            f"[{index}:a]"
            f"atrim=start={seconds(clip.trim_start_ms)}{duration},"
            "asetpts=PTS-STARTPTS,"
            f"volume={volume:.6f},"
            f"adelay={clip.start_ms}:all=1,"
            f"aformat=sample_rates={sample_rate}:channel_layouts={'stereo' if channels == 2 else 'mono'}"
            f"[{label}]"
        )
        music_labels.append(f"[{label}]")

    next_input = len(session.clips)
    if music_labels:
        filters.append(f"{''.join(music_labels)}amix=inputs={len(music_labels)}:duration=longest:normalize=0[musicmix]")
        current_music = "musicmix"
    else:
        filters.append(
            f"anullsrc=r={sample_rate}:cl={'stereo' if channels == 2 else 'mono'}:d={seconds(session_duration_ms(session))}[musicmix]"
        )
        current_music = "musicmix"

    for idx, (start_ms, end_ms, duck_volume) in enumerate(collect_master_automation(session, "duck_volume")):
        out = f"duck{idx}"
        filters.append(
            f"[{current_music}]volume=enable='between(t,{seconds(start_ms)},{seconds(end_ms)})':volume={duck_volume:.6f}[{out}]"
        )
        current_music = out

    for idx, (start_ms, end_ms, lowpass_hz) in enumerate(collect_master_automation(session, "lowpass_hz")):
        out = f"lowpass{idx}"
        filters.append(
            f"[{current_music}]lowpass=enable='between(t,{seconds(start_ms)},{seconds(end_ms)})':f={lowpass_hz:.3f}[{out}]"
        )
        current_music = out

    mix_labels = [f"[{current_music}]"]
    for index, lean_in in enumerate(session.mic_lean_ins):
        audio_path = lean_in_audio.get(lean_in.id)
        if audio_path is None:
            continue
        input_index = next_input
        next_input += 1
        label = f"lean{index}"
        volume = max(0.0, float(lean_in.volume))
        filters.append(
            f"[{input_index}:a]"
            "asetpts=PTS-STARTPTS,"
            f"volume={volume:.6f},"
            f"adelay={lean_in.start_ms}:all=1,"
            f"aformat=sample_rates={sample_rate}:channel_layouts={'stereo' if channels == 2 else 'mono'}"
            f"[{label}]"
        )
        mix_labels.append(f"[{label}]")

    filters.append(f"{''.join(mix_labels)}amix=inputs={len(mix_labels)}:duration=longest:normalize=0,alimiter=limit=0.98[out]")
    return ";".join(filters)


def ffmpeg_command(session: MixSession, lean_in_audio: dict[str, Path], output: Path, sample_rate: int, channels: int) -> list[str]:
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for clip in session.clips:
        command.extend(["-i", clip.path])
    for lean_in in session.mic_lean_ins:
        audio_path = lean_in_audio.get(lean_in.id)
        if audio_path is not None:
            command.extend(["-i", str(audio_path)])
    command.extend(
        [
            "-filter_complex",
            build_filter_complex(session, lean_in_audio, sample_rate, channels),
            "-map",
            "[out]",
            "-acodec",
            "pcm_s16le",
            "-ac",
            str(channels),
            "-ar",
            str(sample_rate),
            str(output),
        ]
    )
    return command


def prepare_lean_in_audio(
    session: MixSession,
    temp_dir: Path,
    kokoro_url: str,
    default_voice: str,
    timeout: int,
    sample_rate: int,
    channels: int,
    skip_tts: bool,
) -> dict[str, Path]:
    audio: dict[str, Path] = {}
    for lean_in in session.mic_lean_ins:
        wav = temp_dir / f"{lean_in.id}.wav"
        if skip_tts:
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    f"anullsrc=r={sample_rate}:cl={'stereo' if channels == 2 else 'mono'}",
                    "-t",
                    "0.1",
                    str(wav),
                ],
                check=True,
            )
        else:
            raw = temp_dir / f"{lean_in.id}-raw.wav"
            synthesize_kokoro(kokoro_url, lean_in.voice or default_voice, lean_in.text, raw, timeout)
            normalize_tts(raw, wav, sample_rate, channels)
        audio[lean_in.id] = wav
    return audio


def main() -> int:
    parser = argparse.ArgumentParser(description="Render a planned SlimeAudio mix session to one Snapcast-ready audio file.")
    parser.add_argument("session", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--kokoro-url", default="http://robokrabs:7862")
    parser.add_argument("--voice", default="am_eric")
    parser.add_argument("--tts-timeout-seconds", type=int, default=90)
    parser.add_argument("--sample-rate", type=int, default=48_000)
    parser.add_argument("--channels", type=int, default=2)
    parser.add_argument("--skip-tts", action="store_true", help="Use tiny silent lean-in placeholders; useful for command validation.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    session = load_session(args.session)
    with tempfile.TemporaryDirectory(prefix="slime-session-mixdown-") as temp:
        lean_audio = prepare_lean_in_audio(
            session,
            Path(temp),
            args.kokoro_url,
            args.voice,
            args.tts_timeout_seconds,
            args.sample_rate,
            args.channels,
            args.skip_tts or args.dry_run,
        )
        command = ffmpeg_command(session, lean_audio, args.output, args.sample_rate, args.channels)
        if args.dry_run:
            print(json.dumps({"command": command, "filter_complex": command[command.index("-filter_complex") + 1]}, indent=2))
            return 0
        subprocess.run(command, check=True)
    print(f"rendered {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
