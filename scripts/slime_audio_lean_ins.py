#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import random
import time
from pathlib import Path

from slime_audio_session import add_mic_lean_in, base_payload, write_payload

DEFAULT_PHRASES = [
    "quick squid note. the mix is still alive and making questionable decisions.",
    "tiny interruption from the department of getting away with it.",
    "this stretch has teeth on it. please keep your hands inside the vehicle.",
    "status report. bass is present. dignity is optional.",
    "the room is now operating under emergency groove procedures.",
    "little lean in. this one is doing exactly what it was hired to do.",
    "control room bulletin. i am no longer neglecting the voice drops. allegedly.",
    "if this sounds too serious, dont worry, it is absolutely not.",
    "squidward check. timed drops are back because apparently i need supervision.",
    "keep moving. the playlist is entering its legally suspicious section.",
]


def parse_ms(value: str) -> int:
    text = value.strip()
    if text.isdigit():
        return int(text)
    parts = text.split(":")
    if len(parts) not in {2, 3}:
        raise ValueError(f"invalid time value: {value}")
    seconds = float(parts[-1])
    minutes = int(parts[-2])
    hours = int(parts[0]) if len(parts) == 3 else 0
    return int(round(((hours * 3600) + (minutes * 60) + seconds) * 1000))


def format_ms(value: int) -> str:
    value = max(0, value)
    minutes, milliseconds = divmod(value, 60_000)
    seconds, milliseconds = divmod(milliseconds, 1000)
    return f"{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def choose_phrase(phrases: list[str], index: int | None = None) -> str:
    if index is not None:
        return phrases[index % len(phrases)]
    return random.choice(phrases)


def append_lean_in(
    session_path: Path,
    *,
    lean_id: str,
    start_ms: int,
    text: str,
    voice: str | None,
    rate: str | None,
    duck_volume: float,
    lowpass_hz: float,
    duck_ms: int,
    create: bool,
) -> None:
    payload = base_payload(session_path, create)
    updated = add_mic_lean_in(
        payload,
        lean_id=lean_id,
        start=format_ms(start_ms),
        text=text,
        voice=voice,
        rate=rate,
        duck_volume=duck_volume,
        lowpass_hz=lowpass_hz,
        duck_ms=duck_ms,
    )
    write_payload(session_path, updated)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Plan SlimeAudio lean-ins as mix-session events. This script intentionally does not send packet-mode audio."
    )
    parser.add_argument("--session", type=Path, default=Path("runtime/mix-session.json"))
    parser.add_argument("--create", action="store_true")
    parser.add_argument("--id-prefix", default="lean-in")
    parser.add_argument("--start", default="00:00.000")
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--gap-ms", type=int, default=240_000)
    parser.add_argument("--text")
    parser.add_argument("--phrases-file", type=Path)
    parser.add_argument("--voice")
    parser.add_argument("--rate")
    parser.add_argument("--duck-volume", type=float, default=0.45)
    parser.add_argument("--lowpass-hz", type=float, default=1400.0)
    parser.add_argument("--duck-ms", type=int, default=3500)
    parser.add_argument("--log", type=Path, default=Path("/tmp/slime-audio-lean-ins.log"))
    args = parser.parse_args()

    phrases = DEFAULT_PHRASES
    if args.phrases_file is not None:
        loaded = json.loads(args.phrases_file.read_text(encoding="utf-8"))
        if not isinstance(loaded, list) or not all(isinstance(item, str) and item.strip() for item in loaded):
            raise SystemExit("--phrases-file must be a JSON list of non-empty strings")
        phrases = loaded

    start_ms = parse_ms(args.start)
    args.log.parent.mkdir(parents=True, exist_ok=True)
    with args.log.open("a", encoding="utf-8") as log:
        for offset in range(args.count):
            lean_id = f"{args.id_prefix}-{int(time.time())}-{offset + 1}"
            text = args.text if args.text is not None else choose_phrase(phrases, offset if args.count > 1 else None)
            event_start_ms = start_ms + (offset * args.gap_ms)
            append_lean_in(
                args.session,
                lean_id=lean_id,
                start_ms=event_start_ms,
                text=text,
                voice=args.voice,
                rate=args.rate,
                duck_volume=args.duck_volume,
                lowpass_hz=args.lowpass_hz,
                duck_ms=args.duck_ms,
                create=args.create,
            )
            log.write(
                json.dumps(
                    {
                        "event": "lean_in_planned",
                        "id": lean_id,
                        "session": str(args.session),
                        "start_ms": event_start_ms,
                        "text": text,
                        "duck_volume": args.duck_volume,
                        "lowpass_hz": args.lowpass_hz,
                        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    },
                    sort_keys=True,
                )
                + "\n"
            )
            print(f"planned {lean_id} at {format_ms(event_start_ms)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
