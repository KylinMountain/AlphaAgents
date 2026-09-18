"""A prior must not wear the clothes of a measured fact.

The repository has exactly one door through which a claim about markets may
become behaviour: ``candidate → evidence → holdout → human approval →
policy version``. A sentence typed into a prompt or a tool docstring never
went through it, yet the agent reads it as a fact about the world.

These tests pin the checker that finds those sentences, and — more
importantly — pin the **exemption** that makes the rule livable: a
policy-shaped claim is allowed the moment it declares where it came from.
The point is not to forbid priors. A trader may hold them. The point is
that an unlabelled prior is a law nobody voted on, and a labelled one is a
hypothesis that can be retired by evidence.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import lint_policy as LP  # noqa: E402


class TestTheCheckerFindsTheThingItClaims:
    def _scan(self, monkeypatch, tmp_path, name, text):
        """Scan a temp file as if it lived in the repo.

        ``scan_file`` reports repo-relative paths, so a bare ``tmp_path``
        file raises before the pattern is even tried. Rebasing REPO at the
        temp dir keeps the test about the patterns.
        """
        monkeypatch.setattr(LP, "REPO", tmp_path)
        f = tmp_path / name
        f.write_text(text, encoding="utf-8")
        return LP.scan_file(f)

    def test_a_verdict_bolted_to_a_threshold_is_found(self, tmp_path,
                                                      monkeypatch):
        found = self._scan(monkeypatch, tmp_path, "tool.py",
                           '"""ROE > 15% = 优质企业"""\n')
        assert found and "verdict" in found[0].rule

    def test_an_action_recommendation_is_found(self, tmp_path, monkeypatch):
        found = self._scan(monkeypatch, tmp_path, "tool.py",
                           'return {"recommendation": "可介入"}\n')
        assert found

    def test_a_causal_theory_is_found(self, tmp_path, monkeypatch):
        found = self._scan(monkeypatch, tmp_path, "prompt.md",
                           "价格是结果，资金是原因。\n")
        assert found


class TestADeclaredSourceClearsIt:
    """The escape hatch, and the reason this rule can be enforced at all."""

    @staticmethod
    def _scan(monkeypatch, tmp_path, text):
        monkeypatch.setattr(LP, "REPO", tmp_path)
        f = tmp_path / "prompt.md"
        f.write_text(text, encoding="utf-8")
        return LP.scan_file(f)

    def test_a_labelled_prior_is_allowed(self, tmp_path, monkeypatch):
        assert self._scan(
            monkeypatch, tmp_path,
            "**资金是原因**〔先验：未经本仓库证据验证，`n=0`〕\n") == []

    def test_an_approved_claim_is_allowed(self, tmp_path, monkeypatch):
        assert self._scan(
            monkeypatch, tmp_path,
            "**资金是原因**〔approved: knowledge#42〕\n") == []

    def test_a_label_on_the_line_above_counts(self, tmp_path, monkeypatch):
        """A multi-line paragraph labels itself in its heading."""
        assert self._scan(
            monkeypatch, tmp_path,
            "〔先验：未验证〕\n一只票放量上涨，资金是原因，后续不同。\n") == []

    def test_silence_is_still_a_finding(self, tmp_path, monkeypatch):
        """The whole point: an unlabelled claim fails even though it is
        plausible, because plausibility is not evidence."""
        assert self._scan(
            monkeypatch, tmp_path,
            "一只票放量上涨，资金是原因，后续不同。\n")

    def test_the_label_must_be_one_of_the_known_kinds(self, tmp_path,
                                                      monkeypatch):
        """A random bracket is not a provenance declaration."""
        assert self._scan(
            monkeypatch, tmp_path, "**资金是原因**〔因为我觉得对〕\n")


class TestTheExemptionsAreNarrowAndJustified:
    def test_the_format_only_file_is_excluded_with_a_reason(self):
        for rel, why in LP.FORMAT_ONLY.items():
            assert why, f"{rel} is exempt with no reason recorded"

    def test_line_exemptions_carry_a_reason(self):
        for (_rel, _frag), why in LP.LINE_EXEMPT.items():
            assert why

    def test_a_format_exempt_file_does_not_disable_the_scan(self, tmp_path,
                                                            monkeypatch):
        """Exempting one file must not turn the checker off."""
        monkeypatch.setattr(LP, "REPO", tmp_path)
        monkeypatch.setattr(LP, "SCAN_ROOTS", (tmp_path,))
        monkeypatch.setattr(LP, "FORMAT_ONLY",
                            {"morning_scan.md": "output format, not a rule"})
        (tmp_path / "morning_scan.md").write_text(
            "操作建议列由模型填写\n", encoding="utf-8")
        (tmp_path / "other.md").write_text(
            "主力净流入 = 大资金看好\n", encoding="utf-8")
        findings = LP.scan()
        assert [f.path for f in findings] == ["other.md"]


class TestTheRepositoryItself:
    def test_the_current_tree_has_no_new_contamination(self):
        """The live tree, against its own baseline. A new unlabelled policy
        statement fails here rather than shipping to every decision."""
        findings = LP.scan()
        baseline = LP.load_baseline()
        fresh = [f for f in findings if f.key() not in baseline]
        assert not fresh, (
            "new policy contamination:\n"
            + "\n".join(f"{f.path}:{f.line} [{f.rule}] {f.text[:70]}"
                        for f in fresh))

    def test_the_t1_prompt_states_its_priors_as_priors(self):
        """The specific defect this round fixed: the T1 prompt asserted
        '价格是结果，资金是原因' and 'A 股是板块驱动的' as facts. They are
        now labelled priors with n=0 — the agent still sees them, and now
        also sees that nothing here has tested them."""
        text = (LP.REPO / "alpha_agents" / "prompts" / "t1_decide.md"
                ).read_text(encoding="utf-8")
        assert "价格是结果，资金是原因" not in text
        assert "A 股是板块驱动的" not in text
        assert "〔先验" in text
        assert "n=0" in text
