const APP_URL = "http://127.0.0.1:8765";
const jobId = new URLSearchParams(location.search).get("job");
const previewMode = new URLSearchParams(location.search).get("preview");

const downloadCard = document.querySelector("#downloadCard");
const stateEyebrow = document.querySelector("#stateEyebrow");
const jobTitle = document.querySelector("#jobTitle");
const sourceHost = document.querySelector("#sourceHost");
const stateIcon = document.querySelector("#stateIcon");
const stageTitle = document.querySelector("#stageTitle");
const stageMessage = document.querySelector("#stageMessage");
const progressValue = document.querySelector("#progressValue");
const progressTrack = document.querySelector("#progressTrack");
const progressBar = document.querySelector("#progressBar");
const statsGrid = document.querySelector("#statsGrid");
const downloadedValue = document.querySelector("#downloadedValue");
const speedValue = document.querySelector("#speedValue");
const etaValue = document.querySelector("#etaValue");
const notice = document.querySelector("#notice");
const noticeTitle = document.querySelector("#noticeTitle");
const noticeText = document.querySelector("#noticeText");
const cancelButton = document.querySelector("#cancelButton");
const retryButton = document.querySelector("#retryButton");
const openFolderButton = document.querySelector("#openFolderButton");
const openAppButton = document.querySelector("#openAppButton");
const closeButton = document.querySelector("#closeButton");
const safeCloseNote = document.querySelector("#safeCloseNote");

let currentJob = null;
let pollTimer = null;
let networkFailures = 0;

function formatBytes(value) {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return "-";
  const units = ["Б", "КБ", "МБ", "ГБ"];
  let amount = value;
  let unit = 0;
  while (amount >= 1024 && unit < units.length - 1) {
    amount /= 1024;
    unit += 1;
  }
  const digits = unit === 0 || amount >= 100 ? 0 : amount >= 10 ? 1 : 2;
  return `${amount.toFixed(digits)} ${units[unit]}`;
}

function formatDuration(value) {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return "-";
  const totalSeconds = Math.round(value);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return minutes > 0 ? `${minutes} мин ${seconds} сек` : `${seconds} сек`;
}

function stageLabel(stage) {
  return {
    preparing: "Подключение",
    pending: "Подготовка",
    downloading: "Скачивание",
    processing: "Обработка",
    saving: "Сохранение",
    cancelling: "Очистка",
    completed: "Готово",
    cancelled: "Отменено",
    error: "Ошибка",
    disconnected: "Нет связи"
  }[stage] || "Загрузка";
}

function setStateIcon(state) {
  stateIcon.className = "state-icon";
  if (state === "completed") stateIcon.classList.add("is-success");
  else if (state === "cancelled") stateIcon.classList.add("is-cancelled");
  else if (state === "failed") stateIcon.classList.add("is-error");
  else stateIcon.classList.add("is-working");
}

function renderJob(job) {
  if (!job) return;
  currentJob = job;
  jobTitle.textContent = job.title || "Видео со страницы";
  sourceHost.textContent = job.fileName || job.sourceHost || "Локальный видеопоток";
  document.title = `${job.title || "Загрузка"} - StreamDock`;
  renderStatus(job);
}

