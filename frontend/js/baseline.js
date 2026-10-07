/* Baseline regression: mark/replicate baselines, auto-compare, diff report. */

let scenes = [];
let currentBaseline = null;
let currentDiff = null;
let chartMain = null;
let chartZ = null;
let labels = {};

/* ------------------------------------------------------------------ */
/* Helpers                                                            */
/* ------------------------------------------------------------------ */

function integrityBadge(doc) {
  const map = {
    ok: '<span class="badge finished">完整</span>',
    building: '<span class="badge running">构建中…</span>',
    corrupt: '<span class="badge error">已被修改</span>',
  };
  return map[doc.integrity || "ok"] || "";
}

function statusPill(status) {
  const map = {
    active: '<span class="badge finished">启用中</span>',
    retired: '<span class="badge ready">已停用</span>',
    deleted: '<span class="badge error">基线已删除</span>',
    building: '<span class="badge running">构建中</span>',
    error: '<span class="badge error">错误</span>',
  };
  return map[status] || `<span class="badge">${esc(status)}</span>`;
}

function verdictBadge(v) {
  const cls = { better: "finished", worse: "error", mixed: "paused",
                no_change: "ready" }[v] || "ready";
  const txt = { better: "变好 ✅", worse: "变坏 ⚠️", mixed: "有好有坏 ↕️",
                no_change: "无显著差异" }[v] || v;
  return `<span class="badge ${cls}">${txt}</span>`;
}

function sceneName(id) {
  const s = scenes.find((x) => x.id === id);
  return s ? s.name : id;
}

/* ------------------------------------------------------------------ */
/* Creation                                                           */
/* ------------------------------------------------------------------ */

async function loadScenes() {
  const res = await get("/api/scenes");
  scenes = res.scenes;
  for (const s of scenes) {
    el("markScene").insertAdjacentHTML(
      "beforeend", `<option value="${esc(s.id)}">${esc(s.name)}</option>`);
    el("repScene").insertAdjacentHTML(
      "beforeend", `<option value="${esc(s.id)}">${esc(s.name)}</option>`);
  }
  el("markScene").onchange = loadRunsForMark;
}

async function loadRunsForMark() {
  const sid = el("markScene").value;
  const { runs } = await get("/api/runs");
  const opts = runs
    .filter((r) => r.scene_id === sid)
    .map((r) => `<option value="${esc(r.id)}">${esc(r.name)} · ${r.current_step}步 · seed ${r.seed}</option>`)
    .join("");
  el("markRuns").innerHTML = opts || '<option value="">（该场景暂无运行）</option>';
}

async function markBaseline() {
  const ids = [...el("markRuns").selectedOptions].map((o) => o.value).filter(Boolean);
  if (!ids.length) { alert("请选择至少一次运行"); return; }
  el("markStatus").textContent = "正在冻结基线…";
  try {
    await post("/api/baselines", { run_ids: ids, name: el("markName").value.trim() });
    el("markStatus").textContent = "已建立 ✓";
    await loadBaselines();
  } catch (e) { el("markStatus").textContent = "失败：" + e.message; }
}

let repPoll = null;
async function replicateBaseline() {
  const sid = el("repScene").value;
  if (!sid) { alert("请选择场景"); return; }
  const body = {
    scene_id: sid,
    replicates: parseInt(el("repN").value || "5", 10),
    steps: parseInt(el("repSteps").value || "150", 10),
    name: el("repName").value.trim(),
  };
  const doc = await post("/api/baselines", body);
  el("repStatus").textContent = "已提交，后台运行复制中…";
  clearInterval(repPoll);
  repPoll = setInterval(async () => {
    const b = await get(`/api/baselines/${doc.id}`);
    if (b.status === "active") {
      clearInterval(repPoll);
      el("repStatus").textContent = "基线已生成 ✓";
      await loadBaselines();
    } else if (b.status === "error") {
      clearInterval(repPoll);
      el("repStatus").textContent = "构建失败：" + (b.error || "");
    }
  }, 1000);
}

/* ------------------------------------------------------------------ */
/* Baseline list                                                      */
/* ------------------------------------------------------------------ */

