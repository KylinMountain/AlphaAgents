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
