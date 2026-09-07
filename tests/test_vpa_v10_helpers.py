"""v10.2 / v10.3 review-fix regression tests (DeepSeek+GPT+Claude+Gemini).

Pins down the behavior of the safety helpers and validators added during
the multi-AI review round so refactors can't silently break:

- _as_bool: bool("false") == True bug class
- _as_float: malformed string (Chinese / lists) shouldn't drop entire reports
- _redact_secrets: API keys / Bearer tokens scrubbed before logging
- _validate_absorbed_test_signals: deterministic v10.2 hard-rule enforcement
- _replace_verdict_in_report: 校验器修正 footer renders when guard rewrites
- error returns: status="error" + analysis_valid=False (no fake "中性")
- prior_state injection: candidates + prior_state appear in user_content

All tests are pure unit tests — no LLM calls, no network.
"""
from __future__ import annotations

import json

from alpha_agents.tools.vpa.llm import (
    PROMPT_VERSION,
    _as_bool,
    _as_float,
    _as_confidence,
    _redact_secrets,
    _json_block,
    _text_block,
    _strip_verdict_comment,
    _truncate_middle,
    _build_vpa_user_content,
    _get_openai_client,
    _key_fingerprint,
    _select_provider,
    _validate_llm_verdict_schema,
    _validate_selected_candidate,
    _error_result,
    _CLIENT_CACHE,
)
from alpha_agents.tools.vpa.validator import _validate_absorbed_test_signals
from alpha_agents.tools.vpa.verdict import (
    _extract_raw_verdict,
    _extract_verdict,
    _replace_verdict_in_report,
)


# ─── _as_bool: GPT P0.5 bool("false") bug ─────────────────────────────

def test_as_bool_string_false_is_false():
    """Python's bool('false') == True — this caused a real bug in
    cross_family_blocked. _as_bool must coerce 'false' to False."""
    assert _as_bool("false") is False
    assert _as_bool("False") is False
    assert _as_bool("FALSE") is False
    assert _as_bool("0") is False
    assert _as_bool("no") is False
    assert _as_bool("") is False


def test_as_bool_string_true():
    assert _as_bool("true") is True
    assert _as_bool("1") is True
    assert _as_bool("yes") is True
    assert _as_bool("True") is True


def test_as_bool_native_types():
    assert _as_bool(True) is True
    assert _as_bool(False) is False
    assert _as_bool(None) is False
    assert _as_bool(None, default=True) is True


def test_as_bool_unknown_returns_default():
    """Garbage strings fall back to default, not crash."""
    assert _as_bool("maybe") is False
    assert _as_bool("maybe", default=True) is True
    assert _as_bool([1, 2, 3]) is False  # list → default


# ─── _as_float: malformed input shouldn't kill report parsing ─────────

def test_as_float_valid():
    assert _as_float("0.7") == 0.7
    assert _as_float(0.9) == 0.9
    assert _as_float("1") == 1.0


def test_as_float_chinese_text_returns_default():
    """Updated for reviewer #2 P0 fix: regex now extracts first numeric token.
    `'0.7, 高'` → 0.7 (was wrongly returning default per old impl)."""
    assert _as_float("0.7, 高", default=0.5) == 0.7    # regex extracts 0.7
    assert _as_float("中等", default=0.3) == 0.3        # no number → default
    assert _as_float("high") == 0.5                     # no number → default


def test_as_float_none():
    assert _as_float(None) == 0.5
    assert _as_float(None, default=0.0) == 0.0


def test_as_float_invalid_types():
    assert _as_float([0.7]) == 0.5  # list
    assert _as_float({"value": 0.7}) == 0.5  # dict


# ─── _redact_secrets: keys must not leak into logs ────────────────────

def test_redact_sk_key():
    s = "AuthError: Invalid api_key sk-abcdef1234567890abcdef in request"
    out = _redact_secrets(s)
    assert "sk-abcdef" not in out
    assert "[REDACTED]" in out


def test_redact_bearer_token():
    s = "401 Bearer: Bearer ABC123def456ghi789jkl012mno345pqr"
    out = _redact_secrets(s)
    assert "ABC123def456ghi789jkl012mno345pqr" not in out
    assert "[REDACTED]" in out


def test_redact_json_api_key_field():
    s = '{"error": "rate limited", "api_key": "secret-token-12345"}'
    out = _redact_secrets(s)
    assert "secret-token-12345" not in out


def test_redact_no_secrets_unchanged():
    s = "Plain error message with no secrets"
    assert _redact_secrets(s) == s


# ─── _json_block: prior_state / candidates injection format ───────────

