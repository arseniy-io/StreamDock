from __future__ import annotations

import math
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Iterable

from app.services.gigaam_transcriber import GigaamSegment


LATIN_TERM_RE = re.compile(r"(?<![\w@])[A-Za-z][A-Za-z0-9.+#/_-]{1,31}(?!\w)")
LATIN_TOKEN_RE = re.compile(r"(?<!\w)[A-Za-z][A-Za-z0-9.+#-]{1,31}(?!\w)")
SUSPICIOUS_LATIN_RE = re.compile(
    r"\b(?:"
    r"gid|gethab|geathab|geatlab|betpaket|appensors|devebs|braing|"
    r"primery|faink|post\s*gress|post\s*cray|orical|derobility|"
    r"no\s*squelle|msq|tba|data\s*pace"
    r")\b",
    re.IGNORECASE,
)
NUMBER_RE = re.compile(r"\d+(?:[.,:]\d+)*")
REPEATED_WORD_RE = re.compile(r"\b([\w-]{2,})(?:\s+\1){4,}\b", re.IGNORECASE)

GLOSSARY_STOP_WORDS = {
    "academy", "and", "calm", "com", "course", "courses", "desktop", "http", "https",
    "keep", "lesson", "link", "medium", "online", "or", "org", "own", "site", "source",
    "telegram", "the", "this", "video", "watch", "www", "youtube", "youtu",
}
LATIN_SAFE_WORDS = {"excel", "id", "it", "relation", "youtube"}

SUSPICIOUS_TERM_RE = re.compile(
    r"\b(?:"
    r"гид|гит|гитхаб|гитлаб|гитаб|бит\s*пакет|битбакет|"
    r"докер|компоуз|ямл|йамл|джейсон|джисон|"
    r"эс\s*кью\s*эл|эскьюэль|сиквел|ди\s*эн\s*эс|днс|"
    r"ай\s*пи|эй\s*пи\s*ай|апи|постгрес|постгрэ|"
    r"джаваскрипт|реакт|энджинкс|асет|ацеме\s*сети"
    r")\b",
    re.IGNORECASE,
)

KNOWN_PRONUNCIATIONS: dict[str, tuple[str, ...]] = {
    "api": ("апи", "эйпиай", "эй пи ай"),
    "dns": ("днс", "диэнэс", "ди эн эс"),
    "docker": ("докер",),
    "github": ("гитхаб", "гитхуб", "гит хаб", "гитаб"),
    "gitlab": ("гитлаб", "гит лаб"),
    "git": ("гит", "гид"),
    "ip": ("айпи", "ай пи"),
    "javascript": ("джаваскрипт", "жаваскрипт"),
    "json": ("джейсон", "джисон", "джей сон"),
    "mysql": ("майэскьюэль", "май эс кью эл", "май скьюэль"),
    "nosql": ("ноускьюэль", "ноу скьюэль", "ноу сиквел"),
    "oracle": ("оракл", "орикал"),
    "postgresql": ("постгрес", "постгрэ", "постгрескьюэль"),
    "sql": ("эскьюэль", "эс кью эл", "сиквел"),
    "yaml": ("ямл", "йамл"),
}


@dataclass(frozen=True, slots=True)
class HybridCandidate:
    segment_index: int
    score: int
    reasons: tuple[str, ...]


def extract_glossary(source_info: dict, *, limit: int = 64) -> tuple[str, ...]:
    """Берёт только реально встречающиеся латинские термины из метаданных."""

    values: list[str] = []
    for key in ("title",):
        value = source_info.get(key)
        if isinstance(value, str):
            values.append(value[:8_000])
    for key in ("tags", "categories"):
        value = source_info.get(key)
        if isinstance(value, (list, tuple)):
            values.extend(str(item) for item in value[:100])
    description = source_info.get("description")
    if isinstance(description, str):
        values.append(description[:8_000])

    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        value = re.sub(r"https?://\S+|\b\S+\.ru/\S*|\b\S+\.com/\S*", " ", value)
        for match in LATIN_TERM_RE.finditer(value):
            term = match.group(0).strip("-_/.")
            normalized = term.casefold()
            if (
                len(term) < 2
                or normalized in GLOSSARY_STOP_WORDS
                or normalized.startswith(("http", "www"))
                or "_" in term
                or "/" in term
                or "." in term
                or normalized in seen
            ):
                continue
            seen.add(normalized)
            result.append(term)
            if len(result) >= limit:
                return tuple(result)
    return tuple(result)


