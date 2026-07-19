from __future__ import annotations

from io import BytesIO
from pathlib import Path
import tarfile
from threading import Event
from types import SimpleNamespace
import wave

import numpy as np
import pytest

from app.services.downloader import DownloadCancelled
from app.services.markdown_builder import TranscriptSegment
from app.services import speaker_diarizer as service
from app.services.speaker_diarizer import (
    SpeakerAssignment,
    SpeakerDiarizationError,
    SpeakerModelPaths,
    SpeakerTurn,
    assign_speakers_to_segments,
    diarize_speakers,
    ensure_speaker_models,
    validate_speaker_count,
)


def _progress_collector():
    events: list[tuple[str, float | None, str, dict | None]] = []

    def callback(stage, progress, message, details=None):
        events.append((stage, progress, message, details))

    return events, callback


def _write_model_archive(
    path: Path,
    *,
    member_name: str | None = None,
    data: bytes | None = None,
    symlink: bool = False,
) -> None:
    name = member_name or (
        f"{service.SEGMENTATION_DIRECTORY_NAME}/{service.SEGMENTATION_MODEL_NAME}"
    )
    payload = data if data is not None else b"model" * 400
    with tarfile.open(path, "w:bz2") as archive:
        member = tarfile.TarInfo(name)
        if symlink:
            member.type = tarfile.SYMTYPE
            member.linkname = "../outside"
            archive.addfile(member)
        else:
            member.size = len(payload)
            archive.addfile(member, BytesIO(payload))


def _write_wav(path: Path, samples: np.ndarray | None = None) -> None:
    values = samples if samples is not None else np.array([0, 1000, -1000], dtype=np.int16)
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(service.SAMPLE_RATE)
        wav_file.writeframes(values.astype("<i2").tobytes())


def test_validate_speaker_count() -> None:
    assert validate_speaker_count(None) is None
    assert validate_speaker_count(2) == 2
    assert validate_speaker_count(10) == 10
    for invalid in (True, 0, 1, 11):
        with pytest.raises(ValueError, match="от 2 до 10"):
            validate_speaker_count(invalid)


def test_speaker_labels_are_human_readable() -> None:
    segment = TranscriptSegment(0, 1, "Текст")
    assert SpeakerTurn(0, 1, 0).label == "Спикер 1"
    assert SpeakerAssignment(segment, 2).label == "Спикер 3"
    assert SpeakerAssignment(segment, None).label is None


def test_assign_speakers_uses_total_overlap_and_earlier_turn_for_tie() -> None:
    segments = [
        TranscriptSegment(0, 10, "Первый"),
        TranscriptSegment(10, 20, "Второй"),
        TranscriptSegment(30, 31, "Без спикера"),
    ]
    turns = [
        SpeakerTurn(0, 3, 0),
        SpeakerTurn(3, 7, 1),
        SpeakerTurn(7, 9, 0),
        SpeakerTurn(10, 15, 2),
        SpeakerTurn(15, 20, 1),
    ]

    assignments = assign_speakers_to_segments(segments, turns)

    assert [item.speaker for item in assignments] == [0, 2, None]
    assert [item.segment for item in assignments] == segments


def test_assign_speakers_ignores_invalid_turns() -> None:
    segment = TranscriptSegment(0, 5, "Текст")
    turns = [SpeakerTurn(3, 2, 0), SpeakerTurn(0, 5, -1)]
    assert assign_speakers_to_segments([segment], turns)[0].speaker is None


def test_assign_speakers_uses_nearest_turn_within_one_second() -> None:
    segment = TranscriptSegment(5.4, 5.8, "Короткая пауза")
    assignments = assign_speakers_to_segments(
        [segment],
        [SpeakerTurn(3.0, 5.0, 2), SpeakerTurn(7.0, 8.0, 1)],
    )

    assert assignments[0].speaker == 2


def test_normalize_turns_numbers_speakers_by_first_appearance() -> None:
    raw = [
        SimpleNamespace(start=4.0, end=6.0, speaker=3),
        SimpleNamespace(start=0.0, end=2.0, speaker=8),
        SimpleNamespace(start=2.0, end=3.0, speaker=3),
    ]

    assert service._normalize_turns(raw) == [
        SpeakerTurn(0.0, 2.0, 0),
        SpeakerTurn(2.0, 3.0, 1),
        SpeakerTurn(4.0, 6.0, 1),
    ]