async function loadBaselines() {
  const { baselines } = await get("/api/baselines");
  if (!baselines.length) {
    el("baselineList").innerHTML =
      '<p class="muted small">还没有基线。先跑几次同场景运行，再在上方标记，或直接生成复制基线。</p>';
    return;
  }
  el("baselineList").innerHTML = baselines.map((b) => `
    <div class="list-item" data-id="${esc(b.id)}">
      <div style="min-width:0">
        <div class="t">${esc(b.name)} ${statusPill(b.status)} ${integrityBadge(b)}</div>
        <div class="s">
          ${esc(sceneName(b.scene_id))} · ${b.n_replicates} 次复制 · ${b.steps} 步
          · ${esc(b.created_at)}
          ${b.superseded_by ? "· 已被 " + esc(b.superseded_by) + " 取代" : ""}
        </div>
      </div>
      <div class="row gap-6">
        <button class="btn small view">查看运行 / 对比</button>
        ${b.status === "active"
          ? '<button class="btn small retire">停用</button>'
          : b.integrity === "corrupt"
            ? '<button class="btn small" disabled>已损坏</button>'
            : '<button class="btn small activate">启用</button>'}
        <button class="btn danger small del">删除</button>
      </div>
    </div>`).join("");

  el("baselineList").querySelectorAll(".list-item").forEach((li) => {
    const id = li.dataset.id;
    li.querySelector(".view").onclick = () => selectBaseline(id);
    const retire = li.querySelector(".retire");
    if (retire) retire.onclick = async () => {
      await post(`/api/baselines/${id}/retire`, {});
      await loadBaselines();
    };
    const activate = li.querySelector(".activate");
    if (activate) activate.onclick = async () => {
      try { await post(`/api/baselines/${id}/activate`, {}); await loadBaselines(); }
      catch (e) { alert(e.message); }
    };
    li.querySelector(".del").onclick = async () => {
      if (!confirm("删除基线后，已生成的历史对比仍会保留（标注「基线已删除」）。确认删除？")) return;
      await del(`/api/baselines/${id}`);
      await loadBaselines();
    };
  });
}

/* ------------------------------------------------------------------ */
/* Baseline detail: runs + cached verdicts                            */
/* ------------------------------------------------------------------ */

async function selectBaseline(id) {
  currentBaseline = await get(`/api/baselines/${id}`);
  if (!currentDiff || currentDiff.baseline_id !== id) currentDiff = null;
  const data = await get(`/api/baselines/${id}/runs`);
  const rows = data.runs.map((r) => {
    const d = r.diff;
    const verdict = d
      ? `${verdictBadge(d.verdict)} <span class="muted small">${esc(d.generated_at || "")}</span>`
      : '<span class="muted small">未对比</span>';
    const stale = d && d.stale ? ' <span class="badge paused">已过时</span>' : "";
    return `<div class="list-item" data-run="${esc(r.meta.id)}">
      <div style="min-width:0">
        <div class="t">${esc(r.meta.name)} ${statusBadge(r.meta.status)}${stale}</div>
        <div class="s">第 ${r.meta.current_step} 步 · seed ${r.meta.seed} · ${esc(r.meta.updated_at)}</div>
      </div>
      <div class="row gap-6">
        <span>${verdict}</span>
        <button class="btn primary small compare">对比</button>
      </div>
    </div>`;
  }).join("");
  const warn = currentBaseline.n_replicates < 3
    ? `<div class="notice warn" style="margin:10px 0">该基线仅含 ${currentBaseline.n_replicates} 次复制，噪声带为稳健估计；建议用 5 次以上复制以获得可靠结论。</div>` : "";
  el("sceneRunList").style.display = "block";
  el("sceneRunList").innerHTML =
    `<h3 style="margin-top:4px">${esc(currentBaseline.name)} · 同场景运行</h3>${warn}`
    + (rows || '<p class="muted">同场景暂无运行</p>');
  el("sceneRunList").querySelectorAll(".compare").forEach((btn) => {
    btn.onclick = () => runDiff(btn.closest(".list-item").dataset.run);
  });
  el("sceneRunList").querySelectorAll(".list-item").forEach((li) => {
    li.style.cursor = "pointer";
    li.onclick = (ev) => {
      if (!ev.target.closest(".compare")) runDiff(li.dataset.run);
    };
  });
  el("diffCard").style.display = "block";
  if (chartMain) { chartMain.clear(); chartZ.clear(); }
  if (!currentDiff) {
    el("metricCards").innerHTML = "";
    el("summaryBox").innerHTML = "";
    el("verdictBox").innerHTML =
      '<p class="muted small">在上方选择一次运行查看差异摘要。</p>';
  }
}

/* ------------------------------------------------------------------ */
/* Diff rendering                                                      */
/* ------------------------------------------------------------------ */

async function runDiff(runId) {
  if (!currentBaseline) return;
  try {
    currentDiff = await post(`/api/runs/${runId}/baseline-diff`,
      { baseline_id: currentBaseline.id });
  } catch (e) { alert("对比失败：" + e.message); return; }
  renderDiff();
}