function renderStatus(status) {
  const state = status.status || "running";
  const stage = status.stage || "preparing";
  const completed = state === "completed";
  const failed = state === "failed";
  const cancelled = state === "cancelled";
  const disconnected = state === "disconnected";
  const terminal = completed || failed || cancelled;
  const progress = typeof status.progress === "number" ? Math.max(0, Math.min(100, status.progress)) : null;

  setStateIcon(state);
  stateEyebrow.textContent = completed ? "Видео сохранено" : failed ? "Загрузка не завершена" : cancelled ? "Загрузка отменена" : "Загрузка видео";
  stageTitle.textContent = stageLabel(stage);
  stageMessage.textContent = status.message || "Загрузка продолжается";

  progressTrack.classList.toggle("is-indeterminate", progress === null && !terminal);
  progressTrack.setAttribute("aria-valuemin", "0");
  progressTrack.setAttribute("aria-valuemax", "100");
  if (progress !== null) {
    progressBar.style.width = `${progress}%`;
    progressValue.textContent = `${Math.round(progress)}%`;
    progressTrack.setAttribute("aria-valuenow", String(Math.round(progress)));
  } else {
    progressBar.style.width = terminal ? "100%" : "38%";
    progressValue.textContent = terminal ? (completed ? "100%" : "Завершено") : "В процессе";
    progressTrack.removeAttribute("aria-valuenow");
  }

  const downloaded = status.downloadedBytes ?? status.downloaded_bytes;
  const total = status.totalBytes ?? status.total_bytes;
  const speed = status.speedBytesPerSecond ?? status.speed_bytes_per_second;
  const eta = status.etaSeconds ?? status.eta_seconds;
  statsGrid.hidden = downloaded == null && total == null && speed == null && eta == null;
  downloadedValue.textContent = total ? `${formatBytes(downloaded || 0)} из ${formatBytes(total)}` : formatBytes(downloaded);
  speedValue.textContent = speed ? `${formatBytes(speed)}/с` : "-";
  etaValue.textContent = formatDuration(eta);

  notice.hidden = !(disconnected || failed);
  notice.classList.toggle("is-error", failed);
  if (disconnected) {
    noticeTitle.textContent = "Связь временно потеряна";
    noticeText.textContent = status.error || "Продолжаем проверять состояние локального приложения.";
  } else if (failed) {
    noticeTitle.textContent = "Не удалось завершить загрузку";
    noticeText.textContent = status.error || "Вернитесь на страницу с видео и попробуйте снова.";
  }

  cancelButton.hidden = terminal || disconnected || !currentJob?.taskId;
  retryButton.hidden = !disconnected || Boolean(currentJob?.taskId);
  openFolderButton.hidden = !completed;
  closeButton.hidden = !terminal;
  safeCloseNote.textContent = currentJob?.taskId
    ? "Эту вкладку можно закрыть: загрузка продолжится в локальном приложении. Позже её можно снова открыть из расширения."
    : "Не закрывайте вкладку, пока видео передаётся локальному приложению."
}

async function saveSnapshot(task) {
  if (!jobId || !globalThis.chrome?.runtime?.sendMessage) return;
  const snapshot = {
    status: task.status,
    stage: task.stage,
    progress: task.progress,
    message: task.message,
    error: task.error,
    fileName: task.files?.[0]?.name || null,
    downloadedBytes: task.downloaded_bytes,
    totalBytes: task.total_bytes,
    speedBytesPerSecond: task.speed_bytes_per_second,
    etaSeconds: task.eta_seconds
  };
  const response = await chrome.runtime.sendMessage({ type: "SAVE_JOB_SNAPSHOT", jobId, snapshot });
  if (response?.job) currentJob = response.job;
}

function renderCancelled(task = {}) {
  clearTimeout(pollTimer);
  renderJob({
    ...currentJob,
    ...task,
    title: task.title || currentJob?.title || "Загрузка отменена",
    sourceHost: task.sourceHost || currentJob?.sourceHost || "StreamDock",
    status: "cancelled",
    stage: "cancelled",
    progress: null,
    message: "Загрузка отменена. Временные файлы и запись загрузки удалены"
  });
}

function schedulePoll(delay = 1000) {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(pollTask, delay);
}

