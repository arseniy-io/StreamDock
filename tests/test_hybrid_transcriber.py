from app.services.gigaam_transcriber import GigaamSegment
from app.services.hybrid_transcriber import (
    align_whisper_boundaries,
    extract_glossary,
    select_hybrid_candidates,
    should_accept_whisper_text,
)


def test_extract_glossary_prioritizes_metadata_and_removes_tracking_links() -> None:
    glossary = extract_glossary(
        {
            "title": "Git и GitHub для новичков",
            "tags": ["GitLab", "commit", "branch"],
            "description": "Курс: https://example.com/course?utm_source=YT. Работа с Bitbucket.",
        }
    )

    assert glossary[:5] == ("Git", "GitHub", "GitLab", "commit", "branch")
    assert "Bitbucket" in glossary
    assert not any("utm" in term.casefold() or "example" in term.casefold() for term in glossary)


def test_candidate_selection_uses_risky_terms_and_limits_review_share() -> None:
    segments = [
        GigaamSegment(index * 10, (index + 1) * 10, "Обычная русская речь.", -0.05)
        for index in range(10)
    ]
    segments[2] = GigaamSegment(20, 30, "Создаём репозиторий командой gid and nead.", -0.12)
    segments[5] = GigaamSegment(50, 60, "Открываем Gethab и делаем gid Comit.", -0.15)
    segments[8] = GigaamSegment(80, 90, "Ещё один Gethab с командой gid at.", -0.2)

    selected = select_hybrid_candidates(segments, ("Git", "GitHub", "commit"))
    selected_duration = sum(
        segments[item.segment_index].end - segments[item.segment_index].start
        for item in selected
    )

    assert selected
    assert all("suspicious_latin" in item.reasons for item in selected)
    assert selected_duration <= 20


def test_boundary_alignment_keeps_original_edges_and_removes_hallucinated_edges() -> None:
    original = (
        "Ну ты не пугайся, в программах будут такие же названия. "
        "Используем команду gid and nead в папке с кодом."
    )
    revised = (
        "В программах будут такие же названия. "
        "Используем команду git init в папке с кодом. Отлично."
    )

    aligned = align_whisper_boundaries(original, revised)

    assert aligned.startswith("Ну ты не пугайся")
    assert "git init" in aligned
    assert not aligned.endswith("Отлично.")


def test_boundary_alignment_keeps_whisper_correction_at_segment_end() -> None:
    original = (
        "Теперь нужно зафиксировать наши изменения. "
        "Делаем это при помощи команды gid Comit."
    )
    revised = (
        "Теперь нужно зафиксировать наши изменения. "
        "Делаем это при помощи команды git commit."
    )

    aligned = align_whisper_boundaries(original, revised)

    assert aligned.endswith("git commit.")


def test_boundary_alignment_restores_long_original_tail_when_whisper_is_cut_off() -> None:
    original = (
        "Нотация JavaScript очень популярна и действительно помогает. "
        "Вы можете удалять пустое пространство и относительно просто парсить данные."
    )
    revised = "Нотация JavaScript очень популярна и действительно помогает. Вы можете удалять пусто"

    aligned = align_whisper_boundaries(original, revised)

    assert aligned.endswith("относительно просто парсить данные.")


def test_replacement_requires_clear_improvement_and_preserves_numbers() -> None:
    glossary = ("Git", "GitHub")
    reasons = ("suspicious_latin", "suspicious_term")

    assert should_accept_whisper_text(
        "Открой Gethab и выполни gid Comit.",
        "Открой GitHub и выполни git commit.",
        glossary,
        reasons,
        -0.1,
    )
    assert not should_accept_whisper_text(
        "Версия 3 работает стабильно.",
        "Версия 2 работает стабильно.",
        glossary,
        reasons,
        -0.1,
    )
    assert not should_accept_whisper_text(
        "Значениями могут быть другие JavaScript вещи.",
        "Значениями могут быть другие JavaScript объекты.",
        glossary,
        ("low_confidence", "technical_context"),
        -0.1,
    )
