from __future__ import annotations

import argparse
import html
import json
import re
import tempfile
import time
import unicodedata
from pathlib import Path
from threading import Event

import yt_dlp
from faster_whisper.audio import decode_audio

from app.services.gigaam_transcriber import SAMPLE_RATE, recognize_gigaam
from app.services.hybrid_transcriber import extract_glossary, select_hybrid_candidates
from app.services.markdown_builder import TranscriptSegment
from app.services.transcriber import _recognize_with_whisper, _refine_with_whisper


CASES = {
    "git": {
        "id": "EeARyFrZsnU",
        "url": "https://www.youtube.com/watch?v=EeARyFrZsnU",
        "terms": ("Git", "GitHub", "GitLab", "commit", "branch", "merge", "rebase"),
    },
    "sql": {
        "id": "bv5UqdWm-5k",
        "url": "https://www.youtube.com/watch?v=bv5UqdWm-5k",
        "terms": ("SQL", "SELECT", "FROM", "WHERE", "JOIN", "MySQL", "PostgreSQL", "NoSQL"),
    },
    "json": {
        "id": "Wc_wd_pNxZM",
        "url": "https://www.youtube.com/watch?v=Wc_wd_pNxZM",
        "terms": ("JSON", "JavaScript", "XML", "ASCII"),
    },
    "dns": {
        "id": "t2NMbSarXC4",
        "url": "https://www.youtube.com/watch?v=t2NMbSarXC4",
        "terms": ("DNS", "IP", "domain", "nameserver", "resolver", "TLD", "ccTLD"),
    },
    "ordinary": {
        "id": "belwlTOS_cE",
        "url": "https://www.youtube.com/watch?v=belwlTOS_cE",
        "terms": (),
    },
}


def _noop_progress(stage, percent, message, details=None) -> None:
    if percent in {0, 100} or percent is None:
        print(f"  {stage}: {message}", flush=True)


def _ensure_case(case_name: str, cache_directory: Path) -> tuple[Path, Path, dict]:
    case = CASES[case_name]
    cache_directory.mkdir(parents=True, exist_ok=True)
    audio_path = cache_directory / f"{case['id']}.m4a"
    subtitle_path = cache_directory / f"{case['id']}.ru.vtt"

    options = {
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "outtmpl": str(cache_directory / "%(id)s.%(ext)s"),
        "writesubtitles": True,
        "subtitleslangs": ["ru"],
        "subtitlesformat": "vtt",
        "writeautomaticsub": False,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "overwrites": False,
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(case["url"], download=not (audio_path.is_file() and subtitle_path.is_file()))
    if not audio_path.is_file() or not subtitle_path.is_file():
        raise RuntimeError(f"Для теста {case_name} не найдены аудио или ручные русские субтитры")
    return audio_path, subtitle_path, info


def _read_vtt(path: Path) -> str:
    cue_lines: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if (
            not stripped
            or stripped == "WEBVTT"
            or stripped.startswith(("Kind:", "Language:", "NOTE"))
            or "-->" in stripped
            or stripped.isdigit()
        ):
            continue
        cleaned = html.unescape(re.sub(r"<[^>]+>", "", stripped)).strip()
        if cleaned:
            cue_lines.append(cleaned)
    return " ".join(cue_lines)


def _normalized_words(text: str) -> list[str]:
    text = unicodedata.normalize("NFKC", html.unescape(text)).casefold().replace("ё", "е")
    return re.findall(r"[a-zа-я0-9]+", text)


def _edit_distance(left: list[str] | str, right: list[str] | str) -> int:
    previous = list(range(len(right) + 1))
    for left_index, left_item in enumerate(left, start=1):
        current = [left_index]
        for right_index, right_item in enumerate(right, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[right_index] + 1,
                    previous[right_index - 1] + (left_item != right_item),
                )
            )
        previous = current
    return previous[-1]


