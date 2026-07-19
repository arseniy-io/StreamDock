from __future__ import annotations

import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, RLock, Thread
from uuid import uuid4

from app.config import DOWNLOADS_DIR
from app.services.downloader import (
    DownloadCancelled,
    VideoAnalysisError,
    download_audio,
    download_video,
)
from app.services.extension_downloader import download_extension_stream
from app.services.transcriber import TranscriptionError, transcribe_local_file, transcribe_video


logger = logging.getLogger(__name__)


def _completed_cleanup_event() -> Event:
    event = Event()
    event.set()
    return event


@dataclass
class TaskRecord:
    task_id: str
    status: str = "pending"
    stage: str = "preparing"
    progress: float | None = 0
    message: str = "Задача создана"
    error: str | None = None
    processed_seconds: float | None = None
    total_seconds: float | None = None
    eta_seconds: float | None = None
    downloaded_bytes: int | None = None
    total_bytes: int | None = None
    speed_bytes_per_second: float | None = None
    files: list[Path] = field(default_factory=list)
    item_errors: list[dict[str, str]] = field(default_factory=list)
    cleanup_paths: list[Path] = field(default_factory=list, repr=False)
    cleanup_complete: Event = field(default_factory=_completed_cleanup_event, repr=False)
    cancel_event: Event = field(default_factory=Event, repr=False)


