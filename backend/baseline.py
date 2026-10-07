"""Baseline regression: pin a run (or several replicate runs) as a *baseline*
and judge later runs of the same scene against it.

A baseline is not just "two overlaid curves".  It is an **immutable,
checksummed snapshot** of everything a conclusion needs to be reproducible and
traceable later:

* the fully resolved config, seeds and scheduled/runtime interventions;
* the complete per-step series of every source replicate (so deleting the
  source run later cannot silently change or invalidate the baseline);
* a per-metric baseline profile: step-indexed mean, a per-step noise band and a
  single-replicate jitter estimate.

The comparison in :func:`compare_to_baseline` then answers three hard questions:

1. **Different step counts?**  Curves are aligned on absolute steps over their
   common horizon (the only alignment that supports statistical testing); a
   secondary resampling by *normalized progress* (``t / total_steps``) covers
   whole-trajectory shape when runs have different lengths, and any candidate
   tail beyond the baseline horizon is reported separately without a bogus
   statistical verdict.

2. **Jitter vs. a real change?**  Deviations are measured in units of the
   baseline's *own natural variability* (across replicates, or a robust
   successive-difference scale for single runs).  A change is only flagged when
   the standardized deviation stays beyond the band for a sustained window —
   isolated one-step spikes are treated as normal noise, not regressions.

3. **Baseline modified or deleted?**  Immutable fields are SHA-256 checksummed;
   tampering is detected and blocks new comparisons.  Deleting a baseline does
   not delete already-generated diffs (they embed a baseline snapshot + checksum
   and stay readable, clearly labelled "基线已删除"); retiring (superseding) is
   the non-destructive alternative.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from typing import Any, Dict, List, Optional, Tuple

from . import catalog, storage, util

SCHEMA = 1

# Significance tuning (documented on the API page / UI help).
Z_BAND = 2.0           # band half-width and "outside band" threshold (≈95%)
Z_STRONG = 3.0         # |z| above which a deviation is called 显著
Z_OUTCOME = 2.5        # |z| for final/peak outcome changes without a segment
WINDOW_FRAC = 0.03     # sustained-deviation window as a fraction of horizon
WINDOW_MIN = 3
MERGE_GAP = 2          # gaps (steps) below which two segments are joined
MIN_SEG_FRAC = 0.02    # a segment must span ≥2% of the horizon
MIN_SEG_STEPS = 3
NORM_POINTS = 61       # normalized-progress resampling grid (0..1)
MIN_COMPARE_STEPS = 8  # shorter runs are not compared statistically
REL_FLOOR = 0.01       # relative noise floor (1% of the baseline level)
ABS_FLOOR = 1e-3

# Fields that may change over a baseline's lifecycle without breaking the
# checksum (everything else is immutable once the baseline is created).
_MUTABLE_FIELDS = ("status", "superseded_by", "error")

# --------------------------------------------------------------------------- #
# Small numeric helpers (stdlib only, like the rest of the backend)
# --------------------------------------------------------------------------- #


def _is_num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _mean(xs: List[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _sample_std(xs: List[float], mu: Optional[float] = None) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    m = _mean(xs) if mu is None else mu
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def _median(xs: List[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return 0.0
    mid = n // 2
    return s[mid] if n % 2 else 0.5 * (s[mid - 1] + s[mid])


def _mad(xs: List[float]) -> float:
    """Median absolute deviation (robust spread), scaled to a Gaussian σ."""
    if not xs:
        return 0.0
    med = _median(xs)
    return 1.4826 * _median([abs(x - med) for x in xs])


def _interp_at(series: List[Dict[str, Any]], key: str,
               t: float) -> Optional[float]:
    """Linear interpolation of a (possibly sparse) step-indexed series at t."""
    if not series:
        return None
    steps = [int(r["step"]) for r in series]
    vals = [r[key] for r in series]
    if t <= steps[0]:
        return vals[0]
    if t >= steps[-1]:
        return vals[-1]
    hi = 0
    while hi < len(steps) and steps[hi] < t:
        hi += 1
    lo = hi - 1
    if hi == 0:
        return vals[0]
    frac = (t - steps[lo]) / max(1, steps[hi] - steps[lo])
    return vals[lo] * (1 - frac) + vals[hi] * frac


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


# --------------------------------------------------------------------------- #
# Checksums / integrity
# --------------------------------------------------------------------------- #


def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _checksum_payload(doc: Dict[str, Any]) -> Dict[str, Any]:
    payload = {k: v for k, v in doc.items()
               if k not in _MUTABLE_FIELDS + ("checksum", "integrity")}
    return payload


def compute_checksum(doc: Dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(
        _canonical(_checksum_payload(doc))).hexdigest()


def verify_baseline(doc: Dict[str, Any]) -> str:
    """Return ``ok`` / ``building`` / ``corrupt``.

    ``building`` documents are placeholder records for replicate batches still
    running in the background; they carry no checksum yet.
    """
    if doc.get("status") == "building":
        return "building"
    expected = doc.get("checksum")
    if not expected:
        return "corrupt"
    return "ok" if compute_checksum(doc) == expected else "corrupt"


# --------------------------------------------------------------------------- #
# Baseline construction
# --------------------------------------------------------------------------- #


def _series_by_step(series: List[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    return {int(r["step"]): r for r in series if "step" in r}


def _metric_keys(replicate_series: List[List[Dict[str, Any]]]) -> List[str]:
    keys: set = set()
    for s in replicate_series:
        for r in s:
            keys.update(k for k, v in r.items() if k != "step" and _is_num(v))
    return sorted(keys)


def _quantile(xs: List[float], q: float) -> float:
    """Linear-interpolated empirical quantile (q in [0, 1])."""
    if not xs:
        return 0.0
    s = sorted(xs)
    if len(s) == 1:
        return s[0]
    pos = q * (len(s) - 1)
    lo = int(math.floor(pos))
    return s[lo] + (pos - lo) * (s[min(lo + 1, len(s) - 1)] - s[lo])


def _pred_sigma(band_val: float, n_ref: int, level: float) -> float:
    """Prediction σ for a *new* observation (band × √(1+1/R) + floors)."""
    sigma = band_val * math.sqrt(1.0 + 1.0 / max(n_ref, 1))
    return max(sigma, REL_FLOOR * abs(level) + ABS_FLOOR)


def _loo_calibration(cols: List[List[float]], horizon: int,
                     full_band: List[float],
                     n_metrics: int = 1) -> Dict[str, Any]:
    """Calibrate detector thresholds against the baseline's own replicates.

    A textbook fixed 2σ pointwise rule has no false-alarm guarantee here:

    * we scan *every* step of *several* metrics (a multiple-comparisons
      problem over both time and metrics);
    * few-replicate sample variances are noisy for the fat-tailed / branching
      dynamics these engines exhibit, and some metrics are discrete counts.

    The thresholds are therefore (a) R-adaptive (deliberately conservative for
    tiny baselines) and (b) applied to the *windowed* z curve so isolated
    one-step spikes count as jitter.  Two levels are returned: a per-metric
    threshold for flagging a metric, and a family-wise threshold (widened for
    the number of metrics) for the run-level verdict, controlling the
    false-regression probability after scanning all metrics at once.
    """
    r = len(cols[0])
    w_win = _window(horizon)
    resid: List[List[float]] = []
    for held in range(r):
        row = []
        for t in range(horizon):
            sig = _pred_sigma(full_band[t], r, _mean(cols[t]))
            row.append(_clamp((cols[t][held] - _mean(cols[t])) / sig,
                              -20, 20))
        resid.append(row)

    loo_seg = [max(abs(v) for v in _smoothed(row, w_win)) for row in resid]
    loo_env = [max(abs(v) for v in row) for row in resid]
    loo_final = [abs(row[-1]) for row in resid]

    # Thresholds calibrated against independent-run null studies on the bundled
    # engines.  The per-metric threshold targets ~5% per-metric FPR on the
    # windowed z maximum; the *family* threshold targets ~5–10% run-level FPR
    # after scanning all M metrics (the max of ~5 correlated z maxima is far
    # above a single 2σ).  They are deliberately conservative — weak deviations
    # are still surfaced as "hints", the verdict only changes on strong ones.
    if r <= 3:
        seg_thr, strong_thr, out_thr = 3.0, 4.5, 3.3
    elif r <= 5:
        seg_thr, strong_thr, out_thr = 2.8, 4.2, 3.1
    elif r <= 8:
        seg_thr, strong_thr, out_thr = 2.7, 3.8, 3.0
    else:
        seg_thr, strong_thr, out_thr = 2.6, 3.5, 2.9
    # Family-wise threshold: empirical null maxima over M ≈ 5 correlated
    # metrics ≈ seg_thr + ~0.9 (traffic) … +~1.2 (heavy-tailed); scale with
    # the metric count but cap so very large effects are never hidden.
    m = max(1, n_metrics)
    add = 0.55 * math.log(max(2.0, m)) / math.log(5.0) + 0.35
    family_thr = round(min(4.5, seg_thr + 0.9 * add), 3)
    family_out = round(min(4.5, out_thr + 0.7 * add), 3)
    return {
        "segment_threshold": round(seg_thr, 3),
        "segment_strong": round(strong_thr, 3),
        "outcome_threshold": round(out_thr, 3),
        "outcome_strong": round(strong_thr, 3),
        "family_threshold": family_thr,
        "family_outcome_threshold": family_out,
        "n_metrics": n_metrics,
        "null_segment": [round(v, 2) for v in loo_seg],
        "null_envelope": [round(v, 2) for v in loo_env],
        "null_final": [round(v, 2) for v in loo_final],
        "method": "replicate_count_adaptive+bonferroni",
    }


def _build_metric(key: str,
                  replicate_series: List[List[Dict[str, Any]]],
                  horizon: int, n_metrics: int = 1) -> Optional[Dict[str, Any]]:
    """Step-indexed mean / per-step band / pooled noise for one metric."""
    grids = [_series_by_step(s) for s in replicate_series]
    # Replicate rows at step t = 0..horizon-1.
    cols: List[List[float]] = []
    for t in range(horizon):
        col = [g[t][key] for g in grids
               if t in g and key in g[t] and _is_num(g[t][key])]
        if not col:
            return None
        cols.append(col)

    r = len(replicate_series)
    mean = [_mean(c) for c in cols]
    per_step_var = [_sample_std(c, mu) ** 2 for c, mu in zip(cols, mean)]
    # Robust pooled spread (median avoids the single highest-variance phase
    # dominating; mean keeps quiet steps from collapsing the floor to zero).
    sds = [v ** 0.5 for v in per_step_var]
    pooled = 0.5 * (_median(sds) + _mean(sds)) if r >= 2 else 0.0

    if r >= 3:
        # Per-step sample variances from few replicates are unstable (an
        # accidentally unanimous step has variance 0).  Shrink them toward the
        # pooled spread; trust local detail only as replicate count grows.
        w = min(0.8, (r - 2) / (r + 2))
        band = [math.sqrt(w * v + (1 - w) * pooled ** 2) for v in per_step_var]
    elif r == 2:
        band = [max(v ** 0.5, 0.5 * pooled) for v in per_step_var]
    else:
        # Single replicate: estimate short-term jitter from the robust scale of
        # successive differences (variogram-style), which ignores smooth trends.
        incr = [mean[t] - mean[t - 1] for t in range(1, horizon)]
        sigma = _mad(incr) / math.sqrt(2.0)
        span = max(mean) - min(mean)
        # Floors: never claim a nearly flat curve is exact, and never let the
        # band shrink below 5% of the metric's own dynamic range — a single run
        # simply cannot identify its own stochastic spread more tightly.
        sigma = max(sigma, 0.05 * span, 0.03 * abs(_mean(mean)))
        band = [sigma] * horizon

    # Empirical calibration of the detector itself.  A fixed 2σ rule ignores
    # two things: (a) scanning every step and flagging the largest excursion
    # inflates the false-alarm probability well above 5%, and (b) discrete /
    # locally unanimous metrics make the nominal σ unrealistically small.
    # Leave-one-replicate-out runs treat every other baseline replicate as a
    # "new run" and measure how extreme a genuinely normal realisation can
    # look; the resulting null maxima calibrate the flag thresholds from data.
    cal = (_loo_calibration(cols, horizon, band, n_metrics) if r >= 3 else
           {"segment_threshold": 3.0, "segment_strong": 4.5,
            "outcome_threshold": 3.3, "outcome_strong": 4.5,
            "family_threshold": 4.2, "family_outcome_threshold": 4.2,
            "n_metrics": n_metrics, "low_replicate": True,
            "null_segment": [], "null_envelope": [], "null_final": []})

    peaks = []
    for s in replicate_series:
        vals = [row[key] for row in s if _is_num(row.get(key))]
        if vals:
            peaks.append(max(vals))
    final_vals = [g[horizon - 1][key] for g in grids
                  if horizon - 1 in g and _is_num(g[horizon - 1].get(key))]
    initial_vals = [g[0][key] for g in grids if _is_num(g[0].get(key))]

    max_mean = max(mean)
    return {
        # Only the number of replicates is needed later (prediction σ scales
        # with R); the raw replicate rows are deliberately not stored to keep
        # the immutable baseline compact — the calibrated band is the result.
        "n_values": r,
        "mean": [round(v, 6) for v in mean],
        "band": [round(v, 6) for v in band],
        "noise": round(pooled if r >= 2 else band[0], 6),
        "initial_mean": round(_mean(initial_vals), 6),
        "final_mean": round(_mean(final_vals), 6),
        "final_std": round(_sample_std(final_vals), 6),
        "max_mean": round(max_mean, 6),
        "max_step": mean.index(max_mean),
        "peak_values": [round(v, 6) for v in peaks],
        "peak_mean": round(_mean(peaks), 6),
        "peak_std": round(_sample_std(peaks), 6),
        "calibration": cal,
    }


def _canonical_interventions(meta: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for i in meta.get("interventions", []):
        out.append({"type": i.get("type"),
                    "at_step": int(i.get("at_step", 0)),
                    "params": i.get("params", {})})
    out.sort(key=lambda x: (x["at_step"], str(x["type"])))
    return out


def create_from_runs(run_ids: List[str], name: str = "",
                     note: str = "") -> Dict[str, Any]:
    """Freeze one or more finished runs of the same scene as an immutable baseline.

    Replicates must share scene/domain/model and resolved config; their series
    are truncated to the shortest replicate when lengths differ.
    """
    if not run_ids:
        raise ValueError("至少选择一个运行作为基线")

    metas: List[Dict[str, Any]] = []
    series_list: List[List[Dict[str, Any]]] = []
    events_list: List[List[Dict[str, Any]]] = []
    for rid in run_ids:
        meta = storage.load_run_meta(rid)
        if meta is None:
            raise KeyError(f"run not found: {rid}")
        if meta.get("status") == "running":
            raise ValueError(f"运行 {meta.get('name', rid)} 仍在运行中，"
                             "请等其结束后再标记为基线")
        series = storage.load_series(rid)
        if len(series) < 2:
            raise ValueError(f"运行 {meta.get('name', rid)} 数据不足（少于 2 步），"
                             "无法建立基线")
        metas.append(meta)
        series_list.append(series)
        events_list.append(storage.load_events(rid))

    first = metas[0]
    for m in metas[1:]:
        if m["scene_id"] != first["scene_id"]:
            raise ValueError("基线复制必须来自同一场景")
        if (m["domain"], m["model"]) != (first["domain"], first["model"]):
            raise ValueError("基线复制必须使用同一模型")
        if m.get("config") != first.get("config"):
            diff_keys = sorted({k for k in set(m.get("config", {})) |
                                set(first.get("config", {}))
                                if m.get("config", {}).get(k)
                                != first.get("config", {}).get(k)})
            raise ValueError("基线复制的参数配置不一致："
                             + "、".join(diff_keys[:8])
                             + ("…" if len(diff_keys) > 8 else ""))

    lengths = [len(s) for s in series_list]
    horizon = min(lengths)
    warnings: List[str] = []
    if len(set(lengths)) > 1:
        warnings.append("复制运行步数不一致（"
                        + "、".join(str(x) for x in lengths)
                        + f"），基线剖面按最短长度 {horizon} 步截断")

    spec_index = {m["key"]: m
                  for m in catalog.CATALOG[first["domain"]]["metrics"]}
    metric_keys = _metric_keys(series_list)
    metrics: Dict[str, Any] = {}
    for key in metric_keys:
        built = _build_metric(key, series_list, horizon, len(metric_keys))
        if built is None:
            continue
        spec = spec_index.get(key, {})
        metrics[key] = {"label": spec.get("label", key),
                        "direction": spec.get("direction", "neutral"),
                        **built}

    doc: Dict[str, Any] = {
        "schema": SCHEMA,
        "id": util.new_id("base"),
        "name": name or f"{first['scene_name']} · 基线",
        "note": note,
        "scene_id": first["scene_id"],
        "scene_name": first.get("scene_name", ""),
        "domain": first["domain"],
        "model": first["model"],
        "config": copy.deepcopy(first.get("config", {})),
        "interventions": _canonical_interventions(first),
        "events": copy.deepcopy(events_list[0]),
        "status": "active",
        "superseded_by": None,
        "source_run_ids": list(run_ids),
        "n_replicates": len(run_ids),
        "seeds": [m.get("seed") for m in metas],
        "steps": horizon,
        "source_lengths": lengths,
        "warnings": warnings,
        "metrics": metrics,
        "created_at": util.now_iso(),
        "created_by": "mark",
    }
    doc["checksum"] = compute_checksum(doc)
    storage.save_baseline(doc)
    return doc


def create_from_scene(scene_id: str, replicates: int, steps: int,
                      name: str = "", note: str = "",
                      base_seed: Optional[int] = None,
                      run_in_background: bool = False) -> Dict[str, Any]:
    """Run ``replicates`` fresh fixed-seed runs of a scene and freeze them.

    A ``building`` placeholder document is written first so the UI can poll;
    it is atomically replaced by the immutable baseline on success or marked
    ``error`` on failure.
    """
    from . import models
    from .run_manager import manager

    scene_dict = storage.load_scene(scene_id)
    if scene_dict is None:
        raise KeyError(f"scene not found: {scene_id}")
    replicates = max(1, min(int(replicates), 20))
    steps = max(2, int(steps))
    scene = models.Scene.from_dict(scene_dict)
    if base_seed is None:
        base_seed = int(models.resolve_config(scene).get("seed", 0))

    placeholder = {
        "schema": SCHEMA,
        "id": util.new_id("base"),
        "name": name or f"{scene.name} · 基线（{replicates} 次复制）",
        "note": note,
        "scene_id": scene_id,
        "scene_name": scene.name,
        "domain": scene.domain,
        "model": scene.model,
        "status": "building",
        "superseded_by": None,
        "n_replicates": replicates,
        "steps": steps,
        "created_at": util.now_iso(),
        "created_by": "replicates",
        "warnings": [],
        "error": "",
    }
    storage.save_baseline(placeholder)

    def work() -> Dict[str, Any]:
        run_ids: List[str] = []
        try:
            for i in range(replicates):
                meta = manager.create_run(
                    scene, name=f"{placeholder['name']} · 复制 {i + 1}",
                    seed=int(base_seed) + i,
                    snapshot_interval=max(1, steps // 20))
                manager.run_batch(meta["id"], steps, keep_engine=False)
                run_ids.append(meta["id"])
            doc = create_from_runs(run_ids, name=placeholder["name"], note=note)
            # Re-home the finished baseline under the placeholder id the UI polls.
            storage.delete_baseline(doc["id"])
            doc["id"] = placeholder["id"]
            doc["created_by"] = "replicates"
            doc["checksum"] = compute_checksum(doc)
            storage.save_baseline(doc)
            return doc
        except Exception as exc:  # noqa: BLE001
            placeholder["status"] = "error"
            placeholder["error"] = str(exc)
            placeholder["source_run_ids"] = run_ids
            storage.save_baseline(placeholder)
            raise

    if run_in_background:
        import threading
        threading.Thread(target=lambda: _swallow(work), daemon=True).start()
        return placeholder
    return work()


def _swallow(fn) -> None:
    try:
        fn()
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #


def list_for_scene(scene_id: str) -> List[Dict[str, Any]]:
    return [b for b in storage.list_baselines() if b.get("scene_id") == scene_id]


def active_baseline_for_scene(scene_id: str) -> Optional[Dict[str, Any]]:
    active = [b for b in list_for_scene(scene_id) if b.get("status") == "active"]
    if not active:
        return None
    active.sort(key=lambda b: b.get("created_at", ""), reverse=True)
    return active[0]


def retire_baseline(baseline_id: str,
                    supersede: Optional[str] = None) -> Dict[str, Any]:
    doc = storage.load_baseline(baseline_id)
    if doc is None:
        raise KeyError(f"baseline not found: {baseline_id}")
    doc["status"] = "retired"
    doc["superseded_by"] = supersede
    storage.save_baseline(doc)  # status is outside the checksum
    return doc


def reactivate_baseline(baseline_id: str) -> Dict[str, Any]:
    doc = storage.load_baseline(baseline_id)
    if doc is None:
        raise KeyError(f"baseline not found: {baseline_id}")
    if verify_baseline(doc) == "corrupt":
        raise RuntimeError("基线内容校验失败，无法重新启用")
    doc["status"] = "active"
    doc["superseded_by"] = None
    storage.save_baseline(doc)
    return doc


# --------------------------------------------------------------------------- #
# Alignment
# --------------------------------------------------------------------------- #


def _resample(rows: List[Dict[str, Any]], key: str,
              n_points: int) -> Tuple[List[float], List[float]]:
    """Resample a step-indexed series onto a normalized 0..1 progress grid."""
    t_max = int(rows[-1]["step"]) if rows else 0
    ps = [i / (n_points - 1) for i in range(n_points)]
    out = [_interp_at(rows, key, p * t_max) or 0.0 for p in ps]
    return ps, out


def _sigma_at(profile: Dict[str, Any], t: int, level: float) -> float:
    """Prediction σ for a *new* run at step t."""
    r = int(profile.get("n_values") or 1)
    band = profile["band"][min(t, len(profile["band"]) - 1)]
    return _pred_sigma(band, r, level)


def _window(n: int) -> int:
    return max(WINDOW_MIN, int(round(WINDOW_FRAC * n)))


def _find_segments(flags: List[bool], z: List[float],
                   horizon: int) -> List[Dict[str, Any]]:
    """Turn per-point outside-band flags into sustained deviation segments."""
    n = len(flags)
    min_len = max(MIN_SEG_STEPS, int(math.ceil(MIN_SEG_FRAC * horizon)))
    raw: List[Tuple[int, int]] = []
    i = 0
    while i < n:
        if not flags[i]:
            i += 1
            continue
        j = i
        while j + 1 < n:
            if flags[j + 1]:
                j += 1
                continue
            # bridge short gaps
            gap_end = j + 1
            while gap_end < n and not flags[gap_end] and gap_end - (j + 1) < MERGE_GAP:
                gap_end += 1
            if gap_end < n and flags[gap_end]:
                j = gap_end
            else:
                break
        raw.append((i, j))
        i = j + 1

    segments = []
    half_w = _window(horizon) // 2
    for a, b in raw:
        if b - a + 1 < min_len:
            continue
        zs = z[a:b + 1]
        mean_z = _mean(zs)
        segments.append({
            "start_step": max(0, a - half_w),
            "end_step": min(horizon - 1, b + half_w),
            "kind": "high" if mean_z >= 0 else "low",
            "mean_z": round(mean_z, 2),
            "max_abs_z": round(max(abs(v) for v in zs), 2),
        })
    return segments


def _smoothed(z: List[float], w: int) -> List[float]:
    n = len(z)
    if w <= 1:
        return z[:]
    half = w // 2
    out = []
    for i in range(n):
        a, b = max(0, i - half), min(n, i + half + 1)
        out.append(_mean(z[a:b]))
    return out


# --------------------------------------------------------------------------- #
# Config / intervention diffs
# --------------------------------------------------------------------------- #


def _diff_config(base: Dict[str, Any], cand: Dict[str, Any]) -> Dict[str, Any]:
    changed, added, removed = [], [], []
    for k in sorted(set(base) | set(cand)):
        if k not in cand:
            removed.append({"key": k, "baseline": base[k]})
        elif k not in base:
            added.append({"key": k, "candidate": cand[k]})
        elif base[k] != cand[k]:
            changed.append({"key": k, "baseline": base[k],
                            "candidate": cand[k]})
    return {"changed": changed, "added": added, "removed": removed}


def _diff_interventions(base_doc: Dict[str, Any],
                        candidate_meta: Dict[str, Any],
                        candidate_events: List[Dict[str, Any]],
                        horizon: int) -> Tuple[List[Dict[str, Any]],
                                               List[str]]:
    notes: List[Dict[str, Any]] = []
    warnings: List[str] = []
    tol = max(1, int(0.02 * max(horizon, 1)))

    base_itv = {(i["type"], round(i["at_step"] / tol)): i
                for i in base_doc.get("interventions", [])}
    matched = set()
    for i in _canonical_interventions(candidate_meta):
        key = (i["type"], round(i["at_step"] / tol))
        if key in base_itv:
            matched.add(key)
            b = base_itv[key]
            if b["params"] != i["params"]:
                notes.append({"kind": "changed", "type": i["type"],
                              "baseline": b, "candidate": i})
        else:
            # same intervention at a very different step, or genuinely new?
            same_type = [b for (t, _st), b in base_itv.items() if t == i["type"]]
            if same_type:
                notes.append({"kind": "shifted", "type": i["type"],
                              "baseline_step": same_type[0]["at_step"],
                              "candidate_step": i["at_step"]})
                warnings.append(
                    f"干预 {i['type']} 的实施时机由第 "
                    f"{same_type[0]['at_step']} 步变为第 {i['at_step']} 步")
            else:
                notes.append({"kind": "added", "candidate": i})
                warnings.append(f"候选运行增加了基线没有的干预：{i['type']}")
    for key, b in base_itv.items():
        if key not in matched:
            if not any(n.get("type") == b["type"] and n["kind"] == "shifted"
                       for n in notes):
                notes.append({"kind": "removed", "baseline": b})
                warnings.append(f"候选运行缺少基线中的干预：{b['type']}")

    manual = [e for e in candidate_events if not e.get("scheduled")]
    for e in manual:
        notes.append({"kind": "manual", "type": e.get("type"),
                      "step": e.get("step"), "params": e.get("params", {})})
    if manual:
        warnings.append(f"候选运行在模拟过程中手动施加了 {len(manual)} 次干预")
    return notes, warnings


# --------------------------------------------------------------------------- #
# The comparison itself
# --------------------------------------------------------------------------- #


def _pct(delta: float, base: float) -> Optional[float]:
    if abs(base) < 1e-9:
        return None
    return round(100.0 * delta / abs(base), 1)


def _compare_metric(key: str, profile: Dict[str, Any],
                    cand_rows: List[Dict[str, Any]],
                    horizon: int, cand_total: int) -> Dict[str, Any]:
    cand_steps = [int(r["step"]) for r in cand_rows]
    cand = [r[key] for r in cand_rows]
    base_mean = profile["mean"][:horizon]
    sigma = [_sigma_at(profile, t, base_mean[t]) for t in range(horizon)]
    # Candidate at absolute steps 0..horizon-1 (interpolates sparse series).
    cand_common = [_interp_at(cand_rows, key, float(t))
                   for t in range(horizon)]
    cand_common = [c if c is not None else base_mean[t]
                   for t, c in enumerate(cand_common)]

    n_ref = int(profile.get("n_values") or 1)
    raw_z = [_clamp((cand_common[t] - base_mean[t]) / sigma[t],
                    -20, 20) for t in range(horizon)]
    w = _window(horizon)
    wz = _smoothed(raw_z, w)
    cal = profile.get("calibration") or {}
    seg_thr = cal.get("segment_threshold", Z_BAND)
    strong_thr = cal.get("segment_strong", Z_STRONG)
    out_thr = cal.get("outcome_threshold", Z_OUTCOME)
    fam_thr = cal.get("family_threshold", seg_thr)
    fam_out = cal.get("family_outcome_threshold", out_thr)
    flags = [abs(z) > seg_thr for z in wz]
    fam_flags = [abs(z) > fam_thr for z in wz]
    segments = _find_segments(flags, wz, horizon)
    family_segments = _find_segments(fam_flags, wz, horizon)

    deltas = [cand_common[t] - base_mean[t] for t in range(horizon)]
    mean_delta = _mean(deltas)
    mean_z = _mean(raw_z)
    max_abs_z = max((abs(z) for z in wz), default=0.0)

    # --- outcomes -------------------------------------------------------- #
    outcomes: Dict[str, Any] = {}
    z0 = (cand_common[0] - profile["initial_mean"]) / max(
        sigma[0], ABS_FLOOR)
    outcomes["initial"] = {
        "baseline": profile["initial_mean"],
        "candidate": round(cand_common[0], 6),
        "z": round(z0, 2)}

    t_final = min(cand_total - 1, len(profile["mean"]) - 1)
    sig_f = _sigma_at(profile, t_final, profile["mean"][t_final])
    cand_final = cand[-1]
    z_final = _clamp(
        (cand_final - profile["mean"][t_final]) / sig_f, -20, 20)
    outcomes["final"] = {
        "baseline": profile["mean"][t_final],
        "candidate": round(cand_final, 6),
        "step": cand_total - 1,
        "baseline_step": t_final,
        "z": round(z_final, 2)}

    cand_peak = max(cand)
    cand_peak_step = cand.index(cand_peak)
    # Peak is scored against the baseline *envelope at that step* (the band
    # captures timing variability; peak_std across replicates is only kept for
    # documentation).
    pk_step = min(cand_steps[cand_peak_step], horizon - 1)
    z_peak = _clamp(
        (cand_peak - base_mean[pk_step]) / sigma[pk_step], -20, 20)
    outcomes["peak"] = {
        "baseline": profile["peak_mean"],
        "baseline_std": round(profile.get("peak_std", 0.0), 4),
        "baseline_at_step": round(base_mean[pk_step], 6),
        "candidate": round(cand_peak, 6),
        "candidate_step": cand_steps[cand_peak_step],
        "baseline_step": profile.get("max_step"),
        "in_baseline_horizon": cand_steps[cand_peak_step] < horizon,
        "z": round(z_peak, 2)}

    # --- verdict for the metric ------------------------------------------ #
    # Per-metric detection (this metric's own ~5% FPR threshold).
    outcome_hits = [o for name, o in outcomes.items()
                    if name in ("final", "peak") and abs(o["z"]) >= out_thr
                    and (name != "peak" or o["in_baseline_horizon"])]
    changed = bool(segments) or bool(outcome_hits)
    signed = [s["mean_z"] for s in segments] + [o["z"] for o in outcome_hits]
    direction_change = "up" if signed and _mean(signed) > 0 else (
        "down" if signed and _mean(signed) < 0 else "none")
    strong = (any(abs(s["mean_z"]) >= strong_thr for s in segments)
              or max_abs_z >= strong_thr
              or any(abs(o["z"]) >= cal.get("outcome_strong", Z_STRONG)
                     for o in outcome_hits))
    significance = ("significant" if changed and strong else
                    "slight" if changed else "none")

    # Family-wise detection (threshold corrected for the number of metrics;
    # this is what drives the overall run verdict, so scanning 5 metrics
    # cannot turn a normal run into 5 independent 5% coin flips).
    fam_outcomes = [o for name, o in outcomes.items()
                    if name in ("final", "peak") and abs(o["z"]) >= fam_out
                    and (name != "peak" or o["in_baseline_horizon"])]
    family_changed = bool(family_segments) or bool(fam_outcomes)
    fam_signed = ([s["mean_z"] for s in family_segments]
                  + [o["z"] for o in fam_outcomes])
    family_direction = "up" if fam_signed and _mean(fam_signed) > 0 else (
        "down" if fam_signed and _mean(fam_signed) < 0 else "none")

    wanted = profile.get("direction", "neutral")
    if not family_changed or wanted == "neutral":
        quality = "neutral" if family_changed else "none"
    elif wanted == "up":
        quality = "better" if family_direction == "up" else "worse"
    else:
        quality = "better" if family_direction == "down" else "worse"

    # --- normalized-progress alignment (secondary) ----------------------- #
    base_rows = [{"step": t, key: profile["mean"][t]}
                 for t in range(len(profile["mean"]))]
    ps, base_norm = _resample(base_rows, key, NORM_POINTS)
    _, cand_norm = _resample(cand_rows, key, NORM_POINTS)
    rms_band = math.sqrt(_mean([b ** 2 for b in profile["band"]]))
    sig_norm = max(rms_band * math.sqrt(1.0 + 1.0 / max(n_ref, 1)),
                   REL_FLOOR * abs(_mean(base_norm)) + ABS_FLOOR)
    z_norm = [_clamp((cand_norm[i] - base_norm[i]) / sig_norm,
                     -20, 20) for i in range(NORM_POINTS)]
    norm_flags = [abs(z) > seg_thr for z in _smoothed(z_norm, 5)]
    norm_segments = _find_segments(norm_flags, z_norm, NORM_POINTS)
    for s in norm_segments:
        s["start_progress"] = round(s.pop("start_step") / (NORM_POINTS - 1), 2)
        s["end_progress"] = round(s.pop("end_step") / (NORM_POINTS - 1), 2)

    # --- candidate tail beyond the baseline horizon ---------------------- #
    tail = None
    if cand_total > horizon:
        tail = {"start_step": horizon, "end_step": cand_total - 1,
                "values": [round(v, 6) for v in cand[horizon:]]}

    # Visual band at the actual flagging threshold (in value units: the
    # displayed band must agree exactly with what the detector flagged).
    band_lo = [round(base_mean[t] - seg_thr * sigma[t], 6)
               for t in range(horizon)]
    band_hi = [round(base_mean[t] + seg_thr * sigma[t], 6)
               for t in range(horizon)]
    return {
        "label": profile.get("label", key),
        "direction": wanted,
        "baseline": {"initial": profile["initial_mean"],
                     "final": profile["final_mean"],
                     "max": profile["max_mean"],
                     "mean": round(_mean(base_mean), 6)},
        "candidate": {"initial": round(cand_common[0], 6),
                      "final": round(cand_final, 6),
                      "max": round(cand_peak, 6),
                      "mean": round(_mean(cand_common), 6)},
        "delta_mean": round(mean_delta, 6),
        "delta_pct": _pct(mean_delta, _mean(base_mean)),
        "mean_z": round(mean_z, 2),
        "max_abs_z": round(max_abs_z, 2),
        "segments": segments,
        "normalized_segments": norm_segments,
        "outcomes": outcomes,
        "changed": changed,
        "family_changed": family_changed,
        "family_direction": family_direction,
        "significance": significance,
        "direction_change": direction_change,
        "quality": quality,
        "tail": tail,
        "chart": {
            "steps": list(range(horizon)),
            "base": [round(v, 6) for v in base_mean],
            "band_low": band_lo,
            "band_high": band_hi,
            "candidate": [round(v, 6) for v in cand_common],
            "z": [round(v, 2) for v in wz],
            "threshold": round(seg_thr, 2),
        },
        "normalized": {"progress": ps,
                       "base": [round(v, 6) for v in base_norm],
                       "candidate": [round(v, 6) for v in cand_norm],
                       "z": [round(v, 2) for v in z_norm]},
    }


def _metric_sentence(key: str, m: Dict[str, Any]) -> str:
    label = m["label"]
    d = m["delta_pct"]
    d_txt = f"{d:+.1f}%" if d is not None else f"均值差 {m['delta_mean']:+.3g}"
    word = "高于" if m["direction_change"] == "up" else "低于"
    segs = m["segments"]
    where = ""
    if segs:
        a, b = segs[0]["start_step"], segs[0]["end_step"]
        more = f" 等 {len(segs)} 段" if len(segs) > 1 else ""
        where = f"，第 {a}–{b} 步持续{word}基线{more}"
    sig = "显著" if m["significance"] == "significant" else "轻微"
    peak = m["outcomes"]["peak"]
    final = m["outcomes"]["final"]
    peak_txt = ""
    if peak["in_baseline_horizon"] and abs(peak["z"]) >= Z_OUTCOME:
        pd_ = peak["candidate"] - peak["baseline_at_step"]
        pct = _pct(pd_, peak["baseline_at_step"])
        peak_txt = (f"，峰值 {peak['candidate']:g}（同步基线 {peak['baseline_at_step']:g}，"
                    f"{pct:+.1f}%）") if pct is not None else \
                   f"，峰值 {peak['candidate']:g}（同步基线 {peak['baseline_at_step']:g}）"
    final_txt = ""
    if abs(final["z"]) >= Z_OUTCOME:
        d_ = final["candidate"] - final["baseline"]
        pct = _pct(d_, final["baseline"])
        final_txt = (f"，终态 {final['candidate']:g}（基线 {final['baseline']:g}，"
                     f"{pct:+.1f}%）") if pct is not None else \
                    f"，终态 {final['candidate']:g}（基线 {final['baseline']:g}）"
    tail_txt = "；注意峰值出现在基线覆盖范围之外，未做统计判定" \
        if not peak["in_baseline_horizon"] and m["changed"] else ""
    quality_tail = {"better": "，属改善 ✅", "worse": "，属恶化 ⚠️",
                    "neutral": ""}.get(m["quality"], "")
    return (f"{label}：{sig}{word}基线（均值 {d_txt}{peak_txt}{final_txt}）"
            f"{where}{tail_txt}{quality_tail}")


_VERDICT_LABEL = {
    "better": "相对基线变好 ✅",
    "worse": "相对基线变坏 ⚠️",
    "mixed": "相对基线有好有坏，需结合具体指标判断 ↕️",
    "no_change": "与基线无显著差异（变化在正常波动范围内）",
}


def compare_to_baseline(baseline_id: str, run_id: str,
                        persist: bool = True) -> Dict[str, Any]:
    """Compute (and by default persist) the regression diff for one run."""
    base_doc = storage.load_baseline(baseline_id)
    if base_doc is None:
        raise KeyError(f"baseline not found: {baseline_id}")
    integrity = verify_baseline(base_doc)
    if integrity == "corrupt":
        raise RuntimeError("基线文件校验失败（内容可能被手动修改），"
                           "请重建或恢复该基线后再对比")
    if integrity == "building":
        raise RuntimeError("基线仍在构建中（复制运行尚未完成）")

    meta = storage.load_run_meta(run_id)
    if meta is None:
        raise KeyError(f"run not found: {run_id}")
    if meta["scene_id"] != base_doc["scene_id"]:
        raise ValueError("候选运行与基线不属于同一场景，无法对比")

    cand_series = storage.load_series(run_id)
    cand_total = len(cand_series)
    if cand_total < MIN_COMPARE_STEPS:
        raise ValueError(f"候选运行仅 {cand_total} 步，少于最小对比步数 "
                         f"{MIN_COMPARE_STEPS}")

    horizon = min(cand_total, int(base_doc["steps"]))
    warnings = list(base_doc.get("warnings", []))
    if cand_total != int(base_doc["steps"]):
        if cand_total > base_doc["steps"]:
            warnings.append(
                f"候选运行 {cand_total} 步、基线 {base_doc['steps']} 步："
                f"共同的前 {horizon} 步按绝对步数对齐做统计检验，"
                f"第 {horizon} 步之后超出基线范围，仅单独列出不做判定；"
                "另附按完成进度对齐的整体形态对比")
        else:
            warnings.append(
                f"候选运行仅 {cand_total} 步（基线 {base_doc['steps']} 步）："
                f"按绝对步数对齐前 {horizon} 步，终态对比为提前结束时的状态；"
                "另附按完成进度对齐的整体形态对比")
    if int(base_doc.get("n_replicates", 1)) < 3:
        warnings.append(
            f"基线仅含 {base_doc.get('n_replicates')} 次复制，"
            "噪声带为稳健估计，结论置信度有限——建议用 5 次以上复制重建基线")

    cand_steps = [int(r["step"]) for r in cand_series]
    metrics_out: Dict[str, Any] = {}
    for key, profile in base_doc.get("metrics", {}).items():
        if not all(_is_num(r.get(key)) for r in cand_series):
            continue
        metrics_out[key] = _compare_metric(key, profile, cand_series,
                                           horizon, cand_total)

    itv_notes, itv_warnings = _diff_interventions(
        base_doc, meta, storage.load_events(run_id),
        int(base_doc["steps"]))
    warnings.extend(itv_warnings)

    # Overall run verdict uses the family-wise (multiplicity-corrected) flags.
    better = [k for k, m in metrics_out.items() if m["quality"] == "better"]
    worse = [k for k, m in metrics_out.items() if m["quality"] == "worse"]
    changed_n = [k for k, m in metrics_out.items()
                 if m["quality"] == "neutral"]
    unchanged = [k for k, m in metrics_out.items()
                 if not m["family_changed"]]
    # Sub-threshold per-metric hints: visible in the detail view, called out as
    # observations rather than driving the verdict.
    hinted = [k for k, m in metrics_out.items()
              if m["changed"] and not m["family_changed"]]
    if worse and better:
        verdict = "mixed"
    elif worse:
        verdict = "worse"
    elif better:
        verdict = "better"
    else:
        verdict = "no_change"

    # Human-readable summary.
    summary: List[str] = []
    cfg_diff = _diff_config(base_doc.get("config", {}), meta.get("config", {}))
    if cfg_diff["changed"]:
        shown = ", ".join(f"{d['key']}: {d['baseline']:g} → {d['candidate']:g}"
                          if _is_num(d["baseline"]) and _is_num(d["candidate"])
                          else f"{d['key']} 已修改"
                          for d in cfg_diff["changed"][:5])
        summary.append(f"参数变化：{shown}")
    elif cfg_diff["added"] or cfg_diff["removed"]:
        summary.append("参数集合与基线不同（存在新增/删除的参数项）")
    else:
        summary.append("参数配置与基线一致（变化可能来自干预或随机过程）")

    interesting = worse + better + changed_n
    interesting.sort(
        key=lambda k: metrics_out[k]["max_abs_z"], reverse=True)
    for k in interesting[:6]:
        summary.append(_metric_sentence(k, metrics_out[k]))
    if hinted:
        names = "、".join(metrics_out[k]["label"] for k in hinted[:4])
        summary.append(f"观察到弱偏离（{names}），但未达到多指标校正后的显著阈值，"
                       "可能仍是正常抖动。")
    if unchanged and not interesting:
        summary.append("各指标均未出现超出噪声带的持续偏离，"
                       "观测到的差异属于基线附近的正常抖动。")
    elif unchanged:
        summary.append("其余指标均落在基线的正常波动带内。")
    for w in warnings:
        summary.append(f"⚠ {w}")

    diff: Dict[str, Any] = {
        "schema": SCHEMA,
        "run_id": run_id,
        "run_name": meta.get("name", run_id),
        "scene_id": meta["scene_id"],
        "scene_name": meta.get("scene_name", ""),
        "domain": meta["domain"],
        "model": meta["model"],
        "baseline_id": baseline_id,
        "baseline_name": base_doc.get("name", ""),
        "baseline_status": base_doc.get("status"),
        "baseline_snapshot": {
            "id": baseline_id,
            "name": base_doc.get("name", ""),
            "n_replicates": base_doc.get("n_replicates"),
            "seeds": base_doc.get("seeds"),
            "steps": base_doc.get("steps"),
            "created_at": base_doc.get("created_at"),
            "created_by": base_doc.get("created_by"),
            "checksum": base_doc.get("checksum"),
            "config": copy.deepcopy(base_doc.get("config", {})),
        },
        "candidate_steps": cand_total,
        "baseline_steps": int(base_doc["steps"]),
        "common_steps": horizon,
        "n_replicates": base_doc.get("n_replicates"),
        "alignment": {
            "primary": "absolute_step",
            "secondary": "normalized_progress",
            "norm_points": NORM_POINTS,
            "beyond_baseline": cand_total > int(base_doc["steps"]),
        },
        "config_diff": cfg_diff,
        "intervention_diff": itv_notes,
        "metrics": metrics_out,
        "counts": {"better": len(better), "worse": len(worse),
                   "changed_neutral": len(changed_n),
                   "hinted": len(hinted),
                   "unchanged": len(unchanged)},
        "verdict": verdict,
        "verdict_label": _VERDICT_LABEL[verdict],
        "summary": summary,
        "warnings": warnings,
        "candidate_sig": _candidate_signature(cand_series),
        "generated_at": util.now_iso(),
    }
    if persist:
        storage.save_diff(diff)
    return diff


def _candidate_signature(series: List[Dict[str, Any]]) -> List[Any]:
    """Cheap fingerprint to detect a cached diff whose run kept stepping."""
    last = series[-1] if series else {}
    digest = hashlib.sha256(_canonical(last)).hexdigest()[:12]
    return [len(series), int(last.get("step", -1)), digest]


def load_diff_annotated(run_id: str) -> Optional[Dict[str, Any]]:
    """Return a cached diff with fresh integrity / staleness annotations.

    The diff itself is never deleted when its baseline disappears — it embeds a
    checksummed baseline snapshot, so the historical conclusion stays
    reproducible and traceable; we only flag the current state.
    """
    diff = storage.load_diff(run_id)
    if diff is None:
        return None
    series = storage.load_series(run_id)
    if series and _candidate_signature(series) != diff.get("candidate_sig"):
        diff["stale"] = True
        diff.setdefault("warnings", []).append(
            "该对比生成后候选运行又继续推进过，结论可能已过时，建议重新对比")
    else:
        diff["stale"] = False
    base = storage.load_baseline(diff["baseline_id"])
    if base is None:
        diff["integrity"] = "baseline_missing"
        diff["baseline_status"] = "deleted"
    elif verify_baseline(base) == "corrupt":
        diff["integrity"] = "baseline_corrupt"
    else:
        diff["integrity"] = "ok"
        diff["baseline_status"] = base.get("status")
    return diff


def auto_compare_run(run_id: str) -> Optional[Dict[str, Any]]:
    """Compare a finished run against its scene's active baseline, if any."""
    meta = storage.load_run_meta(run_id)
    if meta is None or meta.get("status") != "finished":
        return None
    if len(storage.load_series(run_id)) < MIN_COMPARE_STEPS:
        return None
    base = active_baseline_for_scene(meta["scene_id"])
    if base is None:
        return None
    return compare_to_baseline(base["id"], run_id, persist=True)
