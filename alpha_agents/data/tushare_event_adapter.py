"""Small Tushare fetch adapter for event ingestion.

This adapter only calls endpoints whose mapping is already explicit:
forecast -> management guidance
disclosure_date -> earnings calendar

It does not guess the sell-side consensus endpoint. Use the source probe first,
then add a dedicated adapter once its vintage/publication fields are known.
"""

from __future__ import annotations

import os


class TushareEventAdapterError(RuntimeError):
    pass


def _client(token: str | None = None):
    token = token or os.environ.get("TUSHARE_TOKEN") or os.environ.get("TS_TOKEN")
    if not token:
        raise TushareEventAdapterError(
            "TUSHARE_TOKEN/TS_TOKEN is required for live Tushare ingestion")
    try:
        import tushare as ts
        return ts.pro_api(token)
    except Exception as exc:
        raise TushareEventAdapterError(f"Tushare init failed: {exc}") from exc


def fetch(*, dataset: str, ts_code: str,
          token: str | None = None):
    pro = _client(token)
    if dataset == "forecast":
        return pro.forecast(ts_code=ts_code)
    if dataset == "disclosure_date":
        return pro.disclosure_date(ts_code=ts_code)
    raise TushareEventAdapterError(
        f"unsupported event dataset {dataset!r}; "
        "supported=['forecast', 'disclosure_date']")