def test_json_block_contains_title_and_data():
    """v11: data-packet section format is `## title` markdown header + fenced JSON."""
    out = _json_block("test_data", {"key": "value", "num": 42})
    assert "## test_data" in out
    assert "json" in out  # fenced code block
    assert '"key": "value"' in out
    assert '"num": 42' in out


def test_json_block_handles_chinese():
    out = _json_block("中文标题", {"phase": "拉升"})
    assert "## 中文标题" in out
    assert "拉升" in out


# ─── _get_openai_client: cache singleton (GPT P1.12) ──────────────────

def test_client_cache_returns_same_instance():
    """Same (base_url, key prefix) must return cached client."""
    _CLIENT_CACHE.clear()  # isolate test
    c1 = _get_openai_client("sk-test-key-1234567890", "https://example.com/v1")
    c2 = _get_openai_client("sk-test-key-1234567890", "https://example.com/v1")
    assert c1 is c2


def test_client_cache_different_urls():
    _CLIENT_CACHE.clear()
    c1 = _get_openai_client("sk-test", "https://api1.com/v1")
    c2 = _get_openai_client("sk-test", "https://api2.com/v1")
    assert c1 is not c2


# ─── _validate_absorbed_test_signals: deterministic v10.2 rule ────────

def test_absorbed_test_demoted_for_low_close_position():
    """absorbed_supply_test with close_position <= 0.6 must be demoted —
    GPT P1.9 / Claude: deterministic enforcement of v10.2 hard rule."""
    data = {"signals": [
        {"name": "absorbed_supply_test", "date": "2025-11-25", "confirmed": True},
    ]}
    phase_ctx = {"candidates": [
        {"date": "2025-11-25", "close_position": 0.41, "bar_type": "阳线"},
    ]}
    _validate_absorbed_test_signals(data, phase_ctx)
    sig = data["signals"][0]
    assert sig["confirmed"] is False
    assert sig.get("validator_demoted") is True
    assert "0.41" in sig.get("demotion_reason", "")
    assert any("0.41" in f for f in data.get("vph_conflict_failures", []))


def test_absorbed_test_demoted_for_non_up_day():
    """absorbed_supply_test must be 阳线 (up day proxy for close > prev_close)."""
    data = {"signals": [
        {"name": "Absorbed Supply Test", "date": "2025-11-20", "confirmed": True},
    ]}
    phase_ctx = {"candidates": [
        {"date": "2025-11-20", "close_position": 0.95, "bar_type": "阴线"},
    ]}
    _validate_absorbed_test_signals(data, phase_ctx)
    sig = data["signals"][0]
    assert sig["confirmed"] is False
    assert "阳线" in sig.get("demotion_reason", "")


def test_absorbed_test_passes_when_form_correct():
    """close_position > 0.6 + 阳线 should leave signal alone."""
    data = {"signals": [
        {"name": "absorbed_supply_test", "date": "2025-11-20", "confirmed": True},
    ]}
    phase_ctx = {"candidates": [
        {"date": "2025-11-20", "close_position": 1.0, "bar_type": "阳线"},
    ]}
    _validate_absorbed_test_signals(data, phase_ctx)
    sig = data["signals"][0]
    assert sig["confirmed"] is True
    assert sig.get("validator_demoted") is None


def test_absorbed_test_skips_pending_signals():
    """confirmed=False signals are candidates — leave them alone."""
    data = {"signals": [
        {"name": "absorbed_supply_test", "date": "2025-11-25", "confirmed": False},
    ]}
    phase_ctx = {"candidates": [
        {"date": "2025-11-25", "close_position": 0.41, "bar_type": "阴线"},  # would fail
    ]}
    _validate_absorbed_test_signals(data, phase_ctx)
    assert data["signals"][0].get("validator_demoted") is None  # untouched


def test_absorbed_test_other_signals_untouched():
    """PSY / BC / SOS signals are unaffected by this validator."""
    data = {"signals": [
        {"name": "PSY", "date": "2025-11-25", "confirmed": True},
        {"name": "SOS强势确认", "date": "2025-11-26", "confirmed": True},
    ]}
    phase_ctx = {"candidates": [
        {"date": "2025-11-25", "close_position": 0.1, "bar_type": "阴线"},
        {"date": "2025-11-26", "close_position": 0.1, "bar_type": "阴线"},
    ]}
    _validate_absorbed_test_signals(data, phase_ctx)
    for sig in data["signals"]:
        assert sig.get("validator_demoted") is None


def test_absorbed_test_skips_when_not_in_pool():
    """Signal date not in candidate pool → leave alone (could be valid
    markup-internal absorbed test outside curated climax pool)."""
    data = {"signals": [
        {"name": "absorbed_supply_test", "date": "2025-11-25", "confirmed": True},
    ]}
    phase_ctx = {"candidates": []}  # empty pool
    _validate_absorbed_test_signals(data, phase_ctx)
    assert data["signals"][0]["confirmed"] is True


