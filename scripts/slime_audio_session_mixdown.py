#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import replace
from pathlib import Path

from slime_audio_session import Automation, AutomationPoint, Clip, MicLeanIn, MixSession, load_payload, load_session, parse_ms


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


def clip_automation_windows(session: MixSession, clip: Clip, param: str) -> list[tuple[int, int, float]]:
    windows: list[tuple[int, int, float]] = []
    automations = [*clip.automations, *(automation for automation in session.automations if automation.target == clip.id)]
    for automation in automations:
        if automation.param != param:
            continue
        points = sorted(automation.points, key=lambda point: point.at_ms)
        if len(points) < 2:
            continue
        start_ms = max(0, points[0].at_ms - clip.start_ms)
        end_ms = max(0, points[-1].at_ms - clip.start_ms)
        if end_ms <= start_ms:
            continue
        windows.append((start_ms, end_ms, float(points[0].value)))
    return sorted(windows, key=lambda item: item[0])


def clip_effect_filters(session: MixSession, clip: Clip) -> str:
    filters: list[str] = []
    for start_ms, end_ms, lowpass_hz in clip_automation_windows(session, clip, "lowpass_hz"):
        filters.append(f"lowpass=enable='between(t,{seconds(start_ms)},{seconds(end_ms)})':f={lowpass_hz:.3f}")
    for start_ms, end_ms, highpass_hz in clip_automation_windows(session, clip, "highpass_hz"):
        filters.append(f"highpass=enable='between(t,{seconds(start_ms)},{seconds(end_ms)})':f={highpass_hz:.3f}")
    for start_ms, end_ms, gain_db in clip_automation_windows(session, clip, "gain_db"):
        filters.append(
            f"volume=enable='between(t,{seconds(start_ms)},{seconds(end_ms)})':volume={gain_multiplier(gain_db):.6f}"
        )
    return ",".join(filters)


def build_filter_complex(
    session: MixSession,
    lean_in_audio: dict[str, Path],
    sample_rate: int,
    channels: int,
    output_duration_ms: int | None = None,
) -> str:
    active_lean_in_ids = set(lean_in_audio)
    session = replace(session, mic_lean_ins=[lean_in for lean_in in session.mic_lean_ins if lean_in.id in active_lean_in_ids])
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
        effect_filters = clip_effect_filters(session, clip)
        effect_filters = f"{effect_filters}," if effect_filters else ""
        filters.append(
            f"[{index}:a]"
            f"atrim=start={seconds(clip.trim_start_ms)}{duration},"
            "asetpts=PTS-STARTPTS,"
            f"{retime_filters}"
            f"{fade_filters}"
            f"{effect_filters}"
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
    output_format: str = "auto",
    mp3_bitrate: str = "192k",
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
            *output_codec_args(output, output_format, sample_rate, channels, mp3_bitrate),
            str(output),
        ]
    )
    return command


def resolved_output_format(output: Path, output_format: str) -> str:
    if output_format != "auto":
        return output_format
    suffix = output.suffix.lower()
    if suffix == ".mp3":
        return "mp3"
    if suffix == ".flac":
        return "flac"
    return "wav"


def output_codec_args(output: Path, output_format: str, sample_rate: int, channels: int, mp3_bitrate: str) -> list[str]:
    resolved = resolved_output_format(output, output_format)
    common = ["-ac", str(channels), "-ar", str(sample_rate)]
    if resolved == "wav":
        return ["-acodec", "pcm_s16le", *common]
    if resolved == "flac":
        return ["-acodec", "flac", *common]
    if resolved == "mp3":
        return ["-acodec", "libmp3lame", "-b:a", mp3_bitrate, *common]
    raise ValueError(f"unsupported output format: {output_format}")


def verify_rendered_audio(output: Path) -> None:
    report = rendered_audio_report(output)
    if report["duration_seconds"] <= 0:
        raise ValueError(f"rendered mix has no positive duration: {output}")
    if report["silent"]:
        raise ValueError(f"rendered mix is silent: {output}")


