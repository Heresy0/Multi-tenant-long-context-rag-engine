// Tokens remain in memory. Only a short-lived PKCE verifier survives the redirect.
const PENDING_KEY = "knowledge-workspace-pkce";

export function base64url(bytes) {
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function tokenClaims(token) {
  const parts = token.split(".");
  if (parts.length !== 3) throw new Error("请输入完整的 JWT Access Token，且不要包含 Bearer 前缀。");
  try {
    const value = parts[1].replace(/-/g, "+").replace(/_/g, "/");
    const claims = JSON.parse(new TextDecoder().decode(Uint8Array.from(atob(value.padEnd(Math.ceil(value.length / 4) * 4, "=")), c => c.charCodeAt(0))));
    if (!Number.isFinite(claims.exp) || claims.exp * 1000 <= Date.now()) throw new Error();
    return claims;
  } catch {
    throw new Error("令牌格式无效或已过期，请重新获取 Access Token。");
  }
}

export class AuthSession {
  constructor(config) {
    this.config = config;
    this.accessToken = null;
    this.refreshToken = null;
    this.idToken = null;
    this.claims = null;
    this.refreshing = null;
    this.epoch = 0;
  }

  get authenticated() { return Boolean(this.accessToken); }
  get name() { return this.claims?.name || this.claims?.preferred_username || "已登录用户"; }

  clear() {
    this.epoch += 1;
    this.accessToken = this.refreshToken = this.idToken = this.claims = null;
    this.refreshing = null;
    sessionStorage.removeItem(PENDING_KEY);
  }

  useToken(value) {
    const token = value.trim().replace(/^Bearer\s+/i, "");
    const claims = tokenClaims(token);
    this.clear();
    this.accessToken = token;
    this.claims = claims; // Display only; the API verifies the signature and permissions.
  }

  applyTokens(data) {
    const claims = tokenClaims(data.access_token || "");
    this.accessToken = data.access_token;
    this.refreshToken = data.refresh_token || this.refreshToken;
    this.idToken = data.id_token || this.idToken;
    this.claims = claims;
  }

  async login() {
    if (!crypto.subtle) throw new Error("企业登录需要 HTTPS 或 localhost / 127.0.0.1 环境。");
    const verifier = base64url(crypto.getRandomValues(new Uint8Array(48)));
    const state = base64url(crypto.getRandomValues(new Uint8Array(32)));
    const redirect = `${location.origin}/`;
    const challenge = base64url(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier))));
    sessionStorage.setItem(PENDING_KEY, JSON.stringify({ verifier, state, redirect, created: Date.now() }));
    const url = new URL(`${this.config.issuer}/protocol/openid-connect/auth`);
    url.search = new URLSearchParams({ client_id: this.config.client_id, redirect_uri: redirect, response_type: "code", scope: "openid profile", state, code_challenge: challenge, code_challenge_method: "S256" });
    location.assign(url.href);
  }

  async callback() {
    const params = new URLSearchParams(location.search);
    if (!params.has("code") && !params.has("error")) return false;
    const saved = sessionStorage.getItem(PENDING_KEY);
    sessionStorage.removeItem(PENDING_KEY);
    history.replaceState({}, "", "/"); // Remove authorization code before other requests.
    const pending = saved ? JSON.parse(saved) : null;
    if (!pending || pending.state !== params.get("state") || Date.now() - pending.created > 600000 || (params.has("iss") && params.get("iss") !== this.config.issuer)) {
      throw new Error("登录校验失败或登录请求已过期，请重新登录。");
    }
    if (params.has("error")) throw new Error("企业登录已取消或被拒绝，请重试。");
    const data = await this.exchange({ grant_type: "authorization_code", code: params.get("code"), redirect_uri: pending.redirect, code_verifier: pending.verifier });
    this.applyTokens(data);
    return true;
  }

  async exchange(values) {
    const response = await fetch(`${this.config.issuer}/protocol/openid-connect/token`, {
      method: "POST", credentials: "omit", signal: AbortSignal.timeout(20000),
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ client_id: this.config.client_id, ...values }),
    });
    if (!response.ok) throw new Error("登录或续期失败，请检查 Keycloak 前端客户端配置后重新登录。");
    return response.json();
  }

  async token() {
    if (!this.accessToken) throw new Error("请先登录工作空间。");
    const remaining = this.claims.exp * 1000 - Date.now();
    if (remaining > 45000) return this.accessToken;
    if (!this.refreshToken) {
      if (remaining > 0) return this.accessToken;
      this.clear();
      throw new Error("访问令牌已过期，请重新登录。");
    }
    if (!this.refreshing) {
      const epoch = this.epoch;
      this.refreshing = this.exchange({ grant_type: "refresh_token", refresh_token: this.refreshToken })
        .then(data => {
          if (epoch !== this.epoch) throw new Error("登录状态已改变。");
          this.applyTokens(data);
          return this.accessToken;
        }).catch(error => {
          if (epoch === this.epoch) this.clear();
          throw error;
        }).finally(() => { if (epoch === this.epoch) this.refreshing = null; });
    }
    return this.refreshing;
  }

  logout() {
    const idToken = this.idToken;
    this.clear();
    if (idToken) {
      const url = new URL(`${this.config.issuer}/protocol/openid-connect/logout`);
      url.search = new URLSearchParams({ id_token_hint: idToken, post_logout_redirect_uri: `${location.origin}/` });
      location.assign(url.href);
    }
  }
}