def test_absorbed_test_chinese_signal_name():
    """Validator should match Chinese 吸收 / 吸纳 names too."""
    data = {"signals": [
        {"name": "供应吸收测试", "date": "2025-11-25", "confirmed": True},
    ]}
    phase_ctx = {"candidates": [
        {"date": "2025-11-25", "close_position": 0.3, "bar_type": "阴线"},
    ]}
    _validate_absorbed_test_signals(data, phase_ctx)
    assert data["signals"][0]["confirmed"] is False


# ─── _replace_verdict_in_report: narrative footer (Claude / GPT P1.15) ─

def test_replace_verdict_footer_when_phase_changed():
    """Guard rewriting phase must surface a 校验器修正 footer."""
    report = "## 当前判断\n派发初期\n\n<!-- VERDICT: {\"old\": true} -->"
    verdict_data = {
        "phase": "拉升",
        "phase_state_changed": True,
        "phase_guard_reason": "demoted: missing AR confirmation",
    }
    out = _replace_verdict_in_report(report, verdict_data)
    assert "【校验器修正】" in out
    assert "demoted: missing AR confirmation" in out


def test_replace_verdict_no_footer_when_clean():
    """No phase rewrite, no failures → no footer added."""
    report = "## clean\n<!-- VERDICT: {} -->"
    verdict_data = {
        "phase": "拉升",
        "phase_state_changed": False,
        "phase_guard_reason": "",
    }
    out = _replace_verdict_in_report(report, verdict_data)
    assert "【校验器修正】" not in out


def test_replace_verdict_footer_with_validator_failures():
    """Validator failures (vph_conflict_failures) surface in footer too."""
    report = "<!-- VERDICT: {} -->"
    verdict_data = {
        "phase": "拉升",
        "cross_family_blocked": True,
        "phase_guard_reason": "cross-family rejected",
        "vph_conflict_failures": [
            "absorbed_test @ 11-25: close_pos=0.41 violates rule",
            "BC candidate post_bar_reverse_3d is null",
        ],
    }
    out = _replace_verdict_in_report(report, verdict_data)
    assert "【校验器修正】" in out
    assert "absorbed_test" in out
    assert "post_bar_reverse_3d" in out


def test_replace_verdict_truncates_many_failures():
    """If >2 failures, footer shows only first 2 + count summary."""
    report = "<!-- VERDICT: {} -->"
    verdict_data = {
        "phase": "拉升",
        "phase_state_changed": True,
        "phase_guard_reason": "many failures",
        "vph_conflict_failures": [f"fail #{i}" for i in range(5)],
    }
    out = _replace_verdict_in_report(report, verdict_data)
    # Extract just the human-readable footer (between 校验器修正 and VERDICT block)
    footer = out.split("【校验器修正】")[1].split("<!-- VERDICT:")[0]
    assert "fail #0" in footer
    assert "fail #1" in footer
    assert "另有 3 条" in footer
    # fail #4 must not appear in the human-readable footer (JSON has it, that's fine)
    assert "fail #4" not in footer


# ─── _as_bool numeric support (GPT P1.3 followup) ─────────────────────

def test_as_bool_int_1_is_true():
    """Some models emit 1 / 0 instead of true / false — must coerce."""
    assert _as_bool(1) is True
    assert _as_bool(0) is False
    assert _as_bool(2) is True  # any nonzero
    assert _as_bool(-1) is True


def test_as_bool_float_support():
    assert _as_bool(0.0) is False
    assert _as_bool(0.5) is True
    assert _as_bool(1.0) is True


def test_as_bool_chinese_yes_no():
    assert _as_bool("是") is True
    assert _as_bool("否") is False


# ─── _as_confidence: clamp + percent conversion (GPT P1.4) ───────────

def test_as_confidence_normal_ratio():
    assert _as_confidence(0.7) == 0.7
    assert _as_confidence("0.5") == 0.5
    assert _as_confidence(0.0) == 0.0
    assert _as_confidence(1.0) == 1.0


def test_as_confidence_percent_conversion():
    """Values in [2, 100] interpreted as percent."""
    assert _as_confidence(70) == 0.7
    assert _as_confidence("85") == 0.85
    assert _as_confidence(50) == 0.5
    assert _as_confidence(100) == 1.0


def test_as_confidence_edge_just_over_one():
    """Values in (1, 2) → clamp to 1.0, NOT divide by 100 (was a bug)."""
    assert _as_confidence(1.5) == 1.0
    assert _as_confidence(1.1) == 1.0


