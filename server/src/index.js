// Hosted reprolog on Cloudflare Workers + D1.
//
//   /                 landing page            /app              dashboard (login required)
//   /auth/github      GitHub login            /app/activity     the library's viewer, per project
//   /v1/ingest        POST logs with a write token (Authorization: Bearer rpl_w_...)
//   /api/*            dashboard API (session cookie) or read API (Bearer token)
//
// Logs live in D1 only: a small row in `captures`, the full JSON gzip-compressed in
// `capture_data`. A daily cron deletes everything older than RETENTION_DAYS.
import SITE from "../../reprolog/site.html";
import VIEWER from "../../reprolog/viewer.html";
import APP from "./app.html";
import LOGIN from "./login.html";

const SESSION_COOKIE = "rpl_session";
const PROJECT_COOKIE = "rpl_project";
const SESSION_DAYS = 30;
const MAX_CAPTURE_BYTES = 1_500_000; // D1 rows max out at 2 MB; stay under after gzip
const MAX_BATCH = 50;

export default {
  async fetch(request, env, ctx) {
    try {
      return await route(request, env, ctx);
    } catch (err) {
      if (err instanceof HttpError) return json({ error: err.message }, err.status);
      console.error(err);
      return json({ error: "internal error" }, 500);
    }
  },
  async scheduled(_event, env) {
    await cleanup(env);
  },
};

class HttpError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

async function route(request, env, ctx) {
  const url = new URL(request.url);
  const { pathname: path } = url;
  const method = request.method;

  if (path === "/" && method === "GET") return html(SITE);
  if (path === "/login") return html(LOGIN);
  if (path === "/auth/github") return githubStart(request, env);
  if (path === "/auth/github/callback") return githubCallback(request, env);
  if (path === "/auth/dev" && env.DEV_LOGIN === "1") return devLogin(request, env);
  if (path === "/auth/logout") return redirect("/", [cookie(SESSION_COOKIE, "", 0)]);
  if (path === "/v1/ingest" && method === "POST") return ingest(request, env);

  if (path === "/app" || path === "/app/") {
    const user = await sessionUser(request, env);
    return user ? html(APP) : redirect("/login");
  }
  if (path === "/app/activity") {
    const user = await sessionUser(request, env);
    return user ? html(VIEWER) : redirect("/login");
  }
  if (path.startsWith("/api/")) return api(request, env, url, ctx);
  return json({ error: "not found" }, 404);
}

// ---------------------------------------------------------------- auth

async function githubStart(request, env) {
  if (!env.GITHUB_CLIENT_ID) throw new HttpError(500, "GitHub login is not configured");
  const state = randomId(16);
  const url = new URL("https://github.com/login/oauth/authorize");
  url.searchParams.set("client_id", env.GITHUB_CLIENT_ID);
  url.searchParams.set("redirect_uri", new URL("/auth/github/callback", request.url).toString());
  url.searchParams.set("scope", "read:user user:email");
  url.searchParams.set("state", state);
  return redirect(url.toString(), [cookie("rpl_oauth_state", state, 600)]);
}

async function githubCallback(request, env) {
  const url = new URL(request.url);
  const state = readCookie(request, "rpl_oauth_state");
  if (!state || state !== url.searchParams.get("state")) throw new HttpError(400, "login expired, try again");
  const tokenRes = await fetch("https://github.com/login/oauth/access_token", {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify({
      client_id: env.GITHUB_CLIENT_ID,
      client_secret: env.GITHUB_CLIENT_SECRET,
      code: url.searchParams.get("code"),
    }),
  }).then((r) => r.json());
  if (!tokenRes.access_token) throw new HttpError(400, "GitHub login failed");
  const gh = { Authorization: `Bearer ${tokenRes.access_token}`, "User-Agent": "reprolog", Accept: "application/vnd.github+json" };
  const profile = await fetch("https://api.github.com/user", { headers: gh }).then((r) => r.json());
  let email = profile.email;
  if (!email) {
    const emails = await fetch("https://api.github.com/user/emails", { headers: gh }).then((r) => r.json());
    email = (Array.isArray(emails) ? emails.find((e) => e.primary && e.verified) : null)?.email;
  }
  if (!email) throw new HttpError(400, "your GitHub account has no verified email");
  const user = await upsertUser(env, email, profile.name || profile.login, "github");
  return redirect("/app", [cookie("rpl_oauth_state", "", 0), await sessionCookie(env, user.id)]);
}

