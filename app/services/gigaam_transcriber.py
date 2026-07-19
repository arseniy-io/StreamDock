from __future__ import annotations

import math
import time
from dataclasses import dataclass
from functools import lru_cache
from threading import Event
from typing import Callable

import numpy as np

from app.config import (
    GIGAAM_BATCH_SIZE,
    GIGAAM_CPU_THREADS,
    GIGAAM_MAX_SEGMENT_SECONDS,
    GIGAAM_QUANTIZATION,
)
from app.services.downloader import DownloadCancelled


ProgressDetails = dict[str, float | int | str | None]
ProgressCallback = Callable[[str, float | None, str, ProgressDetails | None], None]
GIGAAM_MODEL_NAME = "gigaam-v3-e2e-rnnt"
GIGAAM_DISPLAY_NAME = "GigaAM v3 E2E RNNT"
SAMPLE_RATE = 16_000


class GigaamError(RuntimeError):
    """Понятная пользователю ошибка локального движка GigaAM."""


@dataclass(frozen=True, slots=True)
class GigaamSegment:
    start: float
    end: float
    text: str
    average_logprob: float | None = None
    minimum_logprob: float | None = None
    token_count: int = 0


def _raise_if_cancelled(cancel_event: Event) -> None:
    if cancel_event.is_set():
        raise DownloadCancelled("Операция отменена")


@lru_cache(maxsize=1)
def _load_gigaam_pipeline():
    try:
        import onnx_asr
        import onnxruntime as ort
    except ImportError as exc:
        raise GigaamError(
            "Компонент GigaAM не установлен. Запустите установку зависимостей из requirements.txt"
        ) from exc

    session_options = ort.SessionOptions()
    session_options.intra_op_num_threads = GIGAAM_CPU_THREADS
    session_options.inter_op_num_threads = 1
    session_options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    session_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

    try:
        model = onnx_asr.load_model(
            GIGAAM_MODEL_NAME,
            quantization=GIGAAM_QUANTIZATION,
            sess_options=session_options,
            providers=["CPUExecutionProvider"],
            preprocessor_config={
                "max_concurrent_workers": GIGAAM_BATCH_SIZE,
                "use_numpy_preprocessors": True,
            },
        )
        vad = onnx_asr.load_vad(
            "silero",
            sess_options=session_options,
            providers=["CPUExecutionProvider"],
        )
    except Exception as exc:
        message = str(exc).lower()
        if "download" in message or "connection" in message or "huggingface" in message:
            friendly = "Не удалось скачать модель GigaAM. Проверьте соединение и попробуйте ещё раз"
        elif "memory" in message or "allocate" in message:
            friendly = "Недостаточно оперативной памяти для запуска GigaAM"
        else:
            friendly = "Не удалось загрузить локальную модель GigaAM"
        raise GigaamError(friendly) from exc

    return model.with_vad(
        vad,
        batch_size=GIGAAM_BATCH_SIZE,
        min_speech_duration_ms=250,
        max_speech_duration_s=GIGAAM_MAX_SEGMENT_SECONDS,
        min_silence_duration_ms=300,
        speech_pad_ms=150,
    ).with_timestamps()


def recognize_gigaam(
    waveform: np.ndarray,
    duration: float,
    progress_callback: ProgressCallback,
    cancel_event: Event,
) -> list[GigaamSegment]:
    """Распознаёт длинную русскую запись локально, разбивая её по паузам."""

    _raise_if_cancelled(cancel_event)
    progress_callback(
        "gigaam_model",
        None,
        "Подготавливаем русскую модель GigaAM",
        None,
    )
    pipeline = _load_gigaam_pipeline()
    _raise_if_cancelled(cancel_event)

    progress_callback(
        "gigaam_transcribing",
        0,
        f"Распознаём русскую речь на процессоре ({GIGAAM_CPU_THREADS} потоков)",
        {"processed_seconds": 0, "total_seconds": duration or None},
    )
    started_at = time.monotonic()

    try:
        results = pipeline.recognize(waveform, sample_rate=SAMPLE_RATE)
        segments: list[GigaamSegment] = []
        for result in results:
            _raise_if_cancelled(cancel_event)
            text = str(result.text).strip()
            if not text:
                continue

            logprobs = [
                float(value)
                for value in (result.logprobs or ())
                if math.isfinite(float(value))
            ]
            average_logprob = math.fsum(logprobs) / len(logprobs) if logprobs else None
            minimum_logprob = min(logprobs) if logprobs else None
            end = max(float(result.start), float(result.end))
            segments.append(
                GigaamSegment(
                    start=max(0.0, float(result.start)),
                    end=end,
                    text=text,
                    average_logprob=average_logprob,
                    minimum_logprob=minimum_logprob,
                    token_count=len(logprobs),
                )
            )

            if duration > 0:
                ratio = min(1.0, end / duration)
                elapsed = time.monotonic() - started_at
                eta = elapsed * (1 - ratio) / ratio if ratio >= 0.01 else None
                progress_callback(
                    "gigaam_transcribing",
                    ratio * 100,
                    "Распознаём речь и собираем фрагменты",
                    {
                        "processed_seconds": min(end, duration),
                        "total_seconds": duration,
                        "eta_seconds": eta,
                    },
                )
    except DownloadCancelled:
        raise
    except GigaamError:
        raise
    except Exception as exc:
        message = str(exc).lower()
        if "memory" in message or "allocate" in message:
            friendly = "Недостаточно оперативной памяти для распознавания через GigaAM"
        elif "audio" in message or "wave" in message:
            friendly = "GigaAM не смог прочитать аудиодорожку этого медиафайла"
        else:
            friendly = "Не удалось распознать русскую речь через GigaAM"
        raise GigaamError(friendly) from exc

    return segments