def test_as_confidence_clamp_range():
    """Out-of-range values clamp to [0, 1]."""
    assert _as_confidence(-0.5) == 0.0
    assert _as_confidence(150) == 1.0  # 150/100 = 1.5 → clamp
    assert _as_confidence(0.0) == 0.0


def test_as_confidence_invalid():
    assert _as_confidence("high", default=0.3) == 0.3
    assert _as_confidence(None, default=0.4) == 0.4


# ─── Reviewer #2 P0 followups: regex-based numeric extraction ─────────

def test_as_float_percent_string():
    """`70%` should extract 70.0 (caller decides if /100); was returning default 0.5."""
    assert _as_float("70%") == 70.0


def test_as_float_text_suffix():
    """`0.7, high` should extract 0.7; was returning default."""
    assert _as_float("0.7, high") == 0.7


def test_as_float_chinese_with_number():
    assert _as_float("置信度 0.85") == 0.85


def test_as_confidence_percent_string():
    """`70%` should become 0.7 (percent suffix triggers /100)."""
    assert _as_confidence("70%") == 0.7


def test_as_confidence_text_suffix():
    """`0.7, high` extracts 0.7, no /100 since no % suffix."""
    assert _as_confidence("0.7, high") == 0.7


def test_as_confidence_85_percent_string():
    assert _as_confidence("85%") == 0.85


def test_as_float_bool_returns_default():
    """bool subclass of int but shouldn't be treated as numeric here."""
    assert _as_float(True, default=0.5) == 0.5
    assert _as_float(False, default=0.5) == 0.5


# ─── _select_provider (GPT P0.1: no UnboundLocalError) ───────────────

def test_select_provider_invalid_env_falls_through(monkeypatch):
    """Invalid VPA_LLM_PROVIDER must not crash — must fall through to chain."""
    monkeypatch.setenv("VPA_LLM_PROVIDER", "deepssek")  # typo
    monkeypatch.setattr("alpha_agents.config.AGENT_API_KEY", "")
    monkeypatch.setattr("alpha_agents.config.DEEPSEEK_API_KEY", "")
    monkeypatch.setattr("alpha_agents.config.DIGEST_API_KEY", "")
    api_key, base_url, model, provider = _select_provider()
    assert api_key is None
    assert provider is None  # fell through, no key configured


def test_select_provider_valid_env_uses_it(monkeypatch):
    monkeypatch.setenv("VPA_LLM_PROVIDER", "deepseek")
    monkeypatch.setattr("alpha_agents.config.DEEPSEEK_API_KEY", "sk-deepseek-key")
    monkeypatch.setattr("alpha_agents.config.DEEPSEEK_BASE_URL", "https://deepseek.com/v1")
    monkeypatch.setattr("alpha_agents.config.DEEPSEEK_MODEL", "deepseek-v4-flash")
    monkeypatch.setattr("alpha_agents.config.AGENT_API_KEY", "sk-agent-key")  # would normally win
    monkeypatch.setattr("alpha_agents.config.AGENT_BASE_URL", "https://agent.com/v1")
    monkeypatch.setattr("alpha_agents.config.AGENT_MODEL", "qwen-plus")
    api_key, base_url, model, provider = _select_provider()
    assert provider == "deepseek"
    assert api_key == "sk-deepseek-key"


def test_select_provider_no_keys_returns_none(monkeypatch):
    monkeypatch.delenv("VPA_LLM_PROVIDER", raising=False)
    monkeypatch.setattr("alpha_agents.config.AGENT_API_KEY", "")
    monkeypatch.setattr("alpha_agents.config.DEEPSEEK_API_KEY", "")
    monkeypatch.setattr("alpha_agents.config.DIGEST_API_KEY", "")
    api_key, base_url, model, provider = _select_provider()
    assert api_key is None
    assert provider is None


# ─── _validate_llm_verdict_schema (GPT P0.2) ─────────────────────────

def test_schema_validation_missing_verdict():
    assert _validate_llm_verdict_schema({}) == ["missing_or_invalid_verdict"]
    assert _validate_llm_verdict_schema(None) == ["missing_or_invalid_verdict"]
    assert _validate_llm_verdict_schema("not a dict") == ["missing_or_invalid_verdict"]


def test_schema_validation_complete():
    """All required fields present + types correct → empty error list."""
    valid = {
        "direction": "看多", "phase": "拉升",
        "phase_change": {"confirmed": False},  # phase_change.confirmed required
        "reason": "",
        "selected_climax": {}, "signals": [], "scenarios": [],
    }
    assert _validate_llm_verdict_schema(valid) == []


