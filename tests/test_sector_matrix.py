"""Formal four-arm Sector-First runner stays isolated and preregistered."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import sector_first as SF  # noqa: E402
import walk_forward as WF  # noqa: E402

from alpha_agents.evolution import sector_experiment as E  # noqa: E402


def _manifest():
    manifest = E.template(capabilities_hash="a" * 64)
    args = WF.build_parser().parse_args([
        "--start", "2026-01-01",
        "--decider", "llm",
    ])
    runtime = WF._experiment_runtime_contract(args)
    manifest.update({
        "baseline_identity": {
            "code_ref": "72ce91a96e8ab0ee499dae0d6e71deef36e67ddd",
            "policy_ref": "policy-v1",
            "input_hash": "i" * 64,
        },
        **runtime,
        "training_window": {
            "start": "2025-01-01", "end": "2025-12-31"},
        "validation_windows": [
            {"start": "2026-01-01", "end": "2026-01-30"},
            {"start": "2026-02-02", "end": "2026-03-13"},
            {"start": "2026-03-16", "end": "2026-04-24"},
            {"start": "2026-04-27", "end": "2026-06-05"},
        ],
        "forward_start": "2026-06-08",
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
    assert E.validate(manifest) == []
    return manifest


def test_run_matrix_requires_registered_content_addressed_manifest(tmp_path):
    manifest = _manifest()
    working = tmp_path / "experiment_manifest.json"
    working.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(E.SectorExperimentError, match="content-addressed"):
        SF._registered_manifest(working)

    registered = E.register(manifest, tmp_path)
    loaded, digest = SF._registered_manifest(registered)
    assert loaded == manifest
    assert registered.name == f"{digest}.json"


def test_window_session_count_requires_both_registered_endpoints(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    conn = sqlite3.connect(corpus / "market_history.db")
    conn.execute(
        "CREATE TABLE daily_kline "
        "(code TEXT, date TEXT, close REAL, PRIMARY KEY(code, date))")
    conn.executemany(
        "INSERT INTO daily_kline(code,date,close) VALUES ('600001',?,10)",
        [("2026-01-01",), ("2026-01-02",), ("2026-01-05",)],
    )
    conn.commit()
    conn.close()

    assert SF._window_session_count(
        corpus, "2026-01-01", "2026-01-05") == 3
    with pytest.raises(E.SectorExperimentError, match="not fully present"):
        SF._window_session_count(
            corpus, "2025-12-31", "2026-01-05")


def test_c_walk_command_is_bound_to_b_frozen_directions(tmp_path):
    manifest = _manifest()
    frozen = tmp_path / "frozen.json"
    cmd = SF._walk_command(
        manifest=manifest,
        manifest_path=tmp_path / "manifests" / "m.json",
        membership_path=tmp_path / "membership.json",
        target=tmp_path / "state" / "C",
        out=tmp_path / "arms" / "C",
        start="2026-01-01",
        days=20,
        arm="C",
        run_id="matrix-C",
        frozen_directions=frozen,
    )
    assert cmd[cmd.index("--experiment-arm") + 1] == "C"
    assert (
        cmd[cmd.index("--selection-architecture") + 1]
        == "sector_first_simple_selector"
    )
    assert cmd[cmd.index("--frozen-directions") + 1] == str(frozen)


def test_non_c_arm_refuses_frozen_directions(tmp_path):
    with pytest.raises(E.SectorExperimentError, match="only C"):
        SF._walk_command(
            manifest=_manifest(),
            manifest_path=tmp_path / "manifest.json",
            membership_path=tmp_path / "membership.json",
            target=tmp_path / "state" / "B",
            out=tmp_path / "arms" / "B",
            start="2026-01-01",
            days=20,
            arm="B",
            run_id="matrix-B",
            frozen_directions=tmp_path / "wrong.json",
        )


def test_matrix_command_carries_all_frozen_decision_knobs(tmp_path):
    manifest = _manifest()
    cmd = SF._walk_command(
        manifest=manifest,
        manifest_path=tmp_path / "manifest.json",
        membership_path=tmp_path / "membership.json",
        target=tmp_path / "state" / "A",
        out=tmp_path / "arms" / "A",
        start="2026-01-01",
        days=20,
        arm="A",
        run_id="matrix-A",
    )
    expected = manifest["decision_config"]
    assert cmd[cmd.index("--trader") + 1] == expected["trader"]
    assert int(cmd[cmd.index("--picks") + 1]) == expected["picks_per_day"]
    assert int(cmd[cmd.index("--panel-size") + 1]) == expected["panel_size"]
    assert float(cmd[cmd.index("--participation") + 1]) == expected["participation"]
    assert float(cmd[cmd.index("--model-timeout") + 1]) == expected["model_timeout_seconds"]
