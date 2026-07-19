from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator


class VideoInfoRequest(BaseModel):
    url: HttpUrl


class VideoQuality(BaseModel):
    height: int = Field(gt=0)
    label: str
    container: str
    approximate_size: int | None = None


class AudioOption(BaseModel):
    container: str
    codec: str | None = None
    bitrate_kbps: int | None = None
    approximate_size: int | None = None


class SubtitleOption(BaseModel):
    language: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=120)
    automatic: bool = False


class VideoInfoResponse(BaseModel):
    source_url: str
    source_name: str
    title: str
    author: str | None = None
    thumbnail: str | None = None
    duration_seconds: int | None = None
    duration_text: str
    video_qualities: list[VideoQuality]
    audio_options: list[AudioOption]
    default_quality: int | None = None
    subtitles: list[SubtitleOption] = Field(default_factory=list)


class TimeRangeRequest(BaseModel):
    start_seconds: float | None = Field(default=None, ge=0, le=604_800)
    end_seconds: float | None = Field(default=None, gt=0, le=604_800)

    @model_validator(mode="after")
    def validate_time_range(self):
        if (self.start_seconds is None) != (self.end_seconds is None):
            raise ValueError("Укажите и начало, и конец фрагмента")
        if self.start_seconds is not None and self.end_seconds is not None:
            if self.end_seconds - self.start_seconds < 0.5:
                raise ValueError("Фрагмент должен быть не короче половины секунды")
        return self


class VideoDownloadRequest(TimeRangeRequest):
    url: HttpUrl
    height: int = Field(ge=144, le=4320)


class AudioDownloadRequest(TimeRangeRequest):
    url: HttpUrl
    format: Literal["mp3", "m4a", "original"] = "mp3"
    bitrate_kbps: Literal[128, 192, 256, 320] = 192


class TranscriptionRequest(TimeRangeRequest):
    url: HttpUrl
    engine: Literal["whisper", "gigaam", "hybrid"] = "hybrid"
    model: Literal["tiny", "base", "small", "medium", "large-v3"] = "large-v3"
    language: Literal["auto", "ru", "en"] = "auto"
    formats: list[Literal["md", "txt", "srt", "vtt"]] = Field(
        default_factory=lambda: ["md"],
        min_length=1,
        max_length=4,
    )
    include_timestamps: bool = True
    paragraphize: bool = True
    remove_short_fragments: bool = True
    text_source: Literal["auto", "subtitles", "speech"] = "auto"
    custom_terms: list[str] = Field(default_factory=list, max_length=64)
    diarize_speakers: bool = False
    speaker_count: int | None = Field(default=None, ge=2, le=10)

    @field_validator("custom_terms", mode="before")
    @classmethod
    def normalize_custom_terms(cls, value):
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("Пользовательские термины должны быть списком")
        result: list[str] = []
        seen: set[str] = set()
        for raw_term in value:
            term = " ".join(str(raw_term).strip().split())
            if not term:
                continue
            if len(term) > 64:
                raise ValueError("Один термин не может быть длиннее 64 символов")
            key = term.casefold()
            if key not in seen:
                seen.add(key)
                result.append(term)
        return result


class QueueAnalysisRequest(BaseModel):
    source: str = Field(min_length=1, max_length=12_000)


class QueueMediaItem(BaseModel):
    index: int = Field(ge=1, le=50)
    url: str
    title: str = Field(min_length=1, max_length=300)
    duration_seconds: int | None = Field(default=None, ge=0)
    duration_text: str


class QueueAnalysisResponse(BaseModel):
    source_title: str | None = Field(default=None, max_length=300)
    truncated: bool = False
    items: list[QueueMediaItem] = Field(min_length=1, max_length=50)


class QueueTaskItem(BaseModel):
    url: HttpUrl
    title: str | None = Field(default=None, max_length=300)


class QueueTaskRequest(BaseModel):
    items: list[QueueTaskItem] = Field(min_length=1, max_length=50)
    action: Literal["transcription", "video", "audio"] = "transcription"
    height: int = Field(default=1080, ge=144, le=4320)
    audio_format: Literal["mp3", "m4a", "original"] = "mp3"
    bitrate_kbps: Literal[128, 192, 256, 320] = 192
    engine: Literal["whisper", "gigaam", "hybrid"] = "hybrid"
    model: Literal["tiny", "base", "small", "medium", "large-v3"] = "large-v3"
    language: Literal["auto", "ru", "en"] = "ru"
    formats: list[Literal["md", "txt", "srt", "vtt"]] = Field(
        default_factory=lambda: ["md"],
        min_length=1,
        max_length=4,
    )
    include_timestamps: bool = True
    paragraphize: bool = True
    remove_short_fragments: bool = True
    text_source: Literal["auto", "subtitles", "speech"] = "auto"
    custom_terms: list[str] = Field(default_factory=list, max_length=64)
    diarize_speakers: bool = False
    speaker_count: int | None = Field(default=None, ge=2, le=10)

    @field_validator("custom_terms", mode="before")
    @classmethod
    def normalize_queue_terms(cls, value):
        return TranscriptionRequest.normalize_custom_terms(value)


class ExtensionStreamDownloadRequest(BaseModel):
    stream_url: HttpUrl
    page_url: HttpUrl | None = None
    title: str = Field(default="Видео со страницы", min_length=1, max_length=240)
    stream_kind: Literal["hls", "dash", "video", "audio", "unknown"] = "unknown"
    request_headers: dict[str, str] = Field(default_factory=dict, max_length=5)
    client_request_id: str | None = Field(default=None, min_length=8, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")


class TaskCreatedResponse(BaseModel):
    task_id: str


class ShutdownResponse(BaseModel):
    status: Literal["stopping"] = "stopping"
    cancelled_tasks: int = Field(ge=0)


class TaskFileInfo(BaseModel):
    name: str
    size: int
    download_url: str


class TaskItemError(BaseModel):
    title: str
    message: str


class TaskStatusResponse(BaseModel):
    task_id: str
    status: str
    stage: str
    progress: float | None = Field(default=0, ge=0, le=100)
    message: str
    error: str | None = None
    processed_seconds: float | None = Field(default=None, ge=0)
    total_seconds: float | None = Field(default=None, ge=0)
    eta_seconds: float | None = Field(default=None, ge=0)
    downloaded_bytes: int | None = Field(default=None, ge=0)
    total_bytes: int | None = Field(default=None, ge=0)
    speed_bytes_per_second: float | None = Field(default=None, ge=0)
    files: list[TaskFileInfo] = Field(default_factory=list)
    item_errors: list[TaskItemError] = Field(default_factory=list)


class TranscriptContentResponse(BaseModel):
    filename: str
    content: str


class TranscriptUpdateRequest(BaseModel):
    content: str = Field(max_length=5_000_000)
