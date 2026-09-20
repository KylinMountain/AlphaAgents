"""Read-only source discovery for sector-first experiments.

This probe does not decide that a dataset is historically usable merely
because a table exists. It reports schema, time fields and coverage so a human
can grade the source before strict replay consumes it.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from alpha_agents.config import DATA_DIR


_TIME_FIELDS = {
    "as_of", "captured_at", "effective_at", "effective_date", "date",
    "trade_date", "start_date", "end_date", "created_at", "updated_at",
}
_MEMBERSHIP_WORDS = ("concept", "industry", "sector", "theme")


class CapabilityError(ValueError):
    """A source capability report is missing, mutable or not formal-grade."""


def _dump(value) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False)


def report_hash(report: dict) -> str:
    """Hash a capability report without trusting its stored hash field."""
    payload = {key: value for key, value in report.items()
               if key != "content_hash"}
    return hashlib.sha256(_dump(payload).encode("utf-8")).hexdigest()


def with_content_hash(report: dict) -> dict:
    payload = {key: value for key, value in report.items()
               if key != "content_hash"}
    return {**payload, "content_hash": report_hash(payload)}


def register(report: dict, root: Path) -> Path:
    """Content-address one audited/verified capability report."""
    canonical = with_content_hash(report)
    digest = canonical["content_hash"]
    directory = Path(root) / "capabilities"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{digest}.json"
    text = _dump(canonical) + "\n"
    try:
        with target.open("x", encoding="utf-8") as fh:
            fh.write(text)
    except FileExistsError:
        existing = json.loads(target.read_text(encoding="utf-8"))
        if with_content_hash(existing) != canonical:
            raise CapabilityError(
                f"registered capability collision at {target}")
    return target


def formal_errors(report: dict) -> list[str]:
    """Requirements for the preregistered A/B/C/D fund-flow ablation."""
    errors = []
    stored = str(report.get("content_hash") or "")
    computed = report_hash(report)
    if not stored:
        errors.append("capability report is missing content_hash")
    elif stored != computed:
        errors.append("capability report content_hash mismatch")

    fund = (report.get("capabilities") or {}).get("fund_flow") or {}
    if fund.get("status") != "available":
        errors.append("fund_flow capability is not available")
    if fund.get("point_in_time_grade") != "A":
        errors.append(
            "fund_flow point_in_time_grade must be A for formal B/D comparison")
    if fund.get("strict_replay_eligible") is not True:
        errors.append("fund_flow must be strict_replay_eligible")
    verification = fund.get("verification") or {}
    if not str(verification.get("verified_by") or "").strip():
        errors.append("fund_flow grade A requires verification.verified_by")
    if not str(verification.get("verified_at") or "").strip():
        errors.append("fund_flow grade A requires verification.verified_at")
    if not str(verification.get("evidence") or "").strip():
        errors.append("fund_flow grade A requires verification.evidence")

    security = (report.get("capabilities") or {}).get("security_status") or {}
    if security.get("status") != "available":
        errors.append("security_status capability is not available")
    if security.get("point_in_time_grade") != "A":
        errors.append(
            "security_status point_in_time_grade must be A for formal replay")
    if security.get("strict_replay_eligible") is not True:
        errors.append("security_status must be strict_replay_eligible")
    security_verification = security.get("verification") or {}
    if not str(security_verification.get("verified_by") or "").strip():
        errors.append(
            "security_status grade A requires verification.verified_by")
    if not str(security_verification.get("verified_at") or "").strip():
        errors.append(
            "security_status grade A requires verification.verified_at")
    if not str(security_verification.get("evidence") or "").strip():
        errors.append(
            "security_status grade A requires verification.evidence")
    return errors


def _tables(path: Path) -> list[str]:
    if not path.exists():
        return []
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    try:
        return [
            str(row[0]) for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' ORDER BY name").fetchall()
        ]
    finally:
        conn.close()


def _columns(path: Path, table: str) -> list[str]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    try:
        return [
            str(row[1]) for row in conn.execute(
                f"PRAGMA table_info('{table}')").fetchall()
        ]
    finally:
        conn.close()


def probe_membership_sources(data_dir: Path = DATA_DIR) -> list[dict]:
    path = data_dir / "stocks.db"
    if not path.exists():
        return [{
            "provider": "local_sqlite",
            "database": "stocks.db",
            "available": False,
            "status": "missing_file",
        }]

    out = []
    for table in _tables(path):
        if not any(word in table.lower() for word in _MEMBERSHIP_WORDS):
            continue
        columns = _columns(path, table)
        time_fields = [
            name for name in columns if name.lower() in _TIME_FIELDS
        ]
        grade = "U" if time_fields else "C"
        note = (
            "time-like fields exist; row semantics still need verification"
            if time_fields else
            "no as-of/effective field found; treat as current-only")
        out.append({
            "provider": "local_sqlite",
            "database": "stocks.db",
            "dataset": table,
            "available": True,
            "columns": columns,
            "time_fields": time_fields,
            "point_in_time_grade": grade,
            "strict_replay_eligible": False,
            "note": note,
        })
    return out


def _coverage(path: Path, table: str,
              candidates: tuple[str, ...]) -> dict:
    if not path.exists():
        return {"status": "missing_file"}
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,)).fetchone()
        if not exists:
            return {"status": "missing_table"}
        columns = {
            str(row[1]) for row in conn.execute(
                f"PRAGMA table_info('{table}')").fetchall()
        }
        field = next((name for name in candidates if name in columns), None)
        if field is None:
            return {
                "status": "ungraded",
                "columns": sorted(columns),
                "reason": "no recognized date/time field",
            }
        row = conn.execute(
            f"SELECT MIN({field}),MAX({field}),COUNT(DISTINCT {field}) "
            f"FROM {table}").fetchone()
        return {
            "status": "available",
            "time_field": field,
            "first": row[0],
            "last": row[1],
            "distinct_times": int(row[2] or 0),
            "columns": sorted(columns),
        }
    except sqlite3.Error as exc:
        return {"status": "unreadable", "error": str(exc)}
    finally:
        conn.close()


def _fund_flow_capability(data_dir: Path) -> dict:
    result = _coverage(
        data_dir / "market_snapshots.db",
        "stock_fund_flow_daily",
        ("captured_at", "trade_date", "date"))
    if result.get("status") != "available":
        return {
            **result,
            "point_in_time_grade": "U",
            "strict_replay_eligible": False,
            "note": "fund-flow source is not available for semantic verification",
        }

    columns = set(result.get("columns") or [])
    if "captured_at" in columns:
        grade = "U"
        note = (
            "captured_at exists, but revision/vintage semantics still need "
            "provider verification before strict replay")
    else:
        grade = "B"
        note = (
            "historical trade_date exists but no capture/revision vintage is "
            "stored; later provider revisions cannot be excluded")
    return {
        **result,
        "point_in_time_grade": grade,
        "strict_replay_eligible": False,
        "verification": None,
        "note": note,
    }


def _security_status_capability(data_dir: Path) -> dict:
    result = _coverage(
        data_dir / "stocks.db",
        "stocks",
        ("effective_at", "available_at", "captured_at", "date"))
    columns = set(result.get("columns") or [])
    status_columns = {"is_st", "is_suspended"}
    if not status_columns.issubset(columns):
        return {
            **result,
            "status": (
                result.get("status")
                if result.get("status") not in {"available", "ungraded"}
                else "missing_status_columns"
            ),
            "point_in_time_grade": "U",
            "strict_replay_eligible": False,
            "verification": None,
            "note": "historical ST/suspension status is not available",
        }

    # The current stocks table has no verified effective/vintage semantics.
    # A time-looking field would still start at U until semantics are audited.
    has_time = result.get("status") == "available"
    return {
        **result,
        "status": "available",
        "point_in_time_grade": "U" if has_time else "C",
        "strict_replay_eligible": False,
        "verification": None,
        "note": (
            "time-like field exists but status semantics are unverified"
            if has_time else
            "stocks.is_st/is_suspended are current snapshot fields; "
            "they cannot prove historical status"
        ),
    }


def probe_all(data_dir: Path = DATA_DIR) -> dict:
    return {
        "membership_sources": probe_membership_sources(data_dir),
        "daily_price": _coverage(
            data_dir / "market_history.db", "daily_kline", ("date",)),
        "fund_flow": _fund_flow_capability(data_dir),
        "security_status": _security_status_capability(data_dir),
        "event_expectations": _coverage(
            data_dir / "market_snapshots.db",
            "event_expectation_snapshots",
            ("captured_at",)),
        "semantics": {
            "A": "verified historical vintages/effective membership",
            "B": "historical observations but revision semantics incomplete",
            "C": "current/latest snapshot only",
            "U": "unknown until semantics are verified",
        },
    }
