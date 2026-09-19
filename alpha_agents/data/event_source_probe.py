"""Read-only discovery for event / expectation data sources.

The job of this module is to answer "what can this machine and account
actually provide?" before an ingest adapter is written.

Default probes are local only. A provider network call happens only when the
caller explicitly asks for it.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import inspect
import os
import sqlite3
from pathlib import Path

from alpha_agents.config import DATA_DIR

_KEYWORDS = (
    "forecast", "profit", "earning", "financial", "event", "expect",
    "consensus", "research", "report", "disclosure",
)

TUSHARE_CANDIDATES = {
    "forecast": {
        "kind": "earnings_forecast",
        "time_fields": ["ann_date", "first_ann_date"],
        "docs_note": "Tushare documents historical earnings forecasts.",
    },
    "express": {
        "kind": "earnings_express",
        "time_fields": ["ann_date"],
        "docs_note": "Actual/preliminary result candidate.",
    },
    "fina_indicator": {
        "kind": "financial_actual",
        "time_fields": ["ann_date", "end_date"],
        "docs_note": "Historical financial indicators candidate.",
    },
    "disclosure_date": {
        "kind": "earnings_calendar",
        "time_fields": ["ann_date", "pre_date", "actual_date", "modify_date"],
        "docs_note": "Disclosure calendar and revisions candidate.",
    },
    "sell_side_earnings_forecast": {
        "kind": "analyst_expectation",
        "time_fields": [],
        "docs_note": (
            "Tushare documents sell-side earnings forecast history extending "
            "back many years. Endpoint/fields/vintage semantics need account "
            "probe before use."),
    },
}

AKSHARE_CANDIDATES = {
    "stock_profit_forecast_em": "analyst_expectation",
    "stock_profit_forecast_ths": "analyst_expectation",
    "stock_yjyg_em": "earnings_forecast",
    "stock_yjkb_em": "earnings_express",
    "stock_yjbb_em": "financial_actual",
}


def _version(package: str) -> str | None:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return None


def _sqlite_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    try:
        return [row[1] for row in conn.execute(
            f"PRAGMA table_info('{table}')").fetchall()]
    except sqlite3.Error:
        return []


def probe_local(data_dir: Path = DATA_DIR) -> list[dict]:
    """Inspect local SQLite schemas without changing them."""
    found = []
    for path in sorted(data_dir.glob("*.db")):
        try:
            conn = sqlite3.connect(
                f"file:{path}?mode=ro", uri=True, timeout=2)
        except sqlite3.Error as exc:
            found.append({
                "provider": "local_sqlite",
                "database": path.name,
                "available": False,
                "error": str(exc),
            })
            continue
        try:
            tables = [row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' ORDER BY name").fetchall()]
            for table in tables:
                name = table.lower()
                if not any(word in name for word in _KEYWORDS):
                    continue
                columns = _sqlite_columns(conn, table)
                time_fields = [
                    field for field in columns
                    if field.lower() in {
                        "captured_at", "ann_date", "first_ann_date",
                        "notice_date", "rec_time", "publish_time",
                        "created_at", "updated_at", "date", "end_date",
                    }
                ]
                found.append({
                    "provider": "local_sqlite",
                    "database": path.name,
                    "dataset": table,
                    "available": True,
                    "columns": columns,
                    "time_fields": time_fields,
                    "point_in_time_grade": "U",
                    "note": (
                        "Schema match only. PIT grade stays unknown until row "
                        "semantics and revision history are inspected."),
                })
        finally:
            conn.close()
    return found


def probe_sdks() -> list[dict]:
    """Discover provider SDKs and candidate functions without network calls."""
    out = []

    tushare_spec = importlib.util.find_spec("tushare")
    out.append({
        "provider": "tushare",
        "sdk_available": tushare_spec is not None,
        "sdk_version": _version("tushare"),
        "credential_available": bool(
            os.environ.get("TUSHARE_TOKEN") or os.environ.get("TS_TOKEN")),
        "live_probe_run": False,
        "datasets": [
            {"name": name, **meta, "point_in_time_grade": "U"}
            for name, meta in TUSHARE_CANDIDATES.items()
        ],
    })

    ak_spec = importlib.util.find_spec("akshare")
    ak_datasets = []
    if ak_spec is not None:
        try:
            ak = importlib.import_module("akshare")
            for name, kind in AKSHARE_CANDIDATES.items():
                fn = getattr(ak, name, None)
                signature = None
                if callable(fn):
                    try:
                        signature = str(inspect.signature(fn))
                    except (TypeError, ValueError):
                        signature = None
                ak_datasets.append({
                    "name": name,
                    "kind": kind,
                    "available": callable(fn),
                    "signature": signature,
                    "point_in_time_grade": "U",
                })
        except Exception as exc:
            ak_datasets.append({
                "available": False,
                "error": f"AKShare import failed: {exc}",
            })
    else:
        ak_datasets = [
            {
                "name": name, "kind": kind, "available": False,
                "point_in_time_grade": "U",
            }
            for name, kind in AKSHARE_CANDIDATES.items()
        ]
    out.append({
        "provider": "akshare",
        "sdk_available": ak_spec is not None,
        "sdk_version": _version("akshare"),
        "live_probe_run": False,
        "datasets": ak_datasets,
    })
    return out


def _frame_probe(df) -> dict:
    if df is None:
        return {"rows": 0, "columns": []}
    columns = [str(c) for c in getattr(df, "columns", [])]
    rows = int(len(df)) if hasattr(df, "__len__") else None
    sample = {}
    if rows:
        try:
            sample = {
                str(k): (None if v is None else str(v)[:120])
                for k, v in df.iloc[0].to_dict().items()
            }
        except Exception:
            sample = {}
    return {"rows": rows, "columns": columns, "sample": sample}


def probe_tushare_live(*, token: str | None = None,
                       ts_code: str = "600000.SH") -> list[dict]:
    """Run small, explicit Tushare samples. Never logs or returns the token."""
    token = token or os.environ.get("TUSHARE_TOKEN") or os.environ.get("TS_TOKEN")
    if not token:
        return [{
            "provider": "tushare",
            "available": False,
            "error": "TUSHARE_TOKEN/TS_TOKEN not set",
        }]
    try:
        import tushare as ts
        pro = ts.pro_api(token)
    except Exception as exc:
        return [{
            "provider": "tushare",
            "available": False,
            "error": f"SDK init failed: {exc}",
        }]

    calls = {
        "forecast": lambda: pro.forecast(ts_code=ts_code),
        "express": lambda: pro.express(ts_code=ts_code),
        "fina_indicator": lambda: pro.fina_indicator(ts_code=ts_code),
        "disclosure_date": lambda: pro.disclosure_date(ts_code=ts_code),
    }
    out = []
    for name, call in calls.items():
        meta = TUSHARE_CANDIDATES[name]
        try:
            frame = call()
            probe = _frame_probe(frame)
            out.append({
                "provider": "tushare",
                "dataset": name,
                "available": True,
                "kind": meta["kind"],
                "time_fields_expected": meta["time_fields"],
                "point_in_time_grade": "U",
                **probe,
            })
        except Exception as exc:
            out.append({
                "provider": "tushare",
                "dataset": name,
                "available": False,
                "kind": meta["kind"],
                "point_in_time_grade": "U",
                "error": str(exc),
            })

    out.append({
        "provider": "tushare",
        "dataset": "sell_side_earnings_forecast",
        "available": None,
        "point_in_time_grade": "U",
        "note": (
            "Documentation lead recorded, but endpoint/permission/vintage "
            "semantics are intentionally not guessed by this probe."),
    })
    return out


def probe_all(*, data_dir: Path = DATA_DIR,
              live_tushare: bool = False,
              ts_code: str = "600000.SH") -> dict:
    result = {
        "local": probe_local(data_dir),
        "sdks": probe_sdks(),
    }
    if live_tushare:
        result["tushare_live"] = probe_tushare_live(ts_code=ts_code)
    return result
