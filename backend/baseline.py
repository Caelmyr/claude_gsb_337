"""Baseline regression: freeze a run as the scene baseline, score later runs
against it.

Workflow
--------
1. :func:`mark_baseline` freezes a run into ``data/baselines/<scene_id>.json``:
   the aggregate series, resolved config, seed and intervention log at freeze
   time, plus a SHA-256 fingerprint.  The baseline is a *self-contained
   snapshot* — deleting or resetting the source run afterwards never changes
   what "the baseline" means, so every comparison stays reproducible.
2. :func:`compare_run` aligns a candidate run with the frozen baseline and
   produces a comparison document: per-metric deviations, a noise band that
   separates real change from natural jitter, anomalous fluctuation intervals,
   a config/event diff and a Chinese plain-language summary.

How "real change" is separated from "normal jitter"
---------------------------------------------------
The baseline's own series supplies the noise scale: each metric is detrended
with a centred rolling mean and the robust scale of the residuals (MAD ->
sigma) becomes the natural-fluctuation level.  A deviation only counts as
significant when it exceeds ``sigma_mult * sigma`` **and** persists for at
least ``min_run`` steps (single-step spikes are treated as transients).  A
small relative floor keeps the band non-zero for perfectly flat baselines.

Alignment
---------
Runs of different lengths are compared on the overlapping step range
``[max(start), min(end)]`` with linear interpolation filling any missing
steps; steps outside the overlap are reported as an uncompared tail instead of
being silently dropped or extrapolated.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from typing import Any, Dict, List, Optional, Tuple

from . import catalog, storage, util

DEFAULT_SIGMA_MULT = 3.0   # deviations beyond ±3σ of baseline noise are suspect
DEFAULT_MIN_RUN = 3        # ...and must persist this many steps to be "real"
MERGE_GAP = 2              # significant steps this close merge into one interval
MAX_CHART_POINTS = 400     # cap on aligned series returned for charting
HISTORY_KEEP = 5           # replaced baseline versions remembered per scene


# --------------------------------------------------------------------------- #
# Fingerprint / provenance
# --------------------------------------------------------------------------- #
def _canonical(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _fingerprint(config: Dict[str, Any], seed: int, domain: str, model: str,
                 series: List[Dict[str, Any]]) -> str:
    """Content hash of everything a comparison depends on."""
    payload = {"config": config, "seed": seed, "domain": domain,
               "model": model, "series": series}
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def source_status(doc: Dict[str, Any]) -> Dict[str, str]:
    """Check the frozen baseline against its source run's current on-disk state.

    Returns ``{"status", "detail"}`` where status is one of:

    * ``intact``   — source run unchanged since freezing;
    * ``extended`` — source run kept stepping (baseline is still its prefix);
    * ``modified`` — source run was reset/re-run, prefix no longer matches;
    * ``missing``  — source run deleted; the frozen copy remains authoritative.
    """
    meta = storage.load_run_meta(doc["run_id"])
    if meta is None:
        return {"status": "missing",
                "detail": "基线来源运行已删除；对比基于冻结快照，结论仍可复现。"}
    current = storage.load_series(doc["run_id"])
    frozen_n = int(doc.get("steps", 0))
    if len(current) < frozen_n:
        return {"status": "modified",
                "detail": "来源运行已被重置（当前序列短于冻结快照），指纹校验失败。"}
    prefix_hash = _fingerprint(doc["config"], doc["seed"], doc["domain"],
                               doc["model"], current[:frozen_n])
    if prefix_hash != doc["hash"]:
        return {"status": "modified",
                "detail": "来源运行内容与冻结快照不一致（可能已重置重跑）。"}
    if len(current) > frozen_n:
        return {"status": "extended",
                "detail": f"来源运行冻结后又推进了 {len(current) - frozen_n} 步；"
                          f"基线仍取冻结时点。"}
    return {"status": "intact", "detail": "来源运行与冻结快照一致。"}


# --------------------------------------------------------------------------- #
# Mark / replace a baseline
# --------------------------------------------------------------------------- #
def mark_baseline(scene_id: str, run_id: str, note: str = "") -> Dict[str, Any]:
    """Freeze ``run_id`` as the regression baseline for ``scene_id``.

    Replacing an existing baseline keeps the previous version's provenance in
    a bounded ``history`` chain so older conclusions stay traceable.
    """
    meta = storage.load_run_meta(run_id)
    if meta is None:
        raise KeyError(f"run not found: {run_id}")
    if meta.get("scene_id") != scene_id:
        raise ValueError(f"运行 {run_id} 不属于场景 {scene_id}")
    series = storage.load_series(run_id)
    if len(series) < 2:
        raise ValueError("运行步数太少（<2），无法作为基线")

    now = util.now_iso()
    prev = storage.load_baseline(scene_id)
    version, created_at, history = 1, now, []
    if prev:
        version = int(prev.get("version", 1)) + 1
        created_at = prev.get("created_at", now)
        history = ([{
            "run_id": prev.get("run_id"),
            "run_name": prev.get("run_name"),
            "version": prev.get("version"),
            "hash": prev.get("hash"),
            "steps": prev.get("steps"),
            "note": prev.get("note", ""),
            "frozen_at": prev.get("updated_at"),
            "replaced_at": now,
        }] + list(prev.get("history") or []))[:HISTORY_KEEP]

    doc = {
        "scene_id": scene_id,
        "scene_name": meta.get("scene_name", ""),
        "run_id": run_id,
        "run_name": meta.get("name", ""),
        "domain": meta["domain"],
        "model": meta["model"],
        "config": meta.get("config", {}),
        "seed": meta.get("seed", 0),
        "steps": len(series),
        "last_step": series[-1]["step"],
        "series": series,
        "events": storage.load_events(run_id),
        "hash": _fingerprint(meta.get("config", {}), meta.get("seed", 0),
                             meta["domain"], meta["model"], series),
        "note": note or "",
        "version": version,
        "created_at": created_at,
        "updated_at": now,
        "history": history,
    }
    storage.save_baseline(scene_id, doc)
    return doc


def public_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Baseline document without the (large) embedded series, plus provenance."""
    out = {k: v for k, v in doc.items() if k != "series"}
    out["source"] = source_status(doc)
    return out