async function pollTask() {
  if (currentJob?.stage === "cancelling") {
    try {
      const response = await chrome.runtime.sendMessage({ type: "GET_JOB", jobId });
      if (response?.cancelled || (!response?.job && response?.ok)) {
        renderCancelled();
        return;
      }
      if (response?.job) renderJob(response.job);
      schedulePoll(response?.pending ? 900 : 1200);
    } catch {
      schedulePoll(1500);
    }
    return;
  }

  if (!currentJob?.taskId) {
    await ensureStarted();
    return;
  }

  try {
    const response = await fetch(`${APP_URL}/api/tasks/${encodeURIComponent(currentJob.taskId)}`, { cache: "no-store" });
    const task = await response.json().catch(() => ({}));
    if (response.status === 404) {
      const stored = await chrome.runtime.sendMessage({ type: "GET_JOB", jobId });
      if (stored?.cancelled || currentJob?.stage === "cancelling") {
        renderCancelled();
        return;
      }
      if (stored?.job) currentJob = stored.job;
      const missing = {
        ...currentJob,
        status: "failed",
        stage: "error",
        message: "Состояние загрузки потеряно после перезапуска приложения",
        error: "Проверьте папку downloads. Если готового файла нет, запустите загрузку со страницы ещё раз."
      };
      renderJob(missing);
      await saveSnapshot(missing);
      return;
    }
    if (!response.ok) throw new Error("Не удалось получить состояние загрузки");

    networkFailures = 0;
    renderStatus(task);
    if (task.status === "cancelled") {
      const cancelled = await chrome.runtime.sendMessage({ type: "CANCEL_JOB", jobId });
      if (cancelled?.removed || cancelled?.cancelled) renderCancelled(task);
      else if (cancelled?.job) {
        renderJob(cancelled.job);
        schedulePoll(900);
      }
      return;
    }
    await saveSnapshot(task);
    if (!["completed", "failed", "cancelled"].includes(task.status)) schedulePoll(1000);
  } catch {
    networkFailures += 1;
    const disconnected = {
      ...currentJob,
      status: "disconnected",
      stage: "disconnected",
      message: "Не удаётся связаться с локальным приложением",
      error: "Проверяем соединение автоматически. Сам факт потери связи ещё не означает ошибку загрузки."
    };
    renderJob(disconnected);
    const delay = Math.min(5000, 1200 + networkFailures * 500);
    schedulePoll(delay);
  }
}

async function ensureStarted() {
  try {
    const response = await chrome.runtime.sendMessage({ type: "ENSURE_JOB_STARTED", jobId });
    if (response?.removed || response?.cancelled) {
      renderCancelled();
      return;
    }
    if (response?.job) renderJob(response.job);
    if (response?.job?.taskId) {
      schedulePoll(250);
    } else if (response?.retryable || response?.job?.status === "disconnected") {
      schedulePoll(2500);
    }
  } catch {
    renderJob({
      ...currentJob,
      title: currentJob?.title || "Видео со страницы",
      status: "disconnected",
      stage: "disconnected",
      message: "Страница потеряла связь с расширением",
      error: "Обновите эту вкладку после перезагрузки StreamDock."
    });
  }
}

async function cancelDownload() {
  if (!currentJob?.taskId) return;
  const confirmed = window.confirm(
    "Отменить текущую загрузку? Недокачанные данные и запись загрузки будут удалены."
  );
  if (!confirmed) return;
  cancelButton.disabled = true;
  cancelButton.textContent = "Отменяем...";
  renderJob({
    ...currentJob,
    status: "running",
    stage: "cancelling",
    progress: null,
    message: "Останавливаем загрузку и удаляем временные файлы...",
    error: null
  });
  try {
    const response = await chrome.runtime.sendMessage({ type: "CANCEL_JOB", jobId });
    if (response?.removed || response?.cancelled) renderCancelled();
    else if (response?.job) {
      renderJob(response.job);
      schedulePoll(900);
    } else throw new Error();
  } catch {
    notice.hidden = false;
    notice.classList.add("is-error");
    noticeTitle.textContent = "Очистка ещё не завершена";
    noticeText.textContent = "Проверьте, запущен ли StreamDock, и нажмите «Отменить загрузку» ещё раз.";
  } finally {
    cancelButton.disabled = false;
    cancelButton.textContent = "Отменить загрузку";
  }
}

