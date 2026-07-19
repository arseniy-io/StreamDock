from threading import Event
from types import SimpleNamespace

import numpy as np
import pytest

from app.services import gigaam_transcriber
from app.services.downloader import DownloadCancelled


def test_gigaam_recognition_exposes_segments_confidence_and_progress(monkeypatch) -> None:
    class FakePipeline:
        def recognize(self, waveform, *, sample_rate):
            assert sample_rate == 16_000
            assert waveform.dtype == np.float32
            return iter(
                [
                    SimpleNamespace(start=0.0, end=5.0, text=" Первый фрагмент. ", logprobs=[-0.1, -0.3]),
                    SimpleNamespace(start=5.0, end=10.0, text="Второй фрагмент.", logprobs=[-0.2]),
                ]
            )

    monkeypatch.setattr(gigaam_transcriber, "_load_gigaam_pipeline", lambda: FakePipeline())
    progress = []
    segments = gigaam_transcriber.recognize_gigaam(
        np.zeros(160_000, dtype=np.float32),
        10.0,
        lambda stage, percent, message, details=None: progress.append((stage, percent, details)),
        Event(),
    )

    assert [item.text for item in segments] == ["Первый фрагмент.", "Второй фрагмент."]
    assert segments[0].average_logprob == pytest.approx(-0.2)
    assert segments[0].minimum_logprob == -0.3
    assert any(stage == "gigaam_transcribing" and percent == 100 for stage, percent, _ in progress)


def test_gigaam_recognition_can_be_cancelled_before_model_loading(monkeypatch) -> None:
    cancel_event = Event()
    cancel_event.set()
    monkeypatch.setattr(
        gigaam_transcriber,
        "_load_gigaam_pipeline",
        lambda: pytest.fail("Модель не должна загружаться после отмены"),
    )

    with pytest.raises(DownloadCancelled):
        gigaam_transcriber.recognize_gigaam(
            np.zeros(1, dtype=np.float32),
            0,
            lambda *args: None,
            cancel_event,
        )
