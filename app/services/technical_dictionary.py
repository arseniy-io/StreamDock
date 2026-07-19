from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Literal, Mapping


DICTIONARY_PATH = Path(__file__).resolve().parents[1] / "data" / "technical_terms.json"

MAX_CUSTOM_TERMS = 64
MAX_CUSTOM_TERM_LENGTH = 64
MAX_CUSTOM_TOTAL_CHARS = 2_048
MAX_SELECTED_TERMS = 96

_CUSTOM_SEPARATOR_RE = re.compile(r"[\n,;]+")
_DISALLOWED_CUSTOM_CHARS_RE = re.compile(r"[^\w\s.+#/_-]", re.UNICODE)
_METADATA_TERM_RE = re.compile(r"(?<![\w@])[A-Za-z][A-Za-z0-9.+#/_-]{1,47}(?!\w)")
_SPACE_RE = re.compile(r"\s+")

_METADATA_STOP_WORDS = {
    "about", "academy", "and", "best", "com", "course", "courses", "example",
    "for", "from", "http", "https", "lesson", "link", "live", "official", "online",
    "org", "part", "site", "the", "this", "tutorial", "video", "watch", "with", "www",
    "youtube", "youtu",
}
_BLOCKED_CUSTOM_PHRASES = {
    "ignore previous", "ignore instructions", "previous instructions",
    "забудь инструкции", "игнорируй инструкции", "выполни команду",
}


@dataclass(frozen=True, slots=True)
class TechnicalTerm:
    value: str
    category: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TechnicalCategory:
    id: str
    title: str
    keywords: tuple[str, ...]
    defaults: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TechnicalDictionary:
    version: int
    categories: tuple[TechnicalCategory, ...]
    terms: tuple[TechnicalTerm, ...]

    @property
    def term_count(self) -> int:
        return len(self.terms)


TermSource = Literal["custom", "metadata", "builtin"]


@dataclass(frozen=True, slots=True)
class SelectedTechnicalTerm:
    value: str
    source: TermSource
    category: str | None = None
    score: int = 0


@lru_cache(maxsize=4)
def load_technical_dictionary(path: str | Path | None = None) -> TechnicalDictionary:
    """Загружает и проверяет встроенный словарь. Повторное чтение берётся из кеша."""

    source_path = Path(path) if path is not None else DICTIONARY_PATH
    try:
        payload = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Не удалось загрузить технический словарь: {source_path}") from error

    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("categories"), list)
        or not payload["categories"]
    ):
        raise RuntimeError("Технический словарь имеет неверную структуру.")

    version = payload.get("version")
    if not isinstance(version, int) or version < 1:
        raise RuntimeError("В техническом словаре не указана корректная версия.")

    categories: list[TechnicalCategory] = []
    terms: list[TechnicalTerm] = []
    seen_categories: set[str] = set()
    seen_terms: set[str] = set()

    for raw_category in payload["categories"]:
        if not isinstance(raw_category, dict):
            raise RuntimeError("Категория технического словаря имеет неверный формат.")
        category_id = _required_text(raw_category.get("id"), "id категории")
        title = _required_text(raw_category.get("title"), "название категории")
        if category_id in seen_categories:
            raise RuntimeError(f"Категория словаря повторяется: {category_id}")
        seen_categories.add(category_id)

        keywords = _text_tuple(raw_category.get("keywords", []), "ключевые слова категории")
        defaults = _text_tuple(raw_category.get("defaults", []), "базовые термины категории")
        raw_terms = raw_category.get("terms")
        if not isinstance(raw_terms, list) or not raw_terms:
            raise RuntimeError(f"В категории {category_id} отсутствуют термины.")

        category_values: set[str] = set()
        for raw_term in raw_terms:
            if isinstance(raw_term, str):
                value = _required_text(raw_term, "термин")
                aliases: tuple[str, ...] = ()
            elif isinstance(raw_term, dict):
                value = _required_text(raw_term.get("term"), "термин")
                aliases = _text_tuple(raw_term.get("aliases", []), "варианты термина")
            else:
                raise RuntimeError(f"Термин категории {category_id} имеет неверный формат.")

            normalized = _normalize(value)
            if normalized in seen_terms:
                raise RuntimeError(f"Термин словаря повторяется: {value}")
            seen_terms.add(normalized)
            category_values.add(normalized)
            terms.append(TechnicalTerm(value=value, category=category_id, aliases=aliases))

        missing_defaults = [item for item in defaults if _normalize(item) not in category_values]
        if missing_defaults:
            raise RuntimeError(
                f"Базовые термины категории {category_id} не найдены: {', '.join(missing_defaults)}"
            )
        categories.append(
            TechnicalCategory(
                id=category_id,
                title=title,
                keywords=keywords,
                defaults=defaults,
            )
        )

    return TechnicalDictionary(version=version, categories=tuple(categories), terms=tuple(terms))


