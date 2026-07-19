const form = document.querySelector('#info-form');
const analysisCard = document.querySelector('#analysis-card');
const urlInput = document.querySelector('#video-url');
const submitButton = document.querySelector('#submit-button');
const buttonLabel = submitButton.querySelector('.button-label');
const clearButton = document.querySelector('#clear-button');
const statusBox = document.querySelector('#status');
const videoCard = document.querySelector('#video-card');
const tabButtons = [...document.querySelectorAll('.tab-button')];
const downloadButton = document.querySelector('#download-button');
const downloadLabel = document.querySelector('#download-label');
const downloadHint = document.querySelector('#download-hint');
const taskProgress = document.querySelector('#task-progress');
const taskMessage = document.querySelector('#task-message');
const taskPercent = document.querySelector('#task-percent');
const taskStage = document.querySelector('#task-stage');
const taskDetail = document.querySelector('#task-detail');
const progressTrack = document.querySelector('#progress-track');
const progressBar = document.querySelector('#progress-bar');
const cancelButton = document.querySelector('#cancel-button');
const downloadResult = document.querySelector('#download-result');
const resultTitle = document.querySelector('#result-title');
const resultSummary = document.querySelector('#result-summary');
const resultFiles = document.querySelector('#result-files');
const openFolderButton = document.querySelector('#open-folder-button');
const fileModeNotice = document.querySelector('#file-mode-notice');
const fileModeLink = document.querySelector('#file-mode-link');
const transcriptionEngine = document.querySelector('#transcription-engine');
const whisperModel = document.querySelector('#whisper-model');
const whisperModelField = document.querySelector('#whisper-model-field');
const transcriptionLanguage = document.querySelector('#transcription-language');
const modelNote = document.querySelector('#model-note');
const includeTimestamps = document.querySelector('#include-timestamps');
const paragraphize = document.querySelector('#paragraphize');
const removeShortFragments = document.querySelector('#remove-short-fragments');
const transcriptionFormats = [...document.querySelectorAll('[name="transcription-format"]')];
const audioBitrate = document.querySelector('#audio-bitrate');
const audioBitrateField = document.querySelector('#audio-bitrate-field');
const audioSourceNote = document.querySelector('#audio-source-note');
const localMediaInput = document.querySelector('#local-media-file');
const localFilePicker = document.querySelector('#local-file-picker');
const localFileTitle = document.querySelector('#local-file-title');
const localFileDescription = document.querySelector('#local-file-description');
const localFileStatus = document.querySelector('#local-file-status');
const thumbnail = document.querySelector('#thumbnail');
const localFilePreview = document.querySelector('#local-file-preview');
const durationLabel = document.querySelector('#duration');
const resultHeading = document.querySelector('#result-heading');
const resultDescription = document.querySelector('#result-description');
const mediaStateLabel = document.querySelector('#media-state-label');
const textSource = document.querySelector('#text-source');
const textSourceField = document.querySelector('#text-source-field');
const subtitleNote = document.querySelector('#subtitle-note');
const customTerms = document.querySelector('#custom-terms');
const termsCount = document.querySelector('#terms-count');
const queueToggle = document.querySelector('#queue-toggle');
const queuePanel = document.querySelector('#queue-panel');
const queueSource = document.querySelector('#queue-source');
const queueAnalyze = document.querySelector('#queue-analyze');
const queueStatus = document.querySelector('#queue-status');
const queueResults = document.querySelector('#queue-results');
const queueSourceTitle = document.querySelector('#queue-source-title');
const queueCount = document.querySelector('#queue-count');
const queueItemsBox = document.querySelector('#queue-items');
const queueSelectAll = document.querySelector('#queue-select-all');
const queueAction = document.querySelector('#queue-action');
const queueStart = document.querySelector('#queue-start');
const queueHelp = document.querySelector('#queue-help');
const rangeSelector = document.querySelector('#range-selector');
const rangeSummary = document.querySelector('#range-summary');
const rangeStart = document.querySelector('#range-start');
const rangeEnd = document.querySelector('#range-end');
const rangeFill = document.querySelector('#range-fill');
const rangeStartTime = document.querySelector('#range-start-time');
const rangeEndTime = document.querySelector('#range-end-time');
const rangeDuration = document.querySelector('#range-duration');
const rangeError = document.querySelector('#range-error');
const transcriptEditor = document.querySelector('#transcript-editor');
const transcriptSearch = document.querySelector('#transcript-search');
const transcriptFindNext = document.querySelector('#transcript-find-next');
const transcriptContent = document.querySelector('#transcript-content');
const transcriptSave = document.querySelector('#transcript-save');
const editorStatus = document.querySelector('#editor-status');
const diarizeSpeakers = document.querySelector('#diarize-speakers');
const speakerCount = document.querySelector('#speaker-count');

const MAX_LOCAL_FILE_SIZE = 20 * 1024 ** 3;
const SUPPORTED_LOCAL_EXTENSIONS = new Set([
  'mp4', 'mkv', 'webm', 'mov', 'avi', 'm4v',
  'mp3', 'm4a', 'wav', 'flac', 'ogg', 'opus', 'aac', 'wma',
]);
const STAGE_LABELS = {
  preparing: 'Подготовка',
  downloading: 'Скачивание видео',
  downloading_audio: 'Скачивание аудио',
  preparing_audio: 'Подготовка аудио',
  model: 'Модель Whisper',
  transcribing: 'Транскрибация',
  gigaam_model: 'Модель GigaAM',
  gigaam_transcribing: 'Распознавание речи',
  hybrid_selecting: 'Поиск технических мест',
  whisper_model: 'Точечная проверка',
  hybrid_checking: 'Проверка терминов',
  speaker_models: 'Модели спикеров',
  speaker_audio: 'Подготовка голосов',
  speaker_diarization: 'Разделение по спикерам',
  processing: 'Обработка FFmpeg',
  saving: 'Сохранение',
  completed: 'Готово',
  cancelled: 'Отменено',
  error: 'Ошибка',
  upload: 'Передача файла',
  queued: 'Ожидание',
  queue: 'Очередь',
};

let selectedHeight = null;
let selectedAudioFormat = 'mp3';
let selectedLocalFile = null;
let sourceMode = 'url';
let activeTab = 'video';
let currentTaskId = null;
let currentTaskKind = null;
let currentUploadRequest = null;
let resultTaskId = null;
let pollTimeout = null;
let currentDurationSeconds = null;
let queueItemsData = [];
let currentQueueAction = null;
let editorTaskId = null;
let editorLoadedTaskId = null;

const requestedUrl = new URLSearchParams(window.location.search).get('url');
const isFileMode = window.location.protocol === 'file:';

if (isFileMode) {
  document.body.classList.add('file-mode');
  fileModeNotice.hidden = false;
  const localUrl = new URL('http://127.0.0.1:8765/');
  if (requestedUrl) localUrl.searchParams.set('url', requestedUrl);
  fileModeLink.href = localUrl.toString();
} else if (requestedUrl) {
  urlInput.value = requestedUrl;
}

