/* Report generation: summary + metrics table + config + events. */

async function generate() {
  const runId = el("runSelect").value;
  if (!runId) { alert("请选择运行"); return; }
  el("genStatus").textContent = "生成中…";
  try {
    const rpt = await post(`/api/reports/${runId}`);
    render(rpt);
    el("genStatus").textContent = "已生成 ✓";
  } catch (e) { el("genStatus").textContent = "生成失败：" + e.message; }
}

function render(rpt) {
  const metrics = Object.values(rpt.metrics || {});
  const metricRows = metrics.map((m) => `
    <tr><td>${esc(m.label)}</td><td class="num">${fmt(m.initial)}</td>
    <td class="num">${fmt(m.final)}</td><td class="num">${fmt(m.min)}</td>
    <td class="num">${fmt(m.max)}</td><td class="num">${m.peak_step}</td></tr>`).join("");

  const events = (rpt.events || []).slice().reverse().map((e) => `
    <tr><td>${e.step}</td><td>${esc(e.type)}</td><td class="muted small">${esc((e.result && e.result.reason) || "")}</td></tr>`).join("");

  const bd = rpt.baseline;
  let baselineCard = "";
  if (bd) {
    const extra = (bd.stale ? ' <span class="badge paused">结论已过时</span>' : "")
      + (bd.integrity === "baseline_missing" ? ' <span class="badge error">基线已删除</span>'
         : bd.integrity === "baseline_corrupt" ? ' <span class="badge error">基线异常</span>'
         : bd.baseline_status === "retired" ? ' <span class="badge ready">基线已停用</span>' : "");
    const c = bd.counts || {};
    baselineCard = `
    <div class="card">
      <div class="row between">
        <h2 style="margin:0">基线回归结论</h2>
        <a class="btn small" href="/baseline.html?run=${esc(rpt.run_id)}">查看详细对比 →</a>
      </div>
      <div class="row gap-6" style="margin-top:10px">
        ${baselineVerdictBadge({ verdict: bd.verdict, baseline_name: bd.baseline_name,
                                 stale: bd.stale, integrity: bd.integrity })}${extra}
        <span class="muted small">基线：${esc(bd.baseline_name || bd.baseline_id)}</span>
      </div>
      <p class="muted small" style="margin:8px 0 0">
        改善 ${c.better || 0} · 恶化 ${c.worse || 0} · 仅变化 ${c.changed_neutral || 0}
        · 弱偏离 ${c.hinted || 0} · 无显著差异 ${c.unchanged || 0}
      </p>
    </div>`;
  }

  el("reportBody").innerHTML = `
    <div class="card">
      <div class="row between">
        <h2 style="margin:0">${esc(rpt.name)}</h2>
        <div class="row gap-6">
          <span class="badge">${DOMAIN_LABEL[rpt.domain] || rpt.domain}</span>
          <span class="badge">${MODEL_LABEL[rpt.model] || rpt.model}</span>
          <span class="badge ready">${rpt.steps} 步</span>
        </div>
      </div>
    </div>

    ${baselineCard}

    <div class="card">
      <h2>结论摘要</h2>
      <ul style="margin:0;padding-left:18px">${(rpt.summary || []).map((s) => `<li style="margin:6px 0">${esc(s)}</li>`).join("")}</ul>
    </div>

    <div class="card">
      <h2>指标统计</h2>
      <div style="overflow-x:auto"><table>
        <thead><tr><th>指标</th><th class="num">初始</th><th class="num">最终</th><th class="num">最小</th><th class="num">最大</th><th class="num">峰值步</th></tr></thead>
        <tbody>${metricRows || '<tr><td colspan="6" class="muted">无指标</td></tr>'}</tbody>
      </table></div>
    </div>

    <div class="grid grid-2">
      <div class="card">
        <h2>干预日志（${(rpt.events || []).length}）</h2>
        <div style="overflow-x:auto"><table>
          <thead><tr><th>步</th><th>类型</th><th>结果</th></tr></thead>
          <tbody>${events || '<tr><td colspan="3" class="muted">无干预</td></tr>'}</tbody>
        </table></div>
      </div>
      <div class="card">
        <h2>配置参数</h2>
        <pre class="mono small" style="margin:0;white-space:pre-wrap;color:var(--accent-2)">${esc(JSON.stringify(rpt.config, null, 2))}</pre>
      </div>
    </div>`;
}

async function init() {
  el("genBtn").onclick = generate;
  await fillRunSelect(el("runSelect"));
  const q = new URLSearchParams(window.location.search).get("run");
  if (q && el("runSelect").querySelector(`option[value="${q}"]`)) {
    el("runSelect").value = q;
    await generate();
  } else if (el("runSelect").options.length > 1) {
    el("runSelect").selectedIndex = 1;
    await generate();
  }
}

init().catch((e) => console.error(e));