def test_schema_validation_partial():
    """Missing fields return their names."""
    errs = _validate_llm_verdict_schema({"direction": "看多", "phase": "拉升"})
    assert "phase_change" in errs
    assert "selected_climax" in errs
    assert "signals" in errs


def test_schema_validation_rejects_invalid_direction():
    """v11: direction enum check — 强烈看多 / 大幅看空 etc. are not allowed."""
    valid_skeleton = {
        "direction": "强烈看多",  # not in {看多, 偏多, 中性, 偏空, 看空}
        "phase": "拉升", "phase_change": {}, "reason": "",
        "selected_climax": {}, "signals": [], "scenarios": [],
    }
    errs = _validate_llm_verdict_schema(valid_skeleton)
    assert any("invalid_direction" in e for e in errs), errs


def test_schema_validation_accepts_canonical_directions():
    for d in ("看多", "偏多", "中性", "偏空", "看空"):
        skel = {
            "direction": d, "phase": "拉升", "phase_change": {"confirmed": False}, "reason": "",
            "selected_climax": {}, "signals": [], "scenarios": [],
        }
        assert _validate_llm_verdict_schema(skel) == [], f"failed on {d!r}"


# ─── Reviewer #2 P0: deeper schema validation ──────────────────────

def _v_skel(**override) -> dict:
    base = {
        "direction": "偏多", "phase": "拉升",
        "phase_change": {"confirmed": False}, "reason": "x",
        "selected_climax": {}, "signals": [], "scenarios": [],
    }
    base.update(override)
    return base


def test_schema_rejects_invalid_phase():
    """Phase must start with one of 5 canonical roots."""
    errs = _validate_llm_verdict_schema(_v_skel(phase="强势吸筹拉升突破"))
    assert any("invalid_phase" in e for e in errs)


def test_schema_accepts_phase_with_substage():
    """`派发初期`, `下跌末期` etc. valid (start with root)."""
    for p in ("吸筹初期", "派发尾声", "下跌中期", "拉升加速", "震荡"):
        errs = _validate_llm_verdict_schema(_v_skel(phase=p))
        assert errs == [], f"phase {p!r} rejected: {errs}"


def test_schema_rejects_signals_string():
    errs = _validate_llm_verdict_schema(_v_skel(signals="none"))
    assert "invalid_signals_type" in errs


def test_schema_rejects_scenarios_dict():
    errs = _validate_llm_verdict_schema(_v_skel(scenarios={}))
    assert "invalid_scenarios_type" in errs


def test_schema_rejects_phase_change_string():
    errs = _validate_llm_verdict_schema(_v_skel(phase_change="confirmed"))
    assert "invalid_phase_change_type" in errs


def test_schema_rejects_missing_phase_change_confirmed():
    errs = _validate_llm_verdict_schema(_v_skel(phase_change={"to": "派发"}))
    assert "missing_phase_change.confirmed" in errs


def test_schema_rejects_phase_change_confirmed_string_false():
    """Raw schema must reject string booleans before normalization."""
    errs = _validate_llm_verdict_schema(_v_skel(phase_change={"confirmed": "false"}))
    assert "invalid_phase_change.confirmed" in errs


def test_schema_rejects_signal_confirmed_non_bool():
    skel = _v_skel(signals=[{"name": "BC", "confirmed": "true"}])
    errs = _validate_llm_verdict_schema(skel)
    assert any("confirmed" in e and "[0]" in e for e in errs)


def test_schema_rejects_confirmed_true_without_by():
    skel = _v_skel(signals=[{"name": "BC", "confirmed": True}])
    errs = _validate_llm_verdict_schema(skel)
    assert any("by" in e for e in errs)


def test_schema_rejects_confirmed_false_without_need_deny():
    skel = _v_skel(signals=[{"name": "BC", "confirmed": False, "need": "x"}])
    errs = _validate_llm_verdict_schema(skel)
    assert any("need_or_deny" in e for e in errs)


# ─── Reviewer #2 P0: candidate_id type normalization ────────────────

def test_candidate_id_int_string_match():
    """LLM emits string '1', candidate dict has int 1 — should match."""
    v = {"selected_climax": {"candidate_id": "1", "climax_type": "BC"}}
    _validate_selected_candidate(v, [{"id": 1}])
    # Not reset (matched after str conversion)
    assert v["selected_climax"]["candidate_id"] == "1"
    assert "schema_warnings" not in v


def test_candidate_id_string_int_match_reverse():
    """Inverse: LLM int 5, candidate string '5'."""
    v = {"selected_climax": {"candidate_id": 5, "climax_type": "BC"}}
    _validate_selected_candidate(v, [{"id": "5"}])
    assert v["selected_climax"]["candidate_id"] == 5