function formatBytes(bytes) {
  if (!Number.isFinite(Number(bytes)) || Number(bytes) <= 0) return null;
  const units = ['Б', 'КБ', 'МБ', 'ГБ', 'ТБ'];
  let value = Number(bytes);
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(unit > 1 ? 1 : 0)} ${units[unit]}`;
}

function formatClock(seconds) {
  if (!Number.isFinite(Number(seconds)) || Number(seconds) < 0) return null;
  const total = Math.round(Number(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  return hours
    ? `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(secs).padStart(2, '0')}`
    : `${String(minutes).padStart(2, '0')}:${String(secs).padStart(2, '0')}`;
}

function formatTimecode(seconds) {
  if (!Number.isFinite(Number(seconds)) || Number(seconds) < 0) return '00:00:00';
  const total = Math.round(Number(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(secs).padStart(2, '0')}`;
}

function parseTimecode(value) {
  const parts = String(value).trim().split(':');
  if (!parts.length || parts.length > 3 || parts.some((part) => !/^\d+$/.test(part))) return null;
  const numbers = parts.map(Number);
  if (numbers.some((number) => !Number.isFinite(number))) return null;
  if (parts.length > 1 && numbers.at(-1) >= 60) return null;
  if (parts.length === 3 && numbers[1] >= 60) return null;
  if (parts.length === 1) return numbers[0];
  if (parts.length === 2) return numbers[0] * 60 + numbers[1];
  return numbers[0] * 3600 + numbers[1] * 60 + numbers[2];
}

function selectedCustomTerms() {
  const seen = new Set();
  const result = [];
  String(customTerms.value)
    .split(/[\n,;]+/)
    .map((item) => item.replace(/\s+/g, ' ').trim())
    .filter(Boolean)
    .forEach((term) => {
      const safeTerm = term.slice(0, 64);
      const key = safeTerm.toLocaleLowerCase('ru');
      if (!seen.has(key) && result.length < 64) {
        seen.add(key);
        result.push(safeTerm);
      }
    });
  return result;
}

function updateTermsCount() {
  const allTerms = String(customTerms.value)
    .split(/[\n,;]+/)
    .map((item) => item.trim())
    .filter(Boolean);
  const selected = selectedCustomTerms();
  termsCount.textContent = `${selected.length} из 64`;
  termsCount.classList.toggle('error', allTerms.length > 64);
  termsCount.title = allTerms.length > 64 ? 'Будут использованы первые 64 уникальных термина' : '';
}

function setInlineStatus(element, message, type = '') {
  element.textContent = message;
  element.classList.remove('success', 'error');
  if (type) element.classList.add(type);
}

function formatEta(seconds) {
  if (!Number.isFinite(Number(seconds)) || Number(seconds) <= 0) return null;
  const total = Math.round(Number(seconds));
  if (total < 60) return `осталось около ${total} сек`;
  if (total < 3600) return `осталось около ${Math.ceil(total / 60)} мин`;
  const hours = Math.floor(total / 3600);
  const minutes = Math.ceil((total % 3600) / 60);
  return `осталось около ${hours} ч ${minutes} мин`;
}

function setStatus(message, type = '') {
  statusBox.textContent = message;
  statusBox.classList.remove('success', 'error');
  if (type) statusBox.classList.add(type);
}

function setLocalFileStatus(message, type = '') {
  localFileStatus.textContent = message;
  localFileStatus.classList.toggle('error', type === 'error');
}

function setLoading(isLoading) {
  submitButton.disabled = isLoading;
  localMediaInput.disabled = isLoading || Boolean(currentTaskId) || Boolean(currentUploadRequest);
  submitButton.classList.toggle('is-loading', isLoading);
  analysisCard.classList.toggle('is-loading', isLoading);
  buttonLabel.textContent = isLoading ? 'Получаем форматы' : 'Проверить ссылку';
}

function setRangeError(message = '') {
  rangeError.textContent = message;
  const invalid = Boolean(message);
  rangeStartTime.setAttribute('aria-invalid', String(invalid));
  rangeEndTime.setAttribute('aria-invalid', String(invalid));
}

function updateRangeVisuals() {
  if (!currentDurationSeconds) return;
  const start = Number(rangeStart.value);
  const end = Number(rangeEnd.value);
  const startPercent = start / currentDurationSeconds * 100;
  const endPercent = end / currentDurationSeconds * 100;
  rangeFill.style.left = `${startPercent}%`;
  rangeFill.style.width = `${Math.max(0, endPercent - startPercent)}%`;
  rangeStartTime.value = formatTimecode(start);
  rangeEndTime.value = formatTimecode(end);
  rangeDuration.textContent = formatClock(end - start) || '00:00';
  rangeSummary.textContent = rangeSelector.open
    ? `${formatTimecode(start)} - ${formatTimecode(end)}`
    : 'Всё медиа';
  rangeStart.style.zIndex = start > currentDurationSeconds * .5 ? '3' : '2';
  rangeEnd.style.zIndex = start > currentDurationSeconds * .5 ? '2' : '3';
  setRangeError('');
}

function configureRange(durationSeconds) {
  const duration = Number(durationSeconds);
  currentDurationSeconds = Number.isFinite(duration) && duration >= 1 ? Math.floor(duration) : null;
  rangeSelector.open = false;
  rangeSelector.classList.toggle('hidden', !currentDurationSeconds || sourceMode !== 'url');
  if (!currentDurationSeconds) return;
  rangeStart.max = String(currentDurationSeconds);
  rangeEnd.max = String(currentDurationSeconds);
  rangeStart.value = '0';
  rangeEnd.value = String(currentDurationSeconds);
  updateRangeVisuals();
}

function syncRangeSlider(changed) {
  if (!currentDurationSeconds) return;
  let start = Number(rangeStart.value);
  let end = Number(rangeEnd.value);
  if (changed === rangeStart && start >= end) start = Math.max(0, end - 1);
  if (changed === rangeEnd && end <= start) end = Math.min(currentDurationSeconds, start + 1);
  rangeStart.value = String(start);
  rangeEnd.value = String(end);
  updateRangeVisuals();
  updateActionUi();
}

function commitRangeTime(field) {
  if (!currentDurationSeconds) return;
  const value = parseTimecode(field.value);
  if (value === null) {
    setRangeError('Введите время в формате ЧЧ:ММ:СС');
    updateActionUi();
    return;
  }
  const start = field === rangeStartTime ? value : Number(rangeStart.value);
  const end = field === rangeEndTime ? value : Number(rangeEnd.value);
  if (start < 0 || end > currentDurationSeconds || end - start < 1) {
    setRangeError(`Выберите фрагмент от 1 секунды в пределах ${formatTimecode(currentDurationSeconds)}`);
    updateActionUi();
    return;
  }
  if (field === rangeStartTime) rangeStart.value = String(Math.round(value));
  else rangeEnd.value = String(Math.round(value));
  updateRangeVisuals();
  updateActionUi();
}

function selectedTimeRange() {
  if (!rangeSelector.open || !currentDurationSeconds || rangeError.textContent) return null;
  return {
    start_seconds: Number(rangeStart.value),
    end_seconds: Number(rangeEnd.value),
  };
}

function withSelectedRange(payload) {
  const range = selectedTimeRange();
  return range ? { ...payload, ...range } : payload;
}

