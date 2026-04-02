import json
from unittest.mock import patch

import pandas as pd
import pytest

from alpha_agents.tools.sector import get_sector_data_fn


def _mock_sector_fund_flow():
    return pd.DataFrame({
        "名称": ["半导体", "白酒"],
        "今日涨跌幅": [2.5, -1.3],
        "今日主力净流入-净额": [500000000.0, -200000000.0],
        "今日主力净流入-净占比": [5.2, -2.1],
    })


@patch("alpha_agents.tools.sector._fetch_sector_fund_flow", side_effect=lambda: _mock_sector_fund_flow())
def test_get_sector_data(mock_flow):
    result = get_sector_data_fn("半导体")
    parsed = json.loads(result)
    assert parsed["sector_name"] == "半导体"
    assert parsed["change_pct"] == 2.5


@patch("alpha_agents.tools.sector._fetch_sector_fund_flow", side_effect=lambda: _mock_sector_fund_flow())
def test_get_sector_data_not_found(mock_flow):
    result = get_sector_data_fn("火星板块")
    parsed = json.loads(result)
    assert parsed["error"] is not None
