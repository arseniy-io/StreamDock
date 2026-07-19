const STORAGE_PREFIX = "media-tab-";
const MAX_CANDIDATES = 24;
const MAX_HEADER_LENGTH = 16_384;
const APP_URL = "http://127.0.0.1:8765";
const NATIVE_HOST_NAME = "com.streamdock.launcher";
const CONTROL_TOKEN_HEADER = "X-StreamDock-Token";
const CONTROL_TOKEN_RESPONSE_HEADER = "X-StreamDock-Control-Token";
const JOBS_KEY = "downloadJobs";
const CURRENT_JOB_KEY = "currentDownloadJobId";
const JOB_SECRET_PREFIX = "download-secret-";
const CANCELLED_JOB_PREFIX = "cancelled-job-";
const MAX_JOBS = 10;
const CANCEL_POLL_INTERVAL_MS = 350;
const CANCEL_MAX_WAIT_MS = 30_000;
const pendingHeaders = new Map();
const candidateWriteQueues = new Map();
const pendingNativeRequests = new Map();
const jobSubmissions = new Map();
const jobCancellations = new Map();
let jobsMutationQueue = Promise.resolve();
let nativePort = null;
let nativeRequestCounter = 0;

const MEDIA_URL_PATTERN = /\.(m3u8|mpd|mp4|webm|m4v|mov|m4a|aac)(?:$|[?#])/i;
const MEDIA_CONTENT_TYPES = [
  "application/vnd.apple.mpegurl",
  "application/x-mpegurl",
  "application/dash+xml",
  "video/",
  "audio/mp4",
  "audio/aac"
];
const ALLOWED_REQUEST_HEADERS = new Map([
  ["authorization", "Authorization"],
  ["cookie", "Cookie"],
  ["origin", "Origin"],
  ["referer", "Referer"],
  ["user-agent", "User-Agent"]
]);

function storageKey(tabId) {
  return `${STORAGE_PREFIX}${tabId}`;
}

function jobSecretKey(jobId) {
  return `${JOB_SECRET_PREFIX}${jobId}`;
}

function cancelledJobKey(jobId) {
  return `${CANCELLED_JOB_PREFIX}${jobId}`;
}

function isHttpUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:";
  } catch {
    return false;
  }
}

function safeHost(value) {
  try {
    return new URL(value).hostname;
  } catch {
    return "Источник видео";
  }
}

