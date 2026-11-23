const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

const staticPath = path.join(__dirname, "..", "static");
const html = fs.readFileSync(path.join(staticPath, "index.html"), "utf8");
const script = fs.readFileSync(path.join(staticPath, "app.js"), "utf8");
const importScript = fs.readFileSync(path.join(staticPath, "import.js"), "utf8");

class TestNode {
  constructor(tag) {
    this.tag = tag;
    this.value = "";
    this.textContent = "";
    this.children = [];
    this.listeners = new Map();
    this.attributes = new Map();
    this.classList = { toggle() {} };
  }

  get valueAsNumber() {
    return this.value.trim() ? Number(this.value) : NaN;
  }

  get lastChild() {
    return this.children.at(-1);
  }

  get firstElementChild() {
    return this.children[0] || null;
  }

  append(...nodes) {
    for (const node of nodes) {
      this.children.push(...(node.tag === "fragment" ? node.children : [node]));
    }
  }

  replaceChildren(...nodes) {
    this.children = [];
    this.append(...nodes);
  }

  addEventListener(event, callback) {
    if (!this.listeners.has(event)) this.listeners.set(event, []);
    this.listeners.get(event).push(callback);
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }

  removeAttribute(name) {
    this.attributes.delete(name);
  }

  focus() {}

  async trigger(event) {
    const listeners = this.listeners.get(event);
    assert.ok(listeners, "Missing " + event + " listener");
    for (const callback of listeners) await callback({ preventDefault() {}, target: this, currentTarget: this });
  }
}

function startApp({ found = true, importRoutes = {} } = {}) {
  const nodes = new Map(
    Array.from(html.matchAll(/\bid="([^"]+)"/g), ([, id]) => [id, new TestNode(id)]),
  );
  const node = (id) => nodes.get(id);
  node("discover").append(new TestNode("text"));
  const episodes = [20, 21, 5, 1, 8].map((number) => ({
    number,
    kind: "mp4",
    url: "https://test.invalid/tap-" + number + ".mp4",
    label: "Tập " + String(number).padStart(2, "0"),
  }));
  const posts = [];
  const timers = new Map();
  let timerId = 0;
  const context = vm.createContext({
    URL,
    document: {
      getElementById: node,
      createElement: (tag) => new TestNode(tag),
      createDocumentFragment: () => new TestNode("fragment"),
    },
    window: {
      setTimeout(callback, delay) {
        timers.set(++timerId, { callback, delay });
        return timerId;
      },
      clearTimeout(id) { timers.delete(id); },
    },
    async fetch(url, options) {
      const payload = options.method === "POST" ? JSON.parse(options.body) : undefined;
      if (payload) posts.push({ url, payload });
      const data = {
        "/api/config": { directory: "C:\\Videos", downloads_directory: "C:\\Users\\An\\Downloads", ffmpeg: true, concurrency: 2 },
        "/api/discover": found
          ? { episodes, messages: [], title: "Video kiểm thử" }
          : { episodes: [], messages: ["Nguồn chưa có link tải."], title: "Tên phim" },
        "/api/jobs": { jobs: [] },
        ...importRoutes,
      };
      assert.ok(url in data, "Unexpected request " + url);
      const result = typeof data[url] === "function" ? await data[url](payload) : data[url];
      return { ok: true, async json() { return result; } };
    },
  });
  vm.runInContext(importScript, context, { filename: "static/import.js" });
  vm.runInContext(script, context, { filename: "static/app.js" });

  const checkboxes = () => node("episode-list").children.map((item) => item.children[0].children);
  const captions = (onlySelected = false) => checkboxes()
    .filter(([checkbox]) => !onlySelected || checkbox.checked)
    .map(([, caption]) => caption.textContent);

  return {
    node,
    captions,
    posts,
    async runTimer() {
      const next = [...timers].sort(([, left], [, right]) => left.delay - right.delay)[0];
      assert.ok(next && next[1].delay < 1100, "Missing import timer");
      timers.delete(next[0]);
      await next[1].callback();
      await new Promise(setImmediate);
    },
    async discover() {
      node("links").value = episodes.map((episode) => episode.url).join("\n");
      await node("source-form").trigger("submit");
      assert.equal(node("source-messages").hidden, true);
    },
    async applyRange(first, last) {
      node("range-start").value = String(first);
      node("range-end").value = String(last);
      await node("range-form").trigger("submit");
    },
  };
}

test("one click discovers and queues all episodes once", async () => {
  const app = startApp();
  app.node("links").value = "https://test.invalid/phim";
  app.node("directory").value = "C:\\Videos";
  await Promise.all([
    app.node("discover-all").trigger("click"),
    app.node("discover-all").trigger("click"),
  ]);
  assert.deepEqual(app.posts.map(({ url }) => url), ["/api/discover", "/api/jobs"]);
  const { title, directory, episodes } = app.posts[1].payload;
  assert.equal(title, "Video kiểm thử");
  assert.equal(directory, "C:\\Videos");
  assert.deepEqual(episodes.map(({ number }) => number), [1, 5, 8, 20, 21]);
  assert.deepEqual(app.captions(true), ["Tập 01", "Tập 05", "Tập 08", "Tập 20", "Tập 21"]);
  assert.equal(app.node("discover-all").disabled, false);
});

