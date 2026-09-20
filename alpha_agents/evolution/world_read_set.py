"""Content-addressed evidence for the exact world a decision consumed.

A capability report says what a source can provide. A read set says what this
particular decision actually consumed. Formal replay must bind both.

The module is storage-agnostic: readers hand it already-observed facts plus
their timestamps and hashes. It never fetches a newer row to complete a
historical packet.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import subprocess


SCHEMA_VERSION = 1
STRICT_GRADE = "A"


class WorldReadSetError(ValueError):
    pass


def _dump(value) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False)


def _hash(value) -> str:
    return hashlib.sha256(_dump(value).encode("utf-8")).hexdigest()


def content_hash(payload: dict) -> str:
    body = {
        key: value for key, value in payload.items()
        if key != "read_set_hash"
    }
    return _hash(body)


def _parse_time(value: str, *, field: str) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise WorldReadSetError(f"{field} is required")
    try:
        return datetime.fromisoformat(raw)
    except ValueError as exc:
        raise WorldReadSetError(
            f"{field} must be ISO-8601, got {raw!r}") from exc


def _validate_ref_time(ref: dict, cutoff: datetime, *, prefix: str) -> None:
    for field in ("available_at", "captured_at", "announced_at"):
        value = ref.get(field)
        if not value:
            continue
        observed = _parse_time(str(value), field=f"{prefix}.{field}")
        compare_cutoff = cutoff
        if observed.tzinfo is None and compare_cutoff.tzinfo is not None:
            observed = observed.replace(tzinfo=compare_cutoff.tzinfo)
        if compare_cutoff.tzinfo is None and observed.tzinfo is not None:
            compare_cutoff = compare_cutoff.replace(tzinfo=observed.tzinfo)
        if observed > compare_cutoff:
            raise WorldReadSetError(
                f"{prefix}.{field}={value!r} is after cutoff")


def _normalize_refs(values: list[dict] | None) -> list[dict]:
    refs = [dict(item) for item in (values or [])]
    return sorted(refs, key=_dump)


def build(*, cutoff: str, ranking_session: str, source_identity_hash: str,
          code_ref: str | None, policy_ref: str | None,
          membership: dict | None, security_status: dict,
          price_refs: list[dict], event_refs: list[dict] | None = None,
          fund_flow_refs: list[dict] | None = None,
          extra_refs: list[dict] | None = None) -> dict:
    """Seal one decision's actual reads into a stable object."""
    payload = {
        "schema_version": SCHEMA_VERSION,
        "cutoff": str(cutoff),
        "ranking_session": str(ranking_session)[:10],
        "identity": {
            "source_identity_hash": str(source_identity_hash),
            "code_ref": code_ref,
            "policy_ref": policy_ref,
        },
        "membership": dict(membership) if membership else None,
        "security_status": dict(security_status),
        "price_refs": _normalize_refs(price_refs),
        "event_refs": _normalize_refs(event_refs),
        "fund_flow_refs": _normalize_refs(fund_flow_refs),
        "extra_refs": _normalize_refs(extra_refs),
    }
    payload["strict_replay_eligible"] = strict_errors(payload) == []
    return {**payload, "read_set_hash": content_hash(payload)}