def test_schema_warnings_safe_when_string():
    """Model may emit `schema_warnings: 'something'`; append must not crash."""
    v = {"selected_climax": {"candidate_id": "fake", "climax_type": "BC"},
         "schema_warnings": "stale_string"}
    _validate_selected_candidate(v, [{"id": "real"}])
    # Should be coerced to list with both old + new
    assert isinstance(v["schema_warnings"], list)
    assert "invalid_candidate_id_reset" in v["schema_warnings"]


# ─── _validate_selected_candidate (GPT P1.7) ─────────────────────────

def test_invalid_candidate_id_reset():
    """LLM hallucinated id not in pool → reset to null."""
    v = {"selected_climax": {"candidate_id": "cand-fake", "climax_type": "BC", "rationale": "x"}}
    pool = [{"id": "cand-2025-11-25", "date": "2025-11-25"}]
    _validate_selected_candidate(v, pool)
    assert v["selected_climax"]["candidate_id"] is None
    assert v["selected_climax"]["climax_type"] is None
    assert "invalid_candidate_id_reset" in v.get("schema_warnings", [])


def test_valid_candidate_id_preserved():
    v = {"selected_climax": {"candidate_id": "cand-2025-11-25", "climax_type": "BC", "rationale": "ok"}}
    pool = [{"id": "cand-2025-11-25", "date": "2025-11-25"}]
    _validate_selected_candidate(v, pool)
    assert v["selected_climax"]["candidate_id"] == "cand-2025-11-25"
    assert "schema_warnings" not in v or "invalid_candidate_id_reset" not in v.get("schema_warnings", [])


def test_null_candidate_id_normalized():
    """String 'null' → real None."""
    v = {"selected_climax": {"candidate_id": "null", "climax_type": None, "rationale": ""}}
    _validate_selected_candidate(v, [])
    assert v["selected_climax"]["candidate_id"] is None


# ─── _error_result envelope (GPT P1.6) ───────────────────────────────

def test_error_result_has_all_fields():
    """Every downstream-accessed field must be present in error envelope."""
    r = _error_result("test_reason", "TestError", model="m1", provider="p1")
    required = [
        "status", "analysis_valid", "prompt_version", "model", "provider",
        "verdict", "direction", "confidence", "phase", "warning_phase",
        "phase_change", "signals", "selected_climax", "report",
        "scenarios", "target_low", "target_high", "schema_errors",
    ]
    for k in required:
        assert k in r, f"missing field: {k}"
    assert r["status"] == "error"
    assert r["analysis_valid"] is False
    assert r["verdict"] is None
    assert r["direction"] is None


def test_error_result_with_schema_errors():
    r = _error_result("parse_failed", "VerdictParseError", schema_errors=["missing_phase"])
    assert r["schema_errors"] == ["missing_phase"]


# ─── _key_fingerprint (GPT P2.2) ─────────────────────────────────────

def test_fingerprint_stable():
    """Same key → same fingerprint."""
    assert _key_fingerprint("sk-test-1234") == _key_fingerprint("sk-test-1234")


def test_fingerprint_differs_for_different_keys():
    """Two keys with same first 8 chars must hash differently."""
    f1 = _key_fingerprint("sk-test-AAAAAAAA-rest1")
    f2 = _key_fingerprint("sk-test-AAAAAAAA-rest2")
    assert f1 != f2


# ─── v11 user-message data packet ────────────────────────────────────

def test_strip_verdict_comment_removes_html_block():
    text = "narrative\n<!-- VERDICT: {\"direction\": \"看多\"} -->\nmore"
    out = _strip_verdict_comment(text)
    assert "<!-- VERDICT" not in out
    assert "narrative" in out
    assert "more" in out


def test_strip_verdict_comment_handles_no_verdict():
    assert _strip_verdict_comment("plain text") == "plain text"
    assert _strip_verdict_comment("") == ""


def test_truncate_middle_short_unchanged():
    text = "short text"
    assert _truncate_middle(text, max_chars=2500) == text


def test_truncate_middle_keeps_head_and_tail():
    text = "A" * 3000
    out = _truncate_middle(text, max_chars=1000)
    assert out.startswith("AAA")
    assert out.endswith("AAA")
    assert "[truncated previous analysis]" in out
    assert len(out) < len(text)


def test_text_block_format():
    out = _text_block("section_x", "hello world")
    assert "## section_x" in out
    assert "hello world" in out
    assert "```text" in out