test("one click displays the source notice without queueing an empty result", async () => {
  const app = startApp({ found: false });
  app.node("links").value = "https://test.invalid/phim";
  await app.node("discover-all").trigger("click");
  assert.deepEqual(app.posts.map(({ url }) => url), ["/api/discover"]);
  assert.equal(app.node("source-messages").children[0].textContent, "Nguồn chưa có link tải.");
  assert.equal(app.node("discover-all").disabled, false);
});

test("sorts detected episodes and toggles select all", async () => {
  const app = startApp();
  await app.discover();
  assert.deepEqual(app.captions(), ["Tập 01", "Tập 05", "Tập 08", "Tập 20", "Tập 21"]);
  assert.equal(app.node("enqueue").disabled, true);

  await app.node("select-all").trigger("click");
  assert.deepEqual(app.captions(true), app.captions());
  assert.equal(app.node("enqueue").disabled, false);

  await app.node("select-all").trigger("click");
  assert.deepEqual(app.captions(true), []);
  assert.equal(app.node("enqueue").disabled, true);
});

test("selects an inclusive range and enqueues only the selected episodes", async () => {
  const app = startApp();
  await app.discover();
  await app.node("select-all").trigger("click");
  await app.applyRange(5, 20);
  assert.deepEqual(app.captions(true), ["Tập 05", "Tập 08", "Tập 20"]);
  assert.equal(app.node("range-message").hidden, true);
  await app.node("enqueue").trigger("click");
  const jobs = app.posts.filter(({ url }) => url === "/api/jobs");
  assert.equal(jobs.length, 1);
  assert.deepEqual(jobs[0].payload.episodes.map(({ number }) => number), [5, 8, 20]);
  assert.equal(jobs[0].payload.title, "Video kiểm thử");
  assert.equal(jobs[0].payload.directory, "C:\\Videos");
  assert.equal(app.node("enqueue-message").hidden, false);
});

test("shows a warning and preserves selection for invalid or empty ranges", async () => {
  const app = startApp();
  await app.discover();
  await app.applyRange(5, 8);
  for (const [first, last, message] of [
    [20, 5, /hai số tập dương/],
    ["", 20, /hai số tập dương/],
    [5.5, 20, /hai số tập dương/],
    [6, 7, /Không có tập nào/],
  ]) {
    await app.applyRange(first, last);
    assert.equal(app.node("range-message").hidden, false);
    assert.match(app.node("range-message").textContent, message);
    assert.deepEqual(app.captions(true), ["Tập 05", "Tập 08"]);
  }
});

test("uses a discovered title until the user supplies their own", async () => {
  const app = startApp();
  await app.discover();
  assert.equal(app.node("title").value, "Video kiểm thử");

  app.node("title").value = "Tên đã sửa";
  await app.node("title").trigger("input");
  await app.discover();
  await app.node("select-all").trigger("click");
  await app.node("enqueue").trigger("click");
  assert.equal(app.posts.find(({ url }) => url === "/api/jobs").payload.title, "Tên đã sửa");

  app.node("title").value = "";
  await app.node("title").trigger("input");
  await app.discover();
  assert.equal(app.node("title").value, "Video kiểm thử");
});

function childWithClass(node, className) {
  for (const child of node.children) {
    if (child.className?.split(" ").includes(className)) return child;
    const match = childWithClass(child, className);
    if (match) return match;
  }
}

function visibleText(node) {
  if (node.hidden) return "";
  return [node.textContent, ...node.children.map(visibleText)].join(" ");
}

