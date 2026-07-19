from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
from threading import Event
import time

from app.services.speaker_diarizer import diarize_speakers


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Локальный замер скорости разделения спикеров",
    )
    parser.add_argument("media", nargs="+", type=Path, help="Один или несколько медиафайлов")
    parser.add_argument(
        "--speakers",
        type=int,
        default=None,
        help="Известное количество спикеров от 2 до 10. По умолчанию определяется автоматически.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    for raw_path in args.media:
        path = raw_path.resolve()
        if not path.is_file():
            print(json.dumps({"file": str(path), "error": "Файл не найден"}, ensure_ascii=False))
            continue

        started = time.perf_counter()
        turns = diarize_speakers(
            path,
            lambda *_args: None,
            Event(),
            speaker_count=args.speakers,
        )
        elapsed = time.perf_counter() - started
        durations: defaultdict[int, float] = defaultdict(float)
        for turn in turns:
            durations[turn.speaker + 1] += turn.end - turn.start
        print(
            json.dumps(
                {
                    "file": str(path),
                    "seconds": round(elapsed, 3),
                    "speakers": len(durations),
                    "turns": len(turns),
                    "speech_seconds": {
                        str(speaker): round(duration, 2)
                        for speaker, duration in durations.items()
                    },
                },
                ensure_ascii=False,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
