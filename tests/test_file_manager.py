import pytest

from app.services.file_manager import sanitize_filename, unique_output_stem


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('Видео: обзор / тест?*', "Видео_ обзор _ тест__"),
        ("  нормальное   имя.  ", "нормальное имя"),
        ("CON", "_CON"),
        ("<>:\"/\\|?*", "_________"),
    ],
)
def test_sanitize_filename(source: str, expected: str) -> None:
    assert sanitize_filename(source) == expected


def test_sanitize_filename_uses_fallback_for_empty_value() -> None:
    assert sanitize_filename("...", fallback="файл") == "файл"


def test_sanitize_filename_limits_length() -> None:
    assert len(sanitize_filename("а" * 300)) == 180


def test_unique_output_stem_does_not_overwrite_existing_file(tmp_path) -> None:
    (tmp_path / "Видео.mp4").write_bytes(b"existing")
    assert unique_output_stem(tmp_path, "Видео").name == "Видео (2)"