def clean_custom_terms(
    values: Iterable[str] | str | None,
    *,
    max_terms: int = MAX_CUSTOM_TERMS,
    max_term_length: int = MAX_CUSTOM_TERM_LENGTH,
    max_total_chars: int = MAX_CUSTOM_TOTAL_CHARS,
) -> tuple[str, ...]:
    """Очищает пользовательские подсказки и применяет безопасные жёсткие лимиты."""

    if values is None:
        return ()
    if isinstance(values, str):
        candidates: Iterable[object] = _CUSTOM_SEPARATOR_RE.split(values)
    else:
        candidates = values

    safe_term_limit = max(0, min(int(max_terms), MAX_CUSTOM_TERMS))
    safe_length_limit = max(1, min(int(max_term_length), MAX_CUSTOM_TERM_LENGTH))
    safe_total_limit = max(0, min(int(max_total_chars), MAX_CUSTOM_TOTAL_CHARS))
    if safe_term_limit == 0 or safe_total_limit == 0:
        return ()

    result: list[str] = []
    seen: set[str] = set()
    total_chars = 0
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        if any(character in candidate for character in '<>{}`"\''):
            continue
        term = unicodedata.normalize("NFKC", candidate)
        term = "".join(char for char in term if not unicodedata.category(char).startswith("C"))
        term = _DISALLOWED_CUSTOM_CHARS_RE.sub(" ", term)
        term = _SPACE_RE.sub(" ", term).strip(" /_-")
        if term.endswith(".") and not term.startswith("."):
            term = term.rstrip(".")
        if not term or len(term) > safe_length_limit or len(term.split()) > 5:
            continue
        normalized = _normalize(term)
        if (
            not normalized
            or normalized in seen
            or "://" in candidate
            or "www." in candidate.casefold()
            or ".." in candidate
            or any(blocked in normalized for blocked in _BLOCKED_CUSTOM_PHRASES)
        ):
            continue
        if len(normalized) == 1 and normalized not in {"c", "r"}:
            continue
        if total_chars + len(term) > safe_total_limit:
            break
        seen.add(normalized)
        result.append(term)
        total_chars += len(term)
        if len(result) >= safe_term_limit:
            break
    return tuple(result)


