"use strict";

function createVideoImport() {
  const source = byId("import-source");
  const scan = byId("import-scan");
  const scanMessage = byId("import-scan-message");
  const details = byId("import-details");
  const filmTitle = byId("import-title");
  const start = byId("import-start");
  const target = byId("import-directory");
  const fileList = byId("import-file-list");
  const chooseAll = byId("import-select-all");
  const planList = byId("import-plan-list");
  const message = byId("import-message");
  const retryPreview = byId("import-preview-retry");
  const enqueueImport = byId("import-enqueue");
  const resultList = byId("import-result-list");
  const resultMessage = byId("import-result-message");
  const filenameOrder = new Intl.Collator("vi", { numeric: true, sensitivity: "base" });

  let scannedSource = null;
  let files = [];
  let planRows = [];
  let resultRows = [];
  let defaultTarget = "";
  let customTarget = false;
  let scanning = false;
  let busy = false;
  let prepared = null;
  let previewTimer;
  let previewVersion = 0;

  function validNumber(value) {
    return Number.isInteger(value) && value >= 1 && value <= 9999;
  }

  function fieldNumber(input) {
    return input.value || input.validity?.badInput ? input.valueAsNumber : null;
  }

  function selectedFiles() {
    return files.filter((file) => file.checkbox.checked);
  }

  function assignNumbers(ordered, first) {
    const numbers = new Map(ordered.filter((file) => file.number !== null).map((file) => [file, file.number]));
    if (first !== null) {
      ordered.forEach((file, index) => {
        if (!numbers.has(file)) numbers.set(file, validNumber(first) ? first + index : NaN);
      });
      return numbers;
    }

    const used = new Set(numbers.values());
    for (const file of ordered) {
      if (!numbers.has(file) && validNumber(file.episode) && !used.has(file.episode)) {
        numbers.set(file, file.episode);
        used.add(file.episode);
      }
    }
    let next = 1;
    for (const file of ordered) {
      if (numbers.has(file)) continue;
      while (used.has(next)) next += 1;
      numbers.set(file, next);
      used.add(next);
    }
    return numbers;
  }

  function refreshControls() {
    source.disabled = scan.disabled = scanning || busy;
    setText(byId("import-scan-label"), scanning ? "Đang tìm…" : "Tìm tệp");
    filmTitle.disabled = start.disabled = target.disabled = busy;
    chooseAll.disabled = busy || files.length === 0;
    for (const file of files) {
      file.checkbox.disabled = busy;
      file.label.classList.toggle("selected", file.checkbox.checked);
    }
    planRows.forEach(({ input, up, down }, index) => {
      input.disabled = busy;
      up.disabled = busy || index === 0;
      down.disabled = busy || index === planRows.length - 1;
    });
    setText(chooseAll, files.length && planRows.length === files.length ? "Bỏ chọn tất cả" : "Chọn tất cả");
    setText(byId("import-selection-count"), "Đã chọn " + unitCount(planRows.length, "tệp"));
    enqueueImport.disabled = busy || !prepared;
    setText(enqueueImport, busy ? "Đang nhập…" : "Nhập các tập đã chọn");
  }

  function refreshPreview(editedInput) {
    if (busy) return;
    window.clearTimeout(previewTimer);
    const version = ++previewVersion;
    prepared = null;
    retryPreview.hidden = true;
    refreshControls();

    const first = fieldNumber(start);
    const ordered = planRows.map(({ file }) => file);
    const numbers = assignNumbers(ordered, first);
    const counts = new Map();
    for (const number of numbers.values()) counts.set(number, (counts.get(number) || 0) + 1);
    const items = planRows.map(({ file, input, filename }) => {
      const number = numbers.get(file);
      input.setAttribute("aria-invalid", !validNumber(number) || counts.get(number) > 1);
      input.placeholder = Number.isFinite(number) ? String(number) : "";
      if (input !== editedInput) input.value = input.placeholder;
      setText(filename, "");
      return { name: file.name, number };
    });

    let warning = "";
    if (items.length && first !== null && !validNumber(first)) warning = "Nhập số tập bắt đầu từ 1 đến 9999.";
    else if (items.some(({ number }) => !validNumber(number))) warning = "Nhập số tập từ 1 đến 9999 cho mỗi tệp.";
    else if (counts.size < items.length) warning = "Số tập bị trùng. Hãy sửa số tập trong bản xem trước.";
    else if (items.length && !filmTitle.value.trim()) warning = "Nhập tên phim để xem tên tệp đích.";
    showNotice(message, warning);
    if (warning || !items.length) return;

    const payload = {
      source_directory: scannedSource,
      directory: target.value.trim() || directory.value.trim() || defaultTarget,
      title: filmTitle.value.trim(),
      files: items,
    };
    showNotice(message, "Đang cập nhật tên tệp đích…", "info");
    previewTimer = window.setTimeout(async () => {
      try {
        const response = await request("/api/import/preview", payload);
        if (version !== previewVersion) return;
        const filenames = new Map(response.files.map(({ name, filename }) => [name, filename]));
        for (const row of planRows) setText(row.filename, filenames.get(row.file.name));
        prepared = payload;
        showNotice(message, "");
        refreshControls();
      } catch (error) {
        if (version !== previewVersion) return;
        showNotice(message, error.message);
        retryPreview.hidden = false;
      }
    }, 200);
  }

  function reorder(file, offset) {
    const ordered = selectedFiles();
    const from = ordered.indexOf(file);
    const other = ordered[from + offset];
    if (!other || busy) return;
    const fromIndex = files.indexOf(file);
    const otherIndex = files.indexOf(other);
    [files[fromIndex], files[otherIndex]] = [other, file];
    renderPlan();
    const row = planRows[from + offset];
    const action = offset < 0 ? row.up : row.down;
    (action.disabled ? (offset < 0 ? row.down : row.up) : action).focus();
  }

  function renderPlan() {
    planRows = selectedFiles().map((file) => {
      const row = element("li", "import-plan-row");
      const original = element("p", "import-plan-original", file.name);
      original.dir = "auto";
      const controls = element("div", "import-plan-controls");
      const input = element("input", "import-number");
      input.type = "number";
      input.min = "1";
      input.max = "9999";
      input.step = "1";
      input.setAttribute("aria-label", "Số tập của " + file.name);
      input.addEventListener("input", () => {
        file.number = fieldNumber(input);
        refreshPreview(input);
      });
      input.addEventListener("blur", () => {
        if (file.number === null) input.value = input.placeholder;
      });

      const up = element("button", "button secondary compact import-up", "↑ Lên");
      const down = element("button", "button secondary compact import-down", "↓ Xuống");
      up.type = down.type = "button";
      up.setAttribute("aria-label", "Đưa lên: " + file.name);
      down.setAttribute("aria-label", "Đưa xuống: " + file.name);
      up.addEventListener("click", () => reorder(file, -1));
      down.addEventListener("click", () => reorder(file, 1));
      controls.append(element("span", "range-label", "Tập"), input, up, down);
      const filename = element("p", "import-preview-name");
      filename.dir = "auto";
      row.append(original, controls, filename);
      return { file, row, input, up, down, filename };
    });
    planList.replaceChildren(...planRows.map(({ row }) => row));
    planList.hidden = !planRows.length;
    byId("import-plan-empty").hidden = Boolean(planRows.length);
    refreshPreview();
  }

  function loadFiles(items) {
    const ordered = [...items].sort((left, right) => {
      const leftNumber = left.episode ?? Infinity;
      const rightNumber = right.episode ?? Infinity;
      return leftNumber - rightNumber || filenameOrder.compare(left.name, right.name);
    });
    const fragment = document.createDocumentFragment();
    files = ordered.map((file) => {
      const row = element("li");
      const label = element("label", "episode-row import-file-label");
      const checkbox = element("input");
      checkbox.type = "checkbox";
      const name = element("span", "import-file-name", file.name);
      name.dir = "auto";
      let meta = formatBytes(file.size);
      if (validNumber(file.episode)) meta += " · Tập " + episodeNumber(file.episode);
      label.append(checkbox, name, element("span", "import-file-meta", meta));
      row.append(label);
      fragment.append(row);
      checkbox.addEventListener("change", renderPlan);
      return { ...file, label, checkbox, number: null };
    });
    fileList.replaceChildren(fragment);
    details.hidden = !files.length;
    setText(byId("import-file-count"), unitCount(files.length, "tệp"));
    renderPlan();
  }

  async function scanFiles(event) {
    event.preventDefault();
    if (scanning || busy) return;
    const folder = source.value.trim();
    if (!folder) {
      showNotice(scanMessage, "Nhập đường dẫn thư mục Cốc Cốc lưu video.");
      source.focus();
      return;
    }
    scanning = true;
    scannedSource = null;
    showNotice(scanMessage, "");
    loadFiles([]);
    try {
      const response = await request("/api/import/scan", { source_directory: folder });
      scannedSource = folder;
      loadFiles(response.files);
      if (!files.length) showNotice(scanMessage, "Không tìm thấy tệp MP4 đã tải xong.", "info");
    } catch (error) {
      showNotice(scanMessage, error.message);
    } finally {
      scanning = false;
      refreshControls();
    }
  }

  function createResult(file) {
    const row = element("li", "job-row");
    const top = element("div", "job-top");
    const original = element("strong", "", file.name);
    original.dir = "auto";
    const status = element("span", "job-status");
    top.append(original, status);
    const size = element("p", "job-meta");
    const progress = element("progress", "job-progress");
    progress.max = 100;
    progress.setAttribute("aria-label", "Tiến độ sao chép " + file.name);
    const filename = element("p", "job-filename");
    filename.dir = "auto";
    const error = element("p", "job-error");
    row.append(top, size, progress, filename, error);
    return { row, status, size, progress, filename, error };
  }

  function showBatch(batch) {
    let completed = 0;
    let failed = 0;
    batch.files.forEach((file, index) => {
      if (file.status === "completed") completed += 1;
      if (file.status === "failed") failed += 1;
      const nodes = resultRows[index];
      nodes.status.className = "job-status " + file.status;
      setText(nodes.status, file.status === "running" ? "Đang sao chép" : statusLabels[file.status]);
      const known = Number.isFinite(file.total) && file.total > 0;
      const percent = known ? Math.min(100, Math.floor(100 * file.copied / file.total)) : null;
      setText(nodes.size, known ? formatBytes(file.copied) + " / " + formatBytes(file.total) + " · " + percent + "%" : formatBytes(file.copied));
      nodes.progress.hidden = !known && file.status !== "running";
      if (percent === null) nodes.progress.removeAttribute("value");
      else nodes.progress.value = percent;
      setText(nodes.filename, file.filename || "");
      nodes.filename.hidden = !file.filename;
      setText(nodes.error, file.error || "");
      nodes.error.hidden = !file.error;
    });
    const finished = completed + failed;
    setText(byId("import-result-summary"), finished + "/" + batch.files.length + " tệp đã xử lý · " + completed + " thành công · " + failed + " lỗi");
    if (finished === batch.files.length) finishBatch();
    else window.setTimeout(() => pollBatch(batch.id), 650);
  }

  function syncTarget() {
    if (customTarget || busy) return;
    target.value = directory.value.trim() || defaultTarget;
    refreshPreview();
  }

  function finishBatch() {
    busy = false;
    if (customTarget) refreshPreview();
    else syncTarget();
  }

  async function pollBatch(id) {
    try {
      const batch = await request("/api/import/batches/" + encodeURIComponent(id));
      showNotice(resultMessage, "");
      showBatch(batch);
    } catch (error) {
      showNotice(resultMessage, error.message);
      if (error.status === 404) finishBatch();
      else window.setTimeout(() => pollBatch(id), 650);
    }
  }

  async function startImport() {
    if (busy || !prepared) return;
    const payload = prepared;
    busy = true;
    prepared = null;
    showNotice(message, "");
    refreshControls();
    try {
      const batch = await request("/api/import/batches", payload);
      resultRows = batch.files.map(createResult);
      resultList.replaceChildren(...resultRows.map(({ row }) => row));
      byId("import-results").hidden = false;
      showNotice(resultMessage, "");
      showBatch(batch);
    } catch (error) {
      busy = false;
      prepared = payload;
      showNotice(message, error.message);
      refreshControls();
    }
  }

  byId("import-scan-form").addEventListener("submit", scanFiles);
  source.addEventListener("input", () => {
    if (source.value.trim() === scannedSource) return;
    scannedSource = null;
    showNotice(scanMessage, "");
    loadFiles([]);
  });
  chooseAll.addEventListener("click", () => {
    const shouldSelect = planRows.length !== files.length;
    for (const file of files) file.checkbox.checked = shouldSelect;
    renderPlan();
  });
  filmTitle.addEventListener("input", () => refreshPreview());
  start.addEventListener("input", () => refreshPreview());
  target.addEventListener("input", () => {
    customTarget = Boolean(target.value.trim());
    refreshPreview();
  });
  directory.addEventListener("input", syncTarget);
  retryPreview.addEventListener("click", () => refreshPreview());
  enqueueImport.addEventListener("click", startImport);

  return {
    configure(config) {
      if (!source.value.trim() && config.downloads_directory) source.value = config.downloads_directory;
      defaultTarget = config.directory;
      syncTarget();
    },
  };
}
