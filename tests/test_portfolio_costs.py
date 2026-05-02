from alpha_agents.data.portfolio import _estimate_net_close_result


def test_net_close_result_deducts_trading_friction():
    result = _estimate_net_close_result(open_price=10.0, close_price=11.0, shares=1000)

    assert result["gross_amount"] == 1000.0
    assert result["costs"] > 0
    assert result["return_amount"] < result["gross_amount"]
    assert result["return_pct"] < 10.0


def test_flat_round_trip_loses_money_after_costs():
    result = _estimate_net_close_result(open_price=10.0, close_price=10.0, shares=1000)

    assert result["gross_amount"] == 0.0
    assert result["return_amount"] < 0
    assert result["return_pct"] < 0