function makeFormatOption(symbol, title, details, isDefault = false, height = null) {
  const option = document.createElement(height ? 'button' : 'div');
  option.className = `format-option${isDefault ? ' default' : ''}`;
  if (height) {
    option.type = 'button';
    option.classList.add('selectable');
    option.dataset.height = String(height);
    option.setAttribute('aria-pressed', String(isDefault));
    option.classList.toggle('selected', isDefault);
    option.addEventListener('click', () => selectQuality(height, option));
  } else {
    option.setAttribute('role', 'listitem');
  }

  const symbolBox = document.createElement('span');
  symbolBox.className = 'format-symbol';
  symbolBox.textContent = symbol;
  const info = document.createElement('span');
  info.className = 'format-info';
  const strong = document.createElement('strong');
  strong.textContent = title;
  const small = document.createElement('small');
  small.textContent = details || 'Размер будет определён при скачивании';
  info.append(strong, small);
  option.append(symbolBox, info);

  if (isDefault) {
    const recommended = document.createElement('span');
    recommended.className = 'recommended';
    recommended.textContent = 'По умолчанию';
    option.append(recommended);
  }
  return option;
}

function selectQuality(height, selectedOption) {
  selectedHeight = height;
  document.querySelectorAll('#quality-list .format-option').forEach((option) => {
    const isSelected = option === selectedOption;
    option.classList.toggle('selected', isSelected);
    option.setAttribute('aria-pressed', String(isSelected));
  });
  updateActionUi();
}

function makeAudioFormatOption(format, symbol, title, details, isDefault = false) {
  const option = document.createElement('button');
  option.type = 'button';
  option.className = `format-option selectable${isDefault ? ' default selected' : ''}`;
  option.dataset.audioFormat = format;
  option.setAttribute('aria-pressed', String(isDefault));

  const symbolBox = document.createElement('span');
  symbolBox.className = 'format-symbol';
  symbolBox.textContent = symbol;
  const info = document.createElement('span');
  info.className = 'format-info';
  const strong = document.createElement('strong');
  strong.textContent = title;
  const small = document.createElement('small');
  small.textContent = details;
  info.append(strong, small);
  option.append(symbolBox, info);

  if (isDefault) {
    const recommended = document.createElement('span');
    recommended.className = 'recommended';
    recommended.textContent = 'По умолчанию';
    option.append(recommended);
  }
  option.addEventListener('click', () => selectAudioFormat(format, option));
  return option;
}

function selectAudioFormat(format, selectedOption) {
  selectedAudioFormat = format;
  document.querySelectorAll('#audio-list .format-option').forEach((option) => {
    const isSelected = option === selectedOption;
    option.classList.toggle('selected', isSelected);
    option.setAttribute('aria-pressed', String(isSelected));
  });
  audioBitrateField.classList.toggle('hidden', format !== 'mp3');
  updateActionUi();
}

function renderSubtitleAvailability(subtitles) {
  const available = Array.isArray(subtitles) ? subtitles : [];
  if (!available.length) {
    subtitleNote.textContent = 'Готовые субтитры не найдены. В автоматическом режиме приложение распознает аудио.';
    subtitleNote.classList.remove('hidden');
    return;
  }
  const russian = available.filter((item) => String(item.language || '').toLowerCase().startsWith('ru'));
  const relevant = russian.length ? russian : available;
  const kind = relevant.some((item) => !item.automatic) ? 'ручные субтитры' : 'автоматические субтитры';
  const firstLanguage = String(relevant[0]?.name || relevant[0]?.language || '').trim();
  const languageText = russian.length ? ' на русском' : firstLanguage ? ` на языке «${firstLanguage}»` : '';
  subtitleNote.textContent = `Найдены ${kind}${languageText}. Их можно быстро сохранить без распознавания аудио.`;
  subtitleNote.classList.remove('hidden');
}

function selectedTranscriptFormats() {
  return transcriptionFormats.filter((input) => input.checked).map((input) => input.value);
}

function selectedSpeakerCount() {
  if (!diarizeSpeakers.checked || !speakerCount.value) return null;
  return Number(speakerCount.value);
}

function isTranscriptionKind(kind) {
  return kind === 'transcription' || kind === 'transcription-file';
}

function syncTranscriptionEngineUi() {
  const engine = transcriptionEngine.value;
  const usesWhisperSettings = engine === 'whisper';
  whisperModelField.classList.toggle('hidden', !usesWhisperSettings);
  if (!usesWhisperSettings) transcriptionLanguage.value = 'ru';

  if (engine === 'hybrid') {
    modelNote.textContent = 'GigaAM распознаёт всю русскую речь, а Whisper large-v3 проверяет только подозрительные технические места.';
  } else if (engine === 'gigaam') {
    modelNote.textContent = 'GigaAM быстро распознаёт русскую речь на процессоре. Whisper в этом режиме не запускается.';
  } else {
    modelNote.textContent = 'Whisper подходит для русского и английского. Large v3 точнее, но заметно медленнее небольших моделей.';
  }
  updateActionUi();
}

function transcriptionModeHint(formats) {
  const files = formats.map((item) => item.toUpperCase()).join(', ');
  const speakerHint = diarizeSpeakers.checked
    ? ' Дополнительно скачаем аудио и разделим реплики по спикерам.'
    : '';
  if (transcriptionEngine.value === 'hybrid') {
    return `Будут созданы: ${files}. Совместный режим проверит технические места через large-v3.${speakerHint}`;
  }
  if (transcriptionEngine.value === 'gigaam') {
    return `Будут созданы: ${files}. GigaAM работает быстро и полностью локально.${speakerHint}`;
  }
  return `Будут созданы: ${files}. Whisper ${whisperModel.value} работает локально.${speakerHint}`;
}

function selectedQueueItems() {
  const selectedIndexes = new Set(
    [...queueItemsBox.querySelectorAll('input[type="checkbox"]:checked')]
      .map((input) => Number(input.dataset.itemIndex)),
  );
  return queueItemsData.filter((_, index) => selectedIndexes.has(index));
}

function updateQueueSelectionUi() {
  const checkboxes = [...queueItemsBox.querySelectorAll('input[type="checkbox"]')];
  const selectedCount = checkboxes.filter((input) => input.checked).length;
  queueSelectAll.checked = Boolean(checkboxes.length) && selectedCount === checkboxes.length;
  queueSelectAll.indeterminate = selectedCount > 0 && selectedCount < checkboxes.length;
  queueStart.disabled = isOperationBusy() || selectedCount === 0;
  queueCount.textContent = `${selectedCount} из ${checkboxes.length} выбрано`;
}

function queueItemHost(url) {
  try {
    return new URL(url).hostname;
  } catch (_) {
    return 'Источник';
  }
}