def rendered_audio_report(output: Path) -> dict[str, float | int | bool | None]:
    probe = subprocess.run(
        [
            "ffprobe",
            "-hide_banner",
            "-loglevel",
            "error",
            "-show_entries",
            "format=duration,bit_rate",
            "-of",
            "json",
            str(output),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    format_payload = json.loads(probe.stdout or "{}").get("format", {})
    duration = float(format_payload.get("duration") or 0)
    bit_rate = int(float(format_payload.get("bit_rate") or 0)) if format_payload.get("bit_rate") else None
    volume = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-i",
            str(output),
            "-af",
            "volumedetect",
            "-f",
            "null",
            "-",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    text = f"{volume.stdout}\n{volume.stderr}"
    mean_match = re.search(r"mean_volume:\s+(-?inf|-?\d+(?:\.\d+)?) dB", text)
    max_match = re.search(r"max_volume:\s+(-?inf|-?\d+(?:\.\d+)?) dB", text)
    mean_volume = None if mean_match is None or mean_match.group(1) == "-inf" else float(mean_match.group(1))
    max_volume = None if max_match is None or max_match.group(1) == "-inf" else float(max_match.group(1))
    return {
        "duration_seconds": duration,
        "bit_rate": bit_rate,
        "mean_volume_db": mean_volume,
        "max_volume_db": max_volume,
        "silent": mean_volume is None or max_volume is None,
        "clipping_risk": max_volume is not None and max_volume >= -0.1,
    }


def clip_start_end_payload(clip: dict) -> tuple[int, int | None]:
    start_ms = parse_ms(clip.get("start_ms", clip.get("start", 0)), "clip start")
    duration = clip.get("duration_ms", clip.get("duration"))
    duration_ms = parse_ms(duration, "clip duration") if duration is not None else None
    return start_ms, start_ms + duration_ms if duration_ms is not None else None


def routine_window(payload: dict, routine_id: str, pad_ms: int) -> tuple[int, int]:
    starts: list[int] = []
    ends: list[int] = []
    for clip in payload.get("clips", []):
        if clip.get("routine_id") != routine_id:
            continue
        start_ms, end_ms = clip_start_end_payload(clip)
        starts.append(start_ms)
        if end_ms is not None:
            ends.append(end_ms)
    for automation in payload.get("automations", []):
        if automation.get("routine_id") != routine_id:
            continue
        points = automation.get("points") or []
        point_times = [
            parse_ms(point.get("at_ms", point.get("at", 0)), "automation point")
            for point in points
            if isinstance(point, dict)
        ]
        starts.extend(point_times[:1])
        ends.extend(point_times[-1:])
    if not starts or not ends:
        raise ValueError(f"routine id not found or has no timed events: {routine_id}")
    return max(0, min(starts) - pad_ms), max(ends) + pad_ms


def routine_taste_report(payload: dict, routine_id: str, start_ms: int, end_ms: int) -> dict:
    routine_clips = [clip for clip in payload.get("clips", []) if clip.get("routine_id") == routine_id]
    active_clips = []
    for clip in payload.get("clips", []):
        clip_start, clip_end = clip_start_end_payload(clip)
        if clip_end is not None and clip_start < end_ms and start_ms < clip_end:
            active_clips.append(clip)
    unrelated = [
        clip
        for clip in active_clips
        if clip.get("routine_id") not in {routine_id, None} and clip.get("source_clip_id") not in {item.get("id") for item in routine_clips}
    ]
    routine_recipes = sorted({str(clip.get("routine_recipe")) for clip in routine_clips if clip.get("routine_recipe")})
    errors: list[str] = []
    warnings: list[str] = []
    if not routine_clips:
        errors.append("routine has no clips")
    if len(active_clips) > 3:
        warnings.append("more than three clips are active during the routine window")
    if unrelated:
        errors.append("unrelated routine clips overlap the audition window")
    if any(clip.get("pitch_shift_semitones") not in {None, 0} for clip in active_clips):
        warnings.append("audition includes rendered pitch correction; verify key fit by ear")
    if any(abs(float(clip.get("tempo_shift_pct") or 0.0)) > 4.0 for clip in active_clips):
        errors.append("tempo shift exceeds conservative routine audition limit")
    return {
        "routine_id": routine_id,
        "routine_recipes": routine_recipes,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "active_clip_ids": [clip.get("id") for clip in active_clips],
        "checks": {
            "key_fit": "unknown_without_analysis_metadata",
            "bpm_fit": "ok" if not any(abs(float(clip.get("tempo_shift_pct") or 0.0)) > 4.0 for clip in active_clips) else "fail",
            "vocal_on_vocal_risk": "unknown_without_stems",
            "dense_drums_risk": "unknown_without_stems",
            "effect_tails": "not_detected",
        },
        "warnings": warnings,
        "errors": errors,
        "accepted": not errors,
    }


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
            try:
                raw = temp_dir / f"{lean_in.id}-raw.wav"
                synthesize_kokoro(kokoro_url, lean_in.voice or default_voice, lean_in.text, raw, timeout)
                normalize_tts(raw, wav, sample_rate, channels)
                report = rendered_audio_report(wav)
                if report["silent"]:
                    print(f"warning: skipping silent lean-in audio for {lean_in.id}", file=sys.stderr)
                    continue
            except Exception as error:
                print(f"warning: skipping failed lean-in audio for {lean_in.id}: {error}", file=sys.stderr)
                continue
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
    parser.add_argument("--format", choices=["auto", "wav", "mp3", "flac"], default="auto")
    parser.add_argument("--mp3-bitrate", default="192k")
    parser.add_argument(
        "--from",
        dest="from_time",
        default="0",
        help="Render a live-edit window starting at this mix timestamp/playhead.",
    )
    parser.add_argument("--duration", help="Render only this much timeline after --from.")
    parser.add_argument("--routine-id", help="Render an audition window around a planned routine id.")
    parser.add_argument("--routine-pad-ms", type=int, default=5_000)
    parser.add_argument("--report-output", type=Path, help="Write a machine-readable audition/render verification report.")
    parser.add_argument("--force-routine-risk", action="store_true", help="Render even if routine taste checks report errors.")
    parser.add_argument("--skip-tts", action="store_true", help="Use tiny silent lean-in placeholders; useful for command validation.")
    parser.add_argument("--verify", action=argparse.BooleanOptionalAction, default=True, help="Probe rendered duration and reject silent output.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    payload = load_payload(args.session)
    routine_report = None
    if args.routine_id:
        routine_start_ms, routine_end_ms = routine_window(payload, args.routine_id, args.routine_pad_ms)
        args.from_time = str(routine_start_ms)
        duration_ms = routine_end_ms - routine_start_ms
        routine_report = routine_taste_report(payload, args.routine_id, routine_start_ms, routine_end_ms)
        if not routine_report["accepted"] and not args.force_routine_risk:
            if args.report_output:
                args.report_output.write_text(json.dumps({"routine": routine_report}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            raise ValueError(f"routine audition rejected: {', '.join(routine_report['errors'])}")
    else:
        duration_ms = parse_ms(args.duration, "render duration") if args.duration else None
    render_start_ms = parse_ms(args.from_time, "render start")
    session = shift_session_window(load_session(args.session), render_start_ms, duration_ms)
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
        command = ffmpeg_command(
            session,
            lean_audio,
            args.output,
            args.sample_rate,
            args.channels,
            duration_ms,
            args.format,
            args.mp3_bitrate,
        )
        if args.dry_run:
            report = {
                "command": command,
                "filter_complex": command[command.index("-filter_complex") + 1],
                "render": {"from_ms": render_start_ms, "duration_ms": duration_ms},
            }
            if routine_report is not None:
                report["routine"] = routine_report
            if args.report_output:
                args.report_output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            print(json.dumps(report, indent=2))
            return 0
        subprocess.run(command, check=True)
        audio_report = None
        if args.verify:
            verify_rendered_audio(args.output)
            audio_report = rendered_audio_report(args.output)
        if args.report_output:
            args.report_output.write_text(
                json.dumps(
                    {
                        "output": str(args.output),
                        "render": {"from_ms": render_start_ms, "duration_ms": duration_ms},
                        "routine": routine_report,
                        "audio": audio_report,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
    print(f"rendered {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
