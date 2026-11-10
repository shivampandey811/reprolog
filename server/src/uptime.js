// Uptime checks (Pro plan). The every-minute cron calls runChecks(); a monitor goes
// down after 2 failed checks in a row and the owner gets an email, and another when
// it recovers. Each check is one upsert into monitor_hours, so history stays cheap.
import { notify } from "./alerts.js";

export const MAX_MONITORS = 10;
const TIMEOUT_MS = 10_000;
const FAILS_TO_DOWN = 2;
const HISTORY_HOURS = 24 * 30;

const now = () => Math.floor(Date.now() / 1000);

export async function userPlan(env, userId) {
  const row = await env.DB.prepare("SELECT plan FROM subscriptions WHERE user_id = ?").bind(userId).first();
  return row?.plan || "free";
}

// Only public http(s) URLs: no credentials in the URL, no localhost or private ranges.
export function checkUrl(raw) {
  let url;
  try {
    url = new URL(String(raw || "").trim());
  } catch {
    return null;
  }
  if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) return null;
  const host = url.hostname.toLowerCase();
  if (host === "localhost" || host.endsWith(".localhost") || host.endsWith(".internal") || host.endsWith(".local")) return null;
  if (/^(127\.|10\.|0\.|169\.254\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)/.test(host) || host.startsWith("[")) return null;
  return url.toString();
}

async function probe(url) {
  const started = Date.now();
  try {
    const res = await fetch(url, {
      method: "GET",
      redirect: "follow",
      headers: { "User-Agent": "reprolog-uptime/1.0 (+https://reprolog.shivampandey78970.workers.dev)" },
      signal: AbortSignal.timeout(TIMEOUT_MS),
      cf: { cacheTtl: 0 },
    });
    await res.body?.cancel();
    const ms = Date.now() - started;
    return { ok: res.status < 400, code: res.status, ms, error: res.status < 400 ? null : `HTTP ${res.status}` };
  } catch (err) {
    const timedOut = err?.name === "TimeoutError" || err?.name === "AbortError";
    return { ok: false, code: null, ms: Date.now() - started, error: timedOut ? `no response in ${TIMEOUT_MS / 1000}s` : String(err?.message || err).slice(0, 200) };
  }
}

export async function runChecks(env) {
  const { results } = await env.DB.prepare(
    `SELECT m.*, u.email FROM monitors m JOIN users u ON u.id = m.user_id
       JOIN subscriptions s ON s.user_id = m.user_id AND s.plan = 'pro'
      WHERE m.paused = 0 ORDER BY m.last_checked_at IS NOT NULL, m.last_checked_at LIMIT 40`,
  ).all();
  const at = now();
  const hour = Math.floor(at / 3600);
  const checked = await Promise.all(results.map(async (m) => ({ m, r: await probe(m.url) })));
  const stmts = [];
  const alerts = [];
  for (const { m, r } of checked) {
    const streak = r.ok ? 0 : m.fail_streak + 1;
    let status = m.status;
    if (r.ok) status = "up";
    else if (streak >= FAILS_TO_DOWN) status = "down";
    const flipped = status !== m.status && (status === "down" || m.status === "down");
    stmts.push(env.DB.prepare(
      `UPDATE monitors SET status = ?, fail_streak = ?, last_checked_at = ?, last_code = ?, last_ms = ?, last_error = ?,
         changed_at = CASE WHEN status != ? THEN ? ELSE changed_at END WHERE id = ?`,
    ).bind(status, streak, at, r.code, r.ms, r.error, status, at, m.id));
    stmts.push(env.DB.prepare(
      `INSERT INTO monitor_hours (monitor_id, hour, checks, fails, total_ms) VALUES (?, ?, 1, ?, ?)
       ON CONFLICT(monitor_id, hour) DO UPDATE SET checks = checks + 1, fails = fails + excluded.fails, total_ms = total_ms + excluded.total_ms`,
    ).bind(m.id, hour, r.ok ? 0 : 1, r.ms));
    if (flipped && status === "down") {
      stmts.push(env.DB.prepare("INSERT INTO incidents (id, monitor_id, started_at, cause) VALUES (?, ?, ?, ?)")
        .bind(crypto.randomUUID().slice(0, 12), m.id, at, r.error));
      alerts.push({ m, r, down: true });
    } else if (flipped) {
      stmts.push(env.DB.prepare("UPDATE incidents SET ended_at = ? WHERE monitor_id = ? AND ended_at IS NULL").bind(at, m.id));
      alerts.push({ m, r, down: false, since: m.changed_at });
    }
  }
  if (stmts.length) await env.DB.batch(stmts);
  await Promise.all(alerts.map((a) => alert(env, a).catch((err) => console.error("uptime alert failed", err))));
  return checked.length;
}

