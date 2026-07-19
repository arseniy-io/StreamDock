from __future__ import annotations

import argparse
import gc
import os
from pathlib import Path
import shutil
import sys


PROJECT_ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = PROJECT_ROOT / "models"
HUGGINGFACE_HOME = MODELS_DIR / "huggingface"
SPEAKER_MODELS_DIR = MODELS_DIR / "speaker-diarization"
WHISPER_MODELS = ("tiny", "base", "small", "medium", "large-v3")
WHISPER_ESTIMATED_GIB = {
    "tiny": 0.1,
    "base": 0.2,
    "small": 0.5,
    "medium": 1.6,
    "large-v3": 3.0,
}


def whisper_is_cached(model_name: str) -> bool:
    snapshots = MODELS_DIR / f"models--Systran--faster-whisper-{model_name}" / "snapshots"
    required_files = {"config.json", "model.bin", "tokenizer.json", "vocabulary.json"}
    return any(
        required_files <= {path.name for path in snapshot.iterdir() if path.is_file()}
        for snapshot in snapshots.glob("*")
        if snapshot.is_dir()
    )


def gigaam_is_cached() -> bool:
    repository_cache = HUGGINGFACE_HOME / "hub" / "models--istupakov--gigaam-v3-onnx"
    required_files = {
        "config.json",
        "v3_e2e_rnnt_decoder.int8.onnx",
        "v3_e2e_rnnt_encoder.int8.onnx",
        "v3_e2e_rnnt_joint.int8.onnx",
        "v3_e2e_rnnt_vocab.txt",
    }
    model_ready = any(
        required_files <= {path.name for path in snapshot.iterdir() if path.is_file()}
        for snapshot in repository_cache.glob("snapshots/*")
        if snapshot.is_dir()
    )
    vad_cache = HUGGINGFACE_HOME / "hub" / "models--istupakov--silero-vad-onnx"
    vad_ready = any(vad_cache.glob("snapshots/*/silero_vad.onnx"))
    return model_ready and vad_ready


def speaker_models_are_cached() -> bool:
    segmentation = (
        SPEAKER_MODELS_DIR
        / "sherpa-onnx-pyannote-segmentation-3-0"
        / "model.int8.onnx"
    )
    embedding = SPEAKER_MODELS_DIR / "nemo_en_titanet_small.onnx"
    return all(path.is_file() and path.stat().st_size >= 1024 for path in (segmentation, embedding))


def required_download_gib(
    whisper_models: list[str],
    include_gigaam: bool,
    include_speaker_models: bool = True,
) -> float:
    size = sum(WHISPER_ESTIMATED_GIB[name] for name in whisper_models if not whisper_is_cached(name))
    if include_gigaam and not gigaam_is_cached():
        size += 0.3
    if include_speaker_models and not speaker_models_are_cached():
        size += 0.05
    return size


def download_gigaam() -> None:
    import onnx_asr

    print("Загружаем GigaAM и детектор речи, около 300 МБ...")
    model = onnx_asr.load_model("gigaam-v3-e2e-rnnt", quantization="int8")
    vad = onnx_asr.load_vad("silero")
    del model, vad
    gc.collect()
    print("GigaAM готова.")


def download_whisper(model_name: str) -> None:
    from huggingface_hub import snapshot_download

    if whisper_is_cached(model_name):
        print(f"Whisper {model_name} уже загружена.")
        return
    print(f"Загружаем Whisper {model_name}...")
    snapshot_download(
        repo_id=f"Systran/faster-whisper-{model_name}",
        cache_dir=MODELS_DIR,
    )
    print(f"Whisper {model_name} готова.")


def download_speaker_models() -> None:
    from threading import Event

    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from app.services.speaker_diarizer import ensure_speaker_models

    last_percent = -1

    def report(_stage, progress, message, _details) -> None:
        nonlocal last_percent
        percent = int(progress or 0)
        if percent == 100 or percent >= last_percent + 10:
            print(f"Модели спикеров: {percent}% - {message}")
            last_percent = percent

    print("Загружаем модели разделения по спикерам, около 42 МБ...")
    ensure_speaker_models(report, Event(), models_directory=SPEAKER_MODELS_DIR)
    print("Модели разделения по спикерам готовы.")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Заранее загружает локальные модели StreamDock. Без аргументов готовит совместный режим."
    )
    parser.add_argument(
        "--whisper-model",
        action="append",
        choices=WHISPER_MODELS,
        dest="whisper_models",
        help="Модель Whisper для загрузки. Аргумент можно повторить.",
    )
    parser.add_argument("--skip-gigaam", action="store_true", help="Не загружать русскую модель GigaAM.")
    parser.add_argument(
        "--skip-speaker-models",
        action="store_true",
        help="Не загружать модели разделения по спикерам.",
    )
    parser.add_argument("--gigaam-only", action="store_true", help="Загрузить только GigaAM.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < (3, 11):
        print("Нужен Python 3.11 или новее.", file=sys.stderr)
        return 1

    args = parse_args(argv)
    include_gigaam = not args.skip_gigaam
    include_speaker_models = not args.skip_speaker_models and not args.gigaam_only
    whisper_models = [] if args.gigaam_only else list(dict.fromkeys(args.whisper_models or ["large-v3"]))

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    HUGGINGFACE_HOME.mkdir(parents=True, exist_ok=True)
    os.environ["HF_HOME"] = str(HUGGINGFACE_HOME)
    os.environ["HF_HUB_DISABLE_XET"] = "1"

    needed_gib = required_download_gib(whisper_models, include_gigaam, include_speaker_models)
    free_gib = shutil.disk_usage(MODELS_DIR).free / 1024**3
    if needed_gib and free_gib < needed_gib + 1.0:
        print(
            f"Недостаточно свободного места: нужно примерно {needed_gib + 1.0:.1f} ГБ, доступно {free_gib:.1f} ГБ.",
            file=sys.stderr,
        )
        return 1

    print("Модели сохраняются только в папке models этого проекта.")
    print("Загрузку можно повторить: уже готовые модели повторно не скачиваются.")
    try:
        if include_gigaam:
            if gigaam_is_cached():
                print("GigaAM уже загружена.")
            else:
                download_gigaam()
        for model_name in whisper_models:
            download_whisper(model_name)
        if include_speaker_models:
            if speaker_models_are_cached():
                print("Модели разделения по спикерам уже загружены.")
            else:
                download_speaker_models()
    except KeyboardInterrupt:
        print("Загрузка остановлена пользователем. Частичные файлы можно продолжить позже.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Не удалось загрузить модели: {exc}", file=sys.stderr)
        print("Проверьте интернет-соединение и свободное место, затем повторите запуск.", file=sys.stderr)
        return 1

    print("Все выбранные модели готовы к локальной работе.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
