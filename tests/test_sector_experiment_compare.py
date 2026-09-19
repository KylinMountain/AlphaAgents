"""Four-arm sector experiment comparison stays preregistered and fail-closed."""

import csv
import json

import pytest

from alpha_agents.evolution import sector_experiment as E
from alpha_agents.evolution import sector_experiment_compare as C


def _manifest():
    manifest = E.template(capabilities_hash="a" * 64)
    manifest.update({
        "model": {"name": "model-x", "temperature": 0},
        "research_budget": {"max_total_calls": 20},
        "cost_model": {"commission_bps": 3},
        "exit_policy": {"name": "incumbent"},
        "training_window": {
            "start": "2025-01-01", "end": "2025-12-31"},
        "validation_windows": [
            {"start": "2026-01-01", "end": "2026-03-31"},
            {"start": "2026-04-01", "end": "2026-06-30"},
            {"start": "2026-07-01", "end": "2026-09-30"},
            {"start": "2026-10-01", "end": "2026-12-31"},
        ],
        "forward_start": "2027-01-01",
        "primary_metric": "portfolio_net_return_pct",
        "minimum_days": 50,
        "stopping_rule": {"validation_windows": 4, "then": "freeze"},
        "risk_boundaries": {
            "max_drawdown_pct": 20.0,
            "max_tail_loss_pct": 10.0,
            "max_turnover_ratio": 20.0,
            "max_theme_cluster_exposure_pct": 50.0,
        },
        "block_method": {
            "method": "moving_block",
            "block_length_days": 5,
            "repetitions": 100,
        },
    })
    return manifest


def _write_csv(path, fieldnames, rows):
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _arm(tmp_path, arm, architecture, slope=0.1, *, exposure=True):
    root = tmp_path / arm
    root.mkdir()
    meta = {
        "run_id": f"run-{arm}",
        "selection_architecture": architecture,
        "window": {
            "start": "2026-01-01",
            "end": "2026-03-31",
            "trading_days": 60,
        },
        "errors": [],
        "model_usage_ok": True,
        "agent_tool_calls": 100,
        "model_calls_made": 60,
        "decider_counters": {},
        "capability_matrix": {},
    }
    (root / "run.json").write_text(
        json.dumps(meta), encoding="utf-8")

    equity = []
    for i in range(61):
        equity.append({
            "date": f"2026-01-{(i % 28) + 1:02d}-{i:02d}",
            "equity": 100000 + slope * i * 1000,
        })
    _write_csv(root / "equity.csv", ["date", "equity"], equity)
    _write_csv(
        root / "fills.csv",
        ["side", "amount"],
        [{"side": "buy", "amount": 1000}],
    )
    if exposure:
        _write_csv(
            root / "theme_exposure.csv",
            ["complete", "max_theme_cluster_exposure_pct"],
            [{
                "complete": 1,
                "max_theme_cluster_exposure_pct": 25.0,
            }],
        )
    (root / "direction_report.json").write_text(
        json.dumps({"sets": 60}), encoding="utf-8")
    (root / "stock_report.json").write_text(
        json.dumps({"sets": 60}), encoding="utf-8")
    return root


def _arms(tmp_path, *, exposure=True):
    return {
        "A": _arm(tmp_path, "A", "dual_rank_v0", 0.10,
                  exposure=exposure),
        "B": _arm(tmp_path, "B", "sector_first_v0", 0.20,
                  exposure=exposure),
        "C": _arm(tmp_path, "C", "sector_first_simple_selector", 0.18,
                  exposure=exposure),
        "D": _arm(tmp_path, "D", "sector_first_no_flow", 0.15,
                  exposure=exposure),
    }


def test_complete_artifacts_produce_reviewable_not_promotable_report(tmp_path):
    got = C.compare(manifest=_manifest(), arm_dirs=_arms(tmp_path))
    assert got["status"] == "ready_for_human_review"
    assert got["promotion_eligible"] is False
    assert got["paired_daily"]["B_minus_A"]["n_days"] == 60
    assert got["arms"]["B"]["risk"]["passed"] is True


def test_missing_cluster_exposure_is_insufficient_not_assumed_safe(tmp_path):
    got = C.compare(
        manifest=_manifest(), arm_dirs=_arms(tmp_path, exposure=False))
    assert got["status"] == "insufficient"
    assert "max_theme_cluster_exposure_pct" in (
        got["arms"]["A"]["risk"]["unverified"])


def test_wrong_arm_architecture_is_refused(tmp_path):
    arms = _arms(tmp_path)
    meta_path = arms["B"] / "run.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["selection_architecture"] = "dual_rank_v0"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(C.SectorCompareError, match="arm B expected"):
        C.compare(manifest=_manifest(), arm_dirs=arms)


def test_window_must_be_preregistered(tmp_path):
    arms = _arms(tmp_path)
    for root in arms.values():
        path = root / "run.json"
        meta = json.loads(path.read_text(encoding="utf-8"))
        meta["window"]["start"] = "2026-02-01"
        meta["window"]["end"] = "2026-04-30"
        path.write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(C.SectorCompareError, match="preregistered"):
        C.compare(manifest=_manifest(), arm_dirs=arms)



def test_incomplete_cluster_file_is_not_treated_as_safe(tmp_path):
    arms = _arms(tmp_path)
    path = arms["B"] / "theme_exposure.csv"
    _write_csv(
        path,
        ["complete", "max_theme_cluster_exposure_pct"],
        [{"complete": 0, "max_theme_cluster_exposure_pct": 5.0}],
    )
    got = C.compare(manifest=_manifest(), arm_dirs=arms)
    assert got["status"] == "insufficient"
    assert "max_theme_cluster_exposure_pct" in (
        got["arms"]["B"]["risk"]["unverified"])
