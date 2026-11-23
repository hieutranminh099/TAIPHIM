"use strict";

const byId = (id) => document.getElementById(id);
const links = byId("links");
const title = byId("title");
const directory = byId("directory");
const discover = byId("discover");
const discoverAll = byId("discover-all");
const selectAll = byId("select-all");
const rangeStart = byId("range-start");
const rangeEnd = byId("range-end");
const applyRange = byId("apply-range");
const enqueue = byId("enqueue");
const episodeList = byId("episode-list");
const jobList = byId("job-list");
const sourceMessages = byId("source-messages");
const rangeMessage = byId("range-message");
const enqueueMessage = byId("enqueue-message");
const queueMessage = byId("queue-message");

const statusLabels = {
  queued: "Đang chờ",
  running: "Đang tải",
  cancelling: "Đang hủy",
  completed: "Hoàn tất",
  cancelled: "Đã hủy",
  failed: "Lỗi",
};
const formatLabels = { mp4: "MP4", hls: "HLS", webm: "WebM", mov: "MOV" };
const numberFormat = new Intl.NumberFormat("vi-VN", { maximumFractionDigits: 1 });
const selected = new Set();
const jobRows = new Map();
const busyJobs = new Set();

let episodes = [];
let episodeRows = [];
let jobs = [];
let manualTitle = false;
let discovering = false;
let enqueueing = false;
let configLoaded = false;
let queueVersion = 0;
let pendingMutations = 0;
let mutationChain = Promise.resolve();

function element(tag, className, value) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (value !== undefined) node.textContent = value;
  return node;
}

function setText(node, value) {
  const text = String(value);
  if (node.textContent !== text) node.textContent = text;
}

function showNotice(node, message, appearance = "warning") {
  node.classList.toggle("warning", appearance === "warning");
  node.classList.toggle("success", appearance === "success");
  node.hidden = !message;
  setText(node, message || "");
}

async function request(path, payload) {
  const options = { credentials: "omit", cache: "no-store" };
  if (payload !== undefined) {
    options.method = "POST";
    options.headers = { "Content-Type": "application/json" };
    options.body = JSON.stringify(payload);
  }

  let response;
  try {
    response = await fetch(path, options);
  } catch {
    throw new Error("Mất kết nối với ứng dụng trên máy. Hãy kiểm tra cửa sổ đang chạy ứng dụng.");
  }
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = data?.detail;
    let message = "Ứng dụng không xử lý được yêu cầu (HTTP " + response.status + ").";
    if (typeof detail === "string" && detail) message = detail;
    if (Array.isArray(detail)) message = "Dữ liệu gửi lên không hợp lệ. Vui lòng kiểm tra các mục đã nhập.";
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  if (!data) throw new Error("Ứng dụng trả về dữ liệu không hợp lệ.");
  return data;
}

function unitCount(value, unit) {
  return numberFormat.format(value) + " " + unit;
}

function episodeNumber(number) {
  return String(number).padStart(2, "0");
}

function sourceLabel(episode, caption) {
  if (episode.label && episode.label !== caption) return episode.label;
  const url = new URL(episode.url);
  const basename = url.pathname.split("/").filter(Boolean).pop();
  if (!basename) return url.hostname;
  try {
    return decodeURIComponent(basename);
  } catch {
    return basename;
  }
}

function formatBytes(count) {
  let size = Number.isFinite(count) && count >= 0 ? count : 0;
  const units = ["B", "KB", "MB", "GB", "TB"];
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return unitCount(size, units[unit]);
}

function updateSelection() {
  for (const { episode, row, checkbox } of episodeRows) {
    checkbox.checked = selected.has(episode);
    row.classList.toggle("selected", checkbox.checked);
  }
  const hasEpisodes = episodes.length > 0;
  selectAll.disabled = !hasEpisodes;
  rangeStart.disabled = !hasEpisodes;
  rangeEnd.disabled = !hasEpisodes;
  applyRange.disabled = !hasEpisodes;
  enqueue.disabled = selected.size === 0 || enqueueing || discovering;
  discover.disabled = discoverAll.disabled = discovering || enqueueing;
  setText(selectAll, hasEpisodes && selected.size === episodes.length ? "Bỏ chọn tất cả" : "Chọn tất cả");
  setText(byId("selection-count"), "Đã chọn " + unitCount(selected.size, "tập"));
  setText(byId("enqueue-label"), enqueueing ? "Đang thêm…" : "Tải các tập đã chọn");
}

function loadEpisodes(items) {
  episodes = [...items].sort((left, right) => left.number - right.number);
  selected.clear();
  const fragment = document.createDocumentFragment();
  episodeRows = episodes.map((episode) => {
    const item = element("li");
    const row = element("label", "episode-row");
    const checkbox = element("input");
    checkbox.type = "checkbox";
    const caption = "Tập " + episodeNumber(episode.number);
    row.append(checkbox, element("span", "episode-name", caption));
    const description = sourceLabel(episode, caption);
    const label = element("span", "episode-label", description);
    label.title = description;
    label.dir = "auto";
    row.append(label);
    row.append(element("span", "file-kind", formatLabels[episode.kind] || "Video"));
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) selected.add(episode);
      else selected.delete(episode);
      showNotice(rangeMessage, "");
      updateSelection();
    });
    item.append(row);
    fragment.append(item);
    return { episode, row, checkbox };
  });
  episodeList.replaceChildren(fragment);
  episodeList.hidden = episodes.length === 0;
  byId("episodes-empty").hidden = episodes.length > 0;
  setText(byId("episode-count"), unitCount(episodes.length, "tập"));
  rangeStart.value = "";
  rangeEnd.value = "";
  showNotice(rangeMessage, "");
  updateSelection();
}

