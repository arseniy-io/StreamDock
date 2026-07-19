const APP_URL = "http://127.0.0.1:8765";
const connectionCard = document.querySelector("#connectionCard");
const connectionTitle = document.querySelector("#connectionTitle");
const connectionText = document.querySelector("#connectionText");
const refreshButton = document.querySelector("#refreshButton");
const startButton = document.querySelector("#startButton");
const stopButton = document.querySelector("#stopButton");
const openAppButton = document.querySelector("#openAppButton");
const activeJobCard = document.querySelector("#activeJobCard");
const activeJobTitle = document.querySelector("#activeJobTitle");
const activeJobText = document.querySelector("#activeJobText");
const openProgressButton = document.querySelector("#openProgressButton");
const streamList = document.querySelector("#streamList");
const streamCount = document.querySelector("#streamCount");
const emptyState = document.querySelector("#emptyState");

let appOnline = false;
let appManaged = false;
let activeTab = null;
let currentDownloadJob = null;
let renderedCandidates = [];

function setConnectionState(result) {
  const state = result?.state || "error";
  appOnline = state === "running";
  appManaged = appOnline && result?.managed === true;

  connectionCard.className = "connection-card";
  const stateClass = appOnline && !appManaged ? "unmanaged" : state === "helper_missing" ? "error" : state;
  connectionCard.classList.add(`is-${stateClass}`);

  const labels = {
    checking: ["Проверяем приложение", "Связываемся с локальным помощником"],
    starting: ["Запускаем StreamDock", "Обычно это занимает несколько секунд"],
    stopping: ["Останавливаем StreamDock", "Завершаем процессы этого проекта"],
    stopped: ["StreamDock остановлен", "Нажмите «Запустить», чтобы начать работу"],
    helper_missing: ["Нужна однократная установка", result?.message],
    error: ["Не удалось выполнить действие", result?.message],
    running: appManaged
      ? ["StreamDock запущен", result?.message || "Можно скачивать видео"]
      : ["StreamDock запущен", result?.message || "Скачивание и остановка доступны"]
  };
  const [title, text] = labels[state] || labels.error;
  connectionTitle.textContent = title;
  connectionText.textContent = text || "Попробуйте ещё раз";

  const busy = state === "checking" || state === "starting" || state === "stopping";
  startButton.hidden = appOnline;
  startButton.disabled = busy || state === "helper_missing";
  startButton.textContent = state === "starting" ? "Запускаем..." : state === "error" ? "Повторить" : "Запустить";
  stopButton.hidden = !appOnline;
  stopButton.disabled = busy;
  stopButton.textContent = state === "stopping" ? "Останавливаем..." : "Остановить";
  openAppButton.disabled = !appOnline;

  renderCandidates(renderedCandidates);
}

function kindLabel(kind) {
  return {
    hls: "HLS",
    dash: "DASH",
    video: "Видео",
    audio: "Аудио",
    unknown: "Поток"
  }[kind] || "Поток";
}

function safeHost(value) {
  try {
    return new URL(value).hostname;
  } catch {
    return "Источник видео";
  }
}

function isCompletePlaylist(candidate) {
  try {
    const path = new URL(candidate.url).pathname.toLowerCase();
    return path.endsWith("/master.m3u8") || path.endsWith("/master.mpd") || path.endsWith("/manifest.mpd");
  } catch {
    return false;
  }
}

function downloadCandidates(candidates) {
  const completePlaylists = candidates.filter(isCompletePlaylist);
  return completePlaylists.length > 0 ? completePlaylists : candidates;
}