def _metrics(reference: str, hypothesis: str, terms: tuple[str, ...]) -> dict:
    reference_words = _normalized_words(reference)
    hypothesis_words = _normalized_words(hypothesis)
    reference_chars = "".join(reference_words)
    hypothesis_chars = "".join(hypothesis_words)
    found_terms = [term for term in terms if term.casefold() in hypothesis.casefold()]
    return {
        "wer_percent": round(_edit_distance(reference_words, hypothesis_words) / max(1, len(reference_words)) * 100, 2),
        "cer_percent": round(_edit_distance(reference_chars, hypothesis_chars) / max(1, len(reference_chars)) * 100, 2),
        "reference_words": len(reference_words),
        "hypothesis_words": len(hypothesis_words),
        "terms_found": found_terms,
        "terms_found_count": len(found_terms),
        "terms_total": len(terms),
    }


def _text(segments: list[TranscriptSegment]) -> str:
    return " ".join(segment.text.strip() for segment in segments if segment.text.strip())


def benchmark_case(case_name: str, cache_directory: Path, include_whisper: bool) -> dict:
    audio_path, subtitle_path, info = _ensure_case(case_name, cache_directory)
    reference = _read_vtt(subtitle_path)
    waveform = decode_audio(str(audio_path), sampling_rate=SAMPLE_RATE)
    duration = len(waveform) / SAMPLE_RATE
    info["duration"] = duration
    terms = CASES[case_name]["terms"]
    cancel_event = Event()

    print(f"\n{case_name}: GigaAM", flush=True)
    started = time.perf_counter()
    gigaam_segments = recognize_gigaam(waveform, duration, _noop_progress, cancel_event)
    gigaam_seconds = time.perf_counter() - started
    gigaam_output = [TranscriptSegment(item.start, item.end, item.text) for item in gigaam_segments]
    glossary = extract_glossary(info)
    candidates = select_hybrid_candidates(gigaam_segments, glossary)
    candidate_seconds = sum(
        gigaam_segments[item.segment_index].end - gigaam_segments[item.segment_index].start
        for item in candidates
    )

    print(f"{case_name}: совместный режим", flush=True)
    started = time.perf_counter()
    hybrid_output = _refine_with_whisper(
        waveform,
        duration,
        gigaam_segments,
        info,
        _noop_progress,
        cancel_event,
    )
    hybrid_refine_seconds = time.perf_counter() - started
    replacements = sum(
        left.text.strip() != right.text.strip()
        for left, right in zip(gigaam_output, hybrid_output, strict=True)
    )

    result = {
        "case": case_name,
        "title": info.get("title"),
        "url": CASES[case_name]["url"],
        "duration_seconds": round(duration, 2),
        "glossary": list(glossary),
        "candidate_count": len(candidates),
        "candidate_audio_percent": round(candidate_seconds / max(1, duration) * 100, 2),
        "replacement_count": replacements,
        "gigaam": {
            "seconds": round(gigaam_seconds, 2),
            **_metrics(reference, _text(gigaam_output), terms),
        },
        "hybrid": {
            "seconds": round(gigaam_seconds + hybrid_refine_seconds, 2),
            **_metrics(reference, _text(hybrid_output), terms),
        },
    }

    if include_whisper:
        print(f"{case_name}: полный Whisper large-v3", flush=True)
        started = time.perf_counter()
        whisper_output, _, _ = _recognize_with_whisper(
            audio_path,
            info,
            _noop_progress,
            cancel_event,
            model_name="large-v3",
            language="ru",
        )
        result["whisper"] = {
            "seconds": round(time.perf_counter() - started, 2),
            **_metrics(reference, _text(whisper_output), terms),
        }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Сравнение GigaAM, совместного режима и Whisper")
    parser.add_argument("cases", nargs="+", choices=tuple(CASES))
    parser.add_argument("--with-whisper", action="store_true", help="Также запустить полный Whisper large-v3")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(tempfile.gettempdir()) / "streamdock-hybrid-benchmark.json",
    )
    args = parser.parse_args()
    cache_directory = Path(tempfile.gettempdir()) / "streamdock-hybrid-bench"
    results = [benchmark_case(case_name, cache_directory, args.with_whisper) for case_name in args.cases]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nРезультаты: {args.output}", flush=True)
    print(json.dumps(results, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