# --------------------------------------------------------------------------- #
# Numeric helpers
# --------------------------------------------------------------------------- #
def _metric_points(series: List[Dict[str, Any]], key: str) -> List[Tuple[int, float]]:
    pts = [(int(r["step"]), float(r[key])) for r in series
           if isinstance(r.get(key), (int, float))]
    pts.sort()
    return pts


def _interp(pts: List[Tuple[int, float]], x: float) -> float:
    """Linear interpolation on sorted (step, value) points, clamped at ends."""
    if x <= pts[0][0]:
        return pts[0][1]
    if x >= pts[-1][0]:
        return pts[-1][1]
    lo, hi = 0, len(pts) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if pts[mid][0] <= x:
            lo = mid
        else:
            hi = mid
    (x0, y0), (x1, y1) = pts[lo], pts[hi]
    if x1 == x0:
        return y0
    return y0 + (y1 - y0) * (x - x0) / (x1 - x0)


def _rolling_mean(vals: List[float], w: int) -> List[float]:
    n = len(vals)
    half = w // 2
    pre = [0.0]
    for v in vals:
        pre.append(pre[-1] + v)
    out = []
    for i in range(n):
        a, b = max(0, i - half), min(n, i + half + 1)
        out.append((pre[b] - pre[a]) / (b - a))
    return out