def select_relevant_term_details(
    *,
    metadata: Mapping[str, object] | None = None,
    text: str = "",
    custom_terms: Iterable[str] | str | None = None,
    limit: int = 64,
    dictionary: TechnicalDictionary | None = None,
) -> tuple[SelectedTechnicalTerm, ...]:
    """Подбирает подсказки в порядке custom -> metadata -> builtin, не меняя транскрипцию."""

    safe_limit = max(1, min(int(limit), MAX_SELECTED_TERMS))
    technical_dictionary = dictionary or load_technical_dictionary()
    metadata_values = _metadata_strings(metadata or {})
    metadata_text = " ".join(metadata_values)
    normalized_metadata = _normalize(metadata_text)
    normalized_text = _normalize(text[:200_000])

    selected: list[SelectedTechnicalTerm] = []
    seen: set[str] = set()

    def add(item: SelectedTechnicalTerm) -> None:
        normalized = _normalize(item.value)
        if not normalized or normalized in seen or len(selected) >= safe_limit:
            return
        seen.add(normalized)
        selected.append(item)

    for value in clean_custom_terms(custom_terms):
        add(SelectedTechnicalTerm(value=value, source="custom", score=1_000))

    metadata_matches: list[tuple[int, TechnicalTerm]] = []
    text_matches: list[tuple[int, TechnicalTerm]] = []
    for term in technical_dictionary.terms:
        metadata_hits = _term_hits(normalized_metadata, term)
        if metadata_hits:
            metadata_matches.append((metadata_hits, term))
            continue
        text_hits = _term_hits(normalized_text, term)
        if text_hits:
            text_matches.append((text_hits, term))

    for hits, term in sorted(metadata_matches, key=lambda item: (-item[0], item[1].value.casefold())):
        add(
            SelectedTechnicalTerm(
                value=term.value,
                source="metadata",
                category=term.category,
                score=100 + hits,
            )
        )

    for value in _extract_metadata_candidates(metadata_values):
        add(SelectedTechnicalTerm(value=value, source="metadata", score=100))

    for hits, term in sorted(text_matches, key=lambda item: (-item[0], item[1].value.casefold())):
        add(
            SelectedTechnicalTerm(
                value=term.value,
                source="builtin",
                category=term.category,
                score=hits,
            )
        )

    combined_context = f"{normalized_metadata} {normalized_text}".strip()
    terms_by_category: dict[str, dict[str, TechnicalTerm]] = {}
    for term in technical_dictionary.terms:
        terms_by_category.setdefault(term.category, {})[_normalize(term.value)] = term

    category_scores: list[tuple[int, TechnicalCategory]] = []
    for category in technical_dictionary.categories:
        score = sum(_phrase_count(combined_context, _normalize(keyword)) for keyword in category.keywords)
        if score:
            category_scores.append((score, category))
    for score, category in sorted(category_scores, key=lambda item: (-item[0], item[1].id)):
        category_terms = terms_by_category[category.id]
        for default in category.defaults:
            term = category_terms[_normalize(default)]
            add(
                SelectedTechnicalTerm(
                    value=term.value,
                    source="builtin",
                    category=term.category,
                    score=score,
                )
            )

    return tuple(selected)


def select_relevant_terms(
    *,
    metadata: Mapping[str, object] | None = None,
    text: str = "",
    custom_terms: Iterable[str] | str | None = None,
    limit: int = 64,
    dictionary: TechnicalDictionary | None = None,
) -> tuple[str, ...]:
    """Короткий API: возвращает только строки, готовые для prompt/hotwords."""

    return tuple(
        item.value
        for item in select_relevant_term_details(
            metadata=metadata,
            text=text,
            custom_terms=custom_terms,
            limit=limit,
            dictionary=dictionary,
        )
    )


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"В техническом словаре не заполнено поле: {field_name}.")
    return value.strip()


def _text_tuple(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise RuntimeError(f"Поле «{field_name}» должно быть списком.")
    return tuple(_required_text(item, field_name) for item in value)


def _metadata_strings(metadata: Mapping[str, object]) -> list[str]:
    result: list[str] = []
    for key in ("title", "author", "uploader", "channel", "description"):
        value = metadata.get(key)
        if isinstance(value, str):
            result.append(value[:8_000])
    for key in ("tags", "categories"):
        value = metadata.get(key)
        if isinstance(value, (list, tuple)):
            result.extend(item[:256] for item in value[:100] if isinstance(item, str))
    return result


def _extract_metadata_candidates(values: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        without_urls = re.sub(r"https?://\S+|\bwww\.\S+", " ", value)
        for match in _METADATA_TERM_RE.finditer(without_urls):
            term = match.group(0).strip("./_-")
            normalized = _normalize(term)
            has_internal_uppercase = any(char.isupper() for char in term[1:])
            looks_technical = (
                term.isupper()
                or has_internal_uppercase
                or any(char.isdigit() or char in ".+#/_-" for char in term)
            )
            if (
                len(term) < 2
                or normalized in _METADATA_STOP_WORDS
                or normalized in seen
                or not looks_technical
            ):
                continue
            seen.add(normalized)
            result.append(term)
    return tuple(result)


def _term_hits(normalized_text: str, term: TechnicalTerm) -> int:
    variants = (term.value, *term.aliases)
    return max((_phrase_count(normalized_text, _normalize(variant)) for variant in variants), default=0)


def _phrase_count(normalized_text: str, normalized_phrase: str) -> int:
    if not normalized_text or not normalized_phrase:
        return 0
    return f" {normalized_text} ".count(f" {normalized_phrase} ")


def _normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    value = value.replace("+", " plus ").replace("#", " sharp ")
    value = re.sub(r"[^\w]+", " ", value, flags=re.UNICODE)
    return _SPACE_RE.sub(" ", value).strip()