function renderDiff() {
  const d = currentDiff;
  el("diffTitle").innerHTML =
    `${esc(d.run_name)} <span class="muted small">vs ${esc(d.baseline_name)}</span>`;

  const integWarn = {
    baseline_missing: '<div class="notice error">该基线已被删除；以下为历史对比结果（嵌入了基线快照与校验和，仍可追溯但不能重新计算）。</div>',
    baseline_corrupt: '<div class="notice error">基线内容校验失败（可能被手动修改）；历史结果仅供参考。</div>',
  }[d.integrity] || "";
  const staleWarn = d.stale
    ? '<div class="notice warn">该对比生成后候选运行又继续推进过，结论可能已过时——请重新对比。</div>' : "";
  const baseTag = d.baseline_status === "retired"
    ? ' <span class="badge ready">基线已停用</span>' : "";

  el("verdictBox").innerHTML = integWarn + staleWarn + `
    <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:8px 0">
      <div style="font-size:18px;font-weight:700">${verdictBadge(d.verdict)}
        <span class="muted small">${esc(d.verdict_label)}</span>${baseTag}</div>
      <span class="muted small">共同 ${d.common_steps} 步 · 候选 ${d.candidate_steps} 步 · 基线 ${d.baseline_steps} 步 · ${d.n_replicates} 次复制</span>
      <button class="btn small" id="recompareBtn">重新对比</button>
    </div>`;
  el("recompareBtn").onclick = () => runDiff(d.run_id);

  // metric cards
  const q = { better: "better", worse: "danger", neutral: "paused", none: "ready" };
  el("metricCards").innerHTML = Object.entries(d.metrics).map(([k, m]) => {
    const state = m.family_changed ? (m.quality === "none" ? "neutral" : m.quality) : "none";
    const sig = m.significance === "significant" ? "显著"
      : (m.significance === "slight" ? "轻微" : "");
    const pct = m.delta_pct == null ? "—" : `${m.delta_pct > 0 ? "+" : ""}${m.delta_pct}%`;
    return `<div class="stat metric-card" data-key="${esc(k)}" style="cursor:pointer;
        border:1px solid var(--border)">
      <div class="k">${esc(m.label)}</div>
      <div class="v" style="font-size:18px">${pct}</div>
      <div class="d">${sig}${sig ? "·" : ""}zmax ${m.max_abs_z} ·
        <span class="${state === "better" ? "" : state === "worse" ? "" : ""}">
        ${m.family_changed ? "已变化" : (m.changed ? "弱偏离" : "正常带内")}</span></div>
    </div>`;
  }).join("");
  el("metricCards").querySelectorAll(".metric-card").forEach((c) => {
    c.onclick = () => { el("metricSel").value = c.dataset.key; renderCharts(); };
  });

  // summary text
  el("summaryBox").innerHTML = `
    <h3 style="margin:6px 0">差异摘要</h3>
    <ul style="margin:0;padding-left:18px;line-height:1.9">
      ${d.summary.map((s) => `<li>${esc(s)}</li>`).join("")}
    </ul>`;

  // metric selector
  const keys = Object.keys(d.metrics);
  el("metricSel").innerHTML = keys.map((k) =>
    `<option value="${esc(k)}">${esc(d.metrics[k].label || k)}</option>`).join("");
  el("metricSel").onchange = renderCharts;
  el("normChk").onchange = renderCharts;
  renderCharts();
  // refresh the cached verdict badge in the run list above
  if (currentBaseline) selectBaseline(currentBaseline.id).catch(() => {});
}

