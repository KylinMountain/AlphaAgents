"""Sector-first S0: source audit and preregistered experiment contract."""

from __future__ import annotations

import sqlite3

from alpha_agents.data import sector_source_probe as P
from alpha_agents.evolution import sector_experiment as E


def test_membership_without_time_field_is_current_only(tmp_path):
    path = tmp_path / "stocks.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE concept_stocks (concept_id INTEGER, stock_code TEXT)")
    conn.commit()
    conn.close()

    rows = P.probe_membership_sources(tmp_path)
    hit = next(row for row in rows if row["dataset"] == "concept_stocks")
    assert hit["point_in_time_grade"] == "C"
    assert hit["strict_replay_eligible"] is False
    assert hit["time_fields"] == []


def test_a_time_field_is_unknown_until_semantics_are_verified(tmp_path):
    path = tmp_path / "stocks.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE industry_membership "
        "(industry_id TEXT, stock_code TEXT, effective_date TEXT)")
    conn.commit()
    conn.close()

    rows = P.probe_membership_sources(tmp_path)
    hit = next(row for row in rows
               if row["dataset"] == "industry_membership")
    assert hit["point_in_time_grade"] == "U"
    assert hit["strict_replay_eligible"] is False
    assert hit["time_fields"] == ["effective_date"]


def test_incomplete_manifest_is_refused():
    manifest = E.template(capabilities_hash="a" * 64)
    errors = E.validate(manifest)
    assert errors
    assert any("risk_boundaries" in error for error in errors)
    assert any("validation_windows" in error for error in errors)


def test_frozen_four_arm_manifest_is_valid():
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
            {"start": "2026-01-01", "end": "2026-02-15"},
            {"start": "2026-02-16", "end": "2026-03-31"},
            {"start": "2026-04-01", "end": "2026-05-15"},
            {"start": "2026-05-16", "end": "2026-06-30"},
        ],
        "forward_start": "2026-07-01",
        "primary_metric": "portfolio_net_return_pct",
        "minimum_meaningful_improvement_pct": 0.10,
        "stopping_rule": {"validation_windows": 4, "then": "freeze"},
        "risk_boundaries": {
            "max_drawdown_pct": 12.0,
            "max_tail_loss_pct": 8.0,
            "max_turnover_ratio": 6.0,
            "max_theme_cluster_exposure_pct": 35.0,
        },
        "block_method": {
            "method": "moving_block",
            "block_length_days": 5,
            "repetitions": 1000,
        },
    })

    assert E.validate(manifest) == []
    first = E.require_valid(manifest)
    second = E.require_valid(dict(manifest))
    assert first == second



def test_manifest_identity_and_improvement_floor_are_required():
    manifest = E.template(capabilities_hash="a" * 64)
    errors = E.validate(manifest)
    assert "baseline_identity.code_ref is required" in errors
    assert "baseline_identity.policy_ref is required" in errors
    assert "baseline_identity.input_hash is required" in errors
    assert "decision_config is required" in errors
    assert "minimum_meaningful_improvement_pct must be numeric" in errors


def test_registered_manifest_is_content_addressed_and_never_overwritten(tmp_path):
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
        "training_window": {"start": "2025-01-01", "end": "2025-12-31"},
        "validation_windows": [
            {"start": "2026-01-01", "end": "2026-02-15"},
            {"start": "2026-02-16", "end": "2026-03-31"},
            {"start": "2026-04-01", "end": "2026-05-15"},
            {"start": "2026-05-16", "end": "2026-06-30"},
        ],
        "forward_start": "2026-07-01",
        "primary_metric": "portfolio_net_return_pct",
        "minimum_meaningful_improvement_pct": 0.10,
        "stopping_rule": {"validation_windows": 4, "then": "freeze"},
        "risk_boundaries": {
            "max_drawdown_pct": 12.0,
            "max_tail_loss_pct": 8.0,
            "max_turnover_ratio": 6.0,
            "max_theme_cluster_exposure_pct": 35.0,
        },
        "block_method": {
            "method": "moving_block",
            "block_length_days": 5,
            "repetitions": 1000,
        },
    })
    first = E.register(manifest, tmp_path)
    second = E.register(dict(manifest), tmp_path)
    assert first == second
    assert first.name == f"{E.require_valid(manifest)}.json"

    changed = dict(manifest)
    changed["model"] = {"name": "model-y", "temperature": 0}
    third = E.register(changed, tmp_path)
    assert third != first
    assert first.read_text(encoding="utf-8") != third.read_text(encoding="utf-8")


