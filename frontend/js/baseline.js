/* Baseline regression: freeze a run as the scene baseline, compare later runs. */

let scenes = [];
let runs = [];
let labels = {};
let currentBaseline = null;
let currentCmp = null;
let chart = null;

const OVERALL = {
  improved: ["整体优于基线 ✓", "good"],
  regressed: ["整体劣于基线 ✗", "bad"],
  mixed: ["有改善也有回退", "mixed"],
  changed: ["相对基线有显著变化", "mixed"],
  unchanged: ["与基线基本持平", ""],
};

const SOURCE_BADGE = {
  intact: ["来源运行一致", "finished"],
  extended: ["来源运行已延长", "running"],
  modified: ["来源运行已改动", "paused"],
  missing: ["来源运行已删除", "stopped"],
};

function verdictBadge(v) {
  const map = { better: ["改善", "finished"], worse: ["恶化", "stopped"],
                unchanged: ["持平", "ready"], changed: ["变化", "paused"] };
  const [label, cls] = map[v] || [v, "ready"];
  return `<span class="badge ${cls}">${label}</span>`;
}

function fmtPct(v) {
  if (v == null) return "—";
  return (v > 0 ? "+" : "") + Number(v).toFixed(1) + "%";
}

function fmtSigned(v) {
  if (v == null) return "—";
  return (v > 0 ? "+" : "") + fmt(v);
}

/* ------------------------------------------------------------------ */
/* Baseline management                                                 */
/* ------------------------------------------------------------------ */
function sceneRuns(sceneId) {
  return runs.filter((r) => r.scene_id === sceneId);
}

function runOptions(rs) {
  return rs.map((r) =>
    `<option value="${esc(r.id)}">${esc(r.name)} · 第${r.current_step}步</option>`).join("");
}

async function onScene() {
  const sid = el("sceneSel").value;
  currentBaseline = null;
  currentCmp = null;
  el("compareCard").style.display = "none";
  el("chartCard").style.display = "none";

  const rs = sceneRuns(sid);
  el("baseRunSel").innerHTML = runOptions(rs) || '<option value="">（该场景暂无运行）</option>';
  el("candRunSel").innerHTML = '<option value="">— 选择运行 —</option>' + runOptions(rs);
  if (!sid) { el("baseInfo").innerHTML = ""; return; }

  try { currentBaseline = await get(`/api/baselines/${sid}`); }
  catch (e) { currentBaseline = null; }
  renderBaseInfo();
  if (currentBaseline) {
    el("compareCard").style.display = "block";
    el("cmpBody").style.display = "none";
    el("verdictBanner").innerHTML =
      '<div class="notice">选择右上角「对比运行」后自动与基线对比。</div>';
  }
}

function renderBaseInfo() {
  const doc = currentBaseline;
  if (!doc) {
    el("delBaseBtn").style.display = "none";
    el("baseInfo").innerHTML =
      '<p class="muted small">该场景暂无基线。选择一次运行，点击「设为 / 更新基线」将其冻结为对比基准。</p>';
    return;
  }
  el("delBaseBtn").style.display = "";
  const [srcLabel, srcCls] = SOURCE_BADGE[(doc.source || {}).status] || ["未知", "ready"];
  const hist = (doc.history || []).map((h) =>
    `<div class="muted small">└ 旧版 v${h.version}：${esc(h.run_name || h.run_id)} · ${h.steps} 步 · 指纹 <span class="pill">${esc((h.hash || "").slice(0, 12))}</span> · 替换于 ${esc(h.replaced_at)}</div>`).join("");
  el("baseInfo").innerHTML = `
    <div class="notice">
      <div class="row between">
        <div><b>当前基线 v${doc.version}</b>：${esc(doc.run_name)}（${doc.steps} 步 · 冻结于 ${esc(doc.updated_at)}）</div>
        <span class="badge ${srcCls}">${srcLabel}</span>
      </div>
      <div class="muted small" style="margin-top:6px">
        指纹 <span class="pill">${esc((doc.hash || "").slice(0, 12))}</span> · 种子 ${doc.seed} · ${esc(doc.note || "无备注")}
      </div>
      <div class="muted small">${esc((doc.source || {}).detail || "")}</div>
      ${hist ? `<div style="margin-top:6px">${hist}</div>` : ""}
    </div>`;
}

