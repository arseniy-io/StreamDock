from pathlib import Path

import pytest

from app.services import desktop


def test_reveal_file_selects_exact_file_in_windows_explorer(monkeypatch, tmp_path: Path) -> None:
    media_file = tmp_path / "Большой эфир.mp4"
    media_file.write_bytes(b"video")
    launched: dict = {}

    def fake_popen(arguments, **options):
        launched["arguments"] = arguments
        launched["options"] = options
        return object()

    monkeypatch.setattr(desktop, "IS_WINDOWS", True)
    monkeypatch.setattr(desktop.subprocess, "Popen", fake_popen)

    status = desktop.reveal_file(media_file)

    assert status == "selected"
    assert launched["arguments"] == ["explorer.exe", "/select,", str(media_file.resolve())]
    assert launched["options"] == {"close_fds": True, "shell": False}


def test_reveal_file_falls_back_to_opening_parent_folder(monkeypatch, tmp_path: Path) -> None:
    media_file = tmp_path / "lesson.mp4"
    media_file.write_bytes(b"video")
    opened: list[str] = []

    def fail_to_launch(*_args, **_kwargs):
        raise OSError("Explorer command failed")

    monkeypatch.setattr(desktop, "IS_WINDOWS", True)
    monkeypatch.setattr(desktop.subprocess, "Popen", fail_to_launch)
    monkeypatch.setattr(desktop.os, "startfile", opened.append)

    status = desktop.reveal_file(media_file)

    assert status == "opened"
    assert opened == [str(tmp_path.resolve())]


def test_reveal_file_rejects_unsupported_platform(monkeypatch, tmp_path: Path) -> None:
    media_file = tmp_path / "lesson.mp4"
    media_file.write_bytes(b"video")
    monkeypatch.setattr(desktop, "IS_WINDOWS", False)

    with pytest.raises(desktop.DesktopRevealUnsupportedError):
        desktop.reveal_file(media_file)


def test_reveal_file_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(desktop.DesktopRevealError, match="больше недоступен"):
        desktop.reveal_file(tmp_path / "missing.mp4")