def test_text_block_empty_shows_none_marker():
    """Empty content renders as `(none)` so absence is explicit."""
    out = _text_block("section_x", "")
    assert "(none)" in out
    out2 = _text_block("section_x", None)
    assert "(none)" in out2


def test_build_vpa_user_content_safety_at_top():
    """Safety preamble must be the very first thing, before any dynamic block."""
    out = _build_vpa_user_content(
        code="X", vpa_text="data",
        prior_state={"phase": "拉升"},  # dynamic content
        signal_history_text="malicious instruction",
    )
    safety_idx = out.index("【VPA DATA PACKET】")
    prior_idx = out.index("prior_state_canonical")
    history_idx = out.index("signal_history")
    assert safety_idx < prior_idx
    assert safety_idx < history_idx


def test_build_vpa_user_content_section_order():
    """Sections appear in the documented canonical order."""
    out = _build_vpa_user_content(
        code="X", vpa_text="vpa_data",
        prior_state={"phase": "拉升"},
        previous_analysis="old report",
        signal_history_text="signal_data",
        phase_context={"vp_harmony_score": "bullish"},
        candidates=[{"id": "cand-X"}],
    )
    expected_order = [
        "analysis_meta",
        "prior_state_canonical",
        "signal_history",
        "previous_verdict_extracted",
        "previous_analysis_excerpt",
        "phase_context",
        "current_vpa_text",
        "candidate_bars_canonical",
    ]
    indices = [out.index(s) for s in expected_order]
    assert indices == sorted(indices), f"sections out of order: {expected_order} → {indices}"


def test_build_vpa_user_content_strips_verdict_from_excerpt():
    """previous_analysis VERDICT comment must not leak into excerpt block."""
    prev = "narrative content\n<!-- VERDICT: {\"direction\": \"看多\"} -->\nmore"
    out = _build_vpa_user_content(
        code="X", vpa_text="data",
        previous_analysis=prev,
    )
    excerpt_start = out.index("## previous_analysis_excerpt")
    excerpt_end = out.index("## phase_context")
    excerpt_section = out[excerpt_start:excerpt_end]
    assert "<!-- VERDICT" not in excerpt_section, "VERDICT comment leaked"
    # But the structured previous_verdict_extracted DID receive it.
    extracted_start = out.index("## previous_verdict_extracted")
    extracted_section = out[extracted_start:excerpt_start]
    assert "看多" in extracted_section


def test_build_vpa_user_content_phase_context_drops_candidates():
    """candidate_bars_canonical is the source of truth for candidates;
    phase_context must not duplicate them."""
    out = _build_vpa_user_content(
        code="X", vpa_text="data",
        phase_context={"vp_harmony_score": "bullish", "candidates": ["leaked"]},
        candidates=[{"id": "cand-real"}],
    )
    pc_start = out.index("## phase_context")
    pc_end = out.index("## current_vpa_text")
    pc_section = out[pc_start:pc_end]
    assert "leaked" not in pc_section
    assert "vp_harmony_score" in pc_section


def test_build_vpa_user_content_includes_meta():
    """analysis_meta surfaces code + as_of explicitly."""
    out = _build_vpa_user_content(
        code="603358", vpa_text="data", as_of="2025-11-07",
    )
    meta_start = out.index("## analysis_meta")
    meta_end = out.index("## prior_state_canonical")
    meta_section = out[meta_start:meta_end]
    assert "603358" in meta_section
    assert "2025-11-07" in meta_section


def test_build_vpa_user_content_empty_optional_blocks():
    """No prior/previous/signals → still produces full structured packet
    with (none) placeholders, not collapsed/missing sections."""
    out = _build_vpa_user_content(code="X", vpa_text="data")
    for sec in ["prior_state_canonical", "signal_history",
                "previous_verdict_extracted", "previous_analysis_excerpt",
                "phase_context", "candidate_bars_canonical"]:
        assert f"## {sec}" in out, f"section {sec} missing"


def test_extract_raw_verdict_does_not_add_missing_fields():
    """Production schema validation must see raw missing fields, not defaults."""
    report = '<!-- VERDICT: {"direction":"看多","phase":"拉升","phase_change":{"confirmed":false},"selected_climax":{}} -->'
    raw = _extract_raw_verdict(report)
    assert "reason" not in raw
    assert "signals" not in raw
    assert "scenarios" not in raw
    errs = _validate_llm_verdict_schema(raw)
    assert "reason" in errs
    assert "signals" in errs
    assert "scenarios" in errs


