"""RP-05: minimal no-flow selection question has a closed contract."""

import pytest

from alpha_agents.evolution import selection_experiment as E


def _manifest():
    m = E.template(capabilities_hash="a" * 64)
    m.update({
        "baseline_identity": {
            "code_ref": "c" * 40,
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
            "run_theme": "WALK-TEST",
        },
        "model": {"name": "model-x", "temperature": 0},
        "cost_model": {"name": "virtual_a_share_v1"},
        "exit_policy": {"name": "shared-exit-v1"},
        "training_window": {
            "start": "2025-01-01", "end": "2025-12-31"},
        "validation_windows": [
            {"start": "2026-01-01", "end": "2026-02-11"},
            {"start": "2026-02-12", "end": "2026-03-25"},
            {"start": "2026-03-26", "end": "2026-05-06"},
            {"start": "2026-05-07", "end": "2026-06-17"},
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
            "repetitions": 1000,
            "cross_window_blocks": False,
        },
    })
    return m


def test_minimal_discovery_contract_changes_only_discovery():
    m = _manifest()
    assert E.validate(m) == []
    control = m["arms"]["CONTROL"]
    sector = m["arms"]["SECTOR"]

    for field in (
            "stock_selection", "planner", "fund_flow_visible",
            "news_visible", "tools_visible", "learning_input"):
        assert control[field] == sector[field]
    assert control["discovery"] != sector["discovery"]
    assert control["fund_flow_visible"] is False
    assert control["news_visible"] is False
    assert control["tools_visible"] is False
    assert control["learning_input"] == "frozen"


def test_metric_name_and_unit_are_not_free_text():
    m = _manifest()
    m["primary_metric"] = "daily_mean_return"
    m["primary_metric_unit"] = "percent_per_day"
    errors = E.validate(m)
    assert f"primary_metric must be {E.PRIMARY_METRIC}" in errors
    assert f"primary_metric_unit must be {E.PRIMARY_METRIC_UNIT}" in errors


def test_four_windows_are_overall_sample_not_four_separate_50_day_gates():
    m = _manifest()
    assert m["minimum_total_days"] == 120
    assert len(m["validation_windows"]) == 4
    assert E.validate(m) == []

    m["minimum_total_days"] = 50
    assert (
        "minimum_total_days must be an integer >= 120"
        in E.validate(m)
    )


def test_overlapping_windows_are_refused():
    m = _manifest()
    m["validation_windows"][1]["start"] = "2026-02-01"
    assert "validation windows must be non-overlapping" in E.validate(m)


def test_manifest_registration_is_content_addressed(tmp_path):
    m = _manifest()
    first = E.register(m, tmp_path)
    second = E.register(dict(m), tmp_path)
    assert first == second
    assert first.name == f"{E.require_valid(m)}.json"

    changed = dict(m)
    changed["minimum_meaningful_improvement_pp"] = 1.5
    third = E.register(changed, tmp_path)
    assert third != first


@pytest.mark.parametrize(
    "field,value,error",
    [
        ("family", "legacy_abcd", f"family must be {E.FAMILY}"),
        ("arms", {}, "arms must match"),
        ("stopping_rule", {"validation_windows": 5}, "stopping_rule"),
    ],
)
def test_closed_protocol_does_not_accept_semantic_relabeling(
        field, value, error):
    m = _manifest()
    m[field] = value
    assert any(error in item for item in E.validate(m))
