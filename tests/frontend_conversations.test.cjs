// Real app.js handlers with an in-memory DOM and mocked HTTP responses.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

class Node {
  constructor(tag = "div") {
    this.tag = tag; this.children = []; this.listeners = {}; this.files = [];
    this.value = ""; this.textContent = ""; this.hidden = false; this.className = "";
    const classes = new Set();
    this.classList = { add: v => classes.add(v), remove: v => classes.delete(v), toggle: v => classes.has(v) ? classes.delete(v) : classes.add(v) };
  }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  get firstChild() { return this.children[0] || this; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  setAttribute() {}
  scrollIntoView() {}
  reset() {}
  close() {}
  querySelector(selector) {
    const cls = selector.slice(1);
    for (const child of this.children) {
      if (child.className.split(" ").includes(cls)) return child;
      const found = child.querySelector(selector); if (found) return found;
    }
    return null;
  }
  set innerHTML(_) { throw new Error("Unsafe HTML rendering"); }
}

function setup() {
  const nodes = new Map(), requests = [], conversations = [];
  const $ = id => { if (!nodes.has(id)) nodes.set(id, new Node()); return nodes.get(id); };
  let sequence = 0;
  const answer = { answer: "令牌过期后重新获取。<script>bad()</script>", answerable: true, citations: [] };
  const sandbox = {
    document: { getElementById: $, createElement: tag => new Node(tag), querySelectorAll: () => [] },
    window: { confirm: () => true }, Headers, FormData, AbortSignal, Intl,
    clearTimeout, setTimeout,
    fetch: async (url, options = {}) => {
      const body = options.body ? JSON.parse(options.body) : null;
      requests.push({ url, method: options.method || "GET", body });
      let payload;
      if (url.endsWith("/conversations") && options.method === "POST") {
        payload = { id: `c${++sequence}`, title: "新对话", turn_count: 0 };
        conversations.unshift(payload);
      } else if (url.endsWith("/conversations")) payload = { items: conversations };
      else if (url.includes("/conversations/")) payload = { turns: [{ question: "令牌过期了？", result: answer }] };
      else if (url === "/api/qa") payload = { ...answer, conversation_id: body.conversation_id };
      else payload = { items: [] };
      return { status: 200, ok: true, headers: new Headers(), json: async () => payload };
    },
  };
  vm.createContext(sandbox);
  const source = fs.readFileSync(path.join(__dirname, "../backend/app/static/app.js"), "utf8")
    .replace(/^import .*;\r?\n/, "").replace(/init\(\);\s*$/, "");
  vm.runInContext(source, sandbox);
  vm.runInContext('state.auth = { authenticated: true, epoch: 0, name: "Alice", token: async () => "test" }; state.kb = { id: "kb1", permission: "viewer" };', sandbox);
  const run = code => vm.runInContext(code, sandbox);
  const ask = async question => { $("question").value = question; await $("question-form").listeners.submit({ preventDefault() {} }); };
  return { $, requests, run, ask, sandbox };
}

test("first question creates a conversation, followup reuses it, new conversation resets it", async () => {
  const { $, requests, run, ask } = setup();
  await ask("令牌有效期？"); await ask("它过期怎么办？");
  const qa = requests.filter(r => r.url === "/api/qa");
  assert.equal(qa[0].body.conversation_id, "c1");
  assert.equal(qa[1].body.conversation_id, "c1");
  assert.equal(requests.filter(r => r.method === "POST" && r.url.endsWith("/conversations")).length, 1);
  $("new-conversation").listeners.click();
  assert.equal(run("state.conversationId"), null);
  await ask("新主题");
  assert.equal(requests.filter(r => r.url === "/api/qa").at(-1).body.conversation_id, "c2");
});

test("restore renders stored text safely and continues the selected conversation", async () => {
  const { $, run, ask, requests } = setup();
  await run('restoreConversation("existing")');
  assert.equal(run("state.conversationId"), "existing");
  assert.equal($("answer-area").children.at(-1).querySelector(".message-text").textContent, "令牌过期后重新获取。<script>bad()</script>");
  await ask("那如何更新？");
  assert.equal(requests.filter(r => r.url === "/api/qa")[0].body.conversation_id, "existing");
  assert.equal(requests.filter(r => r.method === "POST" && r.url.endsWith("/conversations")).length, 0);
});

test("switching knowledge base clears memory and ignores late answers from the old scope", async () => {
  const { $, run, ask, sandbox } = setup();
  await ask("先建立历史");
  const fetch = sandbox.fetch;
  let finish, started;
  const qaStarted = new Promise(resolve => { started = resolve; });
  sandbox.fetch = async (url, options) => {
    if (url === "/api/qa") { started(); return new Promise(resolve => { finish = resolve; }); }
    return fetch(url, options);
  };
  const pending = ask("旧知识库的追问"); await qaStarted;
  await run('selectBase({ id: "kb2", name: "第二个知识库", permission: "viewer" })');
  assert.equal(run("state.conversationId"), null);
  finish({ status: 200, ok: true, headers: new Headers(), json: async () => ({ answer: "不应显示", citations: [] }) });
  await pending;
  assert.equal(run("state.conversationId"), null);
  assert.equal($("answer-area").children.length, 1);
  assert.equal($("welcome").hidden, false);
});

test("logout clears the current conversation and recent conversation list", async () => {
  const { run, ask } = setup();
  await ask("一个问题");
  run("state.auth.authenticated = false; resetWorkspace()");
  assert.equal(run("state.conversationId"), null);
  assert.equal(run("state.conversations.length"), 0);
});

test("all-accessible mode uses global conversations and null KB, without enabling uploads", async () => {
  const { $, run, requests, ask } = setup();
  run('state.bases = [{ id: "kb1", name: "技术部", permission: "admin" }, { id: "kb2", name: "公共", permission: "viewer" }]');
  await run("selectBase(null)");
  assert.equal($("current-kb").textContent, "全部可访问知识库");
  assert.equal($("question").disabled, false);
  assert.equal($("upload-file").disabled, true);
  assert.equal($("upload-button").disabled, true);
  assert.equal($("kb-list").children.length, 3);
  await ask("哪些资料适用？"); await ask("它的条件呢？");
  const qa = requests.filter(r => r.url === "/api/qa");
  assert.equal(qa[0].body.knowledge_base_id, null);
  assert.equal(qa[0].body.conversation_id, qa[1].body.conversation_id);
  assert.equal(requests.filter(r => r.url === "/api/conversations" && r.method === "POST").length, 1);
  assert.equal(requests.some(r => r.url.includes("/documents")), false);
  await run('selectBase({ id: "kb1", name: "技术部", permission: "admin" })');
  assert.equal(run("state.conversationId"), null);
  assert.equal($("upload-file").disabled, false);
  await ask("仅在技术部查询");
  assert.equal(requests.filter(r => r.url === "/api/qa").at(-1).body.knowledge_base_id, "kb1");
});

test("login defaults to all-accessible mode; no readable bases disables asking", async () => {
  const { $, run, sandbox, requests } = setup();
  const original = sandbox.fetch;
  sandbox.fetch = async (url, options) => url === "/api/knowledge-bases"
    ? { status: 200, ok: true, headers: new Headers(), json: async () => ({ items: [{ id: "kb1", name: "技术部", permission: "admin" }] }) }
    : original(url, options);
  await run("connect()");
  assert.equal(run("state.kb"), null);
  assert.equal($("question").disabled, false);
  assert.equal(requests.some(r => r.url === "/api/conversations"), true);
  run("state.bases = []; state.kb = null; updateControls()");
  assert.equal($("question").disabled, true);
  assert.equal($("ask-button").disabled, true);
});

test("switching from a loading single KB to all-accessible mode does not leave questions disabled", async () => {
  const { $, run } = setup();
  run('state.bases = [{ id: "kb1", name: "技术部", permission: "admin" }]; state.loading = true');
  await run("selectBase(null)");
  assert.equal(run("state.loading"), false);
  assert.equal($("question").disabled, false);
});

test("cross-KB citations render the server KB name safely", () => {
  const { $, run } = setup();
  run('renderAnswer(message("assistant", ""), { answer: "结论", answerable: true, citations: [{ citation_id: "资料1", document_name: "手册", section_path: "条件", knowledge_base_name: "技术部<script>" }] })');
  assert.equal($("answer-area").children.at(-1).querySelector(".citation").textContent, "[资料1] 技术部<script> · 手册 · 条件");
});