export async function pruneHistory(env) {
  const oldest = Math.floor(now() / 3600) - HISTORY_HOURS;
  await env.DB.prepare("DELETE FROM monitor_hours WHERE hour < ?").bind(oldest).run();
}

function duration(seconds) {
  if (seconds < 90) return `${seconds} seconds`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} minutes`;
  return `${(seconds / 3600).toFixed(1)} hours`;
}

async function alert(env, { m, r, down, since }) {
  const site = env.SITE_URL || "https://reprolog.shivampandey78970.workers.dev";
  const subject = down ? `🔴 ${m.name} is down` : `🟢 ${m.name} is back up`;
  const detail = down ? `Reason: ${r.error}` : `It was down for ${duration(now() - (since || now()))}. Response time now ${r.ms} ms.`;
  const text = `${subject}\n\n${m.url}\n${detail}\n\nUptime dashboard: ${site}/app#uptime`;
  const color = down ? "#d4553a" : "#2f8f5b";
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  const html = `<!doctype html><html><body style="margin:0;background:#f3f1ec;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:36px 16px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:460px">
<tr><td align="center" style="padding:0 0 18px;font:800 22px/1 -apple-system,Segoe UI,Arial,sans-serif;color:#1d1f1c">repro<span style="color:#0d8a7a">log</span></td></tr>
<tr><td style="background:#fff;border:1px solid #e2ded4;border-top:4px solid ${color};border-radius:14px;padding:28px">
<p style="margin:0 0 6px;font:700 12px/1 Arial,sans-serif;letter-spacing:.08em;text-transform:uppercase;color:${color}">${down ? "Down" : "Recovered"}</p>
<h1 style="margin:0 0 10px;font-size:20px;color:#1d1f1c">${esc(m.name)} ${down ? "is not responding" : "is back up"}</h1>
<p style="margin:0 0 16px;font:13px/1.5 Menlo,Consolas,monospace;color:#5f625b;word-break:break-all">${esc(m.url)}</p>
<p style="margin:0 0 22px;font-size:15px;line-height:1.55;color:#1d1f1c">${esc(detail)}</p>
<a href="${site}/app#uptime" style="display:inline-block;background:#0d8a7a;color:#fff;text-decoration:none;font-weight:600;padding:10px 18px;border-radius:9px">Open uptime dashboard</a>
</td></tr>
<tr><td align="center" style="padding:16px 0 0;font-size:12px;color:#92948c">You get this because you added this URL to reprolog uptime checks.</td></tr>
</table></td></tr></table></body></html>`;
  const slack = `${down ? ":red_circle:" : ":large_green_circle:"} *${m.name}* ${down ? "is down" : "is back up"}\n${m.url}\n${detail} · <${site}/app#uptime|Uptime dashboard>`;
  await notify(env, { id: m.user_id, email: m.email }, "uptime", { email: { subject, text, html }, slack });
}

// ---------------------------------------------------------------- API (session only)

