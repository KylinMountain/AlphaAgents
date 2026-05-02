from alpha_agents.data import market_history


class _FakeStockBasic:
    error_code = "0"

    def __init__(self, rows):
        self._rows = rows
        self._idx = -1

    def next(self):
        self._idx += 1
        return self._idx < len(self._rows)

    def get_row_data(self):
        return self._rows[self._idx]


def test_get_all_codes_includes_delisted_names_overlapping_window(monkeypatch):
    rows = [
        ["sh.600000", "A", "1999-11-10", "", "1", "1"],
        ["sz.000001", "B", "1991-04-03", "2026-04-01", "1", "0"],
        ["sz.000002", "C", "1991-01-29", "2025-01-01", "1", "0"],
        ["sh.000001", "上证指数", "1990-12-19", "", "2", "1"],
    ]
    monkeypatch.setattr(market_history, "_bs_login", lambda: None)
    monkeypatch.setattr(market_history, "_bs_logout", lambda: None)
    monkeypatch.setattr(
        market_history.bs,
        "query_stock_basic",
        lambda: _FakeStockBasic(rows),
    )

    codes = market_history.get_all_codes(include_inactive=True, start_date="2026-01-01")

    assert codes == ["600000", "000001"]
