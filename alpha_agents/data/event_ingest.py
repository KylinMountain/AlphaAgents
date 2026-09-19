"""Normalize provider event data into the PIT Event Expectations store.

Provider rows are not interchangeable:

* company earnings forecast = management guidance / event realization;
* disclosure calendar = calendar knowledge;
* sell-side analyst forecast = market expectation.

The adapters keep those types separate. A date-only provider timestamp becomes
visible at 23:59:59 on that date, a conservative rule that prevents an EOD
announcement from leaking into the same day's 09:00 replay.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable

from alpha_agents.data import event_expectations as events


class EventIngestError(ValueError):
    """A provider row cannot be mapped to a PIT event honestly."""


def _text(value) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "nat", "none"} else text


def pit_timestamp(value) -> str:
    """Normalize provider dates/times; date-only values are visible at EOD."""
    text = _text(value)
    if not text:
        raise EventIngestError("a point-in-time record needs a timestamp")

    if len(text) == 8 and text.isdigit():
        parsed = datetime.strptime(text, "%Y%m%d")
        return parsed.strftime("%Y-%m-%d 23:59:59")
    if len(text) == 10:
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d")
        except ValueError:
            pass
        else:
            return parsed.strftime("%Y-%m-%d 23:59:59")
    # Keep provider times, but normalize ISO T to a space for SQLite ordering.
    return text.replace("T", " ", 1)


def stock_code(value) -> str:
    """600000.SH -> 600000; preserve already-normalized A-share codes."""
    text = _text(value)
    if not text:
        raise EventIngestError("stock event needs a code")
    return text.split(".", 1)[0]


def _records(rows) -> list[dict]:
    """Accept pandas frames, iterables of mappings, or one mapping."""
    if rows is None:
        return []
    if isinstance(rows, dict):
        return [dict(rows)]
    to_dict = getattr(rows, "to_dict", None)
    if callable(to_dict):
        try:
            return [dict(row) for row in to_dict("records")]
        except TypeError:
            pass
    return [dict(row) for row in rows]


def _clean(row: dict) -> dict:
    out = {}
    for key, value in row.items():
        text = _text(value)
        out[str(key)] = None if not text else value
    return out


def ingest_tushare_forecast(rows, *,
                            conn=None,
                            source: str = "tushare:forecast") -> dict:
    """Ingest company earnings forecasts as management guidance.

    Tushare forecast rows describe a company's own earnings forecast. They are
    not analyst consensus. Revisions for one code/period share an event key and
    remain separate append-only realizations by announcement time.
    """
    written = skipped = 0
    ids = []
    for raw in _records(rows):
        row = _clean(raw)
        try:
            code = stock_code(row.get("ts_code"))
            period = _text(row.get("end_date"))
            announced = pit_timestamp(row.get("ann_date"))
        except EventIngestError:
            skipped += 1
            continue
        if not period:
            skipped += 1
            continue

        event_key = f"cn-stock:{code}:earnings-guidance:{period}"
        actual = {
            "kind": "management_guidance",
            "period": period,
            "forecast_type": row.get("type"),
            "profit_change_min": row.get("p_change_min"),
            "profit_change_max": row.get("p_change_max"),
            "net_profit_min": row.get("net_profit_min"),
            "net_profit_max": row.get("net_profit_max"),
            "last_parent_net": row.get("last_parent_net"),
            "summary": row.get("summary"),
            "change_reason": row.get("change_reason"),
        }
        metadata = {
            "provider_dataset": "forecast",
            "period": period,
            "first_ann_date": row.get("first_ann_date"),
            "guidance_not_consensus": True,
        }
        event_id = events.record_event(
            event_key=event_key,
            event_type="earnings_guidance",
            scope="stock",
            subject=code,
            scheduled_at=announced,
            captured_at=announced,
            source=source,
            metadata=metadata,
            conn=conn)
        realization_id = events.record_realization(
            event_key=event_key,
            announced_at=announced,
            actual=actual,
            source=source,
            conn=conn)
        ids.append({
            "event_id": event_id,
            "realization_id": realization_id,
            "event_key": event_key,
        })
        written += 1
    return {"dataset": "forecast", "written": written,
            "skipped": skipped, "rows": ids}


def ingest_tushare_disclosure(rows, *,
                              conn=None,
                              source: str = "tushare:disclosure_date") -> dict:
    """Ingest knowable earnings-report calendar snapshots.

    A row is admitted only when ann_date exists, because that is the timestamp
    used here for "the market could know this schedule". The scheduled date
    prefers actual_date, then pre_date. modify_date is metadata until its exact
    publication semantics are verified locally; it is never used as a PIT
    timestamp by guess.
    """
    written = skipped = 0
    ids = []
    for raw in _records(rows):
        row = _clean(raw)
        try:
            code = stock_code(row.get("ts_code"))
            period = _text(row.get("end_date"))
            captured = pit_timestamp(row.get("ann_date"))
        except EventIngestError:
            skipped += 1
            continue
        scheduled_raw = row.get("actual_date") or row.get("pre_date")
        try:
            scheduled = pit_timestamp(scheduled_raw)
        except EventIngestError:
            skipped += 1
            continue
        if not period:
            skipped += 1
            continue

        event_key = f"cn-stock:{code}:earnings-report:{period}"
        event_id = events.record_event(
            event_key=event_key,
            event_type="earnings_report",
            scope="stock",
            subject=code,
            scheduled_at=scheduled,
            captured_at=captured,
            source=source,
            metadata={
                "provider_dataset": "disclosure_date",
                "period": period,
                "pre_date": row.get("pre_date"),
                "actual_date": row.get("actual_date"),
                "modify_date": row.get("modify_date"),
            },
            conn=conn)
        ids.append({"event_id": event_id, "event_key": event_key})
        written += 1
    return {"dataset": "disclosure_date", "written": written,
            "skipped": skipped, "rows": ids}


def ingest_expectation_snapshots(rows: Iterable[dict], *,
                                 conn=None,
                                 source: str) -> dict:
    """Provider-neutral path for genuine consensus / market-implied vintages.

    Every row must already name its event and capture timestamp. This function
    intentionally does not infer those fields from a provider-specific
    "latest forecast" API, because that would turn current values into fake
    historical expectations.
    """
    written = skipped = 0
    ids = []
    for raw in _records(rows):
        row = _clean(raw)
        event_key = _text(row.get("event_key"))
        if not event_key:
            skipped += 1
            continue
        try:
            captured = pit_timestamp(row.get("captured_at"))
        except EventIngestError:
            skipped += 1
            continue
        consensus = row.get("consensus")
        market_implied = row.get("market_implied")
        if consensus is None and market_implied is None:
            skipped += 1
            continue
        snapshot_id = events.record_expectation(
            event_key=event_key,
            captured_at=captured,
            consensus=consensus if isinstance(consensus, dict) else None,
            market_implied=(
                market_implied if isinstance(market_implied, dict) else None),
            source=source,
            conn=conn)
        ids.append({"expectation_id": snapshot_id, "event_key": event_key})
        written += 1
    return {"dataset": "expectation_snapshot", "written": written,
            "skipped": skipped, "rows": ids}