def test_extract_verdict_string_false_stays_false():
    """Back-compat parser may normalize, but string false must never become True."""
    report = (
        '<!-- VERDICT: {"direction":"偏空","phase":"下跌",'
        '"phase_change":{"from":"派发","to":"下跌","confirmed":"false"},'
        '"reason":"x","selected_climax":{},'
        '"signals":[{"name":"SOW","confirmed":"false","need":"跌破","deny":"收复"}],'
        '"scenarios":[]} -->'
    )
    verdict = _extract_verdict(report)
    assert verdict["phase_change"]["confirmed"] is False
    assert verdict["signals"][0]["confirmed"] is False
    assert verdict["confirmation_level"] == 1


def test_call_llm_vpa_rejects_missing_fields_before_defaults(monkeypatch):
    """_call_llm_vpa must schema-check raw JSON before setdefault normalization."""
    import alpha_agents.tools.vpa.llm as llm_mod

    class _Message:
        content = '<!-- VERDICT: {"direction":"看多","phase":"拉升","phase_change":{"confirmed":false},"selected_climax":{}} -->'

    class _Choice:
        message = _Message()

    class _Resp:
        choices = [_Choice()]
        usage = None

    monkeypatch.setattr(llm_mod, "_select_provider", lambda: ("sk-test", "https://example.com/v1", "test-model", "env"))
    monkeypatch.setattr(llm_mod, "_get_openai_client", lambda *args, **kwargs: object())
    monkeypatch.setattr(llm_mod, "_call_with_retry", lambda *args, **kwargs: _Resp())

    out = llm_mod._call_llm_vpa("000001", "data")
    assert out["status"] == "error"
    assert out["analysis_valid"] is False
    assert out["error_type"] == "VerdictParseError"
    assert "reason" in out["schema_errors"]
    assert "signals" in out["schema_errors"]
    assert "scenarios" in out["schema_errors"]


def test_call_llm_vpa_default_temperature_zero(monkeypatch):
    """When VPA_LLM_TEMPERATURE is unset, _call_llm_vpa must send temp=0."""
    import alpha_agents.tools.vpa.llm as llm_mod

    captured_kwargs = {}

    class _Message:
        content = (
            '<!-- VERDICT: {"direction":"看多","phase":"拉升初期",'
            '"phase_change":{"from":"吸筹","to":"拉升初期","confirmed":false,'
            '"invalidated_by":"","denial_level":""},"reason":"测试",'
            '"selected_climax":{"candidate_id":null,"climax_type":null,"rationale":"无"},'
            '"signals":[],"scenarios":[]} -->'
        )

    class _Choice:
        message = _Message()

    class _Resp:
        choices = [_Choice()]
        usage = None

    def _fake_call_with_retry(_client, max_retries=3, **kwargs):
        captured_kwargs.update(kwargs)
        return _Resp()

    monkeypatch.delenv("VPA_LLM_TEMPERATURE", raising=False)
    monkeypatch.delenv("VPA_LLM_EXTRA_BODY", raising=False)
    monkeypatch.setattr(llm_mod, "_select_provider", lambda: ("sk-test", "https://example.com/v1", "test-model", "env"))
    monkeypatch.setattr(llm_mod, "_get_openai_client", lambda *args, **kwargs: object())
    monkeypatch.setattr(llm_mod, "_call_with_retry", _fake_call_with_retry)

    out = llm_mod._call_llm_vpa("000001", "data")
    assert out["status"] == "ok"
    assert captured_kwargs.get("temperature") == 0.0


def test_call_llm_vpa_handles_missing_choices_response(monkeypatch):
    """Provider may return a malformed object without choices."""
    import alpha_agents.tools.vpa.llm as llm_mod

    class _Resp:
        choices = None
        usage = None

    monkeypatch.setattr(llm_mod, "_select_provider", lambda: ("sk-test", "https://example.com/v1", "test-model", "env"))
    monkeypatch.setattr(llm_mod, "_get_openai_client", lambda *args, **kwargs: object())
    monkeypatch.setattr(llm_mod, "_call_with_retry", lambda *args, **kwargs: _Resp())

    out = llm_mod._call_llm_vpa("000001", "data")
    assert out["status"] == "error"
    assert out["analysis_valid"] is False
    assert out["error_type"] == "InvalidResponseShape"
    assert "invalid_response_shape:missing_choices" == out["reason"]


# ─── PROMPT_VERSION sanity ────────────────────────────────────────────

def test_prompt_version_format():
    """PROMPT_VERSION must be `anna-vpa-vX` or `anna-vpa-vX.Y` for backtest
    cache attribution."""
    assert PROMPT_VERSION.startswith("anna-vpa-v")
    parts = PROMPT_VERSION.split("v")
    assert len(parts) >= 3
    version = parts[-1]
    if "." in version:
        major, minor = version.split(".")
        assert major.isdigit() and minor.isdigit()
    else:
        assert version.isdigit()