def _noise_sigma(vals: List[float]) -> float:
    """Natural-fluctuation scale of a baseline metric.

    Detrend with a centred rolling mean, then take the robust MAD-based sigma
    of the residuals.  A small relative floor keeps the band usable when the
    baseline is (near-)flat, where any non-zero deviation would otherwise look
    infinitely significant.
    """
    n = len(vals)
    if n == 0:
        return 0.0
    rng = max(vals) - min(vals)
    floor = max(rng * 0.01, abs(statistics.median(vals)) * 0.005, 1e-9)
    if n < 8:
        return floor
    trend = _rolling_mean(vals, max(5, (n // 10) | 1))
    res = [v - t for v, t in zip(vals, trend)]
    med = statistics.median(res)
    mad = statistics.median(abs(r - med) for r in res)
    sigma = 1.4826 * mad
    if sigma <= 0:
        mean_r = sum(res) / n
        sigma = math.sqrt(sum((r - mean_r) ** 2 for r in res) / n)
    return max(sigma, floor)


def _anomaly_intervals(steps: List[int], diffs: List[float], band: float,
                       min_run: int) -> List[Dict[str, Any]]:
    """Maximal clusters of beyond-band steps, merged across tiny gaps.

    A cluster only counts when it contains at least ``min_run`` significant
    steps — shorter excursions are transient spikes, not sustained change.
    """
    sig = [i for i, d in enumerate(diffs) if abs(d) > band]
    if not sig:
        return []
    groups: List[Tuple[int, int]] = []
    start = prev = sig[0]
    for i in sig[1:]:
        if i - prev <= MERGE_GAP + 1:
            prev = i
        else:
            groups.append((start, prev))
            start = prev = i
    groups.append((start, prev))

    out = []
    for a, b in groups:
        if sum(1 for i in range(a, b + 1) if abs(diffs[i]) > band) < min_run:
            continue
        seg = diffs[a:b + 1]
        k = max(range(a, b + 1), key=lambda i: abs(diffs[i]))
        out.append({
            "start": steps[a],
            "end": steps[b],
            "steps": b - a + 1,
            "side": "above" if sum(seg) > 0 else "below",
            "mean_dev": sum(seg) / len(seg),
            "max_dev": diffs[k],
            "max_step": steps[k],
        })
    return out


def _downsample(steps: List[int], *cols: List[float]
                ) -> Tuple[List[int], List[List[float]]]:
    """Thin aligned series to at most MAX_CHART_POINTS, keeping the last point."""
    n = len(steps)
    if n <= MAX_CHART_POINTS:
        return steps, [list(c) for c in cols]
    stride = math.ceil(n / MAX_CHART_POINTS)
    idx = list(range(0, n, stride))
    if idx[-1] != n - 1:
        idx.append(n - 1)
    return [steps[i] for i in idx], [[c[i] for i in idx] for c in cols]


def _r(x: Optional[float], digits: int = 4) -> Optional[float]:
    return None if x is None else round(float(x), digits)


# --------------------------------------------------------------------------- #
# Diff helpers
# --------------------------------------------------------------------------- #
def _config_diff(domain: str, model: str, base_cfg: Dict[str, Any],
                 cand_cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    labels = {p["key"]: p["label"]
              for p in catalog.model_params(domain, model)}
    out = []
    for key in sorted(set(base_cfg) | set(cand_cfg)):
        b, c = base_cfg.get(key), cand_cfg.get(key)
        if b != c:
            out.append({"key": key, "label": labels.get(key, key),
                        "baseline": b, "candidate": c})
    return out


def _events_summary(domain: str,
                    events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    labels = {i["type"]: i["label"] for i in catalog.interventions(domain)}
    return [{"step": e.get("step"), "type": e.get("type"),
             "label": labels.get(e.get("type"), e.get("type")),
             "params": e.get("params", {})} for e in events]


def _verdict(direction: Optional[str], significant: bool, mean_delta: float,
             band: float, anomalies: List[Dict[str, Any]]) -> str:
    """Classify a metric as better / worse / changed / unchanged.

    A sustained level shift (|mean_delta| beyond the band) decides direction
    directly.  When only anomaly intervals fire, their side decides — but
    intervals on *both* sides mean the run oscillates around the baseline
    rather than shifting level, which is a "changed", never a better/worse.
    """
    if not significant:
        return "unchanged"
    shift = 0
    if abs(mean_delta) > band:
        shift = 1 if mean_delta > 0 else -1
    elif anomalies:
        sides = {a["side"] for a in anomalies}
        if len(sides) == 1:
            shift = 1 if sides.pop() == "above" else -1
    if shift == 0:
        return "changed"
    if direction == "high":
        return "better" if shift > 0 else "worse"
    if direction == "low":
        return "better" if shift < 0 else "worse"
    return "changed"


# --------------------------------------------------------------------------- #
# Comparison
# --------------------------------------------------------------------------- #
def compare_run(run_id: str, sigma_mult: float = DEFAULT_SIGMA_MULT,
                min_run: int = DEFAULT_MIN_RUN) -> Dict[str, Any]:
    """Compare ``run_id`` against its scene's frozen baseline.

    Raises :class:`KeyError` when the run or the scene baseline does not
    exist, :class:`ValueError` when the two are not comparable.
    """
    meta = storage.load_run_meta(run_id)
    if meta is None:
        raise KeyError(f"run not found: {run_id}")
    scene_id = meta.get("scene_id", "")
    doc = storage.load_baseline(scene_id)
    if doc is None:
        raise KeyError(f"场景暂无基线，请先将一次运行设为基线: {scene_id}")
    if doc["domain"] != meta["domain"] or doc["model"] != meta["model"]:
        raise ValueError(
            f"模型不一致（基线 {doc['domain']}/{doc['model']}，"
            f"本次 {meta['domain']}/{meta['model']}），无法对比")

    base_series: List[Dict[str, Any]] = doc["series"]
    cand_series = storage.load_series(run_id)
    if not base_series or not cand_series:
        raise ValueError("序列为空，无法对比")

    base_start, base_end = base_series[0]["step"], base_series[-1]["step"]
    cand_start, cand_end = cand_series[0]["step"], cand_series[-1]["step"]
    s0, s1 = max(int(base_start), int(cand_start)), min(int(base_end), int(cand_end))
    if s1 - s0 < 2:
        raise ValueError("两次运行的重叠步数太少（<3），无法对比")
    steps = list(range(s0, s1 + 1))

    sigma_mult = float(sigma_mult)
    min_run = max(1, int(min_run))
    metrics: Dict[str, Any] = {}
    for spec in catalog.metric_specs(meta["domain"]):
        key = spec["key"]
        base_pts = _metric_points(base_series, key)
        cand_pts = _metric_points(cand_series, key)
        if not base_pts or not cand_pts:
            continue
        base_vals = [_interp(base_pts, s) for s in steps]
        cand_vals = [_interp(cand_pts, s) for s in steps]
        diffs = [c - b for b, c in zip(base_vals, cand_vals)]

        # Noise scale from the *full* frozen baseline (more data = better).
        sigma = _noise_sigma([v for _, v in base_pts])
        band = sigma_mult * sigma
        anomalies = _anomaly_intervals(steps, diffs, band, min_run)

        n = len(steps)
        base_mean = sum(base_vals) / n
        cand_mean = sum(cand_vals) / n
        mean_delta = cand_mean - base_mean
        significant = bool(anomalies) or abs(mean_delta) > band
        verdict = _verdict(spec.get("better"), significant, mean_delta,
                           band, anomalies)

        ds_steps, (ds_base, ds_cand) = _downsample(steps, base_vals, cand_vals)
        metrics[key] = {
            "label": spec["label"],
            "direction": spec.get("better"),
            "base_mean": _r(base_mean),
            "cand_mean": _r(cand_mean),
            "mean_delta": _r(mean_delta),
            "mean_delta_pct": _r(mean_delta / abs(base_mean) * 100)
            if base_mean else None,
            "base_final": _r(base_vals[-1]),
            "cand_final": _r(cand_vals[-1]),
            "final_delta": _r(cand_vals[-1] - base_vals[-1]),
            "final_delta_pct": _r((cand_vals[-1] - base_vals[-1])
                                  / abs(base_vals[-1]) * 100)
            if base_vals[-1] else None,
            "sigma": _r(sigma),
            "band": _r(band),
            "significant": significant,
            "verdict": verdict,
            "anomalies": [{**a, "mean_dev": _r(a["mean_dev"]),
                           "max_dev": _r(a["max_dev"])} for a in anomalies],
            "aligned": {"steps": ds_steps,
                        "base": [_r(v) for v in ds_base],
                        "cand": [_r(v) for v in ds_cand]},
        }

    counts = {"better": 0, "worse": 0, "changed": 0, "unchanged": 0}
    for m in metrics.values():
        counts[m["verdict"]] += 1
    if counts["worse"] and counts["better"]:
        overall = "mixed"
    elif counts["worse"]:
        overall = "regressed"
    elif counts["better"]:
        overall = "improved"
    elif counts["changed"]:
        overall = "changed"
    else:
        overall = "unchanged"

    config_diff = _config_diff(meta["domain"], meta["model"],
                               doc.get("config", {}), meta.get("config", {}))
    base_events = _events_summary(meta["domain"], doc.get("events", []))
    cand_events = _events_summary(meta["domain"], storage.load_events(run_id))
    src = source_status(doc)

    result = {
        "run_id": run_id,
        "run_name": meta.get("name", ""),
        "scene_id": scene_id,
        "baseline": {
            "run_id": doc["run_id"],
            "run_name": doc.get("run_name", ""),
            "version": doc.get("version", 1),
            "hash": doc.get("hash", ""),
            "steps": doc.get("steps", 0),
            "frozen_at": doc.get("updated_at", ""),
            "note": doc.get("note", ""),
            "source_status": src["status"],
            "source_detail": src["detail"],
        },
        "alignment": {
            "method": "step 轴重叠区间 + 线性插值",
            "overlap_start": s0,
            "overlap_end": s1,
            "points": len(steps),
            "baseline_last_step": int(base_end),
            "candidate_last_step": int(cand_end),
            "cand_tail": max(0, int(cand_end) - s1),
            "base_tail": max(0, int(base_end) - s1),
        },
        "noise": {"sigma_mult": sigma_mult, "min_run": min_run,
                  "method": "基线去趋势残差的稳健 σ（MAD），显著 = 超出 "
                            "±kσ 且持续 ≥ min_run 步"},
        "metrics": metrics,
        "config_diff": config_diff,
        "seed": {"baseline": doc.get("seed", 0),
                 "candidate": meta.get("seed", 0)},
        "events": {"baseline": base_events, "candidate": cand_events},
        "verdict": {**counts, "overall": overall},
        "summary": [],
        "generated_at": util.now_iso(),
    }
    result["summary"] = _summary(result)
    return result


# --------------------------------------------------------------------------- #
# Plain-language summary
# --------------------------------------------------------------------------- #
def _summary(res: Dict[str, Any]) -> List[str]:
    lines: List[str] = []
    v = res["verdict"]
    noise = res["noise"]
    head = {
        "improved": "整体优于基线",
        "regressed": "整体劣于基线",
        "mixed": "相对基线有改善也有回退",
        "changed": "相对基线有显著变化（无明确优劣结论）",
        "unchanged": "与基线基本持平",
    }[v["overall"]]
    lines.append(
        f"{head}：{v['better']} 项改善、{v['worse']} 项恶化、"
        f"{v['changed']} 项显著波动、{v['unchanged']} 项持平"
        f"（判定标准：偏差超出 ±{noise['sigma_mult']:g}σ 噪声带且持续 "
        f"≥{noise['min_run']} 步）。")

    diff = res["config_diff"]
    if diff:
        parts = [f"{d['label']} {d['baseline']}→{d['candidate']}" for d in diff]
        lines.append("参数变化：" + "；".join(parts) + "。")
    else:
        lines.append("参数与基线完全一致。")
    seed = res["seed"]
    if seed["baseline"] != seed["candidate"]:
        lines.append(f"随机种子不同（{seed['baseline']}→{seed['candidate']}），"
                     f"部分差异可能来自随机波动而非改动本身。")
    elif diff:
        lines.append("随机种子相同，观测到的差异可归因于参数/干预改动。")
    ev = res["events"]
    if ev["baseline"] or ev["candidate"]:
        lines.append(f"干预事件：基线 {len(ev['baseline'])} 次 / "
                     f"本次 {len(ev['candidate'])} 次。")

    jitter = []
    for key, m in res["metrics"].items():
        if not m["significant"]:
            jitter.append(m["label"])
            continue
        pct = (f"{m['mean_delta_pct']:+.1f}%"
               if m["mean_delta_pct"] is not None else "—")
        tag = {"better": "真实改善", "worse": "真实恶化",
               "changed": "真实变化"}[m["verdict"]]
        line = (f"{m['label']}：均值 {m['base_mean']:g} → {m['cand_mean']:g}"
                f"（{pct}），超出噪声带，判定为{tag}")
        if m["anomalies"]:
            spans = "、".join(
                f"第{a['start']}–{a['end']}步"
                f"（{'偏高' if a['side'] == 'above' else '偏低'}）"
                for a in m["anomalies"])
            line += f"；异常波动区间 {spans}"
        lines.append(line + "。")
    if jitter:
        lines.append("以下指标偏差处于基线自然波动范围内（正常抖动）："
                     + "、".join(jitter) + "。")

    al = res["alignment"]
    if al["cand_tail"]:
        lines.append(f"本次运行比基线多 {al['cand_tail']} 步"
                     f"（第 {al['overlap_end'] + 1}–{al['candidate_last_step']} 步），"
                     f"超出部分未纳入对比。")
    elif al["base_tail"]:
        lines.append(f"基线比本次运行多 {al['base_tail']} 步，"
                     f"仅对比前 {al['points']} 步的重叠区间。")

    b = res["baseline"]
    lines.append(f"基线 v{b['version']}（运行「{b['run_name']}」，"
                 f"指纹 {b['hash'][:12]}）：{b['source_detail']}")
    return lines