function mediaKind(url, contentType = "") {
  const value = `${url} ${contentType}`.toLowerCase();
  if (value.includes(".m3u8") || value.includes("mpegurl")) return "hls";
  if (value.includes(".mpd") || value.includes("dash+xml")) return "dash";
  if (value.includes("audio/")) return "audio";
  if (/\.(mp4|webm|m4v|mov)(?:$|[?#])/i.test(url) || value.includes("video/")) return "video";
  return "unknown";
}

function candidatePriority(candidate) {
  try {
    const path = new URL(candidate.url).pathname.toLowerCase();
    if (path.endsWith("/master.m3u8") || path.endsWith("/master.mpd")) return 1000;
    if (path.endsWith("/manifest.mpd")) return 900;
    if (path.endsWith(".m3u8")) return 500;
    if (path.endsWith(".mpd")) return 450;
    if (/\.(mp4|webm|m4v|mov)$/.test(path)) return 200;
    if (/\.(m4a|aac)$/.test(path)) return 100;
  } catch {
    return 0;
  }
  return 0;
}

function filterRequestHeaders(requestHeaders = []) {
  const result = {};
  let totalLength = 0;
  for (const header of requestHeaders) {
    const safeName = ALLOWED_REQUEST_HEADERS.get(String(header.name || "").toLowerCase());
    const value = typeof header.value === "string" ? header.value : "";
    if (!safeName || !value || /[\r\n]/.test(value) || value.length > MAX_HEADER_LENGTH) continue;
    if (totalLength + value.length > MAX_HEADER_LENGTH * 2) continue;
    result[safeName] = value;
    totalLength += value.length;
  }
  return result;
}

function sanitizeStoredHeaders(headers = {}) {
  const requestHeaders = Object.entries(headers).map(([name, value]) => ({ name, value }));
  return filterRequestHeaders(requestHeaders);
}

function safeText(value, fallback = "", maxLength = 300) {
  const text = typeof value === "string" ? value.trim() : "";
  return (text || fallback).slice(0, maxLength);
}

function safeNumber(value) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
}

async function configureStorageAccess() {
  try {
    await chrome.storage.local.setAccessLevel({ accessLevel: "TRUSTED_CONTEXTS" });
  } catch {
    // На старой сборке Chrome доступ всё равно ограничен контекстами расширения,
    // потому что content script в проекте не используется.
  }
}

void configureStorageAccess();
chrome.runtime.onInstalled.addListener(() => {
  void configureStorageAccess();
  void resumePendingCancellations();
});
chrome.runtime.onStartup.addListener(() => {
  void configureStorageAccess();
  void resumePendingCancellations();
});

async function readJobs() {
  const stored = await chrome.storage.local.get([JOBS_KEY, CURRENT_JOB_KEY]);
  const jobs = stored[JOBS_KEY];
  return {
    jobs: jobs && typeof jobs === "object" && !Array.isArray(jobs) ? jobs : {},
    currentJobId: typeof stored[CURRENT_JOB_KEY] === "string" ? stored[CURRENT_JOB_KEY] : null
  };
}

function queueJobMutation(mutation) {
  const queued = jobsMutationQueue.then(mutation, mutation);
  jobsMutationQueue = queued.then(
    () => undefined,
    () => undefined
  );
  return queued;
}

async function persistJobs(jobs, requestedCurrentJobId = null) {
  const ordered = Object.values(jobs).sort((left, right) => (right.createdAt || 0) - (left.createdAt || 0));
  const kept = ordered.slice(0, MAX_JOBS);
  const keptIds = new Set(kept.map((item) => item.id));
  const removedIds = Object.keys(jobs).filter((jobId) => !keptIds.has(jobId));
  const trimmedJobs = Object.fromEntries(kept.map((item) => [item.id, item]));
  const nextCurrentJobId = requestedCurrentJobId && trimmedJobs[requestedCurrentJobId]
    ? requestedCurrentJobId
    : kept[0]?.id || null;

  await chrome.storage.local.set({ [JOBS_KEY]: trimmedJobs });
  if (nextCurrentJobId) {
    await chrome.storage.local.set({ [CURRENT_JOB_KEY]: nextCurrentJobId });
  } else {
    await chrome.storage.local.remove(CURRENT_JOB_KEY);
  }
  if (removedIds.length > 0) {
    await chrome.storage.session.remove(removedIds.map(jobSecretKey));
  }
  return { jobs: trimmedJobs, currentJobId: nextCurrentJobId };
}

async function writeJob(job, { makeCurrent = true } = {}) {
  return queueJobMutation(async () => {
    const { jobs, currentJobId } = await readJobs();
    jobs[job.id] = job;
    const saved = await persistJobs(jobs, makeCurrent ? job.id : currentJobId);
    await chrome.storage.session.remove(cancelledJobKey(job.id));
    return saved.jobs[job.id] || null;
  });
}

async function updateJob(jobId, patch) {
  return queueJobMutation(async () => {
    const { jobs, currentJobId } = await readJobs();
    const current = jobs[jobId];
    if (!current) return null;
    let safePatch = patch;
    if (current.stage === "cancelling" && patch.stage !== "cancelling") {
      safePatch = !current.taskId && typeof patch.taskId === "string" && patch.taskId
        ? { taskId: patch.taskId }
        : {};
    }
    if (Object.keys(safePatch).length === 0) return current;
    const updated = { ...current, ...safePatch, id: jobId, updatedAt: Date.now() };
    jobs[jobId] = updated;
    const saved = await persistJobs(jobs, currentJobId);
    return saved.jobs[jobId] || null;
  });
}

async function getJob(jobId) {
  const { jobs } = await readJobs();
  return jobs[jobId] || null;
}

async function deleteJob(jobId, { markCancelled = false } = {}) {
  return queueJobMutation(async () => {
    const { jobs, currentJobId } = await readJobs();
    delete jobs[jobId];
    await persistJobs(jobs, currentJobId === jobId ? null : currentJobId);
    await chrome.storage.session.remove(jobSecretKey(jobId));
    if (markCancelled) {
      await chrome.storage.session.set({ [cancelledJobKey(jobId)]: Date.now() });
    }
    return { ok: true, removed: true, cancelled: markCancelled };
  });
}

async function currentJob() {
  const { jobs, currentJobId } = await readJobs();
  return currentJobId ? jobs[currentJobId] || null : null;
}

function wait(delayMs) {
  return new Promise((resolve) => setTimeout(resolve, delayMs));
}

async function isCancelledJob(jobId) {
  const stored = await chrome.storage.session.get(cancelledJobKey(jobId));
  return Boolean(stored[cancelledJobKey(jobId)]);
}

async function markCancellationPending(jobId, message, error = null) {
  return updateJob(jobId, {
    status: "running",
    stage: "cancelling",
    progress: null,
    message,
    error
  });
}

async function performJobCancellation(jobId) {
  let job = await getJob(jobId);
  if (!job) {
    return { ok: true, removed: true, cancelled: await isCancelledJob(jobId), job: null };
  }

  job = await markCancellationPending(
    jobId,
    "Останавливаем загрузку и удаляем временные файлы..."
  );
  if (!job?.taskId) {
    const stored = await chrome.storage.session.get(jobSecretKey(jobId));
    const secret = stored[jobSecretKey(jobId)];
    if (secret) {
      try {
        const recoveryResponse = await fetch(`${APP_URL}/api/extension/download`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Save-Video-Extension": "1"
          },
          body: JSON.stringify(secret)
        });
        const recoveryData = await recoveryResponse.json().catch(() => ({}));
        const recoveredTaskId = safeText(recoveryData.task_id, "", 80);
        if (!recoveryResponse.ok || !recoveredTaskId) {
          const pending = await markCancellationPending(
            jobId,
            "Ждём подтверждения от StreamDock. Отмена продолжится автоматически.",
            null
          );
          return { ok: false, pending: true, job: pending };
        }
        job = await updateJob(jobId, { taskId: recoveredTaskId });
        await chrome.storage.session.remove(jobSecretKey(jobId));
      } catch {
        const pending = await markCancellationPending(
          jobId,
          "Нет связи со StreamDock. Отмена продолжится после восстановления связи.",
          "Запустите StreamDock и снова откройте страницу загрузки."
        );
        return { ok: false, pending: true, job: pending };
      }
    } else if (!jobSubmissions.has(jobId)) {
      await deleteJob(jobId, { markCancelled: true });
      return { ok: true, removed: true, cancelled: true, job: null };
    } else {
      return {
        ok: false,
        pending: true,
        job,
        message: "Ждём, пока локальное приложение примет загрузку для отмены."
      };
    }
  }

  if (!job?.taskId) {
    return { ok: true, removed: true, cancelled: await isCancelledJob(jobId), job: null };
  }

  const taskUrl = `${APP_URL}/api/tasks/${encodeURIComponent(job.taskId)}`;
  let backendTask = null;
  let backendTaskMissing = false;
  try {
    const cancelResponse = await fetch(`${taskUrl}/cancel`, { method: "POST" });
    backendTask = await cancelResponse.json().catch(() => ({}));
    if (cancelResponse.status === 404) {
      backendTaskMissing = true;
    } else if (!cancelResponse.ok) {
      const pending = await markCancellationPending(
        jobId,
        "Не удалось подтвердить отмену. Повторим при следующем открытии.",
        "Проверьте, запущен ли StreamDock."
      );
      return { ok: false, pending: true, job: pending };
    }
  } catch {
    const pending = await markCancellationPending(
      jobId,
      "Нет связи со StreamDock. Отмена продолжится после восстановления связи.",
      "Запустите StreamDock и снова откройте страницу загрузки."
    );
    return { ok: false, pending: true, job: pending };
  }

  const terminalStatuses = new Set(["cancelled", "completed", "failed"]);
  const deadline = Date.now() + CANCEL_MAX_WAIT_MS;
  while (!backendTaskMissing && !terminalStatuses.has(backendTask?.status) && Date.now() < deadline) {
    await wait(CANCEL_POLL_INTERVAL_MS);
    try {
      const taskResponse = await fetch(taskUrl, { cache: "no-store" });
      if (taskResponse.status === 404) {
        backendTaskMissing = true;
        break;
      }
      if (!taskResponse.ok) throw new Error("task status unavailable");
      backendTask = await taskResponse.json().catch(() => ({}));
    } catch {
      const pending = await markCancellationPending(
        jobId,
        "Связь прервалась во время очистки. Повторим при следующем открытии.",
        "Запустите StreamDock и снова откройте страницу загрузки."
      );
      return { ok: false, pending: true, job: pending };
    }
  }

  if (!backendTaskMissing && !terminalStatuses.has(backendTask?.status)) {
    const pending = await markCancellationPending(
      jobId,
      "StreamDock ещё удаляет временные файлы. Можно закрыть эту вкладку.",
      null
    );
    return { ok: false, pending: true, job: pending };
  }

  try {
    const deleteResponse = await fetch(taskUrl, { method: "DELETE" });
    if (!deleteResponse.ok && deleteResponse.status !== 404) {
      const pending = await markCancellationPending(
        jobId,
        "Очистка ещё не завершена. Повторим при следующем открытии.",
        "StreamDock пока не подтвердил удаление данных загрузки."
      );
      return { ok: false, pending: true, job: pending };
    }
  } catch {
    const pending = await markCancellationPending(
      jobId,
      "Нет связи во время финальной очистки. Повторим при следующем открытии.",
      "Запустите StreamDock и снова откройте страницу загрузки."
    );
    return { ok: false, pending: true, job: pending };
  }

  await deleteJob(jobId, { markCancelled: true });
  return { ok: true, removed: true, cancelled: true, job: null };
}

