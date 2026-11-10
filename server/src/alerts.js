// Pro features built on ingest: request metrics, error groups (one alert per new kind
// of error), and alert delivery by email and Slack. Uptime alerts reuse notify().
import { sendMail } from "./mail.js";

const now = () => Math.floor(Date.now() / 1000);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
export const LATENCY_BUCKETS_MS = [5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000];

export async function alertSettings(env, userId) {
  const row = await env.DB.prepare("SELECT email_on_error, email_on_uptime, slack_webhook FROM alert_settings WHERE user_id = ?")
    .bind(userId).first();
  return { email_on_error: row ? !!row.email_on_error : true, email_on_uptime: row ? !!row.email_on_uptime : true,
    slack_webhook: row?.slack_webhook || null };
}

export function validSlackWebhook(url) {
  return /^https:\/\/hooks\.slack\.com\/(services|workflows|triggers)\/[\w/-]+$/.test(String(url || "").trim());
}

export async function sendSlack(webhook, text, blocks) {
  const res = await fetch(webhook, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(blocks ? { text, blocks } : { text }),
    signal: AbortSignal.timeout(8000),
  });
  if (!res.ok) throw new Error(`Slack returned ${res.status}: ${(await res.text()).slice(0, 120)}`);
}

// kind: "error" | "uptime". email: {subject, text, html}. slack: plain text (mrkdwn).
export async function notify(env, user, kind, { email, slack }) {
  const settings = await alertSettings(env, user.id);
  const jobs = [];
  if (email && env.SMTP_USER && (kind === "error" ? settings.email_on_error : settings.email_on_uptime)) {
    jobs.push(sendMail(env, { to: user.email, ...email }));
  }
  if (slack && settings.slack_webhook) {
    jobs.push(sendSlack(settings.slack_webhook, slack, [{ type: "section", text: { type: "mrkdwn", text: slack } }]));
  }
  const results = await Promise.allSettled(jobs);
  for (const r of results) if (r.status === "rejected") console.error(`${kind} alert failed`, r.reason);
}

export function alertEmail({ color, label, title, lines, link, linkText, footer }) {
  return `<!doctype html><html><body style="margin:0;background:#f3f1ec;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:36px 16px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:480px">
<tr><td align="center" style="padding:0 0 18px;font:800 22px/1 -apple-system,Segoe UI,Arial,sans-serif;color:#1d1f1c">repro<span style="color:#0d8a7a">log</span></td></tr>
<tr><td style="background:#fff;border:1px solid #e2ded4;border-top:4px solid ${color};border-radius:14px;padding:28px">
<p style="margin:0 0 6px;font:700 12px/1 Arial,sans-serif;letter-spacing:.08em;text-transform:uppercase;color:${color}">${esc(label)}</p>
<h1 style="margin:0 0 12px;font-size:20px;line-height:1.3;color:#1d1f1c">${esc(title)}</h1>
${lines.map((l) => `<p style="margin:0 0 10px;font:${l.mono ? "13px/1.5 Menlo,Consolas,monospace" : "15px/1.55 -apple-system,Segoe UI,Arial,sans-serif"};color:${l.muted ? "#5f625b" : "#1d1f1c"};word-break:break-word">${esc(l.text)}</p>`).join("")}
<a href="${esc(link)}" style="display:inline-block;margin-top:12px;background:#0d8a7a;color:#fff;text-decoration:none;font-weight:600;padding:10px 18px;border-radius:9px">${esc(linkText)}</a>
</td></tr>
<tr><td align="center" style="padding:16px 0 0;font-size:12px;color:#92948c">${esc(footer)}</td></tr>
</table></td></tr></table></body></html>`;
}

// ---------------------------------------------------------------- ingest hooks

export function metricStatements(env, projectId, rows) {
  const out = [];
  for (const m of (Array.isArray(rows) ? rows : []).slice(0, 500)) {
    const buckets = Array.isArray(m.buckets) ? m.buckets.slice(0, LATENCY_BUCKETS_MS.length + 1).map((n) => Math.max(0, n | 0)) : [];
    if (!Number.isFinite(m.minute) || !(m.count > 0)) continue;
    out.push(env.DB.prepare(
      "INSERT INTO metric_minutes (project_id, minute, method, route, count, errors, total_ms, buckets) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
    ).bind(projectId, m.minute | 0, String(m.method || "GET").slice(0, 10), String(m.route || "/").slice(0, 200),
      m.count | 0, Math.max(0, m.errors | 0), Number(m.total_ms) || 0, JSON.stringify(buckets)));
  }
  return out;
}

// Same error = same type, same endpoint (ids collapsed) and same message shape.
export async function fingerprint(cap) {
  const req = cap.request || {};
  const err = cap.error || {};
  const path = String(req.path || "/").replace(/\/\d+(?=\/|$)/g, "/:id").replace(/[0-9a-f]{8,}/gi, ":id");
  const type = err.type || `HTTP ${cap.response?.status ?? "?"}`;
  const msg = String(err.message || "").replace(/\d+/g, "N").replace(/'[^']*'|"[^"]*"/g, "…").slice(0, 120);
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(`${type}|${req.method}|${path}|${msg}`));
  return { fp: [...new Uint8Array(d)].slice(0, 10).map((b) => b.toString(16).padStart(2, "0")).join(""), type, path };
}

// Upserts error groups for failed captures; returns the groups seen for the first time.
export async function trackErrors(env, project, items) {
  const fresh = [];
  for (const { cap, id } of items) {
    const failed = cap.error || (cap.response?.status ?? 0) >= 500;
    if (!failed) continue;
    const { fp, type, path } = await fingerprint(cap);
    const at = Math.floor(Number(cap.created_at) || now());
    const res = await env.DB.prepare(
      `INSERT INTO error_groups (project_id, fingerprint, error_type, message, method, path, first_seen, last_seen, sample_id)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
       ON CONFLICT(project_id, fingerprint) DO UPDATE SET last_seen = excluded.last_seen, count = count + 1, sample_id = excluded.sample_id
       RETURNING count`,
    ).bind(project.id, fp, type, String(cap.error?.message || "").slice(0, 300), cap.request?.method || null, path, at, at, id).first();
    if (res?.count === 1) fresh.push({ type, path, method: cap.request?.method, message: cap.error?.message || "", id });
  }
  return fresh;
}

export async function alertNewErrors(env, userId, project, fresh, site) {
  if (!fresh.length) return;
  const user = await env.DB.prepare("SELECT id, email FROM users WHERE id = ?").bind(userId).first();
  if (!user) return;
  for (const e of fresh.slice(0, 3)) {  // a bad deploy can create many at once; cap the burst
    const link = `${site}/app#activity`;
    const title = `${e.type} in ${e.method || ""} ${e.path}`.replace(/\s+/g, " ");
    await notify(env, user, "error", {
      email: {
        subject: `New error in ${project.name}: ${e.type}`,
        text: `${title}\n${e.message}\n\nProject: ${project.name}\nCapture: ${e.id}\n${link}\n\nReproduce locally: reprolog replay ${e.id} --app main:app`,
        html: alertEmail({ color: "#d4553a", label: `New error · ${project.name}`, title, link, linkText: "Open in reprolog",
          lines: [{ text: e.message || "(no message)" }, { text: `reprolog replay ${e.id} --app main:app`, mono: true, muted: true }],
          footer: "You get one email per new kind of error. Change this under Alerts in your dashboard." }),
      },
      slack: `:red_circle: *New error in ${project.name}*\n*${title}*\n${e.message ? `> ${String(e.message).slice(0, 300)}\n` : ""}Reproduce: \`reprolog replay ${e.id} --app main:app\` · <${link}|Open in reprolog>`,
    });
  }
}