async function markBaseline() {
  const scene_id = el("sceneSel").value;
  const run_id = el("baseRunSel").value;
  if (!scene_id || !run_id) { alert("请选择场景与运行"); return; }
  try {
    await post("/api/baselines", { scene_id, run_id, note: el("baseNote").value.trim() });
    await onScene();
  } catch (e) { alert("设置基线失败：" + e.message); }
}

async function deleteBaseline() {
  const sid = el("sceneSel").value;
  if (!sid || !confirm("确认删除该场景的基线？删除后同场景运行将无法再做基线对比。")) return;
  await del(`/api/baselines/${sid}`);
  await onScene();
}

/* ------------------------------------------------------------------ */
/* Comparison                                                          */
/* ------------------------------------------------------------------ */
async function compare() {
  const rid = el("candRunSel").value;
  if (!rid) return;
  try {
    currentCmp = await get(`/api/runs/${rid}/baseline`);
    renderComparison();
  } catch (e) {
    currentCmp = null;
    el("cmpBody").style.display = "none";
    el("chartCard").style.display = "none";
    el("verdictBanner").innerHTML = `<div class="notice error">${esc(e.message)}</div>`;
  }
}

function renderComparison() {
  const cmp = currentCmp;
  el("cmpBody").style.display = "block";

  const [text, kind] = OVERALL[cmp.verdict.overall] || [cmp.verdict.overall, ""];
  el("verdictBanner").innerHTML = `
    <div class="notice ${kind}">
      <b>${text}</b> —— 相对基线 v${cmp.baseline.version}「${esc(cmp.baseline.run_name)}」
      （指纹 <span class="pill">${esc(cmp.baseline.hash.slice(0, 12))}</span> ·
      重叠区间 第${cmp.alignment.overlap_start}–${cmp.alignment.overlap_end}步）
    </div>`;

  const anomalies = Object.values(cmp.metrics)
    .reduce((n, m) => n + m.anomalies.length, 0);
  el("kpis").innerHTML = [
    ["改善指标", cmp.verdict.better, "项"],
    ["恶化指标", cmp.verdict.worse, "项"],
    ["显著波动", cmp.verdict.changed, "项"],
    ["持平指标", cmp.verdict.unchanged, "项"],
  ].map(([k, v, d]) => `<div class="stat"><div class="k">${k}</div><div class="v">${v}</div><div class="d">${d}</div></div>`).join("");

  el("summaryList").innerHTML = cmp.summary.map((s) => `<li>${esc(s)}</li>`).join("");

  el("cfgDiffWrap").innerHTML = cmp.config_diff.length ? `
    <table><thead><tr><th>参数</th><th class="num">基线值</th><th class="num">本次值</th></tr></thead>
    <tbody>${cmp.config_diff.map((d) => `
      <tr><td>${esc(d.label)}</td><td class="num">${fmt(d.baseline)}</td><td class="num">${fmt(d.candidate)}</td></tr>`).join("")}
    </tbody></table>` : '<p class="muted small">参数与基线完全一致。</p>';

  const rows = Object.values(cmp.metrics).map((m) => {
    const cls = { better: "delta-good", worse: "delta-bad" }[m.verdict] || "";
    return `<tr>
      <td>${esc(m.label)}</td>
      <td class="num">${fmt(m.base_mean)}</td>
      <td class="num">${fmt(m.cand_mean)}</td>
      <td class="num ${cls}">${fmtSigned(m.mean_delta)}</td>
      <td class="num ${cls}">${fmtPct(m.mean_delta_pct)}</td>
      <td class="num">±${fmt(m.band)}</td>
      <td class="num">${fmt(m.base_final)} → ${fmt(m.cand_final)}</td>
      <td>${verdictBadge(m.verdict)}</td>
    </tr>`;
  }).join("");
  el("metricTable").innerHTML = `
    <thead><tr><th>指标</th><th class="num">基线均值</th><th class="num">本次均值</th>
    <th class="num">Δ均值</th><th class="num">Δ%</th><th class="num">噪声带</th>
    <th class="num">末步 基线→本次</th><th>判定</th></tr></thead>
    <tbody>${rows}</tbody>`;

  const anomRows = [];
  for (const m of Object.values(cmp.metrics)) {
    for (const a of m.anomalies) {
      anomRows.push(`<tr>
        <td>${esc(m.label)}</td>
        <td class="num">第 ${a.start}–${a.end} 步</td>
        <td class="num">${a.steps}</td>
        <td>${a.side === "above" ? "偏高" : "偏低"}</td>
        <td class="num">${fmtSigned(a.max_dev)}（第 ${a.max_step} 步）</td>
        <td class="num">${fmtSigned(a.mean_dev)}</td>
      </tr>`);
    }
  }
  el("anomWrap").innerHTML = anomRows.length ? `
    <p class="muted small">共 ${anomalies} 个异常波动区间（超出噪声带且持续 ≥${cmp.noise.min_run} 步）：</p>
    <table><thead><tr><th>指标</th><th class="num">区间</th><th class="num">持续步数</th>
    <th>方向</th><th class="num">最大偏差</th><th class="num">平均偏差</th></tr></thead>
    <tbody>${anomRows.join("")}</tbody></table>`
    : '<p class="muted small">未检测到异常波动区间 —— 所有偏差均在基线自然抖动范围内。</p>';

  const keys = Object.keys(cmp.metrics);
  el("chartMetricSel").innerHTML = keys.map((k) =>
    `<option value="${esc(k)}">${esc(cmp.metrics[k].label)}</option>`).join("");
  el("chartCard").style.display = keys.length ? "block" : "none";
  renderChart();
}