function cancelJob(jobId) {
  if (jobCancellations.has(jobId)) return jobCancellations.get(jobId);
  const cancellation = performJobCancellation(jobId).finally(() => jobCancellations.delete(jobId));
  jobCancellations.set(jobId, cancellation);
  return cancellation;
}

async function getJobResponse(jobId) {
  const job = await getJob(jobId);
  if (job?.stage === "cancelling") {
    const result = await cancelJob(jobId);
    if (result.removed) return { ok: true, job: null, cancelled: true };
    return { ok: Boolean(result.job), job: result.job, pending: true };
  }
  if (job) return { ok: true, job };
  return { ok: false, job: null, cancelled: await isCancelledJob(jobId) };
}

async function currentJobResponse() {
  let job = await currentJob();
  if (job?.stage === "cancelling") {
    await cancelJob(job.id);
    job = await currentJob();
  }
  return { ok: true, job };
}

async function resumePendingCancellations() {
  const { jobs } = await readJobs();
  const pendingJobIds = Object.values(jobs)
    .filter((job) => job?.stage === "cancelling")
    .map((job) => job.id);
  await Promise.allSettled(pendingJobIds.map(cancelJob));
}

function connectNativePort() {
  if (nativePort) return nativePort;

  const port = chrome.runtime.connectNative(NATIVE_HOST_NAME);
  nativePort = port;
  port.onMessage.addListener((response) => {
    const pending = pendingNativeRequests.get(response?.requestId);
    if (!pending) return;
    clearTimeout(pending.timer);
    pendingNativeRequests.delete(response.requestId);
    pending.resolve(response);

    if (!(response?.state === "running" && response?.managed === true)) {
      setTimeout(() => {
        if (nativePort === port) {
          try {
            port.disconnect();
          } catch {
            // Host уже мог завершиться после ответа stopped.
          }
          nativePort = null;
        }
      }, 0);
    }
  });
  port.onDisconnect.addListener(() => {
    const detail = chrome.runtime.lastError?.message || "Локальный помощник отключился";
    if (nativePort === port) nativePort = null;
    for (const [requestId, pending] of pendingNativeRequests) {
      clearTimeout(pending.timer);
      pending.reject(new Error(detail));
      pendingNativeRequests.delete(requestId);
    }
  });
  return port;
}

