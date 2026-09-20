"""RP-04: actual decision reads are content-addressed and fail closed."""

import json

import pytest

from alpha_agents.evolution import world_read_set as W


def _price(code="600001", *, captured_at="2026-01-29 15:00:00"):
    return {
        "code": code,
        "session": "2026-01-29",
        "captured_at": captured_at,
        "content_hash": f"price-{code}",
        "point_in_time_grade": "A",
        "strict_replay_eligible": True,
    }


def _base(**over):
    args = dict(
        cutoff="2026-01-30 09:00:00",
        ranking_session="2026-01-29",
        source_identity_hash="input-hash",
        code_ref="code-v1",
        policy_ref="policy-v1",
        membership={
            "snapshot_id": "m1",
            "content_hash": "membership-hash",
            "available_at": "2026-01-29 15:00:00",
        },
        security_status={
            "point_in_time_grade": "A",
            "strict_replay_eligible": True,
            "source": "historical-security-status-v1",
        },
        price_refs=[_price()],
        event_refs=[],
        fund_flow_refs=[],
    )
    args.update(over)
    return W.build(**args)


def test_strict_world_read_set_is_stable_and_content_addressed():
    first = _base()
    second = _base()

    assert first == second
    assert first["strict_replay_eligible"] is True
    assert W.require_valid(first, strict=True) == first["read_set_hash"]

    second["price_refs"][0]["content_hash"] = "rewritten"
    with pytest.raises(W.WorldReadSetError, match="hash mismatch"):
        W.require_valid(second, strict=True)


def test_future_revision_cannot_enter_an_earlier_world():
    read_set = _base(event_refs=[{
        "event_key": "earnings",
        "captured_at": "2026-01-30 10:00:00",
        "content_hash": "future",
        "point_in_time_grade": "A",
        "strict_replay_eligible": True,
    }])

    assert read_set["strict_replay_eligible"] is False
    with pytest.raises(W.WorldReadSetError, match="after cutoff"):
        W.require_valid(read_set, strict=True)


def test_current_only_security_status_explicitly_downgrades_strict_replay():
    read_set = _base(security_status={
        "point_in_time_grade": "C",
        "strict_replay_eligible": False,
        "source": "stocks.db current flags",
    })

    assert read_set["strict_replay_eligible"] is False
    with pytest.raises(W.WorldReadSetError, match="security_status"):
        W.require_valid(read_set, strict=True)


def test_unverified_fund_flow_cannot_be_promoted_by_a_label_only():
    read_set = _base(fund_flow_refs=[{
        "code": "600001",
        "session": "2026-01-29",
        "content_hash": "flow-day-value",
        "point_in_time_grade": "B",
        "strict_replay_eligible": False,
    }])

    with pytest.raises(W.WorldReadSetError, match="fund_flow_refs"):
        W.require_valid(read_set, strict=True)


def test_file_identity_hashes_bytes_not_file_names(tmp_path):
    price = tmp_path / "market_history.db"
    membership = tmp_path / "membership.json"
    price.write_bytes(b"v1")
    membership.write_text(json.dumps({"m": 1}), encoding="utf-8")

    first = W.file_identity({
        "market_history": price,
        "membership": membership,
    })
    same = W.file_identity({
        "membership": membership,
        "market_history": price,
    })
    assert first == same

    membership.write_text(json.dumps({"m": 2}), encoding="utf-8")
    changed = W.file_identity({
        "market_history": price,
        "membership": membership,
    })
    assert changed["input_hash"] != first["input_hash"]


def test_missing_source_is_part_of_input_identity(tmp_path):
    identity = W.file_identity({
        "present": tmp_path / "present.db",
        "missing": tmp_path / "missing.db",
    })
    assert identity["files"]["missing"] == {
        "present": False, "sha256": None}