def test_dated_fund_flow_without_vintage_is_not_strict_pit(tmp_path):
    path = tmp_path / "market_snapshots.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE stock_fund_flow_daily ("
        "code TEXT, trade_date TEXT, net_amount REAL, net_amount_rate REAL)")
    conn.execute(
        "INSERT INTO stock_fund_flow_daily VALUES "
        "('600001','20260105',1.0,0.1)")
    conn.commit()
    conn.close()

    got = P.probe_all(tmp_path)["fund_flow"]
    assert got["status"] == "available"
    assert got["time_field"] == "trade_date"
    assert got["point_in_time_grade"] == "B"
    assert got["strict_replay_eligible"] is False
    assert "revision" in got["note"]


def test_capability_report_is_content_addressed_and_formal_gate_is_explicit(
        tmp_path):
    report = P.with_content_hash({
        "as_of": "2026-09-20",
        "data_dir": "/fixture",
        "capabilities": {
            "fund_flow": {
                "status": "available",
                "point_in_time_grade": "A",
                "strict_replay_eligible": True,
                "verification": {
                    "verified_by": "test-reviewer",
                    "verified_at": "2026-09-20T09:00:00+08:00",
                    "evidence": "provider revision semantics + archived vintages checked",
                },
            },
            "security_status": {
                "status": "available",
                "point_in_time_grade": "A",
                "strict_replay_eligible": True,
                "verification": {
                    "verified_by": "test-reviewer",
                    "verified_at": "2026-09-20T09:00:00+08:00",
                    "evidence": "dated ST/suspension archive checked",
                },
            },
        },
    })
    assert P.formal_errors(report) == []

    first = P.register(report, tmp_path)
    second = P.register(dict(report), tmp_path)
    assert first == second
    assert first.parent.name == "capabilities"
    assert first.name == f"{report['content_hash']}.json"

    tampered = dict(report)
    tampered["data_dir"] = "/changed"
    errors = P.formal_errors(tampered)
    assert "capability report content_hash mismatch" in errors


def test_fund_flow_grade_a_needs_named_verification_evidence():
    report = P.with_content_hash({
        "capabilities": {
            "fund_flow": {
                "status": "available",
                "point_in_time_grade": "A",
                "strict_replay_eligible": True,
                "verification": {
                    "verified_by": "reviewer",
                    "verified_at": "2026-09-20",
                },
            },
        },
    })
    assert (
        "fund_flow grade A requires verification.evidence"
        in P.formal_errors(report)
    )


def test_current_stock_status_columns_are_not_historical_pit(tmp_path):
    path = tmp_path / "stocks.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE stocks ("
        "code TEXT PRIMARY KEY, name TEXT, is_st INTEGER, "
        "is_suspended INTEGER)")
    conn.execute(
        "INSERT INTO stocks VALUES ('600001','甲',0,0)")
    conn.commit()
    conn.close()

    got = P.probe_all(tmp_path)["security_status"]
    assert got["status"] == "available"
    assert got["point_in_time_grade"] == "C"
    assert got["strict_replay_eligible"] is False
    assert "current snapshot" in got["note"]


def test_formal_gate_requires_historical_security_status():
    report = P.with_content_hash({
        "capabilities": {
            "fund_flow": {
                "status": "available",
                "point_in_time_grade": "A",
                "strict_replay_eligible": True,
                "verification": {
                    "verified_by": "reviewer",
                    "verified_at": "2026-09-20",
                    "evidence": "vintages",
                },
            },
        },
    })
    errors = P.formal_errors(report)
    assert "security_status capability is not available" in errors
