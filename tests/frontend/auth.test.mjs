import test from "node:test";
import assert from "node:assert/strict";
import { AuthSession, tokenClaims } from "../../backend/app/static/auth.js";

const config = { issuer: "http://127.0.0.1:8080/realms/test", client_id: "web" };
const token = (claims = {}) => `header.${Buffer.from(JSON.stringify({ exp: Math.floor(Date.now() / 1000) + 300, preferred_username: "alice", ...claims })).toString("base64url")}.signature`;

function browser() {
  const values = new Map();
  globalThis.sessionStorage = { setItem: (key, value) => values.set(key, value), getItem: key => values.get(key) || null, removeItem: key => values.delete(key) };
  globalThis.location = { origin: "http://127.0.0.1:8000", search: "", assign: url => { location.destination = url; } };
  globalThis.history = { replaceState: () => { location.search = ""; } };
  return values;
}

test("reject expired and malformed tokens; accept Unicode display names", () => {
  assert.throws(() => tokenClaims("placeholder"));
  assert.throws(() => tokenClaims(token({ exp: 1 })));
  assert.equal(tokenClaims(token({ name: "技术部用户" })).name, "技术部用户");
});

test("manual token login does not persist credentials", () => {
  const values = browser(), auth = new AuthSession(config);
  auth.useToken(`Bearer ${token()}`);
  assert.equal(auth.name, "alice");
  assert.equal(values.size, 0);
  auth.clear();
  assert.equal(auth.authenticated, false);
});

test("PKCE redirect uses S256 and callback consumes verifier once", async () => {
  const values = browser(), auth = new AuthSession(config);
  await auth.login();
  const url = new URL(location.destination);
  assert.equal(url.searchParams.get("code_challenge_method"), "S256");
  assert.equal(url.searchParams.get("redirect_uri"), `${location.origin}/`);
  assert.ok(url.searchParams.get("code_challenge"));
  const pending = JSON.parse(values.get("knowledge-workspace-pkce"));
  auth.exchange = async fields => { assert.equal(fields.code_verifier, pending.verifier); return { access_token: token(), refresh_token: "refresh" }; };
  location.search = `?code=one-time-code&state=${pending.state}`;
  assert.equal(await auth.callback(), true);
  assert.equal(values.size, 0);
  location.search = `?code=one-time-code&state=${pending.state}`;
  await assert.rejects(() => auth.callback(), /登录校验失败/);
});

test("reject mismatched state and issuer before token exchange", async () => {
  for (const invalid of ["state=wrong", `iss=${encodeURIComponent("http://attacker/realm")}`]) {
    const values = browser(), auth = new AuthSession(config);
    await auth.login();
    const pending = JSON.parse(values.get("knowledge-workspace-pkce"));
    const params = new URLSearchParams({ code: "code", state: pending.state });
    const override = new URLSearchParams(invalid);
    for (const [key, value] of override) params.set(key, value);
    location.search = `?${params}`;
    auth.exchange = () => { assert.fail("must not exchange invalid callback"); };
    await assert.rejects(() => auth.callback(), /登录校验失败/);
  }
});

test("concurrent requests share one refresh", async () => {
  browser();
  const auth = new AuthSession(config);
  auth.applyTokens({ access_token: token({ exp: Math.floor(Date.now() / 1000) + 10 }), refresh_token: "refresh" });
  let calls = 0;
  auth.exchange = async () => { calls += 1; return { access_token: token(), refresh_token: "rotated" }; };
  const results = await Promise.all([auth.token(), auth.token()]);
  assert.equal(calls, 1);
  assert.equal(results[0], results[1]);
  assert.equal(auth.refreshToken, "rotated");
});

test("logout during refresh cannot restore previous session", async () => {
  browser();
  const auth = new AuthSession(config);
  auth.applyTokens({ access_token: token({ exp: Math.floor(Date.now() / 1000) + 10 }), refresh_token: "refresh" });
  let resolve;
  auth.exchange = () => new Promise(done => { resolve = done; });
  const pending = auth.token();
  auth.clear();
  resolve({ access_token: token() });
  await assert.rejects(() => pending, /登录状态已改变/);
  assert.equal(auth.authenticated, false);
});

test("expired PKCE callback clears redirect and verifier without exchange", async () => {
  const values = browser(), auth = new AuthSession(config);
  await auth.login();
  const pending = JSON.parse(values.get("knowledge-workspace-pkce"));
  pending.created = Date.now() - 600001;
  values.set("knowledge-workspace-pkce", JSON.stringify(pending));
  location.search = `?code=expired-code&state=${pending.state}`;
  auth.exchange = () => assert.fail("must not exchange expired callback");
  await assert.rejects(() => auth.callback(), /登录校验失败/);
  assert.equal(values.size, 0);
  assert.equal(location.search, "");
  assert.equal(auth.authenticated, false);
});

test("cancelled login consumes callback without retaining credentials", async () => {
  const values = browser(), auth = new AuthSession(config);
  await auth.login();
  const pending = JSON.parse(values.get("knowledge-workspace-pkce"));
  location.search = `?error=access_denied&state=${pending.state}`;
  auth.exchange = () => assert.fail("must not exchange cancelled login");
  await assert.rejects(() => auth.callback(), /已取消或被拒绝/);
  assert.equal(values.size, 0);
  assert.equal(location.search, "");
  assert.equal(auth.authenticated, false);
});

test("failed refresh clears credentials and allows a new login", async () => {
  browser();
  const auth = new AuthSession(config);
  auth.applyTokens({ access_token: token({ exp: Math.floor(Date.now() / 1000) + 10 }), refresh_token: "refresh", id_token: "id-token" });
  auth.exchange = async () => { throw new Error("refresh rejected"); };
  await assert.rejects(() => auth.token(), /refresh rejected/);
  assert.equal(auth.authenticated, false);
  assert.equal(auth.refreshToken, null);
  assert.equal(auth.idToken, null);
  assert.equal(auth.refreshing, null);
  auth.useToken(token());
  assert.equal(await auth.token(), auth.accessToken);
});