function scanPageForMedia() {
  const urls = new Set();
  const add = (value) => {
    if (typeof value !== "string") return;
    try {
      const parsed = new URL(value, document.baseURI);
      if (parsed.protocol === "http:" || parsed.protocol === "https:") urls.add(parsed.href);
    } catch {
      // Неполные и blob-адреса не передаются локальному приложению.
    }
  };

  document.querySelectorAll("video, audio, source").forEach((element) => {
    add(element.currentSrc);
    add(element.src);
  });
  performance.getEntriesByType("resource").forEach((entry) => {
    if (/\.(m3u8|mpd|mp4|webm|m4v|mov|m4a|aac)(?:$|[?#])/i.test(entry.name)) add(entry.name);
  });

  return [...urls].slice(0, 12).map((url) => ({ url }));
}

async function collectPageCandidates() {
  if (!activeTab?.id || !activeTab.url?.startsWith("http")) return;
  try {
    const results = await chrome.scripting.executeScript({
      target: { tabId: activeTab.id, allFrames: true },
      func: scanPageForMedia
    });
    const candidates = results.flatMap((result) => result.result || []);
    if (candidates.length > 0) {
      await chrome.runtime.sendMessage({
        type: "SAVE_CANDIDATES",
        tabId: activeTab.id,
        candidates
      });
    }
  } catch {
    // Системные страницы Chrome запрещают выполнение скриптов.
  }
}

async function listCandidates() {
  if (!activeTab?.id) return [];
  const response = await chrome.runtime.sendMessage({ type: "LIST_CANDIDATES", tabId: activeTab.id });
  return Array.isArray(response?.candidates) ? response.candidates : [];
}

function renderCandidates(candidates) {
  renderedCandidates = Array.isArray(candidates) ? candidates : [];
  streamList.replaceChildren();
  streamCount.textContent = String(renderedCandidates.length);
  emptyState.hidden = renderedCandidates.length > 0;

  renderedCandidates.forEach((candidate) => {
    const completePlaylist = isCompletePlaylist(candidate);
    const card = document.createElement("article");
    card.className = "stream-card";

    const top = document.createElement("div");
    top.className = "stream-card__top";

    const host = document.createElement("span");
    host.className = "stream-card__host";
    host.textContent = safeHost(candidate.url);

    const kind = document.createElement("span");
    kind.className = completePlaylist ? "kind-badge is-recommended" : "kind-badge";
    kind.textContent = completePlaylist ? "Видео + звук" : kindLabel(candidate.kind);

    const url = document.createElement("p");
    url.className = "stream-card__url";
    url.textContent = candidate.url;
    url.title = candidate.url;

    const button = document.createElement("button");
    button.className = "primary-button";
    button.type = "button";
    button.textContent = completePlaylist ? "Скачать в хорошем качестве" : "Скачать видео";
    button.disabled = !appOnline;
    button.addEventListener("click", () => startDownload(candidate, button));

    top.append(host, kind);
    card.append(top, url);
    if (completePlaylist) {
      const recommendation = document.createElement("p");
      recommendation.className = "stream-card__recommendation";
      recommendation.textContent = "Рекомендуемый поток: приложение объединит лучшее изображение и звук.";
      card.append(recommendation);
    }
    if (candidate.kind === "dash") {
      const warning = document.createElement("p");
      warning.className = "stream-card__warning";
      warning.textContent = "Защищённые DRM-потоки не поддерживаются.";
      card.append(warning);
    }
    card.append(button);
    streamList.append(card);
  });
}

async function checkApp() {
  setConnectionState({ state: "checking", message: "Проверяем приложение" });
  if (!globalThis.chrome?.runtime?.sendMessage) {
    try {
      const response = await fetch(`${APP_URL}/api/health`, { cache: "no-store" });
      setConnectionState(response.ok ? { state: "running", managed: false } : { state: "stopped" });
    } catch {
      setConnectionState({ state: "stopped" });
    }
    return;
  }
  const result = await chrome.runtime.sendMessage({ type: "APP_COMMAND", command: "status" });
  setConnectionState(result);
}

function isActiveJob(job) {
  return Boolean(job && !["completed", "failed", "cancelled"].includes(job.status));
}

async function loadCurrentJob() {
  if (!globalThis.chrome?.runtime?.sendMessage) return;
  const response = await chrome.runtime.sendMessage({ type: "GET_CURRENT_JOB" });
  currentDownloadJob = response?.job || null;
  activeJobCard.hidden = !isActiveJob(currentDownloadJob);
  if (!activeJobCard.hidden) {
    activeJobTitle.textContent = currentDownloadJob.status === "disconnected" ? "Загрузка ждёт подключения" : "Загрузка продолжается";
    activeJobText.textContent = currentDownloadJob.title || "Откройте постоянную страницу, чтобы видеть прогресс.";
  }
}

async function runAppCommand(command) {
  if (command === "stop" && isActiveJob(currentDownloadJob)) {
    const confirmed = window.confirm("Остановка отменит текущую загрузку и завершит процессы StreamDock. Остановить?");
    if (!confirmed) return;
  }

  setConnectionState({ state: command === "start" ? "starting" : "stopping" });
  const result = await chrome.runtime.sendMessage({ type: "APP_COMMAND", command });
  setConnectionState(result);
  if (result?.state === "running") {
    await refreshCandidates();
  }
}

async function startDownload(candidate, button) {
  button.disabled = true;
  button.textContent = "Открываем загрузку...";
  try {
    const response = await chrome.runtime.sendMessage({
      type: "START_DOWNLOAD",
      candidate,
      page: {
        url: activeTab?.url || null,
        title: activeTab?.title || "Видео со страницы"
      }
    });
    if (response?.ok) {
      window.close();
      return;
    }
    connectionCard.className = "connection-card is-error";
    connectionTitle.textContent = "Не удалось открыть загрузку";
    connectionText.textContent = response?.message || "Обновите страницу и попробуйте снова";
  } catch {
    connectionCard.className = "connection-card is-error";
    connectionTitle.textContent = "Расширение нужно обновить";
    connectionText.textContent = "Откройте chrome://extensions, перезагрузите StreamDock и повторите скачивание.";
  }
  button.disabled = !appOnline;
  button.textContent = "Повторить скачивание";
}

async function refreshCandidates() {
  await collectPageCandidates();
  renderCandidates(downloadCandidates(await listCandidates()));
}

async function refresh() {
  refreshButton.disabled = true;
  refreshButton.setAttribute("aria-busy", "true");
  await Promise.all([checkApp(), refreshCandidates(), loadCurrentJob()]);
  refreshButton.disabled = false;
  refreshButton.removeAttribute("aria-busy");
}

refreshButton.addEventListener("click", refresh);
startButton.addEventListener("click", () => runAppCommand("start"));
stopButton.addEventListener("click", () => runAppCommand("stop"));
openAppButton.addEventListener("click", () => chrome.tabs.create({ url: APP_URL }));
openProgressButton.addEventListener("click", async () => {
  if (!currentDownloadJob?.id) return;
  await chrome.runtime.sendMessage({ type: "OPEN_JOB_PROGRESS", jobId: currentDownloadJob.id });
  window.close();
});

async function initialize() {
  if (!globalThis.chrome?.tabs?.query) {
    await checkApp();
    if (new URLSearchParams(location.search).get("preview") === "long") {
      renderCandidates([
        {
          url: "https://very-long-media-domain.example.com/training/course/lesson/live/master.m3u8?temporary_access_token=preview-value-with-a-very-long-address",
          kind: "hls"
        }
      ]);
    } else {
      emptyState.hidden = false;
    }
    return;
  }
  [activeTab] = await chrome.tabs.query({ active: true, currentWindow: true });
  await refresh();
}

void initialize();