function renderCharts() {
  const d = currentDiff;
  if (!d) return;
  const key = el("metricSel").value;
  const m = d.metrics[key];
  if (!m) return;
  const useNorm = el("normChk").checked;

  if (!chartMain) chartMain = echarts.init(el("mainchart"), "dark");
  if (!chartZ) chartZ = echarts.init(el("zchart"), "dark");

  if (useNorm) {
    const p = m.normalized;
    chartMain.setOption({
      backgroundColor: "transparent",
      title: { text: `${m.label} · 按完成进度对齐`, textStyle: { fontSize: 13, color: "#8b98a5" } },
      tooltip: { trigger: "axis" },
      legend: { data: ["基线均值", "候选运行"], top: 22, textStyle: { color: "#8b98a5" } },
      grid: { left: 60, right: 24, top: 56, bottom: 40 },
      xAxis: { type: "value", min: 0, max: 1, name: "完成进度",
               axisLine: { lineStyle: { color: "#26313f" } } },
      yAxis: { type: "value", splitLine: { lineStyle: { color: "#1b2430" } } },
      series: [
        { name: "基线均值", type: "line", showSymbol: false, data: p.progress.map((x, i) => [x, p.base[i]]) },
        { name: "候选运行", type: "line", showSymbol: false, data: p.progress.map((x, i) => [x, p.candidate[i]]) },
      ],
    }, true);
    chartZ.setOption({
      backgroundColor: "transparent",
      title: { text: "标准化偏差 z（进度对齐）", textStyle: { fontSize: 12, color: "#8b98a5" } },
      tooltip: { trigger: "axis" },
      grid: { left: 60, right: 24, top: 40, bottom: 40 },
      xAxis: { type: "value", min: 0, max: 1 },
      yAxis: { type: "value" },
      series: [{ type: "line", showSymbol: false, data: p.progress.map((x, i) => [x, p.z[i]]) }],
    }, true);
    return;
  }

  const c = m.chart;
  const thr = c.threshold || 2;
  chartMain.setOption({
    backgroundColor: "transparent",
    title: { text: `${m.label} · 绝对步数对齐（阴影 = ${thr}σ 正常波动带）`,
             textStyle: { fontSize: 13, color: "#8b98a5" } },
    tooltip: { trigger: "axis" },
    legend: { data: ["候选运行", "基线均值"], top: 22, textStyle: { color: "#8b98a5" } },
    grid: { left: 60, right: 24, top: 56, bottom: 40 },
    xAxis: { type: "category", data: c.steps, name: "时间步",
             axisLine: { lineStyle: { color: "#26313f" } } },
    yAxis: { type: "value", splitLine: { lineStyle: { color: "#1b2430" } } },
    series: [
      { name: "候选运行", type: "line", showSymbol: false, data: c.candidate,
        lineStyle: { color: "#e8b339", width: 2 } },
      { name: "基线均值", type: "line", showSymbol: false, data: c.base,
        lineStyle: { color: "#4a90d9" } },
      { name: "波动带上界", type: "line", showSymbol: false, data: c.band_high,
        lineStyle: { opacity: 0 }, stack: "band", symbol: "none" },
      { name: "波动带", type: "line", showSymbol: false,
        data: c.band_high.map((h, i) => Math.max(0, h - c.band_low[i])),
        lineStyle: { opacity: 0 }, areaStyle: { color: "rgba(74,144,217,0.18)" },
        stack: "band", symbol: "none" },
    ],
  }, true);
  chartZ.setOption({
    backgroundColor: "transparent",
    title: { text: `窗口化标准化偏差 z（单指标阈值 ${thr}，持续越界才是真变化）`,
             textStyle: { fontSize: 12, color: "#8b98a5" } },
    tooltip: { trigger: "axis" },
    grid: { left: 60, right: 24, top: 40, bottom: 40 },
    xAxis: { type: "category", data: c.steps },
    yAxis: { type: "value" },
    series: [{
      type: "line", showSymbol: false, data: c.z,
      lineStyle: { color: "#e8b339" },
      markLine: { silent: true, symbol: "none", lineStyle: { type: "dashed" },
        data: [{ yAxis: thr, lineStyle: { color: "#57b37a" } },
               { yAxis: -thr, lineStyle: { color: "#57b37a" } }] },
    }],
  }, true);
}

/* ------------------------------------------------------------------ */

async function init() {
  await loadScenes();
  await loadRunsForMark();
  await loadBaselines();
  el("markBtn").onclick = markBaseline;
  el("repBtn").onclick = replicateBaseline;
  // poll any in-flight builds while page is open
  setInterval(loadBaselines, 4000);

  // deep links from history page
  const q = new URLSearchParams(window.location.search);
  if (q.get("baseline")) {
    await selectBaseline(q.get("baseline"));
  } else if (q.get("run")) {
    const { runs } = await get("/api/runs");
    const run = runs.find((r) => r.id === q.get("run"));
    if (run) {
      const active = await get(
        `/api/baselines?scene_id=${encodeURIComponent(run.scene_id)}`);
      const b = active.baselines.find((x) => x.status === "active");
      if (b) { await selectBaseline(b.id); await runDiff(run.id); }
      else { alert("该场景还没有启用中的基线，请先建立基线。"); }
    }
  }
}

window.addEventListener("resize", () => {
  if (chartMain) chartMain.resize();
  if (chartZ) chartZ.resize();
});

init().catch((e) => console.error(e));