function renderQueueItems(data) {
  queueItemsData = Array.isArray(data.items) ? data.items.slice(0, 50) : [];
  const rows = queueItemsData.map((item, index) => {
    const row = document.createElement('label');
    row.className = 'queue-item';
    row.setAttribute('role', 'listitem');

    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.checked = true;
    checkbox.dataset.itemIndex = String(index);
    checkbox.setAttribute('aria-label', `Выбрать ${item.title || `элемент ${index + 1}`}`);
    checkbox.addEventListener('change', updateQueueSelectionUi);

    const itemIndex = document.createElement('span');
    itemIndex.className = 'queue-item-index';
    itemIndex.textContent = String(item.index || index + 1);
    const copy = document.createElement('span');
    copy.className = 'queue-item-copy';
    const title = document.createElement('strong');
    title.textContent = item.title || `Материал ${index + 1}`;
    title.title = title.textContent;
    const source = document.createElement('small');
    source.textContent = queueItemHost(item.url);
    copy.append(title, source);
    const duration = document.createElement('span');
    duration.className = 'queue-item-duration';
    duration.textContent = item.duration_text || 'Неизвестно';
    row.append(checkbox, itemIndex, copy, duration);
    return row;
  });
  queueItemsBox.replaceChildren(...rows);
  queueSourceTitle.textContent = data.source_title || 'Найденные материалы';
  queueResults.classList.remove('hidden');
  const suffix = data.truncated ? ' Показаны первые 50.' : '';
  setInlineStatus(queueStatus, `Найдено: ${queueItemsData.length}.${suffix}`, 'success');
  updateQueueSelectionUi();
}

function renderQueueTaskCard(items, action) {
  sourceMode = 'queue';
  clearLocalFileSelection();
  videoCard.classList.remove('local-source');
  videoCard.classList.add('queue-source');
  mediaStateLabel.textContent = 'Очередь подготовлена';
  resultHeading.textContent = 'Обработка очереди';
  resultDescription.textContent = 'Материалы обрабатываются по одному, чтобы не перегружать компьютер.';
  thumbnail.hidden = true;
  thumbnail.removeAttribute('src');
  localFilePreview.classList.remove('hidden');
  durationLabel.hidden = true;
  rangeSelector.classList.add('hidden');
  document.querySelector('#video-title').textContent = `${items.length} ${items.length === 1 ? 'материал' : 'материала'}`;
  document.querySelector('#author').textContent = action === 'transcription'
    ? 'Транскрибация с общими настройками'
    : action === 'video' ? 'Скачивание MP4' : 'Скачивание аудио';
  document.querySelector('#source-name').textContent = 'Очередь';
  resetTaskUi();
  videoCard.classList.remove('hidden');
}

function queueRequestBody(items, action) {
  const formats = selectedTranscriptFormats();
  return {
    items: items.map((item) => ({ url: item.url, title: item.title || null })),
    action,
    height: selectedHeight || 1080,
    audio_format: selectedAudioFormat,
    bitrate_kbps: Number(audioBitrate.value),
    engine: transcriptionEngine.value,
    model: whisperModel.value,
    language: transcriptionLanguage.value,
    formats: formats.length ? formats : ['md'],
    include_timestamps: includeTimestamps.checked,
    paragraphize: paragraphize.checked,
    remove_short_fragments: removeShortFragments.checked,
    text_source: textSource.value,
    custom_terms: selectedCustomTerms(),
    diarize_speakers: diarizeSpeakers.checked,
    speaker_count: selectedSpeakerCount(),
  };
}

function resetTranscriptEditor() {
  editorTaskId = null;
  editorLoadedTaskId = null;
  transcriptEditor.open = false;
  transcriptEditor.classList.add('hidden');
  transcriptContent.value = '';
  transcriptSearch.value = '';
  transcriptSave.disabled = false;
  setInlineStatus(editorStatus, '');
}

function prepareTranscriptEditor(taskId, files) {
  const hasMarkdown = files.some((file) => String(file.name).toLowerCase().endsWith('.md'));
  if (!hasMarkdown) {
    resetTranscriptEditor();
    return;
  }
  editorTaskId = taskId;
  editorLoadedTaskId = null;
  transcriptEditor.open = false;
  transcriptEditor.classList.remove('hidden');
  transcriptContent.value = '';
  transcriptSearch.value = '';
  setInlineStatus(editorStatus, 'Откройте редактор, если хотите проверить текст.');
}

async function loadTranscriptEditor() {
  if (!editorTaskId || editorLoadedTaskId === editorTaskId) return;
  transcriptContent.disabled = true;
  transcriptSave.disabled = true;
  setInlineStatus(editorStatus, 'Загружаем Markdown...');
  try {
    const response = await fetch(`/api/tasks/${editorTaskId}/transcript`);
    if (!response.ok) throw new Error(await readError(response));
    const data = await response.json();
    transcriptContent.value = data.content || '';
    editorLoadedTaskId = editorTaskId;
    setInlineStatus(editorStatus, data.filename ? `Открыт ${data.filename}` : 'Markdown открыт.', 'success');
  } catch (error) {
    setInlineStatus(editorStatus, error.message || 'Не удалось открыть Markdown', 'error');
  } finally {
    transcriptContent.disabled = false;
    transcriptSave.disabled = false;
  }
}

function findNextInTranscript() {
  const query = transcriptSearch.value.trim();
  if (!query || !transcriptContent.value) return;
  const haystack = transcriptContent.value.toLocaleLowerCase('ru');
  const needle = query.toLocaleLowerCase('ru');
  let index = haystack.indexOf(needle, transcriptContent.selectionEnd);
  if (index < 0) index = haystack.indexOf(needle);
  if (index < 0) {
    setInlineStatus(editorStatus, 'Совпадений не найдено.', 'error');
    return;
  }
  transcriptContent.focus();
  transcriptContent.setSelectionRange(index, index + query.length);
  const line = transcriptContent.value.slice(0, index).split('\n').length;
  const approximateLineHeight = 16;
  transcriptContent.scrollTop = Math.max(0, (line - 3) * approximateLineHeight);
  setInlineStatus(editorStatus, `Найдено в строке ${line}.`, 'success');
}

async function saveTranscriptEditor() {
  if (!editorTaskId || editorLoadedTaskId !== editorTaskId) return;
  if (new Blob([transcriptContent.value]).size > 5_000_000) {
    setInlineStatus(editorStatus, 'Текст больше 5 МБ. Сократите его перед сохранением.', 'error');
    return;
  }
  transcriptSave.disabled = true;
  setInlineStatus(editorStatus, 'Сохраняем изменения...');
  try {
    const response = await fetch(`/api/tasks/${editorTaskId}/transcript`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content: transcriptContent.value }),
    });
    if (!response.ok) throw new Error(await readError(response));
    const data = await response.json();
    transcriptContent.value = data.content || transcriptContent.value;
    setInlineStatus(editorStatus, 'Изменения сохранены в Markdown.', 'success');
  } catch (error) {
    setInlineStatus(editorStatus, error.message || 'Не удалось сохранить изменения', 'error');
  } finally {
    transcriptSave.disabled = false;
  }
}

function isOperationBusy() {
  return Boolean(currentTaskKind) || Boolean(currentTaskId) || Boolean(currentUploadRequest);
}