function sendNativeCommand(command) {
  const port = connectNativePort();
  const requestId = `${Date.now()}-${++nativeRequestCounter}`;
  const timeoutMs = command === "start" ? 25_000 : command === "stop" ? 12_000 : 4_000;

  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pendingNativeRequests.delete(requestId);
      reject(new Error("Локальный помощник не ответил вовремя"));
    }, timeoutMs);
    pendingNativeRequests.set(requestId, { resolve, reject, timer });
    try {
      port.postMessage({ command, requestId });
    } catch (error) {
      clearTimeout(timer);
      pendingNativeRequests.delete(requestId);
      reject(error);
    }
  });
}

async function readHttpAppStatus() {
  try {
    const response = await fetch(`${APP_URL}/api/health`, { cache: "no-store" });
    const data = await response.json().catch(() => ({}));
    const online = response.ok && data.status === "ok" && response.headers.get("X-StreamDock-App") === "1";
    return {
      online,
      controlToken: online ? response.headers.get(CONTROL_TOKEN_RESPONSE_HEADER) || "" : ""
    };
  } catch {
    return { online: false, controlToken: "" };
  }
}

async function stopHttpApp(status = null) {
  const current = status?.online ? status : await readHttpAppStatus();
  if (!current.online) {
    return { ok: true, state: "stopped", managed: false, message: "StreamDock уже остановлен." };
  }
  if (!current.controlToken) {
    return {
      ok: false,
      state: "running",
      managed: false,
      message: "Безопасная остановка недоступна. Перезапустите StreamDock и повторите попытку."
    };
  }

  try {
    const response = await fetch(`${APP_URL}/api/control/shutdown`, {
      method: "POST",
      cache: "no-store",
      headers: { [CONTROL_TOKEN_HEADER]: current.controlToken }
    });
    if (response.status !== 202) {
      return {
        ok: false,
        state: "running",
        managed: false,
        message: "StreamDock отклонил команду остановки. Повторите попытку."
      };
    }
  } catch {
    // Сервер может закрыть соединение сразу после принятия команды.
  }

  for (let attempt = 0; attempt < 30; attempt += 1) {
    await wait(150);
    const refreshed = await readHttpAppStatus();
    if (!refreshed.online) {
      return { ok: true, state: "stopped", managed: false, message: "StreamDock остановлен." };
    }
  }
  return {
    ok: false,
    state: "running",
    managed: false,
    message: "StreamDock не успел остановиться. Повторите попытку через несколько секунд."
  };
}