export async function monitorsApi(request, env, url, userId, { json, HttpError }) {
  const path = url.pathname;
  const method = request.method;
  const plan = await userPlan(env, userId);

  if (path === "/api/monitors" && method === "GET") {
    const hour = Math.floor(now() / 3600);
    const { results: monitors } = await env.DB.prepare(
      `SELECT id, name, url, status, paused, last_checked_at, last_code, last_ms, last_error, changed_at, created_at
         FROM monitors WHERE user_id = ? ORDER BY created_at`).bind(userId).all();
    const { results: hours } = await env.DB.prepare(
      `SELECT h.monitor_id, h.hour, h.checks, h.fails, h.total_ms FROM monitor_hours h
         JOIN monitors m ON m.id = h.monitor_id WHERE m.user_id = ? AND h.hour > ?`).bind(userId, hour - 24 * 30).all();
    const { results: incidents } = await env.DB.prepare(
      `SELECT i.monitor_id, i.started_at, i.ended_at, i.cause FROM incidents i JOIN monitors m ON m.id = i.monitor_id
        WHERE m.user_id = ? ORDER BY i.started_at DESC LIMIT 50`).bind(userId).all();
    for (const m of monitors) {
      const mine = hours.filter((h) => h.monitor_id === m.id);
      const sum = (rows, k) => rows.reduce((a, h) => a + h[k], 0);
      const pct = (rows) => (sum(rows, "checks") ? 100 * (1 - sum(rows, "fails") / sum(rows, "checks")) : null);
      const day = mine.filter((h) => h.hour > hour - 24);
      m.uptime_24h = pct(day);
      m.uptime_30d = pct(mine);
      m.avg_ms_24h = sum(day, "checks") ? Math.round(sum(day, "total_ms") / sum(day, "checks")) : null;
      // 24 hourly buckets, oldest first: null = no checks, else share of failed checks.
      m.hours = Array.from({ length: 24 }, (_, i) => {
        const h = day.find((x) => x.hour === hour - 23 + i);
        return h ? { fails: h.fails, checks: h.checks, ms: Math.round(h.total_ms / h.checks) } : null;
      });
      m.incidents = incidents.filter((i) => i.monitor_id === m.id).slice(0, 5);
    }
    return json({ plan, limit: MAX_MONITORS, monitors });
  }

  if (plan !== "pro") throw new HttpError(402, "Uptime checks are part of Pro");

  if (path === "/api/monitors" && method === "POST") {
    const body = await request.json().catch(() => ({}));
    const target = checkUrl(body.url);
    if (!target) throw new HttpError(400, "use a public http:// or https:// URL");
    const { n } = await env.DB.prepare("SELECT COUNT(*) AS n FROM monitors WHERE user_id = ?").bind(userId).first();
    if (n >= MAX_MONITORS) throw new HttpError(403, `Pro includes ${MAX_MONITORS} uptime checks`);
    const name = String(body.name || "").trim().slice(0, 60) || new URL(target).host;
    const m = { id: crypto.randomUUID().slice(0, 12), name, url: target };
    await env.DB.prepare("INSERT INTO monitors (id, user_id, name, url, created_at) VALUES (?, ?, ?, ?, ?)")
      .bind(m.id, userId, name, target, now()).run();
    return json(m);
  }

  const act = path.match(/^\/api\/monitors\/([\w-]+)\/(delete|pause|resume)$/);
  if (act && method === "POST") {
    const [, id, verb] = act;
    const sql = verb === "delete" ? "DELETE FROM monitors WHERE id = ? AND user_id = ?"
      : `UPDATE monitors SET paused = ${verb === "pause" ? 1 : 0}, status = CASE WHEN ${verb === "pause" ? 1 : 0} = 1 THEN status ELSE 'pending' END WHERE id = ? AND user_id = ?`;
    const res = await env.DB.prepare(sql).bind(id, userId).run();
    if (verb === "delete" && res.meta.changes) {
      await env.DB.batch([
        env.DB.prepare("DELETE FROM monitor_hours WHERE monitor_id = ?").bind(id),
        env.DB.prepare("DELETE FROM incidents WHERE monitor_id = ?").bind(id),
      ]);
    }
    return json({ ok: res.meta.changes > 0 });
  }
  throw new HttpError(404, "not found");
}