function updateActionUi() {
  const isBusy = isOperationBusy();
  const rangeInvalid = rangeSelector.open && (!selectedTimeRange() || Boolean(rangeError.textContent));
  urlInput.disabled = isBusy;
  submitButton.disabled = isBusy;
  clearButton.disabled = isBusy;
  localMediaInput.disabled = isBusy;
  queueToggle.disabled = isBusy;
  queueSource.disabled = isBusy;
  queueAnalyze.disabled = isBusy;
  queueAction.disabled = isBusy;
  queueSelectAll.disabled = isBusy || !queueItemsData.length;
  queueItemsBox.querySelectorAll('input[type="checkbox"]').forEach((input) => { input.disabled = isBusy; });
  queueStart.disabled = isBusy || selectedQueueItems().length === 0;
  audioBitrate.disabled = isBusy;
  tabButtons.forEach((button) => { button.disabled = isBusy; });
  document.querySelectorAll('#quality-list button, #audio-list button').forEach((button) => {
    button.disabled = isBusy;
  });
  transcriptionEngine.disabled = isBusy;
  whisperModel.disabled = isBusy || transcriptionEngine.value !== 'whisper';
  transcriptionLanguage.disabled = isBusy || transcriptionEngine.value !== 'whisper';
  textSource.disabled = isBusy || sourceMode === 'local';
  customTerms.disabled = isBusy;
  diarizeSpeakers.disabled = isBusy;
  speakerCount.disabled = isBusy || !diarizeSpeakers.checked;
  transcriptionFormats.forEach((input) => { input.disabled = isBusy; });
  [includeTimestamps, paragraphize, removeShortFragments].forEach((input) => { input.disabled = isBusy; });
  [rangeStart, rangeEnd, rangeStartTime, rangeEndTime].forEach((input) => { input.disabled = isBusy; });
  if (isBusy) {
    downloadButton.disabled = true;
    return;
  }
  if (sourceMode === 'queue') {
    downloadButton.disabled = true;
    downloadLabel.textContent = 'Настройте новую очередь сверху';
    downloadHint.textContent = 'Результаты текущей очереди показаны ниже.';
    return;
  }
  if (activeTab === 'transcript') {
    const formats = selectedTranscriptFormats();
    downloadButton.disabled = rangeInvalid || formats.length === 0 || (sourceMode === 'local' && !selectedLocalFile);
    downloadLabel.textContent = formats.length
      ? sourceMode === 'local' ? 'Транскрибировать файл' : 'Создать транскрибацию'
      : 'Выберите формат';
    downloadHint.textContent = formats.length
      ? transcriptionModeHint(formats)
      : 'Нужно выбрать хотя бы один формат результата.';
    if (rangeInvalid) downloadHint.textContent = rangeError.textContent || 'Проверьте границы фрагмента.';
    return;
  }
  if (activeTab === 'audio') {
    downloadButton.disabled = rangeInvalid;
    const label = selectedAudioFormat === 'original' ? 'оригинал' : selectedAudioFormat.toUpperCase();
    downloadLabel.textContent = `Скачать аудио · ${label}`;
    downloadHint.textContent = selectedAudioFormat === 'mp3'
      ? `MP3 будет создан с качеством ${audioBitrate.value} кбит/с.`
      : selectedAudioFormat === 'm4a'
        ? 'M4A сохраняется без лишней перекодировки, когда источник это позволяет.'
        : 'Будет сохранён лучший исходный аудиопоток.';
    if (rangeInvalid) downloadHint.textContent = rangeError.textContent || 'Проверьте границы фрагмента.';
    return;
  }
  downloadButton.disabled = rangeInvalid || !selectedHeight;
  downloadLabel.textContent = selectedHeight ? `Скачать MP4 · ${selectedHeight}p` : 'Выберите качество';
  downloadHint.textContent = rangeInvalid
    ? rangeError.textContent || 'Проверьте границы фрагмента.'
    : 'Готовый MP4 скачивается напрямую, а раздельные дорожки объединяются через FFmpeg.';
}

function resetProgressUi() {
  taskStage.textContent = 'Подготовка';
  taskMessage.textContent = 'Подготавливаем операцию...';
  taskDetail.textContent = '';
  taskPercent.textContent = '0%';
  progressBar.style.width = '0%';
  progressTrack.classList.remove('indeterminate');
  progressTrack.setAttribute('aria-valuenow', '0');
}

function resetTaskUi() {
  if (pollTimeout) clearTimeout(pollTimeout);
  pollTimeout = null;
  currentTaskId = null;
  currentTaskKind = null;
  currentUploadRequest = null;
  resultTaskId = null;
  currentQueueAction = null;
  taskProgress.classList.add('hidden');
  downloadResult.classList.add('hidden');
  resultFiles.replaceChildren();
  resetTranscriptEditor();
  cancelButton.classList.remove('hidden');
  cancelButton.disabled = false;
  resetProgressUi();
  updateActionUi();
}

function activateTab(tabName, moveFocus = false) {
  if (isOperationBusy()) return;
  if (sourceMode === 'local' && tabName !== 'transcript') tabName = 'transcript';
  activeTab = tabName;
  tabButtons.forEach((button) => {
    const isActive = button.dataset.tab === tabName;
    button.classList.toggle('active', isActive);
    button.setAttribute('aria-selected', String(isActive));
    button.tabIndex = isActive ? 0 : -1;
    document.querySelector(`#${button.dataset.tab}-panel`).classList.toggle('hidden', !isActive);
    if (isActive && moveFocus) button.focus();
  });
  updateActionUi();
}

function clearLocalFileSelection() {
  selectedLocalFile = null;
  localMediaInput.value = '';
  localFilePicker.classList.remove('has-file');
  localFileTitle.textContent = 'Транскрибировать файл с компьютера';
  localFileDescription.textContent = 'Видео или аудио до 20 ГБ · MP4, MKV, WebM, MP3, M4A, WAV и другие';
  setLocalFileStatus('');
}

function renderVideo(data) {
  sourceMode = 'url';
  clearLocalFileSelection();
  videoCard.classList.remove('local-source');
  videoCard.classList.remove('queue-source');
  textSourceField.classList.remove('hidden');
  renderSubtitleAvailability(data.subtitles);
  mediaStateLabel.textContent = 'Медиа найдено';
  resultHeading.textContent = 'Доступные форматы';
  resultDescription.textContent = 'Выберите видео, аудио или транскрибацию.';
  thumbnail.src = data.thumbnail || '';
  thumbnail.hidden = !data.thumbnail;
  localFilePreview.classList.add('hidden');
  durationLabel.hidden = false;
  durationLabel.textContent = data.duration_text;
  document.querySelector('#video-title').textContent = data.title;
  document.querySelector('#author').textContent = data.author || 'Автор не указан';
  document.querySelector('#source-name').textContent = data.source_name || 'Источник';
  configureRange(data.duration_seconds);

  const qualityList = document.querySelector('#quality-list');
  selectedHeight = data.default_quality;
  const qualityOptions = data.video_qualities.map((quality) => {
    const size = formatBytes(quality.approximate_size);
    const details = [quality.container, size].filter(Boolean).join(' · ');
    return makeFormatOption(
      quality.container.slice(0, 4),
      quality.label,
      details,
      quality.height === data.default_quality,
      quality.height,
    );
  });
  qualityList.replaceChildren(...(qualityOptions.length
    ? qualityOptions
    : [makeFormatOption('—', 'Нет видеодорожки', 'Для этой ссылки доступно только аудио')]
  ));

  const audioList = document.querySelector('#audio-list');
  const bestSourceAudio = data.audio_options[0];
  const sourceDetails = bestSourceAudio
    ? [bestSourceAudio.container, bestSourceAudio.codec, bestSourceAudio.bitrate_kbps ? `${bestSourceAudio.bitrate_kbps} кбит/с` : null].filter(Boolean).join(' · ')
    : 'Лучший поток источника';
  audioSourceNote.textContent = sourceDetails;
  selectedAudioFormat = 'mp3';
  audioBitrateField.classList.remove('hidden');
  audioList.replaceChildren(
    makeAudioFormatOption('mp3', 'MP3', 'MP3', 'Совместимый формат с выбранным качеством', true),
    makeAudioFormatOption('m4a', 'M4A', 'M4A', 'Без лишней перекодировки, когда возможно'),
    makeAudioFormatOption('original', 'SRC', 'Оригинальный поток', sourceDetails),
  );

  resetTaskUi();
  activateTab(data.video_qualities.length ? 'video' : 'audio');
  videoCard.classList.remove('hidden');
}

