const assert = require("assert");
const cryptoModule = require("crypto");
const fs = require("fs");
const vm = require("vm");

function listenerSlot(onAdd = () => {}) {
  return { addListener: onAdd };
}

function storageArea() {
  const data = {};
  return {
    data,
    async get(keys) {
      if (keys == null) return { ...data };
      const names = Array.isArray(keys) ? keys : [keys];
      return Object.fromEntries(names.filter((name) => name in data).map((name) => [name, data[name]]));
    },
    async set(values) {
      Object.assign(data, values);
    },
    async remove(keys) {
      for (const key of Array.isArray(keys) ? keys : [keys]) delete data[key];
    },
    async setAccessLevel() {}
  };
}

async function main() {
  const local = storageArea();
  const session = storageArea();
  const createdTabs = [];
  const requests = [];
  const cancelledBackendTasks = new Set();
  let messageHandler = null;
  let failTabCreation = false;

  function mockResponse(status, body = {}) {
    return {
      ok: status >= 200 && status < 300,
      status,
      async json() {
        return body;
      }
    };
  }

  const chrome = {
    runtime: {
      id: "test-extension",
      getURL: (path) => `chrome-extension://test-extension/${path}`,
      connectNative() {
        throw new Error("Native Messaging не нужен в этом сценарии");
      },
      onMessage: listenerSlot((handler) => {
        messageHandler = handler;
      }),
      onInstalled: listenerSlot(),
      onStartup: listenerSlot()
    },
    storage: { local, session },
    tabs: {
      async create(options) {
        if (failTabCreation) throw new Error("Тестовая ошибка открытия вкладки");
        createdTabs.push(options);
        return { id: 77, ...options };
      },
      async get() {
        return { url: "https://training.example/lesson", title: "Учебный эфир" };
      },
      onUpdated: listenerSlot(),
      onRemoved: listenerSlot()
    },
    action: {
      async setBadgeBackgroundColor() {},
      async setBadgeText() {}
    },
    webRequest: {
      onBeforeSendHeaders: listenerSlot(),
      onHeadersReceived: listenerSlot(),
      onCompleted: listenerSlot(),
      onErrorOccurred: listenerSlot()
    }
  };

  const context = {
    chrome,
    URL,
    crypto: cryptoModule.webcrypto,
    fetch: async (url, options = {}) => {
      requests.push({ url, options });
      if (url === "http://127.0.0.1:8765/api/extension/download") {
        const payload = JSON.parse(options.body || "{}");
        const taskId = payload.client_request_id === "recover-job"
          ? "backend-task-recover"
          : "backend-task-1";
        return mockResponse(202, { task_id: taskId });
      }
      const taskMatch = url.match(/\/api\/tasks\/([^/]+)(\/cancel)?$/);
      if (!taskMatch) throw new Error(`Неожиданный запрос: ${url}`);
      const taskId = decodeURIComponent(taskMatch[1]);
      if (taskMatch[2] && options.method === "POST") {
        return mockResponse(200, { id: taskId, status: "running", stage: "cancelling" });
      }
      if (options.method === "DELETE") {
        cancelledBackendTasks.add(taskId);
        return mockResponse(204);
      }
      await new Promise((resolve) => setTimeout(resolve, 5));
      return mockResponse(200, { id: taskId, status: "cancelled", stage: "cancelled" });
    },
    setTimeout,
    clearTimeout,
    console: { log: console.log, warn: console.warn, error() {} }
  };

  const backgroundPath = require("path").resolve(__dirname, "..", "browser-extension", "background.js");
  vm.runInNewContext(fs.readFileSync(backgroundPath, "utf8"), context, { filename: backgroundPath });
  assert.equal(typeof messageHandler, "function", "background.js не зарегистрировал обработчик сообщений");

  const candidateUrls = [
    "https://kinescope.io/video-id/master.m3u8",
    "https://kinescope.io/video-id/media.m3u8?quality=1080&type=video",
    "https://kinescope.io/video-id/media.m3u8?quality=1080&type=audio&lang=und"
  ];
  const savedCandidatesResponse = await new Promise((resolve) => {
    const keepChannelOpen = messageHandler(
      {
        type: "SAVE_CANDIDATES",
        tabId: 42,
        candidates: candidateUrls.map((url) => ({ url }))
      },
      { id: "test-extension" },
      resolve
    );
    assert.equal(keepChannelOpen, true, "канал сохранения кандидатов был закрыт раньше времени");
  });
  assert.equal(savedCandidatesResponse.ok, true);
  const savedCandidates = session.data["media-tab-42"];
  assert.equal(savedCandidates.length, candidateUrls.length, "параллельная запись потеряла видеопоток");
  assert.equal(savedCandidates[0].url, candidateUrls[0], "master.m3u8 должен оставаться первым");
  assert.deepEqual(
    savedCandidates.map((candidate) => candidate.url).sort(),
    [...candidateUrls].sort(),
    "список потоков изменился при параллельной записи"
  );

  const response = await new Promise((resolve) => {
    const keepChannelOpen = messageHandler(
      {
        type: "START_DOWNLOAD",
        candidate: {
          url: "https://media.example/master.m3u8?token=secret-value",
          kind: "hls",
          pageTitle: "Учебный эфир",
          pageUrl: "https://training.example/lesson",
          requestHeaders: { Cookie: "session=private", Referer: "https://training.example/lesson" }
        },
        page: { title: "Учебный эфир", url: "https://training.example/lesson" }
      },
      { id: "test-extension" },
      resolve
    );
    assert.equal(keepChannelOpen, true, "канал ответа popup был закрыт раньше времени");
  });

  assert.equal(response.ok, true, response.message || "создание загрузки завершилось ошибкой");
  assert.equal(createdTabs.length, 1, "страница прогресса не была открыта");
  assert.match(createdTabs[0].url, /^chrome-extension:\/\/test-extension\/progress\.html\?job=/);

  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(requests.length, 1, "backend должен получить ровно один запрос");
  assert.equal(requests[0].url, "http://127.0.0.1:8765/api/extension/download");
  const requestBody = JSON.parse(requests[0].options.body);
  assert.equal(requestBody.request_headers.Cookie, "session=private");
  assert.equal(requestBody.client_request_id, response.jobId);

  const jobs = local.data.downloadJobs;
  assert.equal(jobs[response.jobId].sourceHost, "media.example");
  assert.equal(jobs[response.jobId].taskId, "backend-task-1");
  assert.doesNotMatch(JSON.stringify(local.data), /secret-value|session=private/);
  assert.equal(
    Object.keys(session.data).filter((key) => key.startsWith("download-secret-")).length,
    0,
    "секреты должны удаляться после передачи backend"
  );

  local.data.downloadJobs["other-job"] = {
    id: "other-job",
    title: "Другая загрузка",
    createdAt: Date.now() - 1000
  };
  session.data[`download-secret-${response.jobId}`] = { token: "temporary" };
  const cancellationPromise = new Promise((resolve) => {
    const keepChannelOpen = messageHandler(
      { type: "CANCEL_JOB", jobId: response.jobId },
      { id: "test-extension" },
      resolve
    );
    assert.equal(keepChannelOpen, true, "канал отмены задачи был закрыт раньше времени");
  });

  await new Promise((resolve) => setTimeout(resolve, 1));
  const staleSnapshotPromise = new Promise((resolve) => {
    messageHandler(
      {
        type: "SAVE_JOB_SNAPSHOT",
        jobId: response.jobId,
        snapshot: { status: "running", stage: "downloading", progress: 55 }
      },
      { id: "test-extension" },
      resolve
    );
  });
  const [cancelledJobResponse] = await Promise.all([cancellationPromise, staleSnapshotPromise]);
  assert.equal(cancelledJobResponse.ok, true);
  assert.equal(cancelledJobResponse.cancelled, true);
  assert.equal(local.data.downloadJobs[response.jobId], undefined, "отменённая задача осталась в local storage");
  assert.equal(local.data.currentDownloadJobId, "other-job", "указатель не переключился на оставшуюся задачу");
  assert.equal(session.data[`download-secret-${response.jobId}`], undefined, "секрет отменённой задачи не удалён");
  assert.ok(session.data[`cancelled-job-${response.jobId}`], "не сохранена отметка для уже открытой страницы прогресса");
  assert.ok(local.data.downloadJobs["other-job"], "удаление затронуло другую задачу");
  assert.ok(cancelledBackendTasks.has("backend-task-1"), "backend-задача не была удалена после отмены");
  assert.ok(
    requests.some((request) => request.url.endsWith("/backend-task-1/cancel") && request.options.method === "POST"),
    "background не отправил команду отмены backend"
  );
  assert.ok(
    requests.some((request) => request.url.endsWith("/backend-task-1") && request.options.method === "DELETE"),
    "background не удалил terminal backend-задачу"
  );

  const cancelledLookup = await new Promise((resolve) => {
    messageHandler(
      { type: "GET_JOB", jobId: response.jobId },
      { id: "test-extension" },
      resolve
    );
  });
  assert.equal(cancelledLookup.cancelled, true, "исчезнувшая отменённая задача должна распознаваться как успешно очищенная");

  await new Promise((resolve) => {
    messageHandler(
      {
        type: "SAVE_JOB_SNAPSHOT",
        jobId: response.jobId,
        snapshot: { status: "running", stage: "downloading", progress: 80 }
      },
      { id: "test-extension" },
      resolve
    );
  });
  assert.equal(local.data.downloadJobs[response.jobId], undefined, "устаревший snapshot воскресил удалённую задачу");

  const repeatedDeleteResponse = await new Promise((resolve) => {
    messageHandler(
      { type: "CANCEL_JOB", jobId: response.jobId },
      { id: "test-extension" },
      resolve
    );
  });
  assert.equal(repeatedDeleteResponse.ok, true, "повторная отмена должна быть безопасной");
  assert.equal(repeatedDeleteResponse.cancelled, true);

  local.data.downloadJobs["resume-job"] = {
    id: "resume-job",
    title: "Возобновляемая отмена",
    taskId: "backend-task-resume",
    status: "running",
    stage: "cancelling",
    createdAt: Date.now()
  };
  local.data.currentDownloadJobId = "resume-job";
  const resumedLookup = await new Promise((resolve) => {
    messageHandler(
      { type: "GET_JOB", jobId: "resume-job" },
      { id: "test-extension" },
      resolve
    );
  });
  assert.equal(resumedLookup.cancelled, true, "GET_JOB не возобновил прерванную очистку");
  assert.equal(local.data.downloadJobs["resume-job"], undefined, "возобновлённая отмена не удалила локальную запись");
  assert.ok(cancelledBackendTasks.has("backend-task-resume"), "возобновлённая отмена не очистила backend-задачу");

  local.data.downloadJobs["recover-job"] = {
    id: "recover-job",
    title: "Отмена после перезапуска",
    taskId: null,
    status: "running",
    stage: "cancelling",
    createdAt: Date.now()
  };
  local.data.currentDownloadJobId = "recover-job";
  session.data["download-secret-recover-job"] = {
    stream_url: "https://media.example/recover-master.m3u8",
    page_url: "https://training.example/recover",
    title: "Отмена после перезапуска",
    stream_kind: "hls",
    request_headers: {},
    client_request_id: "recover-job"
  };
  const recoveredLookup = await new Promise((resolve) => {
    messageHandler(
      { type: "GET_JOB", jobId: "recover-job" },
      { id: "test-extension" },
      resolve
    );
  });
  assert.equal(recoveredLookup.cancelled, true, "отмена без сохранённого taskId не была восстановлена");
  assert.equal(local.data.downloadJobs["recover-job"], undefined);
  assert.equal(session.data["download-secret-recover-job"], undefined);
  assert.ok(cancelledBackendTasks.has("backend-task-recover"), "восстановленная backend-задача не очищена");

  failTabCreation = true;
  const failedTabResponse = await new Promise((resolve) => {
    messageHandler(
      {
        type: "START_DOWNLOAD",
        candidate: { url: "https://media.example/second-master.m3u8", kind: "hls" },
        page: { title: "Второй эфир", url: "https://training.example/lesson/2" }
      },
      { id: "test-extension" },
      resolve
    );
  });
  assert.equal(failedTabResponse.ok, false);
  assert.doesNotMatch(JSON.stringify(session.data), /second-master/);
  const failedJobs = Object.values(local.data.downloadJobs).filter((job) => job.title === "Второй эфир");
  assert.equal(failedJobs.length, 1);
  assert.equal(failedJobs[0].status, "failed");

  process.stdout.write("background download flow ok\n");
}

main().catch((error) => {
  console.error(error.stack || error.message || error);
  process.exitCode = 1;
});
