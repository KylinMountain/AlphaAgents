"""G5 — a judge commits to its own answer before assessing a candidate."""

from pathlib import Path

from alpha_agents.evolution.two_stage_judge import (
    INDEPENDENT_FIRST_INSTRUCTION, split_judgement, wrap_judge_prompt,
)


class TestWrapJudgePrompt:
    def test_instruction_comes_first(self):
        wrapped = wrap_judge_prompt("评价这条推荐")
        assert wrapped.startswith(INDEPENDENT_FIRST_INSTRUCTION)
        assert "评价这条推荐" in wrapped

    def test_original_prompt_is_preserved_verbatim(self):
        base = "多行\n提示词\n内容"
        assert base in wrap_judge_prompt(base)

    def test_instruction_forbids_skipping_the_first_step(self):
        assert "严禁跳过第一步" in INDEPENDENT_FIRST_INSTRUCTION


class TestSplitJudgement:
    def test_splits_a_compliant_three_part_response(self):
        resp = (
            "第一步 — 独立判断：\n今日资金净流出，市场偏弱。\n"
            "第二步 — 对照：\n与原推荐的看多不一致。\n"
            "第三步 — 结论：\n以数据为准，推荐过于乐观。"
        )
        got = split_judgement(resp)
        assert got["complied"] is True
        assert "资金净流出" in got["independent"]
        assert "不一致" in got["comparison"]
        assert "过于乐观" in got["verdict"]

    def test_bare_verdict_is_flagged_non_compliant(self):
        """The exact failure this guard exists to catch."""
        got = split_judgement("推荐很合理，继续持有。")
        assert got["complied"] is False
        assert got["independent"] == ""
        assert got["verdict"] == "推荐很合理，继续持有。"

    def test_verdict_only_with_a_heading_is_still_non_compliant(self):
        got = split_judgement("第三步 — 结论：推荐合理")
        assert got["complied"] is False

    def test_empty_response(self):
        got = split_judgement("")
        assert got["complied"] is False and got["verdict"] == ""

    def test_none_response_does_not_raise(self):
        assert split_judgement(None)["complied"] is False

    def test_accepts_the_alternate_wording(self):
        resp = ("独立判断：市场走弱\n"
                "对照：与推荐相反\n"
                "结论：应减仓")
        got = split_judgement(resp)
        assert got["complied"] is True
        assert "市场走弱" in got["independent"]

    def test_sections_do_not_bleed_into_each_other(self):
        resp = ("第一步：AAA\n第二步：BBB\n第三步：CCC")
        got = split_judgement(resp)
        assert "BBB" not in got["independent"]
        assert "CCC" not in got["comparison"]


class TestReviewPromptCarriesTheGuard:
    """The review's lesson section is the live self-assessment point."""

    def _prompt(self) -> str:
        p = (Path(__file__).parent.parent / "alpha_agents" / "prompts"
             / "review.md")
        return p.read_text(encoding="utf-8")

    def test_requires_an_independent_read_before_the_lessons(self):
        text = self._prompt()
        idx_independent = text.find("独立判断")
        idx_right = text.find("今日做对了什么")
        assert idx_independent >= 0
        assert idx_independent < idx_right

    def test_requires_naming_the_disagreement(self):
        assert "分歧" in self._prompt()

    def test_forbids_rationalising_the_recommendation(self):
        assert "不要为推荐找补" in self._prompt()

    def test_defers_hit_determination_to_market_data(self):
        assert "由行情数据判定" in self._prompt()
