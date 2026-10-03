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
