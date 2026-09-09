"""给决策打分，而不是给结果打分。

结果要等几天，而且大部分是市场噪声；决策质量当天可见，且完全不需要市场
配合。这是这个样本量下唯一能天天迭代的东西——真实记录里，几小时内两轮
晨扫分别写出「低涨幅+流动性好」和「机构评分−8，融资融券显示去杠杆」，
两条都被接受了，没有任何机制在衡量这个差距。
"""

import pytest

from alpha_agents.data import thesis as T
from alpha_agents.evolution import process_quality as P


@pytest.fixture()
def store(tmp_path, monkeypatch):
    from alpha_agents.data import memory_store
    monkeypatch.setattr(memory_store, "MEMORY_DB_PATH",
                        tmp_path / "memory.db", raising=False)
    monkeypatch.setattr(memory_store._local, "conn", None, raising=False)
    yield
    conn = getattr(memory_store._local, "conn", None)
    if conn is not None:
        conn.close()
    memory_store._local.conn = None


def th(conditions, claim="小金属主线资金连续3日流入，东方钽业补涨",
       prob=0.62, size=0.03):
    return T.Thesis(code="000962", claim=claim, prob=prob, size_pct=size,
                    conditions=conditions)


class TestFamilyCoverage:
    def test_three_price_conditions_are_one_way_of_being_wrong(self):
        g = P.grade_thesis(th([
            T.Condition("price_below", 49.5),
            T.Condition("loss_exceeds", 5),
            T.Condition("drawdown_from_peak", 8),
        ]))
        assert g["families"] == ["价格"]
        assert not g["multi_family"]

    def test_price_plus_theme_plus_time_is_a_plan(self):
        g = P.grade_thesis(th([
            T.Condition("price_below", 49.5),
            T.Condition("theme_daily_score_below", 0),
            T.Condition("no_progress_by_day", 5),
        ]))
        assert set(g["families"]) == {"价格", "主线", "时间"}
        assert g["multi_family"]

    def test_no_conditions_covers_nothing(self):
        assert not P.grade_thesis(th([]))["multi_family"]


class TestClaimQuality:
    def test_a_claim_with_a_verifiable_object_passes(self):
        assert P.grade_thesis(th([]))["specific_claim"]

    def test_a_phrase_that_fits_any_stock_fails(self):
        """「技术面走弱」放在任何一只票任何一天都成立。"""
        assert not P.grade_thesis(th([], claim="技术面走强，值得关注"))["specific_claim"]

    def test_a_number_rescues_an_otherwise_vague_claim(self):
        """引用了数据就有了可验证的对象。"""
        assert P.grade_thesis(th([], claim="技术面走强，量能放大到3.2倍"))["specific_claim"]

    def test_too_short_to_check(self):
        assert not P.grade_thesis(th([], claim="会涨"))["specific_claim"]


class TestStatedDecisions:
    def test_the_default_probability_is_not_a_decision(self):
        assert not P.grade_thesis(th([], prob=0.5))["stated_probability"]

    def test_a_stated_probability_counts(self):
        assert P.grade_thesis(th([], prob=0.62))["stated_probability"]

    def test_no_size_means_it_took_the_default(self):
        assert not P.grade_thesis(th([], size=0.0))["stated_size"]


class TestReasonGrading:
    def test_a_reason_citing_data_is_checkable(self):
        g = P.grade_reason("主线今日分转−1，主力净流出28.34亿")
        assert g["cites_number"] and not g["empty_talk"]

    def test_a_reason_asserting_nothing_is_not(self):
        g = P.grade_reason("技术面走弱")
        assert not g["cites_number"] and g["empty_talk"]


class TestSummary:
    def test_empty_history(self, store):
        assert P.summarise() == {"n": 0}
        assert P.inject_process_quality() == ""

    def test_names_the_families_it_never_guards(self, store):
        """从不设防的失效路径，最后都会变成盲点——而这条在亏钱之前就能看见。"""
        for i in range(4):
            t = th([T.Condition("price_below", 10)])
            t.code = f"00000{i}"
            T.create(t)
        s = P.summarise()
        assert "主线" in s["never_guarded"] and "时间" in s["never_guarded"]
        assert "从不设防" in P.inject_process_quality()

    def test_flags_single_family_coverage(self, store):
        for i in range(4):
            t = th([T.Condition("price_below", 10)])
            t.code = f"00000{i}"
            T.create(t)
        assert "同一种错法查了三遍" in P.inject_process_quality()

    def test_a_well_formed_book_says_little(self, store):
        for i in range(4):
            t = th([T.Condition("price_below", 10),
                    T.Condition("theme_daily_score_below", 0),
                    T.Condition("no_progress_by_day", 5),
                    T.Condition("breadth_below", 0.8)])
            t.code = f"00000{i}"
            T.create(t)
        out = P.inject_process_quality()
        assert "决策质量" in out
        assert "同一种错法" not in out and "从不设防" not in out