async function devLogin(request, env) {
  const user = await upsertUser(env, "dev@localhost", "Local developer", "dev");
  return redirect("/app", [await sessionCookie(env, user.id)]);
}

async function upsertUser(env, email, name, provider) {
  email = email.toLowerCase();
  const found = await env.DB.prepare("SELECT id, email, name FROM users WHERE email = ?").bind(email).first();
  if (found) return found;
  const user = { id: randomId(8), email, name };
  await env.DB.prepare("INSERT INTO users (id, email, name, provider, created_at) VALUES (?, ?, ?, ?, ?)")
    .bind(user.id, email, name, provider, now()).run();
  return user;
}

// Session = "<user id>.<expiry>.<hmac>", signed with SESSION_SECRET; no sessions table needed.
async function sessionCookie(env, userId) {
  const exp = now() + SESSION_DAYS * 86400;
  const payload = `${userId}.${exp}`;
  return cookie(SESSION_COOKIE, `${payload}.${await hmac(env, payload)}`, SESSION_DAYS * 86400);
}

async function sessionUser(request, env) {
  const raw = readCookie(request, SESSION_COOKIE);
  if (!raw) return null;
  const [id, exp, sig] = raw.split(".");
  if (!sig || Number(exp) < now() || sig !== (await hmac(env, `${id}.${exp}`))) return null;
  return env.DB.prepare("SELECT id, email, name FROM users WHERE id = ?").bind(id).first();
}

async function hmac(env, text) {
  if (!env.SESSION_SECRET) throw new HttpError(500, "SESSION_SECRET is not set");
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(env.SESSION_SECRET),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(text));
  return b64url(new Uint8Array(sig));
}

// ---------------------------------------------------------------- tokens

async function findToken(env, request) {
  const auth = request.headers.get("Authorization") || "";
  if (!auth.toLowerCase().startsWith("bearer ")) return null;
  const row = await env.DB.prepare(
    "SELECT id, user_id, scope, project_id FROM tokens WHERE hash = ? AND revoked_at IS NULL",
  ).bind(await sha256(auth.slice(7).trim())).first();
  if (!row) throw new HttpError(401, "invalid or revoked token");
  await env.DB.prepare("UPDATE tokens SET last_used_at = ? WHERE id = ?").bind(now(), row.id).run();
  return row;
}

async function createToken(env, userId, body) {
  const scope = body.scope === "write" ? "write" : "read";
  const name = String(body.name || "").trim().slice(0, 80) || "Untitled";
  let projectId = null;
  if (body.project_id) {
    const p = await ownProject(env, userId, body.project_id);
    projectId = p.id;
  }
  const token = `rpl_${scope[0]}_${randomId(24)}`;
  const row = { id: randomId(6), name, scope, project_id: projectId, prefix: token.slice(0, 10), created_at: now() };
  await env.DB.prepare(
    "INSERT INTO tokens (id, user_id, name, scope, project_id, prefix, hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
  ).bind(row.id, userId, name, scope, projectId, row.prefix, await sha256(token), row.created_at).run();
  return { ...row, token };
}

// ---------------------------------------------------------------- projects

async function ownProject(env, userId, projectId) {
  const p = await env.DB.prepare("SELECT id, name FROM projects WHERE id = ? AND user_id = ?").bind(projectId, userId).first();
  if (!p) throw new HttpError(404, "project not found");
  return p;
}

async function projectByName(env, userId, name, create) {
  name = String(name || "").trim().slice(0, 40);
  if (!/^[\w.-]+$/.test(name)) throw new HttpError(400, "project names may use letters, digits, '-', '_' and '.'");
  const found = await env.DB.prepare("SELECT id, name FROM projects WHERE user_id = ? AND name = ?").bind(userId, name).first();
  if (found || !create) return found;
  const { n } = await env.DB.prepare("SELECT COUNT(*) AS n FROM projects WHERE user_id = ?").bind(userId).first();
  if (n >= Number(env.MAX_PROJECTS || 10)) throw new HttpError(403, `project limit reached (${env.MAX_PROJECTS})`);
  const p = { id: randomId(6), name };
  await env.DB.prepare("INSERT INTO projects (id, user_id, name, created_at) VALUES (?, ?, ?, ?)")
    .bind(p.id, userId, name, now()).run();
  return p;
}

// ---------------------------------------------------------------- ingest