async function runNativeOrHttpCommand(command) {
  const nativeResult = await sendNativeCommand(command);
  if (command === "stop" && nativeResult?.state === "running" && nativeResult?.managed !== true) {
    return stopHttpApp();
  }
  return nativeResult;
}

async function appCommand(command) {
  if (!["status", "start", "stop"].includes(command)) {
    return { ok: false, state: "error", managed: false, message: "Неизвестная команда приложения." };
  }
  try {
    return await runNativeOrHttpCommand(command);
  } catch {
    const httpStatus = await readHttpAppStatus();
    if (httpStatus.online) {
      if (command === "stop") return stopHttpApp(httpStatus);
      return {
        ok: true,
        state: "running",
        managed: false,
        message: "StreamDock запущен. Его можно остановить из расширения."
      };
    }
    if (command === "stop") {
      return { ok: true, state: "stopped", managed: false, message: "StreamDock остановлен." };
    }
    return {
      ok: false,
      state: "helper_missing",
      managed: false,
      message: "Локальный помощник не найден. Запустите install.bat в папке проекта и перезагрузите расширение."
    };
  }
}

async function createDownloadJob(candidate, fallbackPage = {}) {
  if (!candidate || !isHttpUrl(candidate.url)) {
    throw new Error("Не удалось определить адрес видеопотока");
  }

  const jobId = crypto.randomUUID();
  const createdAt = Date.now();
  const title = safeText(candidate.pageTitle || fallbackPage.title, "Видео со страницы", 240);
  const pageUrl = isHttpUrl(candidate.pageUrl) ? candidate.pageUrl : isHttpUrl(fallbackPage.url) ? fallbackPage.url : null;
  const streamKind = ["hls", "dash", "video", "audio", "unknown"].includes(candidate.kind)
    ? candidate.kind
    : "unknown";

  const safeJob = {
    id: jobId,
    title,
    sourceHost: safeHost(candidate.url),
    status: "preparing",
    stage: "preparing",
    progress: null,
    message: "Открываем постоянную страницу загрузки",
    error: null,
    taskId: null,
    fileName: null,
    downloadedBytes: null,
    totalBytes: null,
    speedBytesPerSecond: null,
    etaSeconds: null,
    createdAt,
    updatedAt: createdAt
  };
  const secretPayload = {
    stream_url: candidate.url,
    page_url: pageUrl,
    title,
    stream_kind: streamKind,
    request_headers: sanitizeStoredHeaders(candidate.requestHeaders || {}),
    client_request_id: jobId
  };

  await chrome.storage.session.set({ [jobSecretKey(jobId)]: secretPayload });
  await writeJob(safeJob);
  let tab;
  try {
    tab = await chrome.tabs.create({ url: chrome.runtime.getURL(`progress.html?job=${encodeURIComponent(jobId)}`) });
  } catch (error) {
    await chrome.storage.session.remove(jobSecretKey(jobId));
    await updateJob(jobId, {
      status: "failed",
      stage: "error",
      message: "Не удалось открыть страницу загрузки",
      error: "Перезагрузите расширение и попробуйте снова."
    });
    throw error;
  }
  void ensureJobStarted(jobId);
  return { ok: true, jobId, tabId: tab.id };
}