function renderLocalFile(file) {
  sourceMode = 'local';
  selectedLocalFile = file;
  selectedHeight = null;
  localFilePicker.classList.add('has-file');
  localFileTitle.textContent = file.name;
  localFileDescription.textContent = `${formatBytes(file.size) || 'Размер неизвестен'} · будет обработан полностью локально`;
  setLocalFileStatus('Файл выбран. Настройте формат и запустите транскрибацию.');

  videoCard.classList.add('local-source');
  videoCard.classList.remove('queue-source');
  textSourceField.classList.add('hidden');
  subtitleNote.classList.add('hidden');
  configureRange(null);
  mediaStateLabel.textContent = 'Файл выбран';
  resultHeading.textContent = 'Транскрибация файла';
  resultDescription.textContent = 'Выберите режим распознавания и форматы результата.';
  thumbnail.hidden = true;
  thumbnail.removeAttribute('src');
  localFilePreview.classList.remove('hidden');
  durationLabel.hidden = true;
  document.querySelector('#video-title').textContent = file.name;
  document.querySelector('#author').textContent = `Локальный файл · ${formatBytes(file.size) || 'размер неизвестен'}`;
  document.querySelector('#source-name').textContent = 'Файл';

  resetTaskUi();
  activateTab('transcript');
  videoCard.classList.remove('hidden');
  videoCard.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function taskProgressDetail(task) {
  const details = [];
  const processed = formatClock(task.processed_seconds);
  const totalDuration = formatClock(task.total_seconds);
  if (processed && totalDuration) details.push(`Обработано ${processed} из ${totalDuration}`);

  const downloaded = formatBytes(task.downloaded_bytes);
  const totalBytes = formatBytes(task.total_bytes);
  if (downloaded && totalBytes) details.push(`${downloaded} из ${totalBytes}`);
  else if (downloaded) details.push(`Получено ${downloaded}`);

  const speed = formatBytes(task.speed_bytes_per_second);
  if (speed) details.push(`${speed}/с`);
  const eta = formatEta(task.eta_seconds);
  if (eta) details.push(eta);
  return details.join(' · ');
}

function showProgress(stage, progress, message, detail = '') {
  const numericProgress = progress === null || progress === undefined ? null : Math.max(0, Math.min(100, Number(progress)));
  const baseStage = String(stage || '').replace(/^queue_/, '');
  taskStage.textContent = STAGE_LABELS[baseStage] || 'Выполняется';
  taskMessage.textContent = message;
  taskDetail.textContent = detail;
  progressTrack.classList.toggle('indeterminate', numericProgress === null);
  if (numericProgress === null) {
    taskPercent.textContent = 'Выполняется';
    progressBar.style.width = '38%';
    progressTrack.removeAttribute('aria-valuenow');
  } else {
    taskPercent.textContent = `${Math.round(numericProgress)}%`;
    progressBar.style.width = `${numericProgress}%`;
    progressTrack.setAttribute('aria-valuenow', String(Math.round(numericProgress)));
  }
}

function renderTask(task) {
  showProgress(task.stage, task.progress, task.error || task.message, taskProgressDetail(task));

  if (task.status === 'completed') {
    if (pollTimeout) clearTimeout(pollTimeout);
    pollTimeout = null;
    cancelButton.classList.add('hidden');
    const finishedKind = currentTaskKind;
    const finishedQueueAction = currentQueueAction;
    const files = Array.isArray(task.files) ? task.files : [];
    const itemErrors = Array.isArray(task.item_errors) ? task.item_errors : [];
    resultTaskId = task.task_id;
    currentTaskId = null;
    currentTaskKind = null;
    localMediaInput.disabled = false;
    updateActionUi();
    downloadLabel.textContent = finishedKind === 'queue'
      ? 'Очередь завершена'
      : isTranscriptionKind(finishedKind)
        ? 'Создать ещё одну транскрибацию'
        : finishedKind === 'audio' ? 'Скачать аудио ещё раз' : 'Скачать ещё раз';
    downloadHint.textContent = 'Готовые файлы сохранены в папке downloads.';
    if (files.length || itemErrors.length) {
      resultTitle.textContent = finishedKind === 'queue'
        ? 'Очередь обработана'
        : isTranscriptionKind(finishedKind)
          ? 'Транскрибация готова'
          : finishedKind === 'audio' ? 'Аудио готово' : 'Видео готово';
      const summaryParts = [`${files.length} ${files.length === 1 ? 'файл' : 'файлов'}`];
      if (itemErrors.length) summaryParts.push(`${itemErrors.length} с ошибкой`);
      resultSummary.textContent = summaryParts.join(' · ');
      const fileRows = files.map((file) => {
        const row = document.createElement('div');
        row.className = 'result-file';
        const name = document.createElement('span');
        name.className = 'result-file-name';
        name.textContent = file.name;
        name.title = file.name;
        const size = document.createElement('span');
        size.className = 'result-file-size';
        size.textContent = formatBytes(file.size) || '';
        const link = document.createElement('a');
        link.className = 'file-link';
        link.href = file.download_url;
        link.setAttribute('download', file.name);
        link.textContent = file.name.toLowerCase().endsWith('.md') ? 'Скачать Markdown' : `Скачать ${file.name.split('.').pop().toUpperCase()}`;
        row.append(name, size, link);
        return row;
      });
      const errorRows = itemErrors.map((item) => {
        const row = document.createElement('div');
        row.className = 'result-file is-error';
        const name = document.createElement('span');
        name.className = 'result-file-name';
        name.textContent = `Не удалось: ${item.title || 'материал'}`;
        const message = document.createElement('span');
        message.className = 'result-file-error';
        message.textContent = item.message || 'Неизвестная ошибка';
        row.append(name, message);
        return row;
      });
      resultFiles.replaceChildren(...fileRows, ...errorRows);
      prepareTranscriptEditor(task.task_id, files);
      downloadResult.classList.remove('hidden');
    }
    if (finishedKind === 'queue' && finishedQueueAction === 'transcription' && !files.length) {
      resetTranscriptEditor();
    }
    return true;
  }

  if (task.status === 'failed' || task.status === 'cancelled') {
    if (pollTimeout) clearTimeout(pollTimeout);
    pollTimeout = null;
    cancelButton.classList.add('hidden');
    currentTaskId = null;
    currentTaskKind = null;
    localMediaInput.disabled = false;
    updateActionUi();
    downloadLabel.textContent = task.status === 'cancelled'
      ? 'Начать заново'
      : activeTab === 'transcript' ? 'Повторить транскрибацию' : 'Повторить скачивание';
    downloadHint.textContent = task.error || task.message;
    return true;
  }
  return false;
}

async function pollTask() {
  if (!currentTaskId) return;
  try {
    const response = await fetch(`/api/tasks/${currentTaskId}`);
    if (!response.ok) throw new Error(await readError(response));
    const task = await response.json();
    if (!renderTask(task)) pollTimeout = setTimeout(pollTask, 1000);
  } catch (error) {
    showProgress('error', null, error.message || 'Не удалось обновить прогресс');
    cancelButton.classList.add('hidden');
    currentTaskId = null;
    currentTaskKind = null;
    updateActionUi();
  }
}

function prepareTask(kind, label) {
  currentTaskKind = kind;
  downloadButton.disabled = true;
  downloadLabel.textContent = label;
  taskProgress.classList.remove('hidden');
  downloadResult.classList.add('hidden');
  resultTaskId = null;
  resetTranscriptEditor();
  cancelButton.classList.remove('hidden');
  cancelButton.disabled = false;
  resetProgressUi();
  updateActionUi();
}

async function startDownload() {
  if (!selectedHeight || currentTaskId) return;
  prepareTask('video', 'Запускаем загрузку...');
  try {
    const response = await fetch('/api/tasks/video', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(withSelectedRange({ url: urlInput.value.trim(), height: selectedHeight })),
    });
    if (!response.ok) throw new Error(await readError(response));
    const task = await response.json();
    currentTaskId = task.task_id;
    downloadLabel.textContent = `Скачиваем ${selectedHeight}p`;
    await pollTask();
  } catch (error) {
    showProgress('error', null, error.message || 'Не удалось запустить скачивание');
    cancelButton.classList.add('hidden');
    currentTaskId = null;
    currentTaskKind = null;
    updateActionUi();
  }
}