function setSourceMessages(messages) {
  sourceMessages.replaceChildren(...messages.map((message) => element("li", "", message)));
  sourceMessages.hidden = messages.length === 0;
}

title.addEventListener("input", () => {
  manualTitle = Boolean(title.value.trim());
});

async function findEpisodes(downloadAll = false) {
  if (discovering || enqueueing) return;
  const source = links.value.trim();
  if (!source) {
    setSourceMessages(["Hãy dán ít nhất một liên kết."]);
    links.focus();
    return;
  }

  discovering = true;
  const label = downloadAll ? byId("discover-all-label") : discover.lastChild;
  const idleLabel = label.textContent;
  setText(label, "Đang tìm…");
  setSourceMessages([]);
  showNotice(enqueueMessage, "");
  updateSelection();

  try {
    const result = await request("/api/discover", { links: source });
    loadEpisodes(result.episodes);
    setSourceMessages(result.messages);
    if (!manualTitle) {
      title.value = result.episodes.length ? result.title || "" : "";
    }
    if (downloadAll && episodes.length) {
      for (const episode of episodes) selected.add(episode);
      setText(label, "Đang thêm…");
      await enqueueEpisodes(episodes);
    }
  } catch (error) {
    loadEpisodes([]);
    setSourceMessages([error.message]);
  } finally {
    discovering = false;
    setText(label, idleLabel);
    updateSelection();
  }
}

byId("source-form").addEventListener("submit", (event) => {
  event.preventDefault();
  return findEpisodes();
});
discoverAll.addEventListener("click", () => findEpisodes(true));

selectAll.addEventListener("click", () => {
  if (selected.size === episodes.length) selected.clear();
  else for (const episode of episodes) selected.add(episode);
  showNotice(rangeMessage, "");
  updateSelection();
});

byId("range-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const first = rangeStart.valueAsNumber;
  const last = rangeEnd.valueAsNumber;
  if (!Number.isSafeInteger(first) || !Number.isSafeInteger(last) || first < 1 || first > last) {
    showNotice(rangeMessage, "Nhập hai số tập dương, với tập bắt đầu nhỏ hơn hoặc bằng tập kết thúc.");
    return;
  }
  const matches = episodes.filter((episode) => episode.number >= first && episode.number <= last);
  if (!matches.length) {
    showNotice(rangeMessage, "Không có tập nào trong khoảng đã chọn.");
    return;
  }
  selected.clear();
  for (const episode of matches) selected.add(episode);
  showNotice(rangeMessage, "");
  updateSelection();
});

function mutation(action) {
  pendingMutations += 1;
  queueVersion += 1;
  const result = mutationChain.then(action);
  mutationChain = result.catch(() => {});
  return result.finally(() => {
    pendingMutations -= 1;
  });
}

function mergeJobs(updates) {
  const next = new Map(jobs.map((job) => [job.id, job]));
  for (const job of updates) next.set(job.id, job);
  jobs = [...next.values()];
  renderJobs();
}

async function enqueueEpisodes(selectedEpisodes) {
  if (!selectedEpisodes.length || enqueueing) return;
  if (!directory.value.trim()) {
    showNotice(enqueueMessage, "Nhập đường dẫn thư mục lưu trên máy.");
    directory.focus();
    return;
  }

  const payload = { title: title.value.trim() || "Tên phim", directory: directory.value.trim(), episodes: selectedEpisodes };
  enqueueing = true;
  showNotice(enqueueMessage, "");
  updateSelection();
  try {
    await mutation(async () => {
      const result = await request("/api/jobs", payload);
      mergeJobs(result.jobs);
    });
    showNotice(enqueueMessage, "Đã thêm " + unitCount(selectedEpisodes.length, "tập") + " vào hàng đợi.", "success");
  } catch (error) {
    showNotice(enqueueMessage, error.message);
  } finally {
    enqueueing = false;
    updateSelection();
  }
}

enqueue.addEventListener("click", () => {
  if (discovering) return;
  return enqueueEpisodes(episodes.filter((episode) => selected.has(episode)));
});

function newJobRow(id) {
  const row = element("li", "job-row");
  row.tabIndex = -1;
  const top = element("div", "job-top");
  const caption = element("strong");
  const status = element("span", "job-status");
  top.append(caption, status);

  const details = element("div", "job-meta");
  const size = element("span");
  const metric = element("span");
  details.append(size, metric);
  const progress = element("progress", "job-progress");
  progress.max = 100;

  const filename = element("p", "job-filename");
  const error = element("p", "job-error");
  const controls = element("div", "job-controls");
  const action = element("button", "button secondary compact");
  action.type = "button";
  action.addEventListener("click", () => updateJob(id, action.dataset.action));
  controls.append(action);
  row.append(top, details, progress, filename, error, controls);
  return { row, caption, status, size, metric, progress, filename, error, controls, action };
}