// Body: {"project": "prod", "captures": [<capture record>, ...]}. The project can also
// come from the X-Reprolog-Project header. Unknown projects are created on first use.
async function ingest(request, env) {
  const token = await findToken(env, request);
  if (!token) throw new HttpError(401, "send a write token: Authorization: Bearer rpl_w_...");
  if (token.scope !== "write") throw new HttpError(403, "this is a read token; ingest needs a write token");
  const body = await request.json().catch(() => null);
  const captures = Array.isArray(body?.captures) ? body.captures : body?.id ? [body] : null;
  if (!captures) throw new HttpError(400, "expected {project, captures: [...]}");
  if (captures.length > MAX_BATCH) throw new HttpError(413, `at most ${MAX_BATCH} captures per request`);

  const projectName = body.project || request.headers.get("X-Reprolog-Project") || "default";
  let project;
  if (token.project_id) {
    project = await ownProject(env, token.user_id, token.project_id);
    if (project.name !== projectName) throw new HttpError(403, `this token only writes to project "${project.name}"`);
  } else {
    project = await projectByName(env, token.user_id, projectName, true);
  }

  const day = new Date().toISOString().slice(0, 10);
  const used = (await env.DB.prepare("SELECT captures FROM usage_daily WHERE user_id = ? AND day = ?")
    .bind(token.user_id, day).first())?.captures || 0;
  const room = Math.max(0, Number(env.DAILY_CAPTURE_LIMIT || 2000) - used);
  const accepted = captures.slice(0, room);

  const stmts = [];
  for (const cap of accepted) {
    const text = JSON.stringify(cap);
    if (text.length > MAX_CAPTURE_BYTES) continue;
    const id = String(cap.id || randomId(6)).slice(0, 40);
    const req = cap.request || {};
    const err = cap.error || {};
    stmts.push(env.DB.prepare(
      `INSERT OR REPLACE INTO captures (id, project_id, created_at, method, path, status, duration_ms, error_type,
         error_message, replay_of, span_count, log_count, size_bytes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
    ).bind(`${project.id}_${id}`, project.id, Number(cap.created_at) || now(), req.method ?? null, req.path ?? null,
      cap.response?.status ?? null, cap.duration_ms ?? null, err.type ?? null, (err.message || "").slice(0, 500) || null,
      cap.replay_of ?? null, (cap.spans || []).length, (cap.logs || []).length, text.length));
    stmts.push(env.DB.prepare("INSERT OR REPLACE INTO capture_data (capture_id, data) VALUES (?, ?)")
      .bind(`${project.id}_${id}`, await gzip(text)));
  }
  if (stmts.length) {
    stmts.push(env.DB.prepare(
      "INSERT INTO usage_daily (user_id, day, captures) VALUES (?, ?, ?) ON CONFLICT(user_id, day) DO UPDATE SET captures = captures + excluded.captures",
    ).bind(token.user_id, day, stmts.length / 2));
    await env.DB.batch(stmts);
  }
  return json({ project: project.name, accepted: stmts.length ? (stmts.length - 1) / 2 : 0,
    dropped: captures.length - accepted.length, daily_limit: Number(env.DAILY_CAPTURE_LIMIT || 2000) });
}

// ---------------------------------------------------------------- API

async function api(request, env, url, ctx) {
  const path = url.pathname;
  const method = request.method;
  const token = await findToken(env, request);
  let userId;
  if (token) {
    if (method !== "GET") throw new HttpError(403, "tokens can only read through the API");
    userId = token.user_id;
  } else {
    const user = await sessionUser(request, env);
    if (!user) throw new HttpError(401, "log in first");
    if (method !== "GET" && request.headers.get("X-Reprolog") !== "1") throw new HttpError(403, "forbidden");
    userId = user.id;
    if (path === "/api/me") return json(user);
  }

  if (path === "/api/projects" && method === "GET") {
    const { results } = await env.DB.prepare(
      `SELECT p.id, p.name, p.created_at, (SELECT COUNT(*) FROM captures c WHERE c.project_id = p.id) AS count
         FROM projects p WHERE p.user_id = ? ORDER BY p.created_at`).bind(userId).all();
    return json(results);
  }
  if (path === "/api/projects" && method === "POST") {
    const body = await request.json().catch(() => ({}));
    return json(await projectByName(env, userId, body.name, true));
  }
  if (path === "/api/tokens" && method === "GET") {
    const { results } = await env.DB.prepare(
      `SELECT t.id, t.name, t.scope, t.prefix, t.created_at, t.last_used_at, p.name AS project
         FROM tokens t LEFT JOIN projects p ON p.id = t.project_id
        WHERE t.user_id = ? AND t.revoked_at IS NULL ORDER BY t.created_at DESC`).bind(userId).all();
    return json(results);
  }
  if (path === "/api/tokens" && method === "POST") {
    return json(await createToken(env, userId, await request.json().catch(() => ({}))));
  }
  const select = path.match(/^\/api\/projects\/([\w-]+)\/select$/);
  if (select && method === "POST" && !token) {
    const p = await ownProject(env, userId, select[1]);
    return new Response(JSON.stringify(p), { headers: { "Content-Type": "application/json",
      "Set-Cookie": cookie(PROJECT_COOKIE, p.id, 365 * 86400) } });
  }
  const revoke = path.match(/^\/api\/tokens\/([\w-]+)\/revoke$/);
  if (revoke && method === "POST") {
    const res = await env.DB.prepare("UPDATE tokens SET revoked_at = ? WHERE id = ? AND user_id = ? AND revoked_at IS NULL")
      .bind(now(), revoke[1], userId).run();
    return json({ revoked: res.meta.changes > 0 });
  }

  // Everything below reads one project: ?project=<name> for tokens, the switcher cookie for the dashboard.
  const project = await currentProject(env, request, url, userId, token);
  if (path === "/api/info") {
    return json({ app: project ? project.name : "no project yet", db: "Cloudflare D1", cwd: "", hosted: true,
      count: project ? (await env.DB.prepare("SELECT COUNT(*) AS n FROM captures WHERE project_id = ?").bind(project.id).first()).n : 0 });
  }
  if (path === "/api/captures") {
    if (!project) return json([]);
    const limit = Math.min(Number(url.searchParams.get("limit")) || 300, 1000);
    const { results } = await env.DB.prepare(
      `SELECT rowid AS seq, ${LIST_COLUMNS} FROM captures WHERE project_id = ? ORDER BY created_at DESC LIMIT ?`,
    ).bind(project.id, limit).all();
    return json(results.map((r) => listRow(r, project)));
  }
  if (path === "/api/stream") return stream(request, env, url, project, ctx);
  const one = path.match(/^\/api\/captures\/([\w-]+)$/);
  if (one) {
    if (!project) throw new HttpError(404, "not found");
    const row = await env.DB.prepare(
      "SELECT d.data FROM capture_data d JOIN captures c ON c.id = d.capture_id WHERE c.project_id = ? AND c.id = ?",
    ).bind(project.id, `${project.id}_${one[1]}`).first();
    if (!row) throw new HttpError(404, "not found");
    const rec = JSON.parse(await gunzip(row.data));
    rec._curl = toCurl(rec);
    return json(rec);
  }
  if (path.startsWith("/api/replay/")) {
    const id = path.split("/").pop();
    return json({ failed: true, message: `Replay runs your code, so it happens on your machine. From your app folder run: reprolog replay ${id} --app main:app` });
  }
  throw new HttpError(404, "not found");
}

const LIST_COLUMNS = "id, created_at, method, path, status, duration_ms, error_type, error_message, replay_of, span_count, log_count";

// Row ids are stored as "<project id>_<capture id>"; the viewer only sees the capture id.
function listRow(r, project) {
  return { ...r, id: r.id.slice(project.id.length + 1), query: null };
}

async function currentProject(env, request, url, userId, token) {
  if (token?.project_id) return ownProject(env, userId, token.project_id);
  const name = url.searchParams.get("project");
  if (name) return projectByName(env, userId, name, false);
  const chosen = readCookie(request, PROJECT_COOKIE);
  if (chosen) {
    const p = await env.DB.prepare("SELECT id, name FROM projects WHERE id = ? AND user_id = ?").bind(chosen, userId).first();
    if (p) return p;
  }
  return env.DB.prepare("SELECT id, name FROM projects WHERE user_id = ? ORDER BY created_at LIMIT 1").bind(userId).first();
}

// Server-sent events held open ~25 s, checking D1 every 3 s, then closed. EventSource
// reconnects on its own with Last-Event-ID, so no Worker stays open for long.
async function stream(request, env, url, project, ctx) {
  let after = Math.max(Number(url.searchParams.get("after")) || 0, Number(request.headers.get("Last-Event-ID")) || 0);
  const { readable, writable } = new TransformStream();
  const writer = writable.getWriter();
  const enc = new TextEncoder();
  const pump = async () => {
    try {
      await writer.write(enc.encode("retry: 1000\n\n"));
      for (let i = 0; i < 9 && project; i++) {
        const { results } = await env.DB.prepare(
          `SELECT rowid AS seq, ${LIST_COLUMNS} FROM captures WHERE project_id = ? AND rowid > ? ORDER BY rowid LIMIT 200`,
        ).bind(project.id, after).all();
        let out = results.length ? "" : ": ping\n\n";
        for (const r of results) {
          after = r.seq;
          out += `id: ${r.seq}\ndata: ${JSON.stringify(listRow(r, project))}\n\n`;
        }
        await writer.write(enc.encode(out));
        await new Promise((res) => setTimeout(res, 3000));
      }
    } catch {
      // client went away
    } finally {
      await writer.close().catch(() => {});
    }
  };
  ctx.waitUntil(pump());
  return new Response(readable, { headers: { "Content-Type": "text/event-stream", "Cache-Control": "no-store" } });
}

// ---------------------------------------------------------------- retention

async function cleanup(env) {
  const cutoff = now() - Number(env.RETENTION_DAYS || 10) * 86400;
  // Small batches so a big backlog doesn't burn the day's free write allowance at once.
  for (let i = 0; i < 20; i++) {
    const { results } = await env.DB.prepare("SELECT id FROM captures WHERE created_at < ? LIMIT 200").bind(cutoff).all();
    if (!results.length) break;
    const ids = results.map((r) => r.id);
    const marks = ids.map(() => "?").join(",");
    await env.DB.batch([
      env.DB.prepare(`DELETE FROM capture_data WHERE capture_id IN (${marks})`).bind(...ids),
      env.DB.prepare(`DELETE FROM captures WHERE id IN (${marks})`).bind(...ids),
    ]);
  }
  const oldDay = new Date((cutoff - 86400) * 1000).toISOString().slice(0, 10);
  await env.DB.prepare("DELETE FROM usage_daily WHERE day < ?").bind(oldDay).run();
}

// ---------------------------------------------------------------- helpers

function toCurl(rec) {
  const req = rec.request || {};
  const parts = [`curl -X ${req.method || "GET"}`];
  for (const [k, v] of Object.entries(req.headers || {})) {
    if (!["host", "content-length"].includes(k.toLowerCase())) parts.push(`-H ${shq(`${k}: ${v}`)}`);
  }
  const body = req.body ?? req.body_text;
  if (body != null && body !== "") parts.push(`--data ${shq(typeof body === "string" ? body : JSON.stringify(body))}`);
  const qs = req.query_string ? `?${req.query_string}` : "";
  parts.push(shq(`http://localhost:8000${req.path || "/"}${qs}`));
  return parts.join(" \\\n  ");
}

const shq = (s) => `'${String(s).replaceAll("'", `'\\''`)}'`;

async function gzip(text) {
  const stream = new Blob([text]).stream().pipeThrough(new CompressionStream("gzip"));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

async function gunzip(data) {
  const bytes = data instanceof ArrayBuffer ? new Uint8Array(data) : Uint8Array.from(data);
  return new Response(new Blob([bytes]).stream().pipeThrough(new DecompressionStream("gzip"))).text();
}

async function sha256(text) {
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function randomId(bytes) {
  return b64url(crypto.getRandomValues(new Uint8Array(bytes)));
}

function b64url(bytes) {
  return btoa(String.fromCharCode(...bytes)).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

const now = () => Math.floor(Date.now() / 1000);

function readCookie(request, name) {
  const header = request.headers.get("Cookie") || "";
  for (const part of header.split(";")) {
    const [k, ...v] = part.trim().split("=");
    if (k === name) return decodeURIComponent(v.join("="));
  }
  return null;
}

function cookie(name, value, maxAge) {
  return `${name}=${encodeURIComponent(value)}; Path=/; Max-Age=${maxAge}; HttpOnly; Secure; SameSite=Lax`;
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json", "Cache-Control": "no-store" } });
}

function html(body) {
  return new Response(body, { headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" } });
}

function redirect(location, cookies = []) {
  const headers = new Headers({ Location: location });
  for (const c of cookies) headers.append("Set-Cookie", c);
  return new Response(null, { status: 302, headers });
}