async function startAudioDownload() {
  if (!selectedAudioFormat || currentTaskId) return;
  prepareTask('audio', 'Запускаем загрузку аудио...');
  try {
    const response = await fetch('/api/tasks/audio', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(withSelectedRange({
        url: urlInput.value.trim(),
        format: selectedAudioFormat,
        bitrate_kbps: Number(audioBitrate.value),
      })),
    });
    if (!response.ok) throw new Error(await readError(response));
    const task = await response.json();
    currentTaskId = task.task_id;
    downloadLabel.textContent = 'Скачиваем аудио';
    await pollTask();
  } catch (error) {
    showProgress('error', null, error.message || 'Не удалось запустить скачивание аудио');
    cancelButton.classList.add('hidden');
    currentTaskId = null;
    currentTaskKind = null;
    updateActionUi();
  }
}

function uploadLocalFile(formats) {
  return new Promise((resolve, reject) => {
    const params = new URLSearchParams({
      filename: selectedLocalFile.name,
      engine: transcriptionEngine.value,
      model: whisperModel.value,
      language: transcriptionLanguage.value,
      formats: formats.join(','),
      include_timestamps: String(includeTimestamps.checked),
      paragraphize: String(paragraphize.checked),
      remove_short_fragments: String(removeShortFragments.checked),
      custom_terms: selectedCustomTerms().join(','),
      diarize_speakers: String(diarizeSpeakers.checked),
    });
    const selectedCount = selectedSpeakerCount();
    if (selectedCount !== null) params.set('speaker_count', String(selectedCount));
    const request = new XMLHttpRequest();
    currentUploadRequest = request;
    localMediaInput.disabled = true;
    request.open('POST', `/api/tasks/transcription/file?${params.toString()}`);
    request.setRequestHeader('Content-Type', 'application/octet-stream');
    request.responseType = 'json';

    request.upload.addEventListener('progress', (event) => {
      const progress = event.lengthComputable ? event.loaded / event.total * 100 : null;
      const detail = event.lengthComputable
        ? `${formatBytes(event.loaded)} из ${formatBytes(event.total)}`
        : `Передано ${formatBytes(event.loaded) || '0 Б'}`;
      showProgress('upload', progress, 'Передаём файл локальному приложению', detail);
    });
    request.addEventListener('load', () => {
      currentUploadRequest = null;
      if (request.status >= 200 && request.status < 300) {
        resolve(request.response);
        return;
      }
      reject(new Error(request.response?.detail || 'Не удалось передать локальный файл'));
    });
    request.addEventListener('error', () => {
      currentUploadRequest = null;
      reject(new Error('Передача файла прервалась'));
    });
    request.addEventListener('abort', () => {
      currentUploadRequest = null;
      reject(new Error('Передача файла отменена'));
    });
    request.send(selectedLocalFile);
  });
}

async function startTranscription() {
  const formats = selectedTranscriptFormats();
  if (!formats.length || currentTaskId || currentUploadRequest) return;
  if (sourceMode === 'local' && !selectedLocalFile) return;
  const kind = sourceMode === 'local' ? 'transcription-file' : 'transcription';
  prepareTask(kind, sourceMode === 'local' ? 'Передаём файл...' : 'Запускаем транскрибацию...');

  try {
    let task;
    if (sourceMode === 'local') {
      task = await uploadLocalFile(formats);
    } else {
      const response = await fetch('/api/tasks/transcription', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(withSelectedRange({
          url: urlInput.value.trim(),
          engine: transcriptionEngine.value,
          model: whisperModel.value,
          language: transcriptionLanguage.value,
          text_source: textSource.value,
          custom_terms: selectedCustomTerms(),
          diarize_speakers: diarizeSpeakers.checked,
          speaker_count: selectedSpeakerCount(),
          formats,
          include_timestamps: includeTimestamps.checked,
          paragraphize: paragraphize.checked,
          remove_short_fragments: removeShortFragments.checked,
        })),
      });
      if (!response.ok) throw new Error(await readError(response));
      task = await response.json();
    }
    currentTaskId = task.task_id;
    currentTaskKind = kind;
    downloadLabel.textContent = 'Создаём транскрибацию';
    await pollTask();
  } catch (error) {
    const cancelled = error.message === 'Передача файла отменена';
    showProgress(cancelled ? 'cancelled' : 'error', null, error.message || 'Не удалось запустить транскрибацию');
    cancelButton.classList.add('hidden');
    currentTaskId = null;
    currentTaskKind = null;
    currentUploadRequest = null;
    localMediaInput.disabled = false;
    updateActionUi();
  }
}

async function readError(response) {
  try {
    const body = await response.json();
    if (typeof body.detail === 'string') return body.detail;
    if (Array.isArray(body.detail)) return 'Проверьте введённые данные';
  } catch (_) {
    // Сервер вернул ответ без JSON.
  }
  return 'Не удалось выполнить операцию. Проверьте подключение и попробуйте ещё раз.';
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (isOperationBusy()) return;
  setLoading(true);
  videoCard.classList.add('hidden');
  setStatus('Получаем информацию и доступные форматы...');
  try {
    const response = await fetch('/api/video/info', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: urlInput.value.trim() }),
    });
    if (!response.ok) throw new Error(await readError(response));
    renderVideo(await response.json());
    setStatus('Информация получена', 'success');
    videoCard.scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (error) {
    setStatus(error.message || 'Произошла неизвестная ошибка', 'error');
  } finally {
    setLoading(false);
  }
});