function resumeCancellationAfterSubmission(jobId, job) {
  if (job?.stage === "cancelling") {
    setTimeout(() => void cancelJob(jobId), 0);
  }
  return job;
}

async function ensureJobStarted(jobId) {
  const storedJob = await getJob(jobId);
  if (storedJob?.stage === "cancelling") return cancelJob(jobId);
  if (jobSubmissions.has(jobId)) return jobSubmissions.get(jobId);

  const submission = (async () => {
    const job = await getJob(jobId);
    if (!job) throw new Error("Загрузка не найдена");
    if (job.taskId) return { ok: true, job };

    const stored = await chrome.storage.session.get(jobSecretKey(jobId));
    const secret = stored[jobSecretKey(jobId)];
    if (!secret) {
      const expired = await updateJob(jobId, {
        status: "failed",
        stage: "error",
        message: "Данные этой загрузки больше недоступны",
        error: "Вернитесь на страницу с видео и начните загрузку заново."
      });
      return { ok: false, job: resumeCancellationAfterSubmission(jobId, expired) };
    }

    await updateJob(jobId, {
      status: "preparing",
      stage: "preparing",
      message: "Передаём видео локальному приложению",
      error: null
    });

    try {
      const response = await fetch(`${APP_URL}/api/extension/download`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Save-Video-Extension": "1"
        },
        body: JSON.stringify(secret)
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        const retryable = response.status >= 500;
        const updated = await updateJob(jobId, {
          status: retryable ? "disconnected" : "failed",
          stage: retryable ? "disconnected" : "error",
          message: retryable ? "Локальное приложение временно недоступно" : "Не удалось начать загрузку",
          error: safeText(data.detail, "Проверьте страницу с видео и попробуйте снова", 300)
        });
        return { ok: false, retryable, job: resumeCancellationAfterSubmission(jobId, updated) };
      }

      const taskId = safeText(data.task_id, "", 80);
      if (!taskId) {
        const invalidResponse = await updateJob(jobId, {
          status: "failed",
          stage: "error",
          message: "Локальное приложение вернуло неполный ответ",
          error: "Перезапустите StreamDock и начните загрузку заново."
        });
        return {
          ok: false,
          retryable: false,
          job: resumeCancellationAfterSubmission(jobId, invalidResponse)
        };
      }

      const updated = await updateJob(jobId, {
        taskId,
        status: "pending",
        stage: "preparing",
        message: "Загрузка передана локальному приложению",
        error: null
      });
      await chrome.storage.session.remove(jobSecretKey(jobId));
      return { ok: true, job: resumeCancellationAfterSubmission(jobId, updated) };
    } catch {
      const updated = await updateJob(jobId, {
        status: "disconnected",
        stage: "disconnected",
        message: "Не удаётся связаться с локальным приложением",
        error: "Запустите StreamDock или дождитесь восстановления связи."
      });
      return {
        ok: false,
        retryable: true,
        job: resumeCancellationAfterSubmission(jobId, updated)
      };
    }
  })().finally(() => jobSubmissions.delete(jobId));

  jobSubmissions.set(jobId, submission);
  return submission;
}

