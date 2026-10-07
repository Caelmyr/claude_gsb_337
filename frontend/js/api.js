/* Shared fetch helpers for the REST API. */

async function api(path, opts = {}) {
  const options = Object.assign({ headers: { "Content-Type": "application/json" } }, opts);
  if (options.body && typeof options.body !== "string") {
    options.body = JSON.stringify(options.body);
  }
  const res = await fetch(path, options);
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try { const j = await res.json(); msg = j.error || j.details || msg; } catch (e) { /* ignore */ }
    throw new Error(msg);
  }
  if (res.status === 204) return null;
  const ct = res.headers.get("Content-Type") || "";
  return ct.includes("json") ? res.json() : res.text();
}

const get = (p) => api(p);
const post = (p, body) => api(p, { method: "POST", body: body || {} });
const put = (p, body) => api(p, { method: "PUT", body: body || {} });
const del = (p) => api(p, { method: "DELETE" });

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function fmt(v, digits = 2) {
  if (v == null) return "—";
  if (typeof v === "number") {
    if (Number.isInteger(v)) return String(v);
    return v.toFixed(digits);
  }
  return String(v);
}

function statusBadge(status) {
  const cls = { ready: "ready", running: "running", paused: "paused",
                finished: "finished", stopped: "stopped", error: "error",
                pending: "ready", building: "running",
                active: "finished", retired: "ready" }[status] || "ready";
  const label = { ready: "就绪", running: "运行中", paused: "已暂停",
                  finished: "已完成", stopped: "已停止", error: "错误",
                  pending: "等待中", building: "构建中",
                  active: "启用中", retired: "已停用" }[status] || status;
  return `<span class="badge ${cls}">${label}</span>`;
}

/* Compact baseline-regression verdict badge for run lists. */
function baselineVerdictBadge(bd) {
  if (!bd) return "";
  const cls = { better: "finished", worse: "error", mixed: "paused",
                no_change: "ready" }[bd.verdict] || "ready";
  const txt = { better: "较基线变好 ✅", worse: "较基线变坏 ⚠️",
                mixed: "较基线有好有坏 ↕️",
                no_change: "与基线无显著差异" }[bd.verdict] || bd.verdict;
  const extra = (bd.stale ? " · 已过时" : "")
    + (bd.integrity === "baseline_missing" ? " · 基线已删"
       : bd.integrity === "baseline_corrupt" ? " · 基线异常" : "");
  return `<span class="badge ${cls}" title="基线：${bd.baseline_name || ""}${extra}">${txt}${extra}</span>`;
}

/* ECharts default dark theme hook (loaded once, shared by all chart pages). */
function echartsTheme() {
  return "dark";
}
