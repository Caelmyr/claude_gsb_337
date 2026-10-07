/* History: browse scenes / runs / experiments / baselines. */

let hist = { scenes: [], runs: [], experiments: [], baselines: [] };
let tab = "scenes";

function renderScenes() {
  return hist.scenes.map((s) => `
    <div class="list-item">
      <div style="min-width:0">
        <div class="t">${esc(s.name)}</div>
        <div class="s">${DOMAIN_LABEL[s.domain] || s.domain} · ${MODEL_LABEL[s.model] || s.model} · ${esc(s.updated_at)}</div>
      </div>
      <div class="row gap-6">
        <a class="btn small" href="/config.html?scene=${esc(s.id)}">编辑</a>
        <button class="btn danger small del" data-kind="scenes" data-id="${esc(s.id)}">删除</button>
      </div>
    </div>`).join("") || '<p class="muted">暂无场景。</p>';
}

function renderRuns() {
  return hist.runs.map((r) => `
    <div class="list-item">
      <div style="min-width:0">
        <div class="t">${esc(r.name)} ${statusBadge(r.status)}</div>
        <div class="s">${DOMAIN_LABEL[r.domain] || r.domain} · ${MODEL_LABEL[r.model] || r.model} · 第 ${r.current_step} 步 · ${esc(r.updated_at)}</div>
        ${r.baseline_diff ? `<div style="margin-top:4px">${baselineVerdictBadge(r.baseline_diff)}</div>` : ""}
      </div>
      <div class="row gap-6">
        <a class="btn small" href="/visualize.html?run=${esc(r.id)}">可视化</a>
        <a class="btn small" href="/replay.html?run=${esc(r.id)}">回放</a>
        <a class="btn small" href="/stats.html?run=${esc(r.id)}">统计</a>
        <a class="btn small" href="/report.html?run=${esc(r.id)}">报告</a>
        <a class="btn small primary" href="/baseline.html?run=${esc(r.id)}">基线对比</a>
        <button class="btn danger small del" data-kind="runs" data-id="${esc(r.id)}">删除</button>
      </div>
    </div>`).join("") || '<p class="muted">暂无运行。</p>';
}

function renderBaselines() {
  return hist.baselines.map((b) => {
    const integ = { ok: "", building: " · 构建中…",
                    corrupt: ' · <span class="badge error">内容已被修改</span>' }[
      b.integrity || "ok"] || "";
    return `<div class="list-item">
      <div style="min-width:0">
        <div class="t">${esc(b.name)} ${statusBadge(b.status)}
          ${b.superseded_by ? '<span class="badge ready">已被取代</span>' : ""}</div>
        <div class="s">
          ${esc(b.scene_name || b.scene_id)} · ${b.n_replicates} 次复制 · ${b.steps} 步
          · ${esc(b.created_at)}${integ}
        </div>
      </div>
      <div class="row gap-6">
        <a class="btn small primary" href="/baseline.html?baseline=${esc(b.id)}">查看 / 对比</a>
      </div>
    </div>`;
  }).join("") || '<p class="muted">暂无基线。可在「基线回归」页标记运行或生成复制基线。</p>';
}

function renderExperiments() {
  return hist.experiments.map((e) => `
    <div class="list-item">
      <div style="min-width:0">
        <div class="t">${esc(e.name)} ${statusBadge(e.status)}</div>
        <div class="s">${e.groups.length} 组 × ${e.steps} 步 · ${esc(e.created_at)}</div>
      </div>
      <div class="row gap-6">
        <a class="btn small" href="/compare.html">查看对比</a>
        <button class="btn danger small del" data-kind="experiments" data-id="${esc(e.id)}">删除</button>
      </div>
    </div>`).join("") || '<p class="muted">暂无实验。</p>';
}

function render() {
  const body = { scenes: renderScenes, runs: renderRuns,
                 experiments: renderExperiments,
                 baselines: renderBaselines }[tab];
  el("list").innerHTML = body();
  el("list").querySelectorAll(".del").forEach((b) => {
    b.onclick = async () => {
      if (!confirm("确认删除？")) return;
      await del(`/api/${b.dataset.kind}/${b.dataset.id}`);
      await load();
    };
  });
}

async function load() {
  hist = await get("/api/history");
  render();
}

function init() {
  document.querySelectorAll(".tab").forEach((t) => {
    t.onclick = () => {
      document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x === t));
      tab = t.dataset.t;
      render();
    };
  });
  load().catch((e) => console.error(e));
}

init();