async function saveJobSnapshot(jobId, snapshot = {}) {
  const allowedStatus = ["pending", "running", "completed", "failed", "cancelled", "disconnected"];
  const safeSnapshot = {
    status: allowedStatus.includes(snapshot.status) ? snapshot.status : "running",
    stage: safeText(snapshot.stage, "preparing", 40),
    progress: safeNumber(snapshot.progress),
    message: safeText(snapshot.message, "Загрузка продолжается", 300),
    error: snapshot.error ? safeText(snapshot.error, "", 300) : null,
    fileName: snapshot.fileName ? safeText(snapshot.fileName, "", 260) : null,
    downloadedBytes: safeNumber(snapshot.downloadedBytes),
    totalBytes: safeNumber(snapshot.totalBytes),
    speedBytesPerSecond: safeNumber(snapshot.speedBytesPerSecond),
    etaSeconds: safeNumber(snapshot.etaSeconds)
  };
  return updateJob(jobId, safeSnapshot);
}

function responseContentType(responseHeaders = []) {
  const header = responseHeaders.find((item) => String(item.name || "").toLowerCase() === "content-type");
  return String(header?.value || "").split(";", 1)[0].trim().toLowerCase();
}

function isMediaResponse(contentType) {
  return MEDIA_CONTENT_TYPES.some((type) => contentType.startsWith(type));
}

async function persistCandidate(tabId, rawCandidate) {
  if (!Number.isInteger(tabId) || tabId < 0 || !isHttpUrl(rawCandidate.url)) return;

  let tab;
  try {
    tab = await chrome.tabs.get(tabId);
  } catch {
    return;
  }

  const key = storageKey(tabId);
  const stored = await chrome.storage.session.get(key);
  const current = Array.isArray(stored[key]) ? stored[key] : [];
  const candidate = {
    url: rawCandidate.url,
    kind: rawCandidate.kind || mediaKind(rawCandidate.url, rawCandidate.contentType),
    contentType: rawCandidate.contentType || "",
    requestHeaders: rawCandidate.requestHeaders || {},
    pageUrl: isHttpUrl(tab.url) ? tab.url : "",
    pageTitle: String(tab.title || "Видео со страницы").slice(0, 240),
    detectedAt: Date.now()
  };

  const previous = current.find((item) => item.url === candidate.url);
  if (previous && Object.keys(candidate.requestHeaders).length === 0) {
    candidate.requestHeaders = previous.requestHeaders || {};
  }

  const updated = [candidate, ...current.filter((item) => item.url !== candidate.url)]
    .sort((left, right) => candidatePriority(right) - candidatePriority(left) || right.detectedAt - left.detectedAt)
    .slice(0, MAX_CANDIDATES);
  await chrome.storage.session.set({ [key]: updated });
  await chrome.action.setBadgeBackgroundColor({ tabId, color: "#315EF4" });
  await chrome.action.setBadgeText({ tabId, text: String(updated.length) });
}

function saveCandidate(tabId, rawCandidate) {
  const previousWrite = candidateWriteQueues.get(tabId) || Promise.resolve();
  const persist = () => persistCandidate(tabId, rawCandidate);
  const queuedWrite = previousWrite.then(persist, persist);
  candidateWriteQueues.set(tabId, queuedWrite);

  const releaseQueue = () => {
    if (candidateWriteQueues.get(tabId) === queuedWrite) {
      candidateWriteQueues.delete(tabId);
    }
  };
  void queuedWrite.then(releaseQueue, releaseQueue);
  return queuedWrite;
}

chrome.webRequest.onBeforeSendHeaders.addListener(
  (details) => {
    if (details.tabId < 0 || !isHttpUrl(details.url)) return;
    const headers = filterRequestHeaders(details.requestHeaders);
    pendingHeaders.set(details.requestId, headers);
    if (MEDIA_URL_PATTERN.test(details.url)) {
      void saveCandidate(details.tabId, {
        url: details.url,
        kind: mediaKind(details.url),
        requestHeaders: headers
      });
    }
  },
  { urls: ["http://*/*", "https://*/*"], types: ["media", "xmlhttprequest", "other"] },
  ["requestHeaders", "extraHeaders"]
);