class TaskManager:
    def __init__(self, output_directory: Path = DOWNLOADS_DIR) -> None:
        self.output_directory = output_directory.resolve()
        self._tasks: dict[str, TaskRecord] = {}
        self._extension_request_tasks: dict[str, str] = {}
        self._lock = RLock()

    def create_video_download(
        self,
        url: str,
        height: int,
        *,
        start_seconds: float | None = None,
        end_seconds: float | None = None,
    ) -> TaskRecord:
        task = TaskRecord(task_id=str(uuid4()))
        with self._lock:
            self._tasks[task.task_id] = task
        thread = Thread(
            target=self._run_video_download,
            args=(task.task_id, url, height, start_seconds, end_seconds),
            daemon=True,
            name=f"download-{task.task_id[:8]}",
        )
        thread.start()
        return task

    def create_audio_download(
        self,
        url: str,
        *,
        output_format: str,
        bitrate_kbps: int,
        start_seconds: float | None = None,
        end_seconds: float | None = None,
    ) -> TaskRecord:
        task = TaskRecord(task_id=str(uuid4()), message="Загрузка аудио добавлена в очередь")
        with self._lock:
            self._tasks[task.task_id] = task
        thread = Thread(
            target=self._run_audio_download,
            args=(task.task_id, url, output_format, bitrate_kbps, start_seconds, end_seconds),
            daemon=True,
            name=f"audio-{task.task_id[:8]}",
        )
        thread.start()
        return task

    def create_extension_download(
        self,
        stream_url: str,
        *,
        title: str,
        stream_kind: str,
        request_headers: dict[str, str],
        client_request_id: str | None = None,
    ) -> TaskRecord:
        with self._lock:
            if client_request_id:
                existing_task_id = self._extension_request_tasks.get(client_request_id)
                existing_task = self._tasks.get(existing_task_id or "")
                if existing_task is not None:
                    return existing_task

            task = TaskRecord(task_id=str(uuid4()), message="Видеопоток добавлен в очередь")
            self._tasks[task.task_id] = task
            if client_request_id:
                self._extension_request_tasks[client_request_id] = task.task_id
        thread = Thread(
            target=self._run_extension_download,
            args=(task.task_id, stream_url, title, stream_kind, dict(request_headers)),
            daemon=True,
            name=f"extension-{task.task_id[:8]}",
        )
        thread.start()
        return task

    def create_transcription(
        self,
        url: str,
        *,
        engine: str = "whisper",
        model_name: str,
        language: str,
        formats: tuple[str, ...],
        include_timestamps: bool,
        paragraphize: bool,
        remove_short_fragments: bool,
        text_source: str = "auto",
        custom_terms: tuple[str, ...] = (),
        diarize_speakers: bool = False,
        speaker_count: int | None = None,
        start_seconds: float | None = None,
        end_seconds: float | None = None,
    ) -> TaskRecord:
        task = TaskRecord(task_id=str(uuid4()), message="Транскрибация добавлена в очередь")
        with self._lock:
            self._tasks[task.task_id] = task
        thread = Thread(
            target=self._run_transcription,
            args=(
                task.task_id,
                url,
                engine,
                model_name,
                language,
                formats,
                include_timestamps,
                paragraphize,
                remove_short_fragments,
                text_source,
                custom_terms,
                diarize_speakers,
                speaker_count,
                start_seconds,
                end_seconds,
            ),
            daemon=True,
            name=f"transcription-{task.task_id[:8]}",
        )
        thread.start()
        return task

    def create_file_transcription(
        self,
        source_path: Path,
        original_name: str,
        *,
        engine: str = "whisper",
        model_name: str,
        language: str,
        formats: tuple[str, ...],
        include_timestamps: bool,
        paragraphize: bool,
        remove_short_fragments: bool,
        custom_terms: tuple[str, ...] = (),
        diarize_speakers: bool = False,
        speaker_count: int | None = None,
    ) -> TaskRecord:
        task = TaskRecord(task_id=str(uuid4()), message="Локальный файл добавлен в очередь")
        with self._lock:
            self._tasks[task.task_id] = task
        thread = Thread(
            target=self._run_file_transcription,
            args=(
                task.task_id,
                source_path.resolve(),
                original_name,
                engine,
                model_name,
                language,
                formats,
                include_timestamps,
                paragraphize,
                remove_short_fragments,
                custom_terms,
                diarize_speakers,
                speaker_count,
            ),
            daemon=True,
            name=f"file-transcription-{task.task_id[:8]}",
        )
        thread.start()
        return task

    def create_queue_task(
        self,
        items: tuple[dict[str, str | None], ...],
        *,
        action: str,
        height: int,
        output_format: str,
        bitrate_kbps: int,
        engine: str,
        model_name: str,
        language: str,
        formats: tuple[str, ...],
        include_timestamps: bool,
        paragraphize: bool,
        remove_short_fragments: bool,
        text_source: str,
        custom_terms: tuple[str, ...],
        diarize_speakers: bool = False,
        speaker_count: int | None = None,
    ) -> TaskRecord:
        if not items or len(items) > 50:
            raise ValueError("Очередь должна содержать от 1 до 50 элементов")
        if action not in {"transcription", "video", "audio"}:
            raise ValueError("Неизвестное действие для очереди")

        task = TaskRecord(task_id=str(uuid4()), message="Очередь добавлена в обработку")
        with self._lock:
            self._tasks[task.task_id] = task
        thread = Thread(
            target=self._run_queue_task,
            args=(
                task.task_id,
                items,
                action,
                height,
                output_format,
                bitrate_kbps,
                engine,
                model_name,
                language,
                formats,
                include_timestamps,
                paragraphize,
                remove_short_fragments,
                text_source,
                custom_terms,
                diarize_speakers,
                speaker_count,
            ),
            daemon=True,
            name=f"queue-{task.task_id[:8]}",
        )
        thread.start()
        return task

    def get(self, task_id: str) -> TaskRecord | None:
        with self._lock:
            return self._tasks.get(task_id)

    @staticmethod
    def _mark_cancelled(task: TaskRecord) -> None:
        task.status = "cancelled"
        task.stage = "cancelled"
        task.progress = None
        task.message = "Загрузка отменена. Временные файлы удалены"
        task.error = None
        task.processed_seconds = None
        task.total_seconds = None
        task.eta_seconds = None
        task.downloaded_bytes = None
        task.total_bytes = None
        task.speed_bytes_per_second = None
        task.files = []
        task.cleanup_paths = []
        task.cleanup_complete.set()

    def _remove_output_files(
        self,
        paths: list[Path],
        *,
        retry_until_removed: bool = False,
    ) -> bool:
        all_removed = True
        for raw_path in paths:
            try:
                path = raw_path.resolve()
            except OSError:
                logger.exception("Не удалось определить путь отменённого файла %s", raw_path)
                all_removed = False
                continue
            if not path.is_relative_to(self.output_directory):
                logger.error("Отказ от удаления файла вне downloads: %s", path)
                all_removed = False
                continue

            attempt = 0
            while True:
                try:
                    path.unlink(missing_ok=True)
                    break
                except OSError:
                    attempt += 1
                    if not retry_until_removed and attempt >= 30:
                        logger.exception("Не удалось удалить отменённый файл %s", path)
                        all_removed = False
                        break
                    time.sleep(0.1 if attempt < 30 else 0.5)
        return all_removed

    def _prepare_cancelled_output_cleanup(self, task: TaskRecord, paths: list[Path]) -> None:
        task.status = "running"
        task.stage = "cancelling"
        task.progress = None
        task.message = "Удаляем загруженные данные..."
        task.files = list(paths)
        task.cleanup_paths = list(paths)
        task.cleanup_complete.clear()

    def _run_cancelled_output_cleanup(self, task_id: str, paths: list[Path]) -> None:
        removed = self._remove_output_files(paths, retry_until_removed=True)
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            if removed:
                self._mark_cancelled(task)
                return
            task.status = "failed"
            task.stage = "error"
            task.error = "Небезопасный путь файла отменённой загрузки"
            task.message = "Очистка отменённой загрузки не завершена"
            task.cleanup_complete.set()

    def _finish_download(self, task: TaskRecord, path: Path, message: str) -> None:
        with self._lock:
            if not task.cancel_event.is_set():
                task.status = "completed"
                task.stage = "completed"
                task.progress = 100
                task.message = message
                task.files = [path]
                return
            self._prepare_cancelled_output_cleanup(task, [path])

        self._run_cancelled_output_cleanup(task.task_id, [path])

    def cancel(self, task_id: str) -> TaskRecord | None:
        completed_files: list[Path] | None = None
        with self._lock:
            task = self._tasks.get(task_id)
            if task and task.status in {"pending", "running"}:
                task.cancel_event.set()
                task.stage = "cancelling"
                task.progress = None
                task.message = "Останавливаем загрузку и удаляем временные файлы..."
            elif task and task.status == "completed":
                task.cancel_event.set()
                completed_files = list(task.files)
                self._prepare_cancelled_output_cleanup(task, completed_files)

        if task and completed_files is not None:
            Thread(
                target=self._run_cancelled_output_cleanup,
                args=(task.task_id, completed_files),
                daemon=True,
                name=f"cancel-cleanup-{task.task_id[:8]}",
            ).start()
        return task

    def remove_terminal(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return False
            if (
                task.status not in {"completed", "failed", "cancelled"}
                or not task.cleanup_complete.is_set()
                or task.cleanup_paths
            ):
                raise ValueError("Нельзя удалить незавершённую задачу")
            files = list(task.files)

        if not self._remove_output_files(files):
            raise OSError("Не удалось удалить файлы задачи")

        with self._lock:
            self._tasks.pop(task_id, None)
            stale_request_ids = [
                request_id
                for request_id, mapped_task_id in self._extension_request_tasks.items()
                if mapped_task_id == task_id
            ]
            for request_id in stale_request_ids:
                self._extension_request_tasks.pop(request_id, None)
        return True

    def cancel_all(self) -> int:
        """Запрашивает отмену всех незавершённых задач и возвращает их число."""
        cancelled_count = 0
        with self._lock:
            for task in self._tasks.values():
                if task.status not in {"pending", "running"}:
                    continue
                task.cancel_event.set()
                task.message = "Отменяем операцию..."
                cancelled_count += 1
        return cancelled_count

    def _update(
        self,
        task_id: str,
        stage: str,
        progress: float | None,
        message: str,
        details: dict | None = None,
    ) -> None:
        with self._lock:
            task = self._tasks[task_id]
            if task.cancel_event.is_set():
                return
            task.status = "running"
            if task.stage != stage:
                task.processed_seconds = None
                task.total_seconds = None
                task.eta_seconds = None
                task.downloaded_bytes = None
                task.total_bytes = None
                task.speed_bytes_per_second = None
            task.stage = stage
            task.progress = None if progress is None else min(100, max(0, round(progress, 1)))
            task.message = message
            if details:
                for field_name in (
                    "processed_seconds",
                    "total_seconds",
                    "eta_seconds",
                    "downloaded_bytes",
                    "total_bytes",
                    "speed_bytes_per_second",
                ):
                    if field_name in details:
                        setattr(task, field_name, details[field_name])

    def _run_video_download(
        self,
        task_id: str,
        url: str,
        height: int,
        start_seconds: float | None,
        end_seconds: float | None,
    ) -> None:
        task = self._tasks[task_id]
        try:
            arguments = (
                url,
                height,
                self.output_directory,
                lambda stage, progress, message, details=None: self._update(
                    task_id, stage, progress, message, details
                ),
                task.cancel_event,
            )
            if start_seconds is None:
                path = download_video(*arguments)
            else:
                path = download_video(
                    *arguments,
                    start_seconds=start_seconds,
                    end_seconds=end_seconds,
                )
            self._finish_download(task, path, "Видео успешно сохранено")
        except DownloadCancelled:
            with self._lock:
                self._mark_cancelled(task)
        except Exception as exc:
            logger.exception("Ошибка фоновой задачи %s", task_id)
            with self._lock:
                task.status = "failed"
                task.stage = "error"
                task.error = str(exc)
                task.message = "Не удалось скачать видео"

    def _run_audio_download(
        self,
        task_id: str,
        url: str,
        output_format: str,
        bitrate_kbps: int,
        start_seconds: float | None,
        end_seconds: float | None,
    ) -> None:
        task = self._tasks[task_id]
        try:
            arguments = (
                url,
                output_format,
                bitrate_kbps,
                self.output_directory,
                lambda stage, progress, message, details=None: self._update(
                    task_id, stage, progress, message, details
                ),
                task.cancel_event,
            )
            if start_seconds is None:
                path = download_audio(*arguments)
            else:
                path = download_audio(
                    *arguments,
                    start_seconds=start_seconds,
                    end_seconds=end_seconds,
                )
            self._finish_download(task, path, "Аудио успешно сохранено")
        except DownloadCancelled:
            with self._lock:
                self._mark_cancelled(task)
        except Exception as exc:
            logger.exception("Ошибка загрузки аудио %s", task_id)
            with self._lock:
                task.status = "failed"
                task.stage = "error"
                task.error = str(exc)
                task.message = "Не удалось скачать аудио"

    def _run_extension_download(
        self,
        task_id: str,
        stream_url: str,
        title: str,
        stream_kind: str,
        request_headers: dict[str, str],
    ) -> None:
        task = self._tasks[task_id]
        try:
            path = download_extension_stream(
                stream_url,
                title,
                stream_kind,
                request_headers,
                self.output_directory,
                lambda stage, progress, message, details=None: self._update(
                    task_id, stage, progress, message, details
                ),
                task.cancel_event,
            )
            self._finish_download(task, path, "Видео со страницы успешно сохранено")
        except DownloadCancelled:
            with self._lock:
                self._mark_cancelled(task)
        except Exception as exc:
            logger.exception("Ошибка загрузки потока в задаче %s", task_id)
            with self._lock:
                task.status = "failed"
                task.stage = "error"
                task.error = str(exc)
                task.message = "Не удалось скачать видео со страницы"

    def _run_transcription(
        self,
        task_id: str,
        url: str,
        engine: str,
        model_name: str,
        language: str,
        formats: tuple[str, ...],
        include_timestamps: bool,
        paragraphize: bool,
        remove_short_fragments: bool,
        text_source: str,
        custom_terms: tuple[str, ...],
        diarize_speakers: bool,
        speaker_count: int | None,
        start_seconds: float | None,
        end_seconds: float | None,
    ) -> None:
        task = self._tasks[task_id]
        try:
            options = {
                "engine": engine,
                "model_name": model_name,
                "language": language,
                "formats": formats,
                "include_timestamps": include_timestamps,
                "paragraphize": paragraphize,
                "remove_short_fragments": remove_short_fragments,
                "text_source": text_source,
                "custom_terms": custom_terms,
                "diarize_speakers": diarize_speakers,
                "speaker_count": speaker_count,
            }
            if start_seconds is not None:
                options.update(
                    start_seconds=start_seconds,
                    end_seconds=end_seconds,
                )
            files = transcribe_video(
                url,
                self.output_directory,
                lambda stage, progress, message, details=None: self._update(
                    task_id, stage, progress, message, details
                ),
                task.cancel_event,
                **options,
            )
            with self._lock:
                task.status = "completed"
                task.stage = "completed"
                task.progress = 100
                task.message = "Транскрибация успешно сохранена"
                task.files = files
        except DownloadCancelled:
            with self._lock:
                task.status = "cancelled"
                task.stage = "cancelled"
                task.message = "Операция отменена"
        except TranscriptionError as exc:
            logger.exception("Ошибка транскрибации %s", task_id)
            with self._lock:
                task.status = "failed"
                task.stage = "error"
                task.error = str(exc)
                task.message = "Не удалось создать транскрибацию"
        except Exception:
            logger.exception("Непредвиденная ошибка транскрибации %s", task_id)
            with self._lock:
                task.status = "failed"
                task.stage = "error"
                task.error = "Произошла внутренняя ошибка транскрибации"
                task.message = "Не удалось создать транскрибацию"

    def _run_queue_task(
        self,
        task_id: str,
        items: tuple[dict[str, str | None], ...],
        action: str,
        height: int,
        output_format: str,
        bitrate_kbps: int,
        engine: str,
        model_name: str,
        language: str,
        formats: tuple[str, ...],
        include_timestamps: bool,
        paragraphize: bool,
        remove_short_fragments: bool,
        text_source: str,
        custom_terms: tuple[str, ...],
        diarize_speakers: bool,
        speaker_count: int | None,
    ) -> None:
        task = self._tasks[task_id]
        output_files: list[Path] = []
        total_items = len(items)

        try:
            for position, item in enumerate(items, start=1):
                if task.cancel_event.is_set():
                    raise DownloadCancelled("Операция отменена")

                url = str(item["url"])
                title = str(item.get("title") or f"Элемент {position}")
                self._update(
                    task_id,
                    "queue",
                    (position - 1) / total_items * 100,
                    f"{position}/{total_items}: начинаем обработку «{title}»",
                )

                def report_progress(
                    stage: str,
                    progress: float | None,
                    message: str,
                    details: dict | None = None,
                    *,
                    item_position: int = position,
                ) -> None:
                    nested_progress = 0.0 if progress is None else min(100.0, max(0.0, progress))
                    overall_progress = (
                        (item_position - 1) + nested_progress / 100
                    ) / total_items * 100
                    self._update(
                        task_id,
                        f"queue_{stage}",
                        overall_progress,
                        f"{item_position}/{total_items}: {message}",
                        details,
                    )

                try:
                    if action == "video":
                        created: Path | list[Path] = download_video(
                            url,
                            height,
                            self.output_directory,
                            report_progress,
                            task.cancel_event,
                        )
                    elif action == "audio":
                        created = download_audio(
                            url,
                            output_format,
                            bitrate_kbps,
                            self.output_directory,
                            report_progress,
                            task.cancel_event,
                        )
                    else:
                        created = transcribe_video(
                            url,
                            self.output_directory,
                            report_progress,
                            task.cancel_event,
                            engine=engine,
                            model_name=model_name,
                            language=language,
                            formats=formats,
                            include_timestamps=include_timestamps,
                            paragraphize=paragraphize,
                            remove_short_fragments=remove_short_fragments,
                            text_source=text_source,
                            custom_terms=custom_terms,
                            diarize_speakers=diarize_speakers,
                            speaker_count=speaker_count,
                        )

                    created_files = created if isinstance(created, list) else [created]
                    output_files.extend(Path(path).resolve() for path in created_files)
                    if task.cancel_event.is_set():
                        raise DownloadCancelled("Операция отменена")
                except DownloadCancelled:
                    raise
                except Exception as exc:
                    logger.exception(
                        "Ошибка элемента %s (%s) в пакетной задаче %s",
                        position,
                        title,
                        task_id,
                    )
                    if isinstance(exc, (VideoAnalysisError, TranscriptionError, ValueError)):
                        public_message = str(exc)
                    else:
                        public_message = "Не удалось обработать этот элемент"
                    with self._lock:
                        task.item_errors.append({"title": title, "message": public_message})

            with self._lock:
                if task.cancel_event.is_set():
                    raise DownloadCancelled("Операция отменена")
                task.files = output_files
                if output_files:
                    task.status = "completed"
                    task.stage = "completed"
                    task.progress = 100
                    completed_items = total_items - len(task.item_errors)
                    task.message = f"Очередь обработана: {completed_items} из {total_items}"
                    if task.item_errors:
                        task.message += f". Ошибок: {len(task.item_errors)}"
                else:
                    task.status = "failed"
                    task.stage = "error"
                    task.progress = 100
                    task.error = "Не удалось обработать ни одного элемента очереди"
                    task.message = "Очередь завершилась с ошибками"
        except DownloadCancelled:
            with self._lock:
                if output_files:
                    self._prepare_cancelled_output_cleanup(task, output_files)
                else:
                    self._mark_cancelled(task)
                    return
            self._run_cancelled_output_cleanup(task_id, output_files)
        except Exception:
            logger.exception("Непредвиденная ошибка пакетной задачи %s", task_id)
            with self._lock:
                task.status = "failed"
                task.stage = "error"
                task.error = "Произошла внутренняя ошибка обработки очереди"
                task.message = "Не удалось обработать очередь"

    def _run_file_transcription(
        self,
        task_id: str,
        source_path: Path,
        original_name: str,
        engine: str,
        model_name: str,
        language: str,
        formats: tuple[str, ...],
        include_timestamps: bool,
        paragraphize: bool,
        remove_short_fragments: bool,
        custom_terms: tuple[str, ...],
        diarize_speakers: bool,
        speaker_count: int | None,
    ) -> None:
        task = self._tasks[task_id]
        try:
            files = transcribe_local_file(
                source_path,
                original_name,
                self.output_directory,
                lambda stage, progress, message, details=None: self._update(
                    task_id, stage, progress, message, details
                ),
                task.cancel_event,
                engine=engine,
                model_name=model_name,
                language=language,
                formats=formats,
                include_timestamps=include_timestamps,
                paragraphize=paragraphize,
                remove_short_fragments=remove_short_fragments,
                custom_terms=custom_terms,
                diarize_speakers=diarize_speakers,
                speaker_count=speaker_count,
            )
            with self._lock:
                task.status = "completed"
                task.stage = "completed"
                task.progress = 100
                task.message = "Транскрибация локального файла успешно сохранена"
                task.files = files
        except DownloadCancelled:
            with self._lock:
                task.status = "cancelled"
                task.stage = "cancelled"
                task.message = "Операция отменена"
        except TranscriptionError as exc:
            logger.exception("Ошибка транскрибации локального файла %s", task_id)
            with self._lock:
                task.status = "failed"
                task.stage = "error"
                task.error = str(exc)
                task.message = "Не удалось создать транскрибацию"
        except Exception:
            logger.exception("Непредвиденная ошибка транскрибации локального файла %s", task_id)
            with self._lock:
                task.status = "failed"
                task.stage = "error"
                task.error = "Произошла внутренняя ошибка транскрибации"
                task.message = "Не удалось создать транскрибацию"
        finally:
            shutil.rmtree(source_path.parent, ignore_errors=True)


task_manager = TaskManager()
