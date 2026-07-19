from datetime import datetime, timezone

from app.services.markdown_builder import (
    TranscriptMetadata,
    TranscriptSegment,
    build_blocks,
    build_markdown,
    build_srt,
    build_text,
    build_vtt,
    clean_segments,
    format_timestamp,
)


def test_format_timestamp_supports_subtitle_milliseconds() -> None:
    assert format_timestamp(95.25) == "00:01:35"
    assert format_timestamp(95.25, milliseconds=True, decimal=",") == "00:01:35,250"


def test_markdown_contains_metadata_and_readable_blocks() -> None:
    segments = clean_segments(
        [
            TranscriptSegment(0, 4, "Первый короткий фрагмент."),
            TranscriptSegment(4.2, 8, "Он продолжается в том же абзаце."),
            TranscriptSegment(12, 15, "После паузы начинается новый блок."),
        ]
    )
    blocks = build_blocks(segments)
    metadata = TranscriptMetadata(
        title="Тестовое видео",
        source_url="https://youtu.be/abcdefghijk",
        author="Тестовый канал",
        duration_text="01:35",
        language="ru",
        model="small",
        created_at=datetime(2026, 7, 16, 12, 30, tzinfo=timezone.utc),
    )

    markdown = build_markdown(metadata, blocks)

    assert markdown.startswith("# Тестовое видео")
    assert "- Модель распознавания: small" in markdown
    assert "## Транскрибация" in markdown
    assert "### 00:00:00" in markdown
    assert "### 00:00:12" in markdown
    assert len(blocks) == 2


def test_subtitle_builders_use_expected_headers_and_timestamps() -> None:
    segments = [TranscriptSegment(1.5, 3.75, "Проверка субтитров")]
    assert "00:00:01,500 --> 00:00:03,750" in build_srt(segments)
    assert build_vtt(segments).startswith("WEBVTT\n\n00:00:01.500 --> 00:00:03.750")


def test_markdown_can_describe_local_file_source() -> None:
    metadata = TranscriptMetadata(
        title="Локальная запись",
        source_url="lesson.mp4",
        author=None,
        duration_text="10:00",
        language="ru",
        model="large-v3",
        created_at=datetime(2026, 7, 18, 12, 30, tzinfo=timezone.utc),
        source_label="Файл",
    )

    markdown = build_markdown(metadata, [])

    assert "- Файл: lesson.mp4" in markdown


def test_speaker_change_starts_new_block_and_is_written_to_all_formats() -> None:
    segments = clean_segments(
        [
            TranscriptSegment(0, 2, "Первый вопрос.", "Спикер 1"),
            TranscriptSegment(2, 4, "Продолжение вопроса.", "Спикер 1"),
            TranscriptSegment(4, 7, "Ответ собеседника.", "Спикер 2"),
        ]
    )
    blocks = build_blocks(segments)
    metadata = TranscriptMetadata(
        title="Интервью",
        source_url="https://example.com/interview",
        author="Канал",
        duration_text="00:07",
        language="ru",
        model="Тестовая модель",
        created_at=datetime(2026, 7, 19, tzinfo=timezone.utc),
    )

    assert len(blocks) == 2
    assert blocks[0].speaker == "Спикер 1"
    assert blocks[1].speaker == "Спикер 2"
    assert "**Спикер 1:** Первый вопрос. Продолжение вопроса." in build_markdown(metadata, blocks)
    assert "Спикер 2: Ответ собеседника." in build_text(blocks)
    assert "Спикер 1: Первый вопрос." in build_srt(segments)
    assert "Спикер 2: Ответ собеседника." in build_vtt(segments)