chrome.webRequest.onHeadersReceived.addListener(
  (details) => {
    if (details.tabId < 0 || !isHttpUrl(details.url)) return;
    const contentType = responseContentType(details.responseHeaders);
    if (!isMediaResponse(contentType)) return;
    void saveCandidate(details.tabId, {
      url: details.url,
      kind: mediaKind(details.url, contentType),
      contentType,
      requestHeaders: pendingHeaders.get(details.requestId) || {}
    });
  },
  { urls: ["http://*/*", "https://*/*"], types: ["media", "xmlhttprequest", "other"] },
  ["responseHeaders", "extraHeaders"]
);

function forgetRequest(details) {
  pendingHeaders.delete(details.requestId);
}

chrome.webRequest.onCompleted.addListener(forgetRequest, { urls: ["http://*/*", "https://*/*"] });
chrome.webRequest.onErrorOccurred.addListener(forgetRequest, { urls: ["http://*/*", "https://*/*"] });

chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (changeInfo.status !== "loading") return;
  void chrome.storage.session.remove(storageKey(tabId));
  void chrome.action.setBadgeText({ tabId, text: "" });
});

chrome.tabs.onRemoved.addListener((tabId) => {
  void chrome.storage.session.remove(storageKey(tabId));
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type === "LIST_CANDIDATES" && Number.isInteger(message.tabId)) {
    chrome.storage.session.get(storageKey(message.tabId)).then((stored) => {
      sendResponse({ candidates: stored[storageKey(message.tabId)] || [] });
    });
    return true;
  }

  if (message?.type === "SAVE_CANDIDATES" && Number.isInteger(message.tabId)) {
    const candidates = Array.isArray(message.candidates) ? message.candidates.slice(0, MAX_CANDIDATES) : [];
    Promise.all(candidates.map((candidate) => saveCandidate(message.tabId, candidate))).then(() => {
      sendResponse({ ok: true });
    });
    return true;
  }

  const trustedMessage = sender.id === chrome.runtime.id;
  if (!trustedMessage) return false;

  let operation = null;
  if (message?.type === "APP_COMMAND") {
    operation = appCommand(message.command);
  } else if (message?.type === "START_DOWNLOAD") {
    operation = createDownloadJob(message.candidate, message.page);
  } else if (message?.type === "ENSURE_JOB_STARTED" && typeof message.jobId === "string") {
    operation = ensureJobStarted(message.jobId);
  } else if (message?.type === "GET_JOB" && typeof message.jobId === "string") {
    operation = getJobResponse(message.jobId);
  } else if (message?.type === "GET_CURRENT_JOB") {
    operation = currentJobResponse();
  } else if (message?.type === "CANCEL_JOB" && typeof message.jobId === "string") {
    operation = cancelJob(message.jobId);
  } else if (message?.type === "DELETE_JOB" && typeof message.jobId === "string") {
    operation = deleteJob(message.jobId);
  } else if (message?.type === "SAVE_JOB_SNAPSHOT" && typeof message.jobId === "string") {
    operation = saveJobSnapshot(message.jobId, message.snapshot).then((job) => ({ ok: Boolean(job), job }));
  } else if (message?.type === "OPEN_JOB_PROGRESS" && typeof message.jobId === "string") {
    operation = chrome.tabs
      .create({ url: chrome.runtime.getURL(`progress.html?job=${encodeURIComponent(message.jobId)}`) })
      .then((tab) => ({ ok: true, tabId: tab.id }));
  }

  if (operation) {
    operation
      .then((response) => sendResponse(response))
      .catch((error) => {
        console.error("StreamDock action failed", error);
        sendResponse({
          ok: false,
          state: "error",
          message: "Не удалось открыть страницу загрузки. Перезагрузите расширение и попробуйте снова."
        });
      });
    return true;
  }

  return false;
});

void resumePendingCancellations();