def build_whisper_prompt(glossary: Iterable[str]) -> str | None:
    terms = ", ".join(dict.fromkeys(term.strip() for term in glossary if term.strip()))
    if not terms:
        return None
    return f"Технические термины и названия: {terms[:500]}"


def select_hybrid_candidates(
    segments: list[GigaamSegment],
    glossary: tuple[str, ...],
    *,
    max_duration_share: float = 0.18,
) -> list[HybridCandidate]:
    """Выбирает рискованные фрагменты, не отдавая Whisper весь ролик."""

    if not segments:
        return []

    confidence_values = sorted(
        segment.average_logprob
        for segment in segments
        if segment.average_logprob is not None and math.isfinite(segment.average_logprob)
    )
    low_confidence_threshold: float | None = None
    if confidence_values:
        low_confidence_threshold = confidence_values[max(0, len(confidence_values) // 4 - 1)]

    scored: list[HybridCandidate] = []
    for index, segment in enumerate(segments):
        reasons: list[str] = []
        score = 0
        compact_text = _compact(segment.text)

        glossary_hits = _glossary_hits(segment.text, glossary)
        unknown_latin = _unknown_latin_tokens(segment.text, glossary)
        if glossary_hits:
            reasons.append("glossary_term")
        if unknown_latin:
            reasons.append("unknown_latin")
            score += 2 if len(unknown_latin) >= 2 else 1
        if SUSPICIOUS_LATIN_RE.search(segment.text):
            reasons.append("suspicious_latin")
            score += 4
        if SUSPICIOUS_TERM_RE.search(segment.text):
            reasons.append("suspicious_term")
            score += 3
        if glossary and _matches_glossary_pronunciation(compact_text, glossary):
            reasons.append("glossary_pronunciation")
            score += 3
        if (
            low_confidence_threshold is not None
            and segment.average_logprob is not None
            and segment.average_logprob <= low_confidence_threshold
        ):
            reasons.append("low_confidence")
            score += 1
        if re.search(r"\b(?:команд|запрос|репозитор|ветк|ключ|сервер|транзакц|баз[а-я]* данных)\b", segment.text, re.I):
            reasons.append("technical_context")
            score += 1
        if NUMBER_RE.search(segment.text):
            reasons.append("numbers")
        duration = max(0.1, segment.end - segment.start)
        if len(segment.text.strip()) / duration < 1.1:
            reasons.append("sparse_text")
            score += 1

        if score >= 3:
            scored.append(HybridCandidate(index, score, tuple(reasons)))

    total_duration = max(segment.end for segment in segments) - min(segment.start for segment in segments)
    duration_budget = max(0.0, total_duration * max_duration_share)
    selected: list[HybridCandidate] = []
    selected_duration = 0.0
    for candidate in sorted(scored, key=lambda item: (-item.score, item.segment_index)):
        segment = segments[candidate.segment_index]
        segment_duration = max(0.0, segment.end - segment.start)
        if selected and selected_duration + segment_duration > duration_budget:
            continue
        selected.append(candidate)
        selected_duration += segment_duration

    return sorted(selected, key=lambda item: item.segment_index)


def should_accept_whisper_text(
    original: str,
    revised: str,
    glossary: tuple[str, ...],
    reasons: tuple[str, ...],
    whisper_average_logprob: float | None,
) -> bool:
    """Принимает замену только при явном улучшении технического текста."""

    original = original.strip()
    revised = revised.strip()
    if not original or not revised or _compact(original) == _compact(revised):
        return False
    if REPEATED_WORD_RE.search(revised):
        return False

    original_length = max(1, len(_compact(original)))
    length_ratio = len(_compact(revised)) / original_length
    if not 0.55 <= length_ratio <= 1.70:
        return False
    if whisper_average_logprob is not None and whisper_average_logprob < -1.0:
        return False

    original_numbers = NUMBER_RE.findall(original)
    revised_numbers = NUMBER_RE.findall(revised)
    if original_numbers and original_numbers != revised_numbers:
        return False

    original_glossary_hits = _glossary_hits(original, glossary)
    revised_glossary_hits = _glossary_hits(revised, glossary)
    if revised_glossary_hits > original_glossary_hits:
        return True

    original_suspicious = _suspicious_count(original)
    revised_suspicious = _suspicious_count(revised)
    if revised_suspicious < original_suspicious and revised_glossary_hits >= original_glossary_hits:
        return True

    if (
        len(_unknown_latin_tokens(revised, glossary)) < len(_unknown_latin_tokens(original, glossary))
        and revised_glossary_hits >= original_glossary_hits
    ):
        return True

    return False


def align_whisper_boundaries(original: str, revised: str) -> str:
    """Убирает выдуманные края Whisper и сохраняет края исходного фрагмента GigaAM."""

    original_words = _word_spans(original)
    revised_words = _word_spans(revised)
    if len(original_words) < 3 or len(revised_words) < 3:
        return revised.strip()

    matcher = SequenceMatcher(
        None,
        [item[0] for item in original_words],
        [item[0] for item in revised_words],
        autojunk=False,
    )
    reliable_blocks = [block for block in matcher.get_matching_blocks() if block.size >= 3]
    if not reliable_blocks:
        return revised.strip()

    first = reliable_blocks[0]
    last = reliable_blocks[-1]
    original_prefix = original[:original_words[first.a][1]].strip()
    revised_prefix = revised[:revised_words[first.b][1]].strip()
    original_suffix = original[original_words[last.a + last.size - 1][2]:].strip()
    revised_suffix = revised[revised_words[last.b + last.size - 1][2]:].strip()
    revised_core = revised[
        revised_words[first.b][1]:revised_words[last.b + last.size - 1][2]
    ].strip()

    prefix = _choose_boundary_text(original_prefix, revised_prefix)
    suffix = _choose_boundary_text(original_suffix, revised_suffix)
    parts = [part for part in (prefix, revised_core, suffix) if part]
    return " ".join(parts).strip()


def _glossary_hits(text: str, glossary: tuple[str, ...]) -> int:
    lowered = text.casefold()
    return sum(
        len(re.findall(rf"(?<!\w){re.escape(term.casefold())}(?!\w)", lowered))
        for term in glossary
    )


def _matches_glossary_pronunciation(compact_text: str, glossary: tuple[str, ...]) -> bool:
    for term in glossary:
        normalized_term = term.casefold().strip("-_/.")
        variants = KNOWN_PRONUNCIATIONS.get(normalized_term, ())
        if any(_compact(variant) in compact_text for variant in variants):
            return True
    return False


def _unknown_latin_tokens(text: str, glossary: tuple[str, ...]) -> tuple[str, ...]:
    known = {term.casefold().strip("-_/.") for term in glossary}
    return tuple(
        token
        for token in LATIN_TOKEN_RE.findall(text)
        if token.casefold().strip("-_/.") not in known
        and token.casefold() not in GLOSSARY_STOP_WORDS
        and token.casefold() not in LATIN_SAFE_WORDS
    )


def _suspicious_count(text: str) -> int:
    return len(SUSPICIOUS_TERM_RE.findall(text)) + len(SUSPICIOUS_LATIN_RE.findall(text))


def _compact(text: str) -> str:
    return re.sub(r"[^a-zа-яё0-9]+", "", text.casefold().replace("ё", "е"))


def _word_spans(text: str) -> list[tuple[str, int, int]]:
    return [
        (match.group(0).casefold().replace("ё", "е"), match.start(), match.end())
        for match in re.finditer(r"[A-Za-zА-Яа-яЁё0-9]+", text)
    ]


def _choose_boundary_text(original: str, revised: str) -> str:
    original_count = len(_word_spans(original))
    revised_count = len(_word_spans(revised))
    if original_count == 0:
        return original if revised_count == 0 else ""
    if revised_count == 0:
        return original
    if (
        revised_count >= max(1, math.floor(original_count * 0.55))
        and revised_count <= max(original_count + 3, math.ceil(original_count * 1.7))
    ):
        return revised
    return original