clearButton.addEventListener('click', () => {
  if (isOperationBusy()) return;
  urlInput.value = '';
  if (sourceMode === 'url') videoCard.classList.add('hidden');
  selectedHeight = null;
  if (sourceMode === 'url') resetTaskUi();
  setStatus('');
  urlInput.focus();
});

localMediaInput.addEventListener('change', () => {
  const file = localMediaInput.files?.[0];
  if (!file) return;
  const extension = file.name.includes('.') ? file.name.split('.').pop().toLowerCase() : '';
  if (!SUPPORTED_LOCAL_EXTENSIONS.has(extension)) {
    setLocalFileStatus('Этот формат не поддерживается. Выберите обычный видео- или аудиофайл.', 'error');
    localMediaInput.value = '';
    return;
  }
  if (file.size > MAX_LOCAL_FILE_SIZE) {
    setLocalFileStatus('Файл больше 20 ГБ. Выберите файл меньшего размера.', 'error');
    localMediaInput.value = '';
    return;
  }
  renderLocalFile(file);
});

downloadButton.addEventListener('click', () => {
  if (activeTab === 'transcript') startTranscription();
  else if (activeTab === 'audio') startAudioDownload();
  else if (activeTab === 'video') startDownload();
});

transcriptionFormats.forEach((input) => input.addEventListener('change', updateActionUi));
transcriptionEngine.addEventListener('change', syncTranscriptionEngineUi);
whisperModel.addEventListener('change', updateActionUi);
transcriptionLanguage.addEventListener('change', updateActionUi);
audioBitrate.addEventListener('change', updateActionUi);
textSource.addEventListener('change', updateActionUi);
customTerms.addEventListener('input', () => {
  updateTermsCount();
  updateActionUi();
});
diarizeSpeakers.addEventListener('change', updateActionUi);
speakerCount.addEventListener('change', updateActionUi);

queueToggle.addEventListener('click', () => {
  if (isOperationBusy()) return;
  const willOpen = queuePanel.classList.contains('hidden');
  queuePanel.classList.toggle('hidden', !willOpen);
  queueToggle.setAttribute('aria-expanded', String(willOpen));
  if (willOpen) queueSource.focus();
});

queueAnalyze.addEventListener('click', async () => {
  const source = queueSource.value.trim();
  if (!source || isOperationBusy()) {
    setInlineStatus(queueStatus, 'Вставьте ссылки или ссылку на плейлист.', 'error');
    return;
  }
  queueAnalyze.disabled = true;
  queueAnalyze.textContent = 'Получаем список...';
  queueResults.classList.add('hidden');
  setInlineStatus(queueStatus, 'Проверяем ссылки...');
  try {
    const response = await fetch('/api/video/queue', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source }),
    });
    if (!response.ok) throw new Error(await readError(response));
    renderQueueItems(await response.json());
  } catch (error) {
    queueItemsData = [];
    queueItemsBox.replaceChildren();
    setInlineStatus(queueStatus, error.message || 'Не удалось получить список', 'error');
  } finally {
    queueAnalyze.disabled = false;
    queueAnalyze.textContent = 'Показать список';
    updateActionUi();
  }
});

queueSelectAll.addEventListener('change', () => {
  queueItemsBox.querySelectorAll('input[type="checkbox"]').forEach((input) => {
    input.checked = queueSelectAll.checked;
  });
  updateQueueSelectionUi();
});

queueAction.addEventListener('change', () => {
  queueHelp.textContent = queueAction.value === 'transcription'
    ? 'Используются настройки вкладки «Текст». Элементы обрабатываются по очереди.'
    : queueAction.value === 'video'
      ? 'Для каждого элемента будет выбран лучший MP4 до 1080p.'
      : 'Для каждого элемента используются выбранные формат и качество аудио.';
});

queueStart.addEventListener('click', async () => {
  const items = selectedQueueItems();
  const action = queueAction.value;
  if (!items.length || isOperationBusy()) return;
  renderQueueTaskCard(items, action);
  currentQueueAction = action;
  prepareTask('queue', 'Запускаем очередь...');
  videoCard.scrollIntoView({ behavior: 'smooth', block: 'start' });
  try {
    const response = await fetch('/api/tasks/queue', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(queueRequestBody(items, action)),
    });
    if (!response.ok) throw new Error(await readError(response));
    const task = await response.json();
    currentTaskId = task.task_id;
    currentTaskKind = 'queue';
    downloadLabel.textContent = 'Обрабатываем очередь';
    await pollTask();
  } catch (error) {
    showProgress('error', null, error.message || 'Не удалось запустить очередь');
    cancelButton.classList.add('hidden');
    currentTaskId = null;
    currentTaskKind = null;
    currentQueueAction = null;
    updateActionUi();
  }
});

rangeSelector.addEventListener('toggle', () => {
  updateRangeVisuals();
  updateActionUi();
});
rangeStart.addEventListener('input', () => syncRangeSlider(rangeStart));
rangeEnd.addEventListener('input', () => syncRangeSlider(rangeEnd));
[rangeStartTime, rangeEndTime].forEach((field) => {
  field.addEventListener('change', () => commitRangeTime(field));
  field.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      commitRangeTime(field);
    }
  });
});

transcriptEditor.addEventListener('toggle', () => {
  if (transcriptEditor.open) loadTranscriptEditor();
});
transcriptFindNext.addEventListener('click', findNextInTranscript);
transcriptSearch.addEventListener('keydown', (event) => {
  if (event.key === 'Enter') {
    event.preventDefault();
    findNextInTranscript();
  }
});
transcriptSave.addEventListener('click', saveTranscriptEditor);

cancelButton.addEventListener('click', async () => {
  if (currentUploadRequest) {
    cancelButton.disabled = true;
    currentUploadRequest.abort();
    return;
  }
  if (!currentTaskId) return;
  cancelButton.disabled = true;
  taskMessage.textContent = 'Отменяем операцию...';
  try {
    await fetch(`/api/tasks/${currentTaskId}/cancel`, { method: 'POST' });
  } finally {
    cancelButton.disabled = false;
  }
});

openFolderButton.addEventListener('click', async () => {
  const taskId = resultTaskId || currentTaskId;
  if (!taskId) return;
  const response = await fetch(`/api/tasks/${taskId}/open-folder`, { method: 'POST' });
  if (!response.ok) downloadHint.textContent = await readError(response);
});

tabButtons.forEach((button, index) => {
  button.addEventListener('click', () => activateTab(button.dataset.tab));
  button.addEventListener('keydown', (event) => {
    if (!['ArrowLeft', 'ArrowRight'].includes(event.key) || sourceMode === 'local') return;
    event.preventDefault();
    const direction = event.key === 'ArrowRight' ? 1 : -1;
    const nextIndex = (index + direction + tabButtons.length) % tabButtons.length;
    activateTab(tabButtons[nextIndex].dataset.tab, true);
  });
});

syncTranscriptionEngineUi();
updateTermsCount();
activateTab('video');
