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


class _TmpData:
    """Redirect the file-system store to a throwaway directory for a test."""

    def __init__(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self._old = storage.DATA_DIR

    def __enter__(self) -> str:
        storage.DATA_DIR = self._td.name
        storage.ensure_dirs()
        return self._td.name

    def __exit__(self, *exc) -> None:
        storage.DATA_DIR = self._old
        self._td.cleanup()


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


def _ep_scene(beta: float = 0.3) -> models.Scene:
    return models.Scene(id="scene_test_epi", domain="epidemic", model="abm",
                        config={"n": 600, "width": 450, "height": 450,
                                "initial_infected": 40, "beta": beta,
                                "gamma": 0.05, "speed": 2.0, "radius": 6.0})


def _run(scene: models.Scene, seed: int, steps: int = 100) -> str:
    meta = manager.create_run(scene, seed=seed, snapshot_interval=1000)
    manager.run_batch(meta["id"], steps, keep_engine=False)
    return meta["id"]


def baseline_lifecycle() -> None:
    with _TmpData():
        scene = _ep_scene()
        base = baseline.create_from_runs(
            [_run(scene, 100 + i) for i in range(5)], name="回归基线")
        assert baseline.verify_baseline(base) == "ok"
        assert base["status"] == "active" and base["n_replicates"] == 5
        assert base["metrics"]["infected"]["band"]
        # it is immutable: lifecycle edits do not break the checksum
        retired = baseline.retire_baseline(base["id"])
        assert retired["status"] == "retired"
        assert baseline.verify_baseline(
            storage.load_baseline(base["id"])) == "ok"
        again = baseline.reactivate_baseline(base["id"])
        assert again["status"] == "active"
        # tampering with immutable content is detected
        doc = storage.load_baseline(base["id"])
        doc["config"]["beta"] = 0.99
        storage.save_baseline(doc)
        assert baseline.verify_baseline(
            storage.load_baseline(base["id"])) == "corrupt"


def baseline_detects_real_change_and_jitter() -> None:
    with _TmpData():
        scene = _ep_scene()
        base = baseline.create_from_runs(
            [_run(scene, 100 + i) for i in range(6)], name="b")
        # strong, sustained parameter change must be flagged on infected
        low = _run(_ep_scene(beta=0.1), seed=100)
        d = baseline.compare_to_baseline(base["id"], low)
        assert d["metrics"]["infected"]["changed"], d["verdict"]
        assert d["metrics"]["infected"]["family_changed"]
        assert d["metrics"]["infected"]["direction_change"] == "down"
        assert d["metrics"]["infected"]["quality"] == "better"
        # config diff captured
        keys = {x["key"] for x in d["config_diff"]["changed"]}
        assert "beta" in keys
        # band + sustained segments exist (one-step spikes would not survive)
        m = d["metrics"]["infected"]
        assert m["chart"]["band_low"] and m["chart"]["band_high"]
        assert m["segments"], "sustained deviation should produce a segment"
        # cached diff is readable without a fresh comparison
        cached = baseline.load_diff_annotated(low)
        assert cached["verdict"] == d["verdict"]


def baseline_alignment_and_deletion() -> None:
    with _TmpData():
        scene = _ep_scene()
        base = baseline.create_from_runs(
            [_run(scene, 100 + i) for i in range(5)], name="b")
        # different step counts: absolute-step + normalized alignment + tail
        longer = _run(scene, 200, steps=140)
        d = baseline.compare_to_baseline(base["id"], longer)
        assert d["common_steps"] == 101 and d["candidate_steps"] == 141
        assert d["alignment"]["secondary"] == "normalized_progress"
        assert any("超出基线" in w for w in d["warnings"])
        m = d["metrics"]["infected"]
        assert m["tail"] and m["tail"]["end_step"] == 140
        assert m["normalized"]["progress"][0] == 0.0
        assert m["normalized"]["progress"][-1] == 1.0
        # deleting the baseline does not destroy the historical conclusion
        storage.delete_baseline(base["id"])
        ann = baseline.load_diff_annotated(longer)
        assert ann["integrity"] == "baseline_missing"
        assert ann["baseline_snapshot"]["checksum"]
        assert ann["verdict_label"]


def baseline_rejects_mismatch() -> None:
    with _TmpData():
        base = baseline.create_from_runs(
            [_run(_ep_scene(), 100 + i) for i in range(3)], name="b")
        other = models.Scene(id="scene_test_traffic", domain="traffic",
                             model="ca",
                             config={"lanes": 3, "length": 120})
        rid = _run(other, seed=1, steps=100)
        try:
            baseline.compare_to_baseline(base["id"], rid)
            raise AssertionError("cross-scene compare must fail")
        except ValueError:
            pass


def main() -> None:
    check("six engines step and snapshot", engines_step)
    check("interventions apply", interventions_apply)
    check("atomic sharded storage", storage_atomic_roundtrip)
    check("run lifecycle + report", run_lifecycle)
    check("baseline lifecycle + checksum", baseline_lifecycle)
    check("baseline separates change from jitter",
          baseline_detects_real_change_and_jitter)
    check("baseline length alignment + deletion survival",
          baseline_alignment_and_deletion)
    check("baseline rejects cross-scene comparison", baseline_rejects_mismatch)
    print("\nall smoke tests passed")


if __name__ == "__main__":
    main()