def test_auto_cleanup_merges_only_tiny_false_speaker_clusters() -> None:
    turns = [
        SpeakerTurn(0.0, 40.0, 0),
        SpeakerTurn(40.0, 42.0, 4),
        SpeakerTurn(42.0, 82.0, 2),
        SpeakerTurn(82.0, 82.4, 5),
    ]

    cleaned = service._collapse_tiny_speaker_clusters(turns)

    assert {turn.speaker for turn in cleaned} == {0, 1}
    assert cleaned[1].speaker == 0
    assert cleaned[3].speaker == 1


def test_extract_segmentation_archive_safely_and_atomically(tmp_path: Path) -> None:
    archive = tmp_path / "model.tar.bz2"
    destination = tmp_path / service.SEGMENTATION_DIRECTORY_NAME
    _write_model_archive(archive)

    service._extract_segmentation_model_atomic(archive, destination, Event())

    model = destination / service.SEGMENTATION_MODEL_NAME
    assert model.is_file()
    assert model.stat().st_size >= service._MIN_MODEL_BYTES
    assert archive.is_file()


@pytest.mark.parametrize(
    ("member_name", "symlink"),
    [("../outside.onnx", False), ("unsafe-link", True), ("C:/outside.onnx", False)],
)
def test_extract_rejects_path_traversal_and_links(
    tmp_path: Path, member_name: str, symlink: bool
) -> None:
    archive = tmp_path / "model.tar.bz2"
    destination = tmp_path / service.SEGMENTATION_DIRECTORY_NAME
    _write_model_archive(archive, member_name=member_name, symlink=symlink)

    with pytest.raises(SpeakerDiarizationError, match="небезопас"):
        service._extract_segmentation_model_atomic(archive, destination, Event())

    assert not (tmp_path.parent / "outside.onnx").exists()
    assert not archive.exists()


def test_download_file_is_atomic_and_reports_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"x" * 4096

    class FakeResponse(BytesIO):
        headers = {"Content-Length": str(len(payload))}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    monkeypatch.setattr(service, "urlopen", lambda *_args, **_kwargs: FakeResponse(payload))
    events, callback = _progress_collector()
    destination = tmp_path / "model.onnx"

    service._download_file_atomic(
        "https://example.test/model.onnx",
        destination,
        callback,
        Event(),
        progress_start=20,
        progress_span=50,
        message="Загрузка",
        minimum_size=1024,
    )

    assert destination.read_bytes() == payload
    assert not list(tmp_path.glob("*.part"))
    assert events[-1][1] == 70
    assert events[-1][3] == {
        "downloaded_bytes": len(payload),
        "total_bytes": len(payload),
    }


