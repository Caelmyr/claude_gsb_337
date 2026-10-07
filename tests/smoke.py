"""Smoke tests for the simulation engines, storage and run lifecycle.

Run directly::

    python3 tests/smoke.py

Each check is independent and prints PASS / FAIL; the script exits non-zero on
the first failure so it can be wired into CI or a pre-commit hook.
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import baseline, models, report, storage  # noqa: E402
from backend.engine import make_engine  # noqa: E402
from backend.run_manager import manager  # noqa: E402

_ENGINES = ["traffic/ca", "traffic/abm", "ecology/ca", "ecology/abm",
            "epidemic/ca", "epidemic/abm"]


def check(name: str, fn) -> None:
    try:
        fn()
        print(f"PASS  {name}")
    except AssertionError as exc:
        print(f"FAIL  {name}: {exc}")
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL  {name}: {exc}")
        sys.exit(1)


def engines_step() -> None:
    for key in _ENGINES:
        d, m = key.split("/")
        eng = make_engine(d, m, seed=42)
        for _ in range(5):
            eng.step()
        assert eng.step_count == 5, key
        assert len(eng.individuals()) > 0, f"{key} has no individuals"
        stats = eng.stats()
        assert stats, f"{key} produced empty stats"
        snap = eng.snapshot()
        assert snap["step"] == 5
        assert snap["stats"] == stats


def interventions_apply() -> None:
    eng = make_engine("epidemic", "abm", seed=1)
    before = eng.stats()["susceptible"]
    res = eng.apply_intervention({"type": "vaccinate", "params": {"fraction": 1.0}})
    assert res["applied"], res
    assert eng.stats()["susceptible"] == 0
    assert before > 0


def storage_atomic_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as td:
        # Redirect the module's DATA_DIR for this isolated check.
        old = storage.DATA_DIR
        storage.DATA_DIR = td
        try:
            storage.ensure_dirs()
            storage.save_scene({"id": "x", "name": "t", "updated_at": "z"})
            assert storage.load_scene("x")["name"] == "t"
            storage.save_step("r1", 0, {"step": 0, "v": 1})
            storage.save_step("r1", 7, {"step": 7, "v": 2})
            assert storage.load_step("r1", 7)["v"] == 2
            assert storage.list_steps("r1") == [0, 7]
        finally:
            storage.DATA_DIR = old


def run_lifecycle() -> None:
    scene = models.Scene(domain="epidemic", model="abm",
                         config={"n": 200, "width": 300, "height": 300,
                                 "initial_infected": 5})
    meta = manager.create_run(scene, seed=1, snapshot_interval=2)
    rid = meta["id"]
    try:
        r = manager.step(rid, 6)
        assert r["step"] == 6
        series = manager.get_series(rid)
        assert series[0]["step"] == 0 and series[-1]["step"] == 6
        assert len(manager.get_individuals(rid, 6)) == 200
        # snapshot_interval=2 -> full snapshots persisted at 0,2,4,6
        steps = storage.list_steps(rid)
        assert steps == [0, 2, 4, 6], steps
        rpt = report.generate_report(rid)
        assert rpt["steps"] == 7
    finally:
        manager.delete_run(rid)


def baseline_regression() -> None:
    scene = models.Scene.from_dict({
        "domain": "epidemic", "model": "ca",
        "config": {"width": 50, "height": 50, "beta": 0.4, "gamma": 0.1,
                   "initial_infected": 5, "vaccination_rate": 0.9},
    })
    base = manager.create_run(scene, seed=7)
    base_id = base["id"]
    cand_same = cand_diff = None
    try:
        manager.run_batch(base_id, 30, keep_engine=False)
        doc = baseline.mark_baseline(scene.id, base_id, note="v1")
        assert doc["steps"] == 31 and doc["version"] == 1
        assert storage.load_baseline(scene.id)["hash"] == doc["hash"]

        # Same config + seed -> identical series -> all jitter, no real change.
        # Longer candidate also exercises step-count alignment.
        cand_same = manager.create_run(scene, seed=7)["id"]
        manager.run_batch(cand_same, 45, keep_engine=False)
        cmp_same = baseline.compare_run(cand_same)
        assert cmp_same["alignment"]["overlap_end"] == 30
        assert cmp_same["alignment"]["cand_tail"] == 15
        assert cmp_same["verdict"]["overall"] == "unchanged", cmp_same["verdict"]
        assert all(not m["significant"] for m in cmp_same["metrics"].values())

        # Dropping the vaccination rate must surface as a real regression.
        scene2 = models.Scene.from_dict({
            "id": scene.id, "domain": "epidemic", "model": "ca",
            "config": {**scene.config, "vaccination_rate": 0.0},
        })
        cand_diff = manager.create_run(scene2, seed=7)["id"]
        manager.run_batch(cand_diff, 30, keep_engine=False)
        cmp_diff = baseline.compare_run(cand_diff)
        assert any(d["key"] == "vaccination_rate"
                   for d in cmp_diff["config_diff"])
        infected = cmp_diff["metrics"]["infected"]
        assert infected["significant"] and infected["verdict"] == "worse"
        assert infected["anomalies"], "expected anomalous intervals"
        assert cmp_diff["verdict"]["overall"] in ("regressed", "mixed")
        assert cmp_diff["summary"], "summary lines missing"

        # Replacing the baseline bumps the version and keeps provenance.
        doc2 = baseline.mark_baseline(scene.id, cand_diff, note="v2")
        assert doc2["version"] == 2
        assert doc2["history"] and doc2["history"][0]["hash"] == doc["hash"]

        # Deleting the source run must not break comparisons (frozen copy).
        manager.delete_run(cand_diff)
        cand_diff = None
        cmp_missing = baseline.compare_run(cand_same)
        assert cmp_missing["baseline"]["source_status"] == "missing"

        # Deleting the baseline removes comparability cleanly.
        storage.delete_baseline(scene.id)
        try:
            baseline.compare_run(cand_same)
            raise AssertionError("expected KeyError without baseline")
        except KeyError:
            pass
    finally:
        for rid in (base_id, cand_same, cand_diff):
            if rid:
                manager.delete_run(rid)
        storage.delete_baseline(scene.id)


def main() -> None:
    check("six engines step and snapshot", engines_step)
    check("interventions apply", interventions_apply)
    check("atomic sharded storage", storage_atomic_roundtrip)
    check("run lifecycle + report", run_lifecycle)
    check("baseline regression", baseline_regression)
    print("\nall smoke tests passed")


if __name__ == "__main__":
    main()