function updateJobRow(job, nodes) {
  const label = job.title + " - Tập " + episodeNumber(job.number);
  setText(nodes.caption, label);
  nodes.caption.title = label;
  setText(nodes.status, statusLabels[job.status] || job.status);
  nodes.status.className = "job-status " + job.status;

  const totalKnown = Number.isFinite(job.total) && job.total > 0;
  const downloaded = formatBytes(job.downloaded);
  setText(nodes.size, totalKnown ? downloaded + " / " + formatBytes(job.total) : downloaded);
  const percent = totalKnown ? Math.min(100, Math.floor(100 * job.downloaded / job.total)) : null;
  let metric = percent === null ? "" : percent + "%";
  if (job.status === "running" && job.speed > 0) {
    metric = (metric ? metric + " · " : "") + formatBytes(job.speed) + "/s";
  }
  setText(nodes.metric, metric);

  const working = job.status === "running" || job.status === "cancelling";
  nodes.progress.hidden = !totalKnown && !working;
  nodes.progress.setAttribute("aria-label", "Tiến độ tải " + label);
  if (percent === null) nodes.progress.removeAttribute("value");
  else nodes.progress.value = percent;

  setText(nodes.filename, job.filename || "");
  nodes.filename.title = job.filename || "";
  nodes.filename.hidden = !job.filename;
  setText(nodes.error, job.error || "");
  nodes.error.hidden = !job.error;

  const cancel = job.status === "queued" || job.status === "running";
  const retry = job.status === "failed" || job.status === "cancelled";
  if (!cancel && !retry && nodes.action === document.activeElement) nodes.row.focus();
  nodes.controls.hidden = !cancel && !retry;
  nodes.action.disabled = busyJobs.has(job.id);
  nodes.action.dataset.action = cancel ? "cancel" : "retry";
  setText(nodes.action, cancel ? "Hủy" : "Thử lại");
  nodes.action.setAttribute("aria-label", nodes.action.textContent + " " + label);
}

function renderJobs() {
  let running = 0;
  let queued = 0;
  let completed = 0;
  const liveIds = new Set();
  let cursor = jobList.firstElementChild;

  for (const job of jobs) {
    liveIds.add(job.id);
    if (job.status === "running" || job.status === "cancelling") running += 1;
    if (job.status === "queued") queued += 1;
    if (job.status === "completed") completed += 1;

    let nodes = jobRows.get(job.id);
    if (!nodes) {
      nodes = newJobRow(job.id);
      jobRows.set(job.id, nodes);
    }
    updateJobRow(job, nodes);
    if (nodes.row !== cursor) jobList.insertBefore(nodes.row, cursor);
    cursor = nodes.row.nextElementSibling;
  }

  for (const [id, nodes] of jobRows) {
    if (!liveIds.has(id)) {
      nodes.row.remove();
      jobRows.delete(id);
    }
  }

  jobList.hidden = jobs.length === 0;
  byId("queue-empty").hidden = jobs.length > 0;
  setText(byId("queue-count"), unitCount(jobs.length, "lượt tải"));
  setText(byId("running-count"), numberFormat.format(running));
  setText(byId("queued-count"), numberFormat.format(queued));
  setText(byId("completed-count"), numberFormat.format(completed));
}

async function updateJob(id, action) {
  if (busyJobs.has(id)) return;
  busyJobs.add(id);
  renderJobs();
  showNotice(queueMessage, "");

  try {
    await mutation(async () => {
      const result = await request("/api/jobs/" + encodeURIComponent(id) + "/" + action, {});
      mergeJobs([result.job]);
    });
  } catch (error) {
    showNotice(queueMessage, error.message);
  } finally {
    busyJobs.delete(id);
    renderJobs();
  }
}

function setConnection(online) {
  const status = byId("service-status");
  status.classList.toggle("online", online);
  status.classList.toggle("offline", !online);
  setText(byId("service-label"), online ? "Máy chủ cục bộ" : "Mất kết nối");
}

async function loadConfig() {
  const config = await request("/api/config");
  if (!directory.value.trim()) directory.value = config.directory;
  videoImport.configure(config);
  byId("ffmpeg-notice").hidden = Boolean(config.ffmpeg);
  setText(byId("concurrency"), unitCount(config.concurrency, "lượt cùng lúc"));
  configLoaded = true;
}

async function poll() {
  try {
    if (!configLoaded) await loadConfig();
    if (!pendingMutations) {
      const version = queueVersion;
      const result = await request("/api/jobs");
      if (version === queueVersion && !pendingMutations) {
        jobs = result.jobs;
        renderJobs();
      }
    }
    setConnection(true);
  } catch {
    setConnection(false);
  } finally {
    window.setTimeout(poll, 1100);
  }
}

const videoImport = createVideoImport();
poll();