function renderChart() {
  if (!currentCmp) return;
  const key = el("chartMetricSel").value;
  const m = currentCmp.metrics[key];
  if (!m) return;
  if (!chart) chart = echarts.init(el("chart"), "dark");
  el("chartTitle").textContent = `${m.label} · 基线 vs 本次运行`;

  const band = m.band || 0;
  const option = {
    backgroundColor: "transparent",
    tooltip: { trigger: "axis" },
    legend: { textStyle: { color: "#8b98a5" }, top: 0,
              data: ["基线", "本次运行", "正常波动带"] },
    grid: { left: 60, right: 24, top: 40, bottom: 40 },
    xAxis: { type: "category", data: m.aligned.steps, name: "时间步",
             axisLine: { lineStyle: { color: "#26313f" } } },
    yAxis: { type: "value", axisLine: { lineStyle: { color: "#26313f" } },
             splitLine: { lineStyle: { color: "#1b2430" } } },
    series: [
      { name: "band-low", type: "line", stack: "band", symbol: "none", silent: true,
        lineStyle: { opacity: 0 }, tooltip: { show: false },
        data: m.aligned.base.map((v) => v - band) },
      { name: "正常波动带", type: "line", stack: "band", symbol: "none", silent: true,
        lineStyle: { opacity: 0 }, areaStyle: { color: "rgba(139,152,165,0.16)" },
        tooltip: { show: false },
        data: m.aligned.base.map(() => 2 * band) },
      { name: "基线", type: "line", showSymbol: false, smooth: true,
        lineStyle: { color: "#8b98a5", width: 2 }, itemStyle: { color: "#8b98a5" },
        data: m.aligned.base },
      { name: "本次运行", type: "line", showSymbol: false, smooth: true,
        lineStyle: { color: "#4f8cff", width: 2 }, itemStyle: { color: "#4f8cff" },
        data: m.aligned.cand,
        markArea: { silent: true, itemStyle: { color: "rgba(231,76,60,0.12)" },
          data: m.anomalies.map((a) => [{ xAxis: a.start }, { xAxis: a.end }]) } },
    ],
  };
  chart.setOption(option, true);
  chart.resize();
}

/* ------------------------------------------------------------------ */
async function init() {
  const { domains } = await get("/api/catalog");
  for (const d of Object.values(domains)) {
    for (const m of d.metrics) labels[m.key] = m.label;
  }
  el("sceneSel").onchange = onScene;
  el("markBtn").onclick = markBaseline;
  el("delBaseBtn").onclick = deleteBaseline;
  el("candRunSel").onchange = compare;
  el("chartMetricSel").onchange = renderChart;

  scenes = await fillSceneSelect(el("sceneSel"));
  const { runs: rs } = await get("/api/runs");
  runs = rs;

  const q = new URLSearchParams(window.location.search).get("run");
  const qr = q && runs.find((r) => r.id === q);
  if (qr) el("sceneSel").value = qr.scene_id;
  else if (el("sceneSel").options.length > 1) el("sceneSel").selectedIndex = 1;
  await onScene();
  if (qr && el("candRunSel").querySelector(`option[value="${q}"]`)) {
    el("candRunSel").value = q;
    await compare();
  }
}

window.addEventListener("resize", () => chart && chart.resize());

init().catch((e) => console.error(e));
