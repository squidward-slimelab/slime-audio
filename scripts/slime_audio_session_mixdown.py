#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import tempfile
import urllib.request
from dataclasses import replace
from pathlib import Path

from slime_audio_session import Automation, AutomationPoint, Clip, MicLeanIn, MixSession, load_session, parse_ms


def seconds(ms: int) -> str:
    return f"{ms / 1000:.3f}"


def shell_escape_filter(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def gain_multiplier(db: float) -> float:
    return 10 ** (db / 20)


def tempo_factor(clip: Clip) -> float:
    factor = 1 + (clip.tempo_shift_pct / 100)
    if factor <= 0:
        raise ValueError(f"clip {clip.id} tempo_shift_pct produces non-positive tempo")
    return factor


def atempo_filters(factor: float) -> list[str]:
    if factor <= 0:
        raise ValueError("tempo factor must be positive")
    filters = []
    remaining = factor
    while remaining > 2.0:
        filters.append("atempo=2.000000")
        remaining /= 2.0
    while remaining < 0.5:
        filters.append("atempo=0.500000")
        remaining /= 0.5
    filters.append(f"atempo={remaining:.6f}")
    return filters


def time_pitch_filters(clip: Clip, sample_rate: int) -> list[str]:
    filters: list[str] = []
    if clip.pitch_shift_semitones:
        pitch_factor = 2 ** (clip.pitch_shift_semitones / 12)
        filters.append(f"asetrate={max(1, int(round(sample_rate * pitch_factor)))}")
        filters.append(f"aresample={sample_rate}")
        filters.extend(atempo_filters(1 / pitch_factor))
    if clip.tempo_shift_pct:
        filters.extend(atempo_filters(tempo_factor(clip)))
    return filters


def source_duration_ms(clip: Clip) -> int | None:
    if clip.duration_ms is None:
        return None
    return max(1, int(round(clip.duration_ms * tempo_factor(clip))))


def shift_automation_window(automation: Automation, from_ms: int) -> Automation | None:
    points = sorted(automation.points, key=lambda point: point.at_ms)
    shifted: list[AutomationPoint] = []
    last_before: AutomationPoint | None = None
    for point in points:
        if point.at_ms < from_ms:
            last_before = point
            continue
        if last_before is not None and not shifted:
            shifted.append(AutomationPoint(at_ms=0, value=last_before.value, curve=last_before.curve))
        shifted.append(replace(point, at_ms=point.at_ms - from_ms))
    if not shifted:
        return None
    return replace(automation, points=shifted)


def shift_session_window(session: MixSession, from_ms: int, duration_ms: int | None = None) -> MixSession:
    if from_ms <= 0 and duration_ms is None:
        return session
    window_end_ms = from_ms + duration_ms if duration_ms is not None else None

    clips: list[Clip] = []
    for clip in session.clips:
        if clip.end_ms is not None and clip.end_ms <= from_ms:
            continue
        if window_end_ms is not None and clip.start_ms >= window_end_ms:
            continue
        if clip.start_ms < from_ms and clip.duration_ms is None:
            continue
        overlap_ms = max(0, from_ms - clip.start_ms)
        duration_ms = clip.duration_ms - overlap_ms if clip.duration_ms is not None else None
        if duration_ms is not None and window_end_ms is not None:
            duration_ms = min(duration_ms, max(0, window_end_ms - max(clip.start_ms, from_ms)))
        if duration_ms is not None and duration_ms <= 0:
            continue
        shifted_automations = [
            shifted for automation in clip.automations if (shifted := shift_automation_window(automation, from_ms)) is not None
        ]
        clips.append(
            replace(
                clip,
                start_ms=max(0, clip.start_ms - from_ms),
                trim_start_ms=clip.trim_start_ms + int(round(overlap_ms * tempo_factor(clip))),
                duration_ms=duration_ms,
                automations=shifted_automations,
            )
        )

    mic_lean_ins = [
        replace(
            lean_in,
            start_ms=lean_in.start_ms - from_ms,
            effects=[
                shifted for effect in lean_in.effects if (shifted := shift_automation_window(effect, from_ms)) is not None
            ],
        )
        for lean_in in session.mic_lean_ins
        if lean_in.start_ms >= from_ms and (window_end_ms is None or lean_in.start_ms < window_end_ms)
    ]
    automations = [
        shifted for automation in session.automations if (shifted := shift_automation_window(automation, from_ms)) is not None
    ]
    return MixSession(
        version=session.version,
        decks=session.decks,
        clips=clips,
        mic_lean_ins=mic_lean_ins,
        automations=automations,
    )


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


def build_filter_complex(
    session: MixSession,
    lean_in_audio: dict[str, Path],
    sample_rate: int,
    channels: int,
    output_duration_ms: int | None = None,
) -> str:
    filters: list[str] = []
    music_labels: list[str] = []
    for index, clip in enumerate(session.clips):
        source_duration = source_duration_ms(clip)
        duration = f":duration={seconds(source_duration)}" if source_duration is not None else ""
        label = f"music{index}"
        volume = gain_multiplier(clip.gain_db)
        fade_in_ms = clip.fade_in_ms
        fade_out_ms = clip.fade_out_ms
        fade_filters = ""
        if fade_in_ms:
            fade_filters += f"afade=t=in:st=0:d={seconds(fade_in_ms)},"
        if fade_out_ms and clip.duration_ms is not None:
            fade_start_ms = max(0, clip.duration_ms - fade_out_ms)
            fade_filters += f"afade=t=out:st={seconds(fade_start_ms)}:d={seconds(fade_out_ms)},"
        retime_filters = ",".join(time_pitch_filters(clip, sample_rate))
        retime_filters = f"{retime_filters}," if retime_filters else ""
        filters.append(
            f"[{index}:a]"
            f"atrim=start={seconds(clip.trim_start_ms)}{duration},"
            "asetpts=PTS-STARTPTS,"
            f"{retime_filters}"
            f"{fade_filters}"
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

    output_filters = "alimiter=limit=0.98"
    if output_duration_ms is not None:
        output_filters = f"atrim=duration={seconds(output_duration_ms)}," + output_filters
    filters.append(f"{''.join(mix_labels)}amix=inputs={len(mix_labels)}:duration=longest:normalize=0,{output_filters}[out]")
    return ";".join(filters)


def ffmpeg_command(
    session: MixSession,
    lean_in_audio: dict[str, Path],
    output: Path,
    sample_rate: int,
    channels: int,
    output_duration_ms: int | None = None,
) -> list[str]:
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
            build_filter_complex(session, lean_in_audio, sample_rate, channels, output_duration_ms),
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
    parser.add_argument(
        "--from",
        dest="from_time",
        default="0",
        help="Render a live-edit window starting at this mix timestamp/playhead.",
    )
    parser.add_argument("--duration", help="Render only this much timeline after --from.")
    parser.add_argument("--skip-tts", action="store_true", help="Use tiny silent lean-in placeholders; useful for command validation.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    duration_ms = parse_ms(args.duration, "render duration") if args.duration else None
    session = shift_session_window(load_session(args.session), parse_ms(args.from_time, "render start"), duration_ms)
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
        command = ffmpeg_command(session, lean_audio, args.output, args.sample_rate, args.channels, duration_ms)
        if args.dry_run:
            print(json.dumps({"command": command, "filter_complex": command[command.index("-filter_complex") + 1]}, indent=2))
            return 0
        subprocess.run(command, check=True)
    print(f"rendered {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