async function startAndRetry() {
  retryButton.disabled = true;
  retryButton.textContent = "Запускаем...";
  const status = await chrome.runtime.sendMessage({ type: "APP_COMMAND", command: "status" });
  const result = status?.state === "running"
    ? status
    : await chrome.runtime.sendMessage({ type: "APP_COMMAND", command: "start" });
  if (result?.state === "running") {
    await ensureStarted();
  } else {
    notice.hidden = false;
    notice.classList.add("is-error");
    noticeTitle.textContent = "StreamDock не запустился";
    noticeText.textContent = result?.message || "Запустите install.bat и повторите попытку.";
  }
  retryButton.disabled = false;
  retryButton.textContent = "Запустить и повторить";
}

async function openFolder() {
  if (!currentJob?.taskId) return;
  openFolderButton.disabled = true;
  try {
    const response = await fetch(`${APP_URL}/api/tasks/${encodeURIComponent(currentJob.taskId)}/open-folder`, { method: "POST" });
    if (!response.ok) throw new Error();
  } catch {
    notice.hidden = false;
    notice.classList.add("is-error");
    noticeTitle.textContent = "Не удалось открыть папку";
    noticeText.textContent = "Откройте папку downloads внутри проекта вручную.";
  } finally {
    openFolderButton.disabled = false;
  }
}

function renderPreview() {
  const preview = {
    id: "preview",
    title: previewMode === "long"
      ? "Очень длинное название учебного эфира о переходе от подробного чертежа к первой рабочей версии кода без сокращений"
      : "Эфир 4 - Из чертежа в первый код",
    sourceHost: "kinescope.io",
    taskId: "preview-task",
    status: "running",
    stage: "downloading",
    progress: 67,
    message: "Скачиваем видеопоток",
    downloadedBytes: 213_490_073,
    totalBytes: 318_590_976,
    speedBytesPerSecond: 7_340_032,
    etaSeconds: 17
  };
  renderJob(preview);
}

cancelButton.addEventListener("click", cancelDownload);
retryButton.addEventListener("click", startAndRetry);
openFolderButton.addEventListener("click", openFolder);
openAppButton.addEventListener("click", () => chrome.tabs.create({ url: APP_URL }));
closeButton.addEventListener("click", () => window.close());

async function initialize() {
  if (previewMode || !globalThis.chrome?.runtime?.sendMessage) {
    renderPreview();
    return;
  }
  if (!jobId) {
    renderJob({
      title: "Загрузка не найдена",
      sourceHost: "Откройте расширение на странице с видео",
      status: "failed",
      stage: "error",
      message: "В адресе страницы нет номера загрузки",
      error: "Вернитесь на страницу с видео и нажмите «Скачать»."
    });
    return;
  }

  let response;
  try {
    response = await chrome.runtime.sendMessage({ type: "GET_JOB", jobId });
  } catch {
    renderJob({
      title: "Расширение было перезагружено",
      sourceHost: "Обновите эту вкладку",
      status: "disconnected",
      stage: "disconnected",
      message: "Старая страница потеряла связь со StreamDock",
      error: "Нажмите Ctrl+R, чтобы восстановить состояние загрузки."
    });
    return;
  }
  if (!response?.job) {
    if (response?.cancelled) {
      renderCancelled();
      return;
    }
    renderJob({
      title: "Загрузка больше не хранится",
      sourceHost: "StreamDock хранит последние 10 задач",
      status: "failed",
      stage: "error",
      message: "Эта запись уже удалена из истории",
      error: "Если файл не появился в downloads, начните загрузку заново."
    });
    return;
  }

  renderJob(response.job);
  if (response.job.taskId) schedulePoll(100);
  else await ensureStarted();
}

void initialize();