def test_download_cancellation_leaves_no_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = False

    def fail_urlopen(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("Сеть не должна вызываться после отмены")

    monkeypatch.setattr(service, "urlopen", fail_urlopen)
    cancel_event = Event()
    cancel_event.set()
    destination = tmp_path / "model.onnx"

    with pytest.raises(DownloadCancelled):
        service._download_file_atomic(
            "https://example.test/model.onnx",
            destination,
            lambda *_args: None,
            cancel_event,
            progress_start=0,
            progress_span=100,
            message="Загрузка",
            minimum_size=1,
        )

    assert called is False
    assert not destination.exists()
    assert not list(tmp_path.glob("*.part"))


def test_ensure_models_uses_local_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = service._speaker_model_paths(tmp_path)
    paths.segmentation.parent.mkdir(parents=True)
    paths.segmentation.write_bytes(b"s" * 2048)
    paths.embedding.write_bytes(b"e" * 2048)
    monkeypatch.setattr(
        service,
        "_download_file_atomic",
        lambda *_args, **_kwargs: pytest.fail("Кэш не должен скачиваться заново"),
    )
    events, callback = _progress_collector()

    result = ensure_speaker_models(callback, Event(), models_directory=tmp_path)

    assert result == paths
    assert events[-1][0:3] == (
        "speaker_models",
        100,
        "Модель разделения голосов готова",
    )


def test_prepare_wav_uses_safe_ffmpeg_argument_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "input video.mp4"
    source.write_bytes(b"media")
    destination = tmp_path / "audio.wav"
    calls: list[tuple[list[str], dict]] = []

    class FakeProcess:
        returncode = 0

        def __init__(self, arguments, **kwargs):
            calls.append((arguments, kwargs))
            Path(arguments[-1]).write_bytes(b"wav")

        def poll(self):
            return 0

        def communicate(self):
            return "", ""

    monkeypatch.setattr(service, "_find_ffmpeg_executable", lambda: Path("ffmpeg.exe"))
    monkeypatch.setattr(service.subprocess, "Popen", FakeProcess)

    service._prepare_diarization_wav(
        source, destination, lambda *_args: None, Event()
    )

    arguments, kwargs = calls[0]
    assert kwargs["shell"] is False
    assert arguments[0] == "ffmpeg.exe"
    assert arguments[arguments.index("-ac") + 1] == "1"
    assert arguments[arguments.index("-ar") + 1] == "16000"
    assert arguments[arguments.index("-i") + 1] == str(source)


def test_read_pcm_wav_returns_float32_samples(tmp_path: Path) -> None:
    path = tmp_path / "audio.wav"
    _write_wav(path, np.array([-32768, 0, 16384], dtype=np.int16))

    samples = service._read_pcm_wav(path)

    assert samples.dtype == np.float32
    assert samples.tolist() == pytest.approx([-1.0, 0.0, 0.5])


def test_diarize_speakers_uses_sherpa_callback_and_normalizes_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "video.mp4"
    source.write_bytes(b"media")
    dummy_paths = SpeakerModelPaths(tmp_path / "seg.onnx", tmp_path / "emb.onnx")
    callback_result: list[int] = []

    class FakeResult:
        def sort_by_start_time(self):
            return [
                SimpleNamespace(start=5.0, end=8.0, speaker=1),
                SimpleNamespace(start=-1.0, end=2.0, speaker=0),
                SimpleNamespace(start=4.0, end=4.0, speaker=2),
            ]

    class FakeDiarizer:
        def process(self, samples, callback):
            assert samples.dtype == np.float32
            callback_result.append(callback(1, 4))
            return FakeResult()

    monkeypatch.setattr(
        service,
        "ensure_speaker_models",
        lambda *_args, **_kwargs: dummy_paths,
    )

    def prepare(_source, destination, *_args):
        _write_wav(destination)

    monkeypatch.setattr(service, "_prepare_diarization_wav", prepare)
    monkeypatch.setattr(service, "_create_diarizer", lambda *_args: FakeDiarizer())
    events, callback = _progress_collector()

    turns = diarize_speakers(
        source, callback, Event(), speaker_count=2
    )

    assert callback_result == [0]
    assert turns == [SpeakerTurn(0.0, 2.0, 0), SpeakerTurn(5.0, 8.0, 1)]
    assert any(event[0] == "speaker_diarization" and event[1] == 25 for event in events)
    assert events[-1][1] == 100
    assert events[-1][2] == "Найдено спикеров: 2"


def test_diarize_speakers_honors_cancellation_from_sherpa_callback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "video.mp4"
    source.write_bytes(b"media")
    cancel_event = Event()
    dummy_paths = SpeakerModelPaths(tmp_path / "seg.onnx", tmp_path / "emb.onnx")

    class FakeDiarizer:
        def process(self, _samples, callback):
            cancel_event.set()
            assert callback(1, 2) == 1
            return SimpleNamespace(sort_by_start_time=lambda: [])

    monkeypatch.setattr(
        service,
        "ensure_speaker_models",
        lambda *_args, **_kwargs: dummy_paths,
    )
    monkeypatch.setattr(
        service,
        "_prepare_diarization_wav",
        lambda _source, destination, *_args: _write_wav(destination),
    )
    monkeypatch.setattr(service, "_create_diarizer", lambda *_args: FakeDiarizer())

    with pytest.raises(DownloadCancelled):
        diarize_speakers(source, lambda *_args: None, cancel_event)


def test_diarize_reports_friendly_memory_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "video.mp4"
    source.write_bytes(b"media")
    paths = SpeakerModelPaths(tmp_path / "seg.onnx", tmp_path / "emb.onnx")
    monkeypatch.setattr(service, "ensure_speaker_models", lambda *_args, **_kwargs: paths)
    monkeypatch.setattr(
        service,
        "_prepare_diarization_wav",
        lambda _source, destination, *_args: _write_wav(destination),
    )
    monkeypatch.setattr(
        service,
        "_create_diarizer",
        lambda *_args: (_ for _ in ()).throw(MemoryError("allocate failed")),
    )

    with pytest.raises(SpeakerDiarizationError, match="оперативной памяти"):
        diarize_speakers(source, lambda *_args: None, Event())
