const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

const staticPath = path.join(__dirname, "..", "static");
const html = fs.readFileSync(path.join(staticPath, "index.html"), "utf8");
const script = fs.readFileSync(path.join(staticPath, "app.js"), "utf8");

class TestNode {
  constructor(tag) {
    this.tag = tag;
    this.value = "";
    this.textContent = "";
    this.children = [];
    this.listeners = new Map();
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
    this.listeners.set(event, callback);
  }

  async trigger(event) {
    const callback = this.listeners.get(event);
    assert.ok(callback, "Missing " + event + " listener");
    return callback({ preventDefault() {} });
  }
}

function startApp() {
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
  vm.runInNewContext(script, {
    URL,
    document: {
      getElementById: node,
      createElement: (tag) => new TestNode(tag),
      createDocumentFragment: () => new TestNode("fragment"),
    },
    window: { setTimeout() {} },
    async fetch(url, options) {
      if (options.method === "POST") posts.push({ url, payload: JSON.parse(options.body) });
      const data = {
        "/api/config": { directory: "C:\\Videos", ffmpeg: true, concurrency: 2 },
        "/api/discover": { episodes, messages: [], title: "Video kiểm thử" },
        "/api/jobs": { jobs: [] },
      };
      assert.ok(url in data, "Unexpected request " + url);
      return { ok: true, async json() { return data[url]; } };
    },
  }, { filename: "static/app.js" });

  const checkboxes = () => node("episode-list").children.map((item) => item.children[0].children);
  const captions = (onlySelected = false) => checkboxes()
    .filter(([checkbox]) => !onlySelected || checkbox.checked)
    .map(([, caption]) => caption.textContent);

  return {
    node,
    captions,
    posts,
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