// ---------------------------------------------------------------- metrics query

function percentile(buckets, p) {
  const total = buckets.reduce((a, b) => a + b, 0);
  if (!total) return null;
  let seen = 0;
  for (let i = 0; i < buckets.length; i++) {
    seen += buckets[i];
    if (seen >= total * p) return i < LATENCY_BUCKETS_MS.length ? LATENCY_BUCKETS_MS[i] : LATENCY_BUCKETS_MS.at(-1) * 2;
  }
  return null;
}

const addBuckets = (into, add) => { for (let i = 0; i < add.length; i++) into[i] = (into[i] || 0) + add[i]; return into; };

export async function metricsSummary(env, projectId, hours) {
  const to = Math.floor(now() / 60);
  const from = to - hours * 60;
  const { results } = await env.DB.prepare(
    "SELECT minute, method, route, count, errors, total_ms, buckets FROM metric_minutes WHERE project_id = ? AND minute > ?",
  ).bind(projectId, from).all();
  const step = hours <= 1 ? 1 : hours <= 24 ? 15 : 60;  // minutes per chart point
  const points = Math.ceil((hours * 60) / step);
  const series = Array.from({ length: points }, (_, i) => ({ t: (from + i * step + step) * 60, count: 0, errors: 0, buckets: [] }));
  const routes = new Map();
  const total = { count: 0, errors: 0, total_ms: 0, buckets: [] };
  for (const r of results) {
    const b = JSON.parse(r.buckets);
    const s = series[Math.min(points - 1, Math.max(0, Math.floor((r.minute - from - 1) / step)))];
    s.count += r.count; s.errors += r.errors; addBuckets(s.buckets, b);
    const key = `${r.method} ${r.route}`;
    const rt = routes.get(key) || { method: r.method, route: r.route, count: 0, errors: 0, total_ms: 0, buckets: [] };
    rt.count += r.count; rt.errors += r.errors; rt.total_ms += r.total_ms; addBuckets(rt.buckets, b);
    routes.set(key, rt);
    total.count += r.count; total.errors += r.errors; total.total_ms += r.total_ms; addBuckets(total.buckets, b);
  }
  const shape = (x) => ({ count: x.count, errors: x.errors, error_rate: x.count ? x.errors / x.count : 0,
    avg_ms: x.count ? Math.round(x.total_ms / x.count) : null, p50: percentile(x.buckets, 0.5), p95: percentile(x.buckets, 0.95) });
  return {
    hours, step_minutes: step,
    total: { ...shape(total), per_minute: total.count / (hours * 60) },
    series: series.map((s) => ({ t: s.t, count: s.count, errors: s.errors, p95: percentile(s.buckets, 0.95) })),
    routes: [...routes.values()].map((r) => ({ method: r.method, route: r.route, ...shape(r) })).sort((a, b) => b.count - a.count).slice(0, 100),
  };
}
