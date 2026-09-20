"""RP-06: nf_discovery_v1 can run from registered protocol to comparison."""

import csv
import json
from pathlib import Path
import sys

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT_ROOT / "scripts"))
import sector_first as SF  # noqa: E402

from alpha_agents.data import sector_source_probe as SP  # noqa: E402
from alpha_agents.evolution import selection_experiment as E  # noqa: E402
from alpha_agents.evolution import selection_experiment_compare as C  # noqa: E402


def _manifest():
    m = E.template(capabilities_hash="c" * 64)
    m.update({
        "baseline_identity": {
            "code_ref": "a" * 40,
            "policy_ref": "policy-v1",
            "input_hash": "i" * 64,
        },
        "decision_config": {
            "trader": "pullback",
            "picks_per_day": 2,
            "panel_size": 40,
            "participation": 0.10,
            "max_turns_per_decision": 1,
            "model_timeout_seconds": 120.0,
            "pace_seconds": 0.0,
            "news_limit": 0,
            "trader_tools_enabled": False,
            "direction_limit": 3,
            "learning_input": "frozen",
            "run_theme": "NF-DISCOVERY",
        },
        "model": {"name": "model-x", "temperature": 0},
        "cost_model": {"name": "virtual_a_share_v1"},
        "exit_policy": {
            "mechanical_stop": True,
            "mechanical_target": True,
            "agent_exits": False,
            "close_buys": False,
            "hard_stop_pct": 8.0,
        },
        "training_window": {
            "start": "2025-01-01", "end": "2025-12-31"},
        "validation_windows": [
            {"start": "2026-01-01", "end": "2026-01-30"},
            {"start": "2026-02-02", "end": "2026-03-03"},
            {"start": "2026-03-04", "end": "2026-04-02"},
            {"start": "2026-04-03", "end": "2026-05-02"},
        ],
        "minimum_meaningful_improvement_pp": 1.0,
        "risk_boundaries": {
            "max_drawdown_pct": 15.0,
            "max_tail_loss_pct": 8.0,
            "max_turnover_ratio": 10.0,
            "max_theme_cluster_exposure_pct": 40.0,
        },
        "block_method": {
            "method": "moving_block",
            "block_length_days": 5,
            "repetitions": 100,
            "cross_window_blocks": False,
        },
    })
    assert E.validate(m) == []
    return m


