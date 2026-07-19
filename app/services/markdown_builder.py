from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class TranscriptSegment:
    start: float
    end: float
    text: str
    speaker: str | None = None


@dataclass(frozen=True)
class TranscriptBlock:
    start: float
    end: float
    text: str
    speaker: str | None = None


@dataclass(frozen=True)
class TranscriptMetadata:
    title: str
    source_url: str
    author: str | None
    duration_text: str
    language: str
    model: str
    created_at: datetime
    source_label: str = "Ссылка"


def format_timestamp(seconds: float, *, milliseconds: bool = False, decimal: str = ".") -> str:
    total_milliseconds = max(0, round(float(seconds) * 1000))
    hours, remainder = divmod(total_milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    base = f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{base}{decimal}{millis:03d}" if milliseconds else base


def clean_segments(
    segments: list[TranscriptSegment],
    *,
    remove_short_fragments: bool = True,
) -> list[TranscriptSegment]:
    cleaned: list[TranscriptSegment] = []
    for segment in segments:
        text = " ".join(segment.text.split()).strip()
        if not text:
            continue
        if remove_short_fragments and len(text) <= 2 and not any(character.isalnum() for character in text):
            continue
        cleaned.append(
            TranscriptSegment(
                max(0, segment.start),
                max(segment.start, segment.end),
                text,
                segment.speaker,
            )
        )
    return cleaned


def build_blocks(
    segments: list[TranscriptSegment],
    *,
    paragraphize: bool = True,
) -> list[TranscriptBlock]:
    if not segments:
        return []
    if not paragraphize:
        return [TranscriptBlock(item.start, item.end, item.text, item.speaker) for item in segments]

    blocks: list[TranscriptBlock] = []
    current: list[TranscriptSegment] = []
    current_length = 0

    def flush() -> None:
        nonlocal current, current_length
        if current:
            blocks.append(
                TranscriptBlock(
                    start=current[0].start,
                    end=current[-1].end,
                    text=" ".join(item.text for item in current),
                    speaker=current[0].speaker,
                )
            )
        current = []
        current_length = 0

    for segment in segments:
        previous = current[-1] if current else None
        next_length = current_length + len(segment.text) + (1 if current else 0)
        long_pause = previous is not None and segment.start - previous.end >= 2.5
        speaker_changed = previous is not None and segment.speaker != previous.speaker
        long_block = bool(current) and segment.end - current[0].start >= 50
        full_block = bool(current) and next_length > 650
        natural_break = (
            previous is not None
            and current_length >= 280
            and previous.text.endswith((".", "!", "?", "…"))
            and segment.start - previous.end >= 0.5
        )
        if speaker_changed or long_pause or long_block or full_block or natural_break:
            flush()
        current.append(segment)
        current_length += len(segment.text) + (1 if len(current) > 1 else 0)
    flush()
    return blocks


def build_markdown(
    metadata: TranscriptMetadata,
    blocks: list[TranscriptBlock],
    *,
    include_timestamps: bool = True,
) -> str:
    title = _single_line(metadata.title)
    author = _single_line(metadata.author or "Не указан")
    lines = [
        f"# {title}",
        "",
        f"- {_single_line(metadata.source_label)}: {_single_line(metadata.source_url)}",
        f"- Автор: {author}",
        f"- Продолжительность: {_single_line(metadata.duration_text)}",
        f"- Язык: {_single_line(metadata.language)}",
        f"- Модель распознавания: {_single_line(metadata.model)}",
        f"- Дата транскрибации: {metadata.created_at.astimezone().strftime('%d.%m.%Y %H:%M')}",
        "",
        "## Транскрибация",
        "",
    ]
    for block in blocks:
        if include_timestamps:
            lines.extend([f"### {format_timestamp(block.start)}", ""])
        speaker = f"**{_single_line(block.speaker)}:** " if block.speaker else ""
        lines.extend([f"{speaker}{block.text}", ""])
    return "\n".join(lines).rstrip() + "\n"


def build_text(blocks: list[TranscriptBlock], *, include_timestamps: bool = True) -> str:
    chunks = []
    for block in blocks:
        prefix = f"[{format_timestamp(block.start)}]\n" if include_timestamps else ""
        speaker = f"{_single_line(block.speaker)}: " if block.speaker else ""
        chunks.append(f"{prefix}{speaker}{block.text}")
    return "\n\n".join(chunks).rstrip() + "\n"


def build_srt(segments: list[TranscriptSegment]) -> str:
    entries = []
    for index, segment in enumerate(segments, start=1):
        start = format_timestamp(segment.start, milliseconds=True, decimal=",")
        end = format_timestamp(segment.end, milliseconds=True, decimal=",")
        speaker = f"{_single_line(segment.speaker)}: " if segment.speaker else ""
        entries.append(f"{index}\n{start} --> {end}\n{speaker}{segment.text}")
    return "\n\n".join(entries).rstrip() + "\n"


def build_vtt(segments: list[TranscriptSegment]) -> str:
    entries = ["WEBVTT"]
    for segment in segments:
        start = format_timestamp(segment.start, milliseconds=True)
        end = format_timestamp(segment.end, milliseconds=True)
        speaker = f"{_single_line(segment.speaker)}: " if segment.speaker else ""
        entries.append(f"{start} --> {end}\n{speaker}{segment.text}")
    return "\n\n".join(entries).rstrip() + "\n"


def _single_line(value: str) -> str:
    return " ".join(str(value).split())