test("previews an edited local selection and shows each import result", async () => {
  const source = "D:\\Cốc Cốc\\Tệp đã tải";
  const directory = "D:\\Phim Việt";
  const title = "Chuyện Cổ Tích";
  const files = [
    { name: "Tập 03.mp4", size: 1024, episode: 3 },
    { name: "EP07.mp4", size: 2300, episode: 7 },
    { name: "Bản sao.mp4", size: 750, episode: null },
    { name: "Không chọn.mp4", size: 2048, episode: null },
  ];
  const makePreview = (payload) => ({
    files: payload.files.map((file) => ({
      ...file,
      filename: `${payload.title} - Tập ${String(file.number).padStart(2, "0")}.mp4`,
    })),
  });
  let started;
  const app = startApp({ importRoutes: {
    "/api/import/scan": { files },
    "/api/import/preview": makePreview,
    "/api/import/batches": (payload) => {
      started = makePreview(payload).files;
      return { id: "batch-1", files: started.map((file) => ({
        ...file, filename: null, status: "running", copied: 500, total: 1024, error: null,
      })) };
    },
    "/api/import/batches/batch-1": () => ({
      id: "batch-1",
      files: started.map((file, index) => ({
        ...file,
        filename: index === 0 ? `${title} - Tập 05 (2).mp4` : index === 1 ? null : file.filename,
        status: index === 1 ? "failed" : "completed",
        copied: index === 1 ? 0 : 1024,
        total: 1024,
        error: index === 1 ? "Tệp nguồn đã bị xóa." : null,
      })),
    }),
  } });
  await new Promise(setImmediate);
  assert.equal(app.node("import-source").value, "C:\\Users\\An\\Downloads");
  assert.equal(app.node("import-directory").value, "C:\\Videos");
  app.node("directory").value = directory;
  await app.node("directory").trigger("input");
  assert.equal(app.node("import-directory").value, directory);

  app.node("import-source").value = source;
  await app.node("import-source").trigger("input");
  await app.node("import-scan-form").trigger("submit");
  assert.deepEqual(app.posts[0], { url: "/api/import/scan", payload: { source_directory: source } });
  const fileList = app.node("import-file-list");
  assert.equal(fileList.children.length, 4);
  assert.match(visibleText(fileList), /1 KB/);

  for (const item of fileList.children) {
    if (visibleText(item).includes("Không chọn.mp4")) continue;
    const [checkbox] = item.children[0].children;
    checkbox.checked = true;
    await checkbox.trigger("change");
  }
  app.node("import-title").value = title;
  await app.node("import-title").trigger("input");
  app.node("import-start").value = "5";
  await app.node("import-start").trigger("input");
  const plan = app.node("import-plan-list");
  const originalNames = () => plan.children.map((row) => childWithClass(row, "import-plan-original").textContent);
  const original = originalNames();
  assert.equal(original.length, 3);
  await childWithClass(plan.children[1], "import-up").trigger("click");
  assert.deepEqual(originalNames(), [original[1], original[0], original[2]]);
  assert.equal(app.node("import-enqueue").disabled, true);
  await app.runTimer();
  assert.deepEqual(plan.children.map((row) => childWithClass(row, "import-preview-name").textContent), [
    `${title} - Tập 05.mp4`, `${title} - Tập 06.mp4`, `${title} - Tập 07.mp4`,
  ]);

  const lastNumber = childWithClass(plan.children[2], "import-number");
  lastNumber.value = "9";
  await lastNumber.trigger("input");
  await app.runTimer();
  assert.equal(childWithClass(plan.children[2], "import-preview-name").textContent, `${title} - Tập 09.mp4`);

  const submit = app.node("import-enqueue");
  assert.equal(submit.disabled, false);
  await Promise.all([submit.trigger("click"), submit.trigger("click")]);
  const batches = app.posts.filter(({ url }) => url === "/api/import/batches");
  assert.equal(batches.length, 1);
  assert.deepEqual(batches[0].payload, {
    source_directory: source,
    directory,
    title,
    files: originalNames().map((name, index) => ({ name, number: [5, 6, 9][index] })),
  });
  const results = app.node("import-result-list");
  assert.match(visibleText(results), /Đang sao chép/);
  await app.runTimer();
  assert.match(visibleText(results), /Chuyện Cổ Tích - Tập 05 \(2\)\.mp4/);
  assert.match(visibleText(results), /Tệp nguồn đã bị xóa\./);
  assert.match(visibleText(results), /Chuyện Cổ Tích - Tập 09\.mp4/);
  await app.runTimer();
  assert.equal(submit.disabled, false);
});

test("selects all local files, preserves detected numbers and resolves missing or repeated hints", async () => {
  const files = [
    { name: "Tập 03.mp4", size: 2000, episode: 3 },
    { name: "EP03.mp4", size: 1000, episode: 3 },
    { name: "Video 1080p.mp4", size: 2100, episode: null },
  ];
  const app = startApp({ importRoutes: {
    "/api/import/scan": { files },
    "/api/import/preview": ({ files }) => ({ files: files.map((file) => ({ ...file, filename: file.name })) }),
  } });
  await new Promise(setImmediate);
  await app.node("import-scan-form").trigger("submit");
  app.node("import-title").value = "Phim thử";
  await app.node("import-title").trigger("input");

  const select = app.node("import-select-all");
  const rows = app.node("import-plan-list");
  await select.trigger("click");
  await app.runTimer();
  const submitted = app.posts.at(-1).payload.files;
  assert.deepEqual(submitted, [
    { name: "EP03.mp4", number: 3 },
    { name: "Tập 03.mp4", number: 1 },
    { name: "Video 1080p.mp4", number: 2 },
  ]);
  app.node("import-start").value = "5";
  await app.node("import-start").trigger("input");
  const second = childWithClass(rows.children[1], "import-number");
  second.value = "5";
  await second.trigger("input");
  assert.match(app.node("import-message").textContent, /Số tập bị trùng/);
  assert.equal(app.node("import-enqueue").disabled, true);

  await select.trigger("click");
  assert.equal(rows.children.length, 0);
  assert.equal(app.node("import-enqueue").disabled, true);
});
