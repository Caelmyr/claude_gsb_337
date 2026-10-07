"""Headless command-line batch runner.

Run a scene from the terminal without the web UI — useful for quick parameter
sweeps and for verifying the simulation + storage layer independently:

    python3 cli.py --list
    python3 cli.py --scene scene_epidemic_abm --steps 200 --report
    python3 cli.py --scene scene_traffic_ca --steps 500 --snapshot-interval 5

Baseline regression from the terminal::

    python3 cli.py --list-baselines [--scene SCENE]
    # mark the run just produced (or an existing one) as the scene's baseline:
    python3 cli.py --scene scene_epidemic_abm --steps 200 --mark-baseline \\
        --baseline-name "β=0.30 基准"
    # make a statistically grounded baseline from N independent replicates:
    python3 cli.py --scene scene_epidemic_abm --baseline-replicates 5 --steps 200
    # judge a run against the active baseline (or an explicit --baseline ID):
    python3 cli.py --scene scene_epidemic_abm --steps 200 --diff
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional

from backend import baseline, models, report, run_manager, storage, util


def list_scenes() -> None:
    for s in storage.list_scenes():
        print(f"{s['id']:24s} {s['domain']:8s}/{s['model']:3s}  {s['name']}")


def list_baselines(scene_id: Optional[str] = None) -> None:
    docs = storage.list_baselines()
    if scene_id:
        docs = [b for b in docs if b.get("scene_id") == scene_id]
    if not docs:
        print("(no baselines)")
        return
    for b in docs:
        integrity = baseline.verify_baseline(b)
        print(f"{b['id']:22s} {b['scene_id']:24s} "
              f"x{b.get('n_replicates', 1):<2d} {b['steps']:>4d}步 "
              f"[{b['status']}/{integrity}]  {b['name']}")


def run_one(scene_id: str, steps: int, snapshot_interval: int,
            make_report: bool, mark_baseline: bool = False,
            baseline_name: str = "", make_diff: bool = False,
            baseline_id: str = "") -> int:
    scene = storage.load_scene(scene_id)
    if scene is None:
        print(f"error: scene not found: {scene_id}", file=sys.stderr)
        return 1
    scene_obj = models.Scene.from_dict(scene)
    meta = run_manager.manager.create_run(
        scene_obj, snapshot_interval=snapshot_interval)
    print(f"run {meta['id']}: {meta['name']} "
          f"({meta['domain']}/{meta['model']}) seed={meta['seed']}")

    result = run_manager.manager.run_batch(meta["id"], steps, keep_engine=True)
    print(f"finished at step {result['step']}")
    for k, v in result["stats"].items():
        print(f"  {k:16s} {v}")

    if make_report:
        rpt = report.generate_report(meta["id"])
        print("\nsummary:")
        for line in rpt["summary"]:
            print(f"  - {line}")

    if mark_baseline:
        doc = baseline.create_from_runs([meta["id"]], name=baseline_name)
        print(f"\nbaseline {doc['id']} marked ({doc['checksum'][:18]}…)")

    target_baseline = baseline_id
    if make_diff:
        if not target_baseline:
            active = baseline.active_baseline_for_scene(scene_id)
            if active is None:
                print("error: no active baseline for scene; "
                      "use --mark-baseline or --baseline-replicates first",
                      file=sys.stderr)
                return 1
            target_baseline = active["id"]
        print_diff(baseline.compare_to_baseline(target_baseline, meta["id"]))
    return 0


def build_baseline(scene_id: str, replicates: int, steps: int,
                   name: str) -> int:
    scene = storage.load_scene(scene_id)
    if scene is None:
        print(f"error: scene not found: {scene_id}", file=sys.stderr)
        return 1
    print(f"building baseline from {replicates} replicates x {steps} steps …")
    doc = baseline.create_from_scene(scene_id, replicates=replicates,
                                     steps=steps, name=name)
    print(f"baseline {doc['id']}: {doc['name']} ({doc['checksum'][:18]}…)")
    return 0


def print_diff(diff: dict) -> None:
    print(f"\n=== 基线回归：{diff['verdict_label']} ===")
    print(f"baseline: {diff['baseline_name']} ({diff['baseline_id']}, "
          f"x{diff['n_replicates']}, {diff['baseline_steps']} 步)")
    print(f"run:      {diff['run_name']} ({diff['run_id']}, "
          f"{diff['candidate_steps']} 步, 共同 {diff['common_steps']} 步)")
    c = diff["counts"]
    print(f"metrics:  改善 {c['better']} / 恶化 {c['worse']} / "
          f"仅变化 {c['changed_neutral']} / 无显著差异 {c['unchanged']}")
    print("\n".join(f"  - {line}" for line in diff["summary"]))


def main() -> int:
    p = argparse.ArgumentParser(description="Headless simulation batch runner")
    p.add_argument("--list", action="store_true", help="list available scenes")
    p.add_argument("--list-baselines", action="store_true",
                   help="list baselines (optionally filtered by --scene)")
    p.add_argument("--scene", help="scene id to run")
    p.add_argument("--steps", type=int, default=200, help="steps to run")
    p.add_argument("--snapshot-interval", type=int, default=1,
                   help="persist a full snapshot every N steps")
    p.add_argument("--report", action="store_true", help="generate a report")
    p.add_argument("--mark-baseline", action="store_true",
                   help="freeze the produced run as its scene's baseline")
    p.add_argument("--baseline-name", default="", help="name for a new baseline")
    p.add_argument("--baseline-replicates", type=int, default=0, metavar="N",
                   help="build a baseline from N fresh replicate runs and exit")
    p.add_argument("--diff", action="store_true",
                   help="compare the produced run with the active baseline")
    p.add_argument("--baseline", default="", help="explicit baseline id for --diff")
    args = p.parse_args()

    storage.ensure_dirs()
    if args.list:
        list_scenes()
        return 0
    if args.list_baselines:
        list_baselines(args.scene)
        return 0
    if not args.scene:
        p.print_help()
        return 1
    if args.baseline_replicates:
        return build_baseline(args.scene, args.baseline_replicates,
                              args.steps, args.baseline_name)
    return run_one(args.scene, args.steps, args.snapshot_interval,
                   args.report, args.mark_baseline, args.baseline_name,
                   args.diff, args.baseline)


if __name__ == "__main__":
    sys.exit(main())
