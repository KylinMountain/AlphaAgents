"""Four-arm sector experiment comparison stays preregistered and fail-closed."""

import csv
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import walk_forward as wf  # noqa: E402

from alpha_agents.evolution import sector_experiment as E
from alpha_agents.evolution import sector_experiment_compare as C


def _manifest():
    manifest = E.template(capabilities_hash="a" * 64)
    manifest.update({
        "baseline_identity": {
            "code_ref": "72ce91a96e8ab0ee499dae0d6e71deef36e67ddd",
            "policy_ref": "policy-v1",
            "input_hash": "i" * 64,
        },
        "decision_config": {
            "trader": "pullback",
            "picks_per_day": 2,
            "panel_size": 40,
            "participation": 0.10,
            "trader_tools_enabled": True,
            "max_turns_per_decision": None,
            "news_limit": 60,
            "model_timeout_seconds": 120.0,
            "pace_seconds": 0.0,
        },
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
        "minimum_meaningful_improvement_pct": 0.10,
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
        "experiment_arm": arm,
        "experiment_manifest_hash": E.require_valid(_manifest()),
        "frozen_directions_hash": (
            "frozen-b-directions" if arm == "C" else None),
        "window": {
            "start": "2026-01-01",
            "end": "2026-03-31",
            "trading_days": 60,
        },
        "initial_account": {
            "kind": "initial_mark",
            "as_of": "2025-12-31",
            "next_session": "2026-01-01",
            "equity": 100000.0,
        },
        "errors": [],
        "model_usage_ok": True,
        "agent_tool_calls": 100,
        "model_calls_made": 60,
        "decider_counters": {
            "decider_refused:outside_panel": 2,
            "research_budget_denied": 1,
        },
        "capability_matrix": {
            "strict_pit_membership": True,
            "news_replay": "free_flash_only",
        },
    }
    (root / "run.json").write_text(
        json.dumps(meta), encoding="utf-8")

    equity = []
    start = date(2026, 1, 1)
    for i in range(60):
        equity.append({
            "date": (start + timedelta(days=i)).isoformat(),
            "equity": 100000 + slope * (i + 1) * 1000,
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
    assert got["paired_daily"]["B_minus_A"]["expected_trading_days"] == 60
    assert got["paired_daily"]["B_minus_A"]["left_observed_days"] == 60
    assert got["paired_daily"]["B_minus_A"]["right_observed_days"] == 60
    assert got["paired_daily"]["B_minus_A"]["common_trading_days"] == 60
    assert got["arms"]["B"]["risk"]["passed"] is True
    assert got["arms"]["B"]["layers"]["direction"]["sets"] == 60
    assert got["arms"]["B"]["layers"]["stock"]["sets"] == 60
    assert got["arms"]["B"]["layers"]["execution"]["fills"] == 1
    assert (
        got["arms"]["B"]["layers"]["portfolio"]
        == got["arms"]["B"]["portfolio"]
    )
    assert got["measurement_contract"]["portfolio"].startswith("ledger")
    operational = got["operational_summary"]["B"]
    assert operational["budget"] == {
        "agent_tool_calls": 100,
        "model_calls_made": 60,
    }
    assert operational["decision_counters"]["decider_refused:outside_panel"] == 2
    assert operational["coverage"]["strict_pit_membership"] is True
    assert got["arms"]["B"]["decision_counters"] == operational["decision_counters"]
    assert got["arms"]["B"]["coverage"] == operational["coverage"]
    assert got["evidence_status"]["data"]["B"] == "complete"
    assert got["evidence_status"]["execution"]["B"] == "has_fills"
    assert got["paired_daily"]["B_minus_A"]["evidence"] in {
        "positive_beyond_floor", "negative_beyond_floor", "inconclusive",
    }


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



def test_mismatched_manifest_hash_is_refused(tmp_path):
    arms = _arms(tmp_path)
    path = arms["D"] / "run.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    meta["experiment_manifest_hash"] = "wrong"
    path.write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(C.SectorCompareError, match="binding mismatch"):
        C.compare(manifest=_manifest(), arm_dirs=arms)


def test_c_without_frozen_direction_hash_is_refused(tmp_path):
    arms = _arms(tmp_path)
    path = arms["C"] / "run.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    meta["frozen_directions_hash"] = None
    path.write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(C.SectorCompareError, match="frozen_directions_hash"):
        C.compare(manifest=_manifest(), arm_dirs=arms)



def test_replay_binds_to_the_frozen_manifest_and_arm(tmp_path):
    manifest = _manifest()
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    args = SimpleNamespace(
        experiment_manifest=path,
        experiment_arm="B",
        decider="llm",
    )
    loaded, digest = wf._experiment_contract(
        args,
        architecture="sector_first_v0",
        membership_archive=(object(),),
    )
    assert loaded == manifest
    assert digest == E.require_valid(manifest)


def test_replay_refuses_an_arm_with_the_wrong_architecture(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(_manifest()), encoding="utf-8")
    args = SimpleNamespace(
        experiment_manifest=path,
        experiment_arm="B",
        decider="llm",
    )
    with pytest.raises(SystemExit, match="arm B requires"):
        wf._experiment_contract(
            args,
            architecture="dual_rank_v0",
            membership_archive=(object(),),
        )


def test_formal_a_arm_also_requires_the_pit_membership_archive(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(_manifest()), encoding="utf-8")
    args = SimpleNamespace(
        experiment_manifest=path,
        experiment_arm="A",
        decider="llm",
    )
    with pytest.raises(SystemExit, match="same --sector-membership"):
        wf._experiment_contract(
            args,
            architecture="dual_rank_v0",
            membership_archive=(),
        )


def test_replay_window_must_equal_a_preregistered_window():
    ctx = SimpleNamespace(experiment_manifest=_manifest())
    wf._verify_experiment_window(
        ctx, ["2026-01-01", "2026-01-15", "2026-03-31"])
    with pytest.raises(SystemExit, match="not one of"):
        wf._verify_experiment_window(
            ctx, ["2026-01-02", "2026-01-15", "2026-03-31"])



def test_ci_that_does_not_clear_preregistered_floor_is_inconclusive(tmp_path):
    manifest = _manifest()
    manifest["minimum_meaningful_improvement_pct"] = 99.0
    arms = _arms(tmp_path)
    digest = E.require_valid(manifest)
    for root in arms.values():
        path = root / "run.json"
        meta = json.loads(path.read_text(encoding="utf-8"))
        meta["experiment_manifest_hash"] = digest
        path.write_text(json.dumps(meta), encoding="utf-8")

    got = C.compare(manifest=manifest, arm_dirs=arms)
    assert got["paired_daily"]["B_minus_A"]["evidence"] == "inconclusive"
    assert got["promotion_eligible"] is False


def test_no_fill_sample_is_reported_separately(tmp_path):
    arms = _arms(tmp_path)
    for root in arms.values():
        _write_csv(root / "fills.csv", ["side", "amount"], [])
    got = C.compare(manifest=_manifest(), arm_dirs=arms)
    assert got["status"] == "insufficient"
    assert set(got["evidence_status"]["execution"].values()) == {"no_fills"}
    assert "no arm produced an executable fill sample" in got["reasons"]



def test_formal_replay_refuses_runtime_that_differs_from_manifest():
    args = wf.build_parser().parse_args([
        "--start", "2026-01-01",
        "--decider", "llm",
    ])
    manifest = _manifest()
    actual = wf._experiment_runtime_contract(args)
    manifest.update(actual)
    wf._verify_experiment_runtime(args, manifest)

    changed = dict(manifest)
    changed["cost_model"] = dict(manifest["cost_model"])
    changed["cost_model"]["commission_rate"] += 0.001
    with pytest.raises(SystemExit, match="frozen manifest"):
        wf._verify_experiment_runtime(args, changed)


def test_runtime_contract_names_full_virtual_cost_model():
    args = wf.build_parser().parse_args([
        "--start", "2026-01-01",
        "--decider", "llm",
    ])
    cost = wf._experiment_runtime_contract(args)["cost_model"]
    assert cost["commission_rate"] > 0
    assert cost["min_commission_rmb"] > 0
    assert cost["stamp_duty_sell_rate"] > 0
    assert cost["transfer_fee_rate"] > 0
    assert cost["slippage_rate"] > 0


def test_load_arm_anchors_return_and_drawdown_to_initial_mark(tmp_path):
    root = _arm(tmp_path, "A", "dual_rank_v0", 0.0)
    meta_path = root / "run.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["window"]["trading_days"] = 2
    meta["initial_account"]["equity"] = 100.0
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    _write_csv(
        root / "equity.csv",
        ["date", "equity"],
        [
            {"date": "2026-01-05", "equity": 95.0},
            {"date": "2026-01-06", "equity": 100.0},
        ],
    )

    got = C.load_arm(root, "A")
    assert got["portfolio"]["net_return_pct"] == 0.0
    assert got["portfolio"]["max_drawdown_pct"] == 5.0
    assert got["daily_returns_pct"]["2026-01-05"] == pytest.approx(
        -5.0, abs=1e-12)
    assert got["daily_returns_pct"]["2026-01-06"] == pytest.approx(
        5.263157894736842)


def test_load_arm_refuses_legacy_run_without_initial_mark(tmp_path):
    root = _arm(tmp_path, "A", "dual_rank_v0", 0.0)
    meta_path = root / "run.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    del meta["initial_account"]
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    with pytest.raises(C.SectorCompareError, match="initial_account"):
        C.load_arm(root, "A")


def test_load_arm_refuses_duplicate_days_even_when_declared_count_matches(tmp_path):
    root = _arm(tmp_path, "A", "dual_rank_v0", 0.0)
    meta_path = root / "run.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["window"]["trading_days"] = 2
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    _write_csv(
        root / "equity.csv",
        ["date", "equity"],
        [
            {"date": "2026-01-05", "equity": 100000.0},
            {"date": "2026-01-05", "equity": 100001.0},
        ],
    )

    with pytest.raises(C.SectorCompareError, match="duplicate"):
        C.load_arm(root, "A")


def test_load_arm_accepts_one_real_trading_day(tmp_path):
    root = _arm(tmp_path, "A", "dual_rank_v0", 0.0)
    meta_path = root / "run.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["window"]["trading_days"] = 1
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    _write_csv(
        root / "equity.csv",
        ["date", "equity"],
        [{"date": "2026-01-05", "equity": 99000.0}],
    )

    got = C.load_arm(root, "A")
    assert got["portfolio"]["net_return_pct"] == -1.0
    assert got["portfolio"]["max_drawdown_pct"] == 1.0


def test_compare_refuses_silent_intersection_of_different_calendars(tmp_path):
    arms = _arms(tmp_path)
    path = arms["B"] / "equity.csv"
    rows = list(csv.DictReader(path.open("r", encoding="utf-8", newline="")))
    rows[-1]["date"] = "2026-03-15"
    _write_csv(path, ["date", "equity"], rows)

    with pytest.raises(
            C.SectorCompareError, match="observed trading-day calendar"):
        C.compare(manifest=_manifest(), arm_dirs=arms)