def _write_run(root: Path, *, manifest: dict, arm: str,
               days: list[str], daily_pct: float):
    root.mkdir(parents=True, exist_ok=True)
    initial = 100000.0
    value = initial
    equity = []
    for day in days:
        value *= 1.0 + daily_pct / 100.0
        equity.append({"date": day, "equity": value})

    with (root / "equity.csv").open(
            "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["date", "equity"])
        writer.writeheader()
        writer.writerows(equity)
    with (root / "fills.csv").open(
            "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["side", "amount"])
        writer.writeheader()

    architecture = E.ARMS[arm]["architecture"]
    meta = {
        "run_id": f"run-{arm}-{days[0]}",
        "selection_architecture": architecture,
        "experiment_arm": arm,
        "experiment_manifest_hash": E.require_valid(manifest),
        "code_ref": manifest["baseline_identity"]["code_ref"],
        "policy_ref": manifest["baseline_identity"]["policy_ref"],
        "input_identity": {
            "input_hash": manifest["baseline_identity"]["input_hash"]},
        "world_read_set_hashes": [f"world-{arm}-{day}" for day in days],
        "errors": [],
        "initial_account": {
            "kind": "initial_mark",
            "equity": initial,
        },
        "window": {
            "start": days[0],
            "end": days[-1],
            "trading_days": len(days),
        },
    }
    (root / "run.json").write_text(
        json.dumps(meta), encoding="utf-8")


def _days(start: int) -> list[str]:
    # Synthetic ISO days need only be unique/ordered for the performance layer.
    from datetime import date, timedelta
    base = date(2026, 1, 1) + timedelta(days=start)
    return [(base + timedelta(days=i)).isoformat() for i in range(30)]


def test_selection_capability_gate_does_not_require_fund_flow():
    report = {
        "capabilities": {
            "daily_price": {"status": "available"},
            "fund_flow": {
                "status": "missing_table",
                "point_in_time_grade": "U",
                "strict_replay_eligible": False,
            },
            "security_status": {
                "status": "available",
                "point_in_time_grade": "A",
                "strict_replay_eligible": True,
                "verification": {
                    "verified_by": "human-review",
                    "verified_at": "2026-09-20T00:00:00+08:00",
                    "evidence": "dated security-status archive",
                },
            },
        },
    }
    assert SF._selection_capability_errors(report) == []


def test_selection_capability_gate_still_requires_historical_security_status():
    report = {
        "capabilities": {
            "daily_price": {"status": "available"},
            "security_status": {
                "status": "available",
                "point_in_time_grade": "C",
                "strict_replay_eligible": False,
                "verification": None,
            },
        },
    }
    errors = SF._selection_capability_errors(report)
    assert any("point_in_time_grade" in item for item in errors)
    assert any("strict_replay_eligible" in item for item in errors)


def test_registered_selection_manifest_is_a_distinct_content_addressed_family(
        tmp_path):
    manifest = _manifest()
    path = E.register(manifest, tmp_path)
    got, digest = SF._registered_selection_manifest(path)
    assert got == manifest
    assert digest == E.require_valid(manifest)
    assert path.parent.name == "selection-manifests"


def test_compare_artifact_windows_recomputes_four_reset_accounts(tmp_path):
    manifest = _manifest()
    windows = []
    starts = [0, 40, 80, 120]
    for index, offset in enumerate(starts):
        days = _days(offset)
        manifest["validation_windows"][index] = {
            "start": days[0], "end": days[-1]}
        root = tmp_path / str(index)
        _write_run(
            root / "CONTROL", manifest=manifest, arm="CONTROL",
            days=days, daily_pct=0.0)
        _write_run(
            root / "SECTOR", manifest=manifest, arm="SECTOR",
            days=days, daily_pct=0.1)
        windows.append(root)

    got = C.compare_artifact_windows(
        manifest=manifest, window_dirs=windows, seed=7)

    assert got["status"] == "ok"
    assert got["total_unique_days"] == 120
    assert got["promotion_allowed"] is False
    assert got["historical_only"] is True
    assert len(got["windows"]) == 4
    assert got["mean_delta_pp"] > 0


def test_compare_artifact_windows_rejects_identity_drift(tmp_path):
    manifest = _manifest()
    roots = []
    for index, offset in enumerate([0, 40, 80, 120]):
        days = _days(offset)
        manifest["validation_windows"][index] = {
            "start": days[0], "end": days[-1]}
        root = tmp_path / str(index)
        for arm in ("CONTROL", "SECTOR"):
            _write_run(
                root / arm, manifest=manifest, arm=arm,
                days=days, daily_pct=0.0)
        roots.append(root)

    meta_path = roots[2] / "SECTOR" / "run.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["policy_ref"] = "changed"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    with pytest.raises(C.SelectionCompareError, match="policy_ref mismatch"):
        C.compare_artifact_windows(
            manifest=manifest, window_dirs=roots, seed=1)


def test_selection_walk_command_binds_closed_protocol_switches(tmp_path):
    manifest = _manifest()
    cmd = SF._selection_walk_command(
        manifest=manifest,
        manifest_path=tmp_path / "selection-manifests" / "m.json",
        membership_path=tmp_path / "membership.json",
        target=tmp_path / "state",
        out=tmp_path / "out",
        start="2026-01-01",
        days=30,
        arm="SECTOR",
        run_id="r1",
    )

    assert cmd[cmd.index("--selection-architecture") + 1] == (
        "sector_rank_price_v1")
    assert cmd[cmd.index("--selection-experiment-arm") + 1] == "SECTOR"
    assert "--no-trader-tools" in cmd
    assert cmd[cmd.index("--news-limit") + 1] == "0"
    assert "--agent-exits" not in cmd
    assert "--close-buys" not in cmd