def strict_errors(read_set: dict) -> list[str]:
    errors: list[str] = []
    if read_set.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")

    cutoff_raw = str(read_set.get("cutoff") or "")
    try:
        cutoff = _parse_time(cutoff_raw, field="cutoff")
    except WorldReadSetError as exc:
        return [str(exc)]

    identity = read_set.get("identity") or {}
    if not str(identity.get("source_identity_hash") or "").strip():
        errors.append("identity.source_identity_hash is required")
    if not str(identity.get("code_ref") or "").strip():
        errors.append("identity.code_ref is required")
    if not str(identity.get("policy_ref") or "").strip():
        errors.append("identity.policy_ref is required")

    status = read_set.get("security_status") or {}
    if status.get("point_in_time_grade") != STRICT_GRADE:
        errors.append("security_status must be point_in_time_grade A")
    if status.get("strict_replay_eligible") is not True:
        errors.append("security_status must be strict_replay_eligible")

    membership = read_set.get("membership")
    if membership is not None:
        if not str(membership.get("snapshot_id") or "").strip():
            errors.append("membership.snapshot_id is required")
        if not str(membership.get("content_hash") or "").strip():
            errors.append("membership.content_hash is required")
        if membership.get("available_at"):
            try:
                _validate_ref_time(
                    membership, cutoff, prefix="membership")
            except WorldReadSetError as exc:
                errors.append(str(exc))

    prices = read_set.get("price_refs")
    if not isinstance(prices, list) or not prices:
        errors.append("price_refs must contain actual consumed price rows")
    else:
        for index, ref in enumerate(prices):
            if not str(ref.get("content_hash") or "").strip():
                errors.append(
                    f"price_refs[{index}].content_hash is required")
            try:
                _validate_ref_time(
                    ref, cutoff, prefix=f"price_refs[{index}]")
            except WorldReadSetError as exc:
                errors.append(str(exc))

    for field in ("event_refs", "fund_flow_refs", "extra_refs"):
        values = read_set.get(field) or []
        if not isinstance(values, list):
            errors.append(f"{field} must be a list")
            continue
        for index, ref in enumerate(values):
            try:
                _validate_ref_time(
                    ref, cutoff, prefix=f"{field}[{index}]")
            except WorldReadSetError as exc:
                errors.append(str(exc))
            grade = ref.get("point_in_time_grade")
            if grade and grade != STRICT_GRADE:
                errors.append(
                    f"{field}[{index}] point_in_time_grade {grade!r} is not A")
            if ref.get("strict_replay_eligible") is False:
                errors.append(
                    f"{field}[{index}] is not strict_replay_eligible")

    return errors


def require_valid(read_set: dict, *, strict: bool = False) -> str:
    if not isinstance(read_set, dict):
        raise WorldReadSetError("world read set must be an object")
    stored = str(read_set.get("read_set_hash") or "")
    computed = content_hash(read_set)
    if not stored or stored != computed:
        raise WorldReadSetError("world read set content hash mismatch")
    errors = strict_errors(read_set)
    if strict and errors:
        raise WorldReadSetError(
            "strict world read set rejected: " + "; ".join(errors))
    return stored


def file_identity(paths: dict[str, Path]) -> dict:
    """Hash immutable input bytes by logical source name."""
    files = {}
    for name, path in sorted(paths.items()):
        target = Path(path)
        if not target.exists():
            files[str(name)] = {"present": False, "sha256": None}
            continue
        digest = hashlib.sha256()
        with target.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
        files[str(name)] = {
            "present": True,
            "sha256": digest.hexdigest(),
            "size": target.stat().st_size,
        }
    payload = {"files": files}
    return {**payload, "input_hash": _hash(payload)}


def git_code_ref(repo: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=Path(repo), check=False, capture_output=True, text=True)
    ref = completed.stdout.strip()
    if completed.returncode != 0 or len(ref) != 40:
        raise WorldReadSetError(
            "formal replay needs a measurable 40-character git code ref")
    return ref


def replay_input_identity(corpus: Path, membership: Path) -> dict:
    return file_identity({
        "market_history.db": Path(corpus) / "market_history.db",
        "market_snapshots.db": Path(corpus) / "market_snapshots.db",
        "stocks.db": Path(corpus) / "stocks.db",
        "sector_membership": Path(membership),
    })


def fact_ref(payload: dict, *, available_at: str | None = None,
             point_in_time_grade: str = "A",
             strict_replay_eligible: bool = True) -> dict:
    """Content-address one consumed fact without mutating the source row."""
    body = dict(payload)
    ref = {
        **body,
        "content_hash": _hash(body),
        "point_in_time_grade": point_in_time_grade,
        "strict_replay_eligible": bool(strict_replay_eligible),
    }
    if available_at is not None:
        ref["available_at"] = str(available_at)
    return ref
