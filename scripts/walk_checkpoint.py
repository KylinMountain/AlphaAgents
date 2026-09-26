"""Sealed end-of-session state for continuous walk-forward experiments.

Only the runner creates checkpoints after completing a clean window. SQLite's
backup API includes committed WAL pages; the manifest is published last. Files
are content-addressed, not authenticated. This is a private research artifact,
not a point-in-time data certificate or a hostile-process security boundary.
"""
from __future__ import annotations

from contextlib import closing
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ("market_history.db", "market_snapshots.db", "stocks.db")
STATE = ("memory.db", "traders", "chroma", "variants")
MANIFEST = "checkpoint.json"
VERSION = 1


class CheckpointError(ValueError):
    """A checkpoint cannot substantiate the requested continuation."""


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(path: Path, value) -> None:
    with path.open("x", encoding="utf-8") as f:
        f.write(canonical(value) + "\n")
        f.flush()
        os.fsync(f.fileno())


def source_identity(root: Path = ROOT) -> str:
    """All application/runner sources, prompts, trader YAMLs and dependency lock."""
    files = []
    for directory, suffixes in (("alpha_agents", {".py", ".md"}),
                                ("scripts", {".py"}), ("traders", {".yaml", ".yml"})):
        files += [p for p in (root / directory).rglob("*") if p.suffix in suffixes]
    files += [root / n for n in ("pyproject.toml", "uv.lock")]
    return digest({p.relative_to(root).as_posix(): file_hash(p) for p in sorted(files)})


def runtime_identity() -> str:
    """Hash effective config and declared model identity, never persist API keys."""
    from alpha_agents import config
    from alpha_agents.model_factory import model_identity
    values = {}
    for key, value in vars(config).items():
        if not key.isupper() or any(s in key for s in ("KEY", "SECRET", "TOKEN", "PASSWORD")):
            continue
        if isinstance(value, (str, int, float, bool, tuple, list, dict)) or value is None:
            try:
                canonical(value)
            except (TypeError, ValueError):
                continue
            values[key] = value
    import sys
    from importlib.metadata import version, PackageNotFoundError
    values["python"] = list(sys.version_info[:3])
    values["dependencies"] = {}
    for package in ("openai", "openai-agents", "pydantic", "numpy", "pandas", "httpx"):
        try:
            values["dependencies"][package] = version(package)
        except PackageNotFoundError:
            values["dependencies"][package] = None
    values["declared_agent"] = model_identity()
    # Storage/run identity and credentials are intentionally not behavior. Other
    # model roles and research budgets may read environment variables directly.
    values["environment"] = {k: v for k, v in os.environ.items()
                              if k.endswith(("_MODEL", "_BASE_URL")) or
                              k.startswith(("RESEARCH_", "TRADABLE_"))
                              or k in {"TOOL_TIMEOUT"}}
    values["branch_retry_policy"] = {"logical_attempts": 1, "transport_retries": 0,
                                      "client_failover": False}
    return digest(values)


def _relative(name: str) -> Path:
    if not isinstance(name, str) or "\\" in name:
        raise CheckpointError("Invalid checkpoint path")
    path = Path(name)
    if path.is_absolute() or not path.parts or any(x in ("..", ".") for x in path.parts):
        raise CheckpointError("Checkpoint paths must be relative and contained")
    return path


def _regular(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise CheckpointError(f"Expected a regular private file: {path}")


def _state_files(source: Path) -> list[Path]:
    out = []
    for name in STATE:
        path = source / name
        if path.is_symlink():
            raise CheckpointError(f"Private state may not be linked: {path}")
        if not path.exists():
            continue
        for item in ([path] if path.is_file() else sorted(path.rglob("*"))):
            if item.is_symlink():
                raise CheckpointError(f"Private state may not be linked: {item}")
            if item.is_dir():
                continue
            _regular(item)
            if not item.name.endswith(("-wal", "-shm", "-journal")):
                out.append(item)
    if source / "memory.db" not in out:
        raise CheckpointError("No private memory.db at the checkpoint boundary")
    return out


def _stamp(paths: list[Path]) -> dict:
    result = {}
    for path in paths:
        path = path.resolve()
        for p in (path, Path(str(path) + "-wal"), Path(str(path) + "-journal")):
            if p.exists():
                s = p.stat()
                result[str(p)] = (s.st_ino, s.st_size, s.st_mtime_ns)
    return result


def _copy_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as f:
        database = f.read(16) == b"SQLite format 3\x00"
    if database:
        with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as src:
            with closing(sqlite3.connect(target)) as dst:
                started = time.monotonic()
                def progress(status, remaining, total):
                    if time.monotonic() - started > 120:
                        raise CheckpointError("SQLite backup deadline exceeded")
                src.backup(dst, pages=256, progress=progress)
                dst.execute("PRAGMA journal_mode=DELETE")
                if dst.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise CheckpointError("Invalid SQLite backup")
    else:
        shutil.copyfile(source, target)
    target.chmod(0o600)


def capture(source: Path, destination: Path, *, metadata: dict) -> dict:
    """Copy quiescent runner state and pin corpus once; publish manifest last."""
    source, destination = source.resolve(strict=True), destination.absolute()
    if destination.resolve().is_relative_to(source) or source.is_relative_to(destination.resolve()):
        raise CheckpointError("Checkpoint and source must not contain one another")
    if metadata.get("source_hash") != source_identity():
        raise CheckpointError("Source changed while the prefix was running")
    paths = _state_files(source)
    corpora = [source / n for n in CORPUS if (source / n).exists()]
    if not (source / "market_history.db").exists():
        raise CheckpointError("A pinned trading calendar is required")
    inputs = metadata.get("input_paths", {})
    input_files = [Path(p).resolve(strict=True) for p in inputs.values()]
    before = _stamp(paths + corpora + input_files)
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    for path in paths:
        _copy_file(path, destination / "state" / path.relative_to(source))
    for path in corpora:
        _copy_file(path, destination / "corpus" / path.name)
    for key, path in inputs.items():
        _copy_file(Path(path).resolve(strict=True), destination / "inputs" / _relative(key))
    if (before != _stamp(paths + corpora + input_files) or paths != _state_files(source)
            or corpora != [source / n for n in CORPUS if (source / n).exists()]):
        raise CheckpointError("Source changed during checkpoint capture; artifact is incomplete")
    if metadata["source_hash"] != source_identity():
        raise CheckpointError("Code changed during checkpoint capture")
    files = {p.relative_to(destination).as_posix(): file_hash(p)
             for p in sorted(destination.rglob("*")) if p.is_file()}
    body = {"version": VERSION, **metadata, "files": files}
    body.pop("input_paths", None)
    manifest = {**body, "checkpoint_id": digest(body)}
    for name in files:
        (destination / name).chmod(0o400)
    write_json(destination / MANIFEST, manifest)
    (destination / MANIFEST).chmod(0o400)
    return manifest


def verify(root: Path, *, check_source: bool = True) -> dict:
    root = root.resolve(strict=True)
    _regular(root / MANIFEST)
    value = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("files"), dict):
        raise CheckpointError("Invalid checkpoint manifest shape")
    body = {k: v for k, v in value.items() if k != "checkpoint_id"}
    if value.get("version") != VERSION or digest(body) != value.get("checkpoint_id"):
        raise CheckpointError("Checkpoint manifest hash/version mismatch")
    if check_source and value.get("source_hash") != source_identity():
        raise CheckpointError("Run this checkpoint with the exact recorded source and lockfile")
    actual = set()
    for p in root.rglob("*"):
        if p.is_symlink():
            raise CheckpointError("A sealed checkpoint cannot contain symlinks")
        if p.is_file() and p != root / MANIFEST:
            actual.add(p.relative_to(root).as_posix())
    if actual != set(value["files"]):
        raise CheckpointError("Checkpoint file set changed")
    for name, expected in value["files"].items():
        path = root / _relative(name)
        _regular(path)
        if file_hash(path) != expected:
            raise CheckpointError(f"Checkpoint content changed: {name}")
    if "state/memory.db" not in actual or "corpus/market_history.db" not in actual:
        raise CheckpointError("Checkpoint is missing its account or calendar")
    for key in ("completed_through", "next_session"):
        if date.fromisoformat(value[key]).isoformat() != value[key]:
            raise CheckpointError("Noncanonical session date")
    if value["next_session"] <= value["completed_through"]:
        raise CheckpointError("Continuation must advance the calendar")
    return value


def materialize(checkpoint: Path, target: Path, *, branch_id: str) -> dict:
    value = verify(checkpoint)
    checkpoint, target = checkpoint.resolve(), target.absolute()
    if target.resolve().is_relative_to(checkpoint) or checkpoint.is_relative_to(target.resolve()):
        raise CheckpointError("A branch may not contain or overwrite its checkpoint")
    target.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name in value["files"]:
        path = _relative(name)
        if path.parts[0] == "state":
            dest = target.joinpath(*path.parts[1:])
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(checkpoint / path, dest)
            dest.chmod(0o600)
    for name in CORPUS:
        path = checkpoint / "corpus" / name
        if path.exists():
            (target / name).symlink_to(path)
    write_json(target / "branch-origin.json", {
        "branch_id": branch_id, "checkpoint_id": value["checkpoint_id"],
        "completed_through": value["completed_through"],
        "next_session": value["next_session"]})
    return value


def validate_runner(args) -> None:
    """Restrict the first continuation adapter rather than silently drop inputs."""
    for key in ("experiment_manifest", "frozen_directions"):
        if getattr(args, key, None) is not None:
            raise CheckpointError("Formal/frozen-direction experiment continuations are not supported")
    if getattr(args, "keep_going", False):
        raise CheckpointError("Checkpoint/branch runs must stop on decision failures")
    if getattr(args, "days", 0) < 1:
        raise CheckpointError("A checkpoint needs at least one session")
    destination = getattr(args, "checkpoint_out", None)
    if destination is not None:
        from alpha_agents.config import DATA_DIR
        source, destination = Path(DATA_DIR).resolve(), Path(destination).absolute()
        if destination.exists() or destination.is_symlink():
            raise CheckpointError("Checkpoint destination already exists")
        if (destination.resolve().is_relative_to(source)
                or source.is_relative_to(destination.resolve())):
            raise CheckpointError("Checkpoint and source must not contain one another")
    args.no_keep_notes = True
    if (getattr(args, "checkpoint_out", None) and args.decider == "llm"
            and os.environ.get("ALPHAAGENTS_LLM_MODE") != "record"):
        raise CheckpointError("An LLM checkpoint prefix requires fresh record mode")


def assert_initial_account(actual: dict, expected: dict) -> None:
    """Reconcile the restored mark before any continuation decision or fill."""
    import math
    for key in ("cash", "invested", "market_value", "realized", "unrealized", "equity",
                "open_positions", "pending_orders"):
        left, right = actual.get(key), expected.get(key)
        if (type(left) not in (int, float) or type(right) not in (int, float)
                or not math.isfinite(left) or not math.isfinite(right)
                or abs(left - right) > 0.01):
            raise CheckpointError(f"Restored account differs at {key}")


def require_fresh_prefix(source: Path) -> None:
    """Do not certify a reused sandbox containing later observations."""
    if (source / "branch-origin.json").exists():
        raise CheckpointError("Use a fresh bootstrap directory for a new prefix")
    for name in STATE[1:]:
        path = source / name
        if path.is_symlink() or path.is_file() or (path.exists() and any(path.rglob("*"))):
            raise CheckpointError("A prefix must not inherit unverified private state")
    db = source / "memory.db"
    _regular(db)
    with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for name in tables:
            quoted = '"' + name.replace('"', '""') + '"'
            if conn.execute(f"SELECT 1 FROM {quoted} LIMIT 1").fetchone():
                raise CheckpointError("A prefix must start with fresh state, not an old run")


def restore_runtime(ctx, value: dict) -> None:
    from alpha_agents.evolution.review import Finding
    if ctx.corpus.previous(ctx.args.start) != value["completed_through"]:
        raise CheckpointError("A branch must start at the next session, not replay or skip a day")
    if ctx.args.start != value["next_session"]:
        raise CheckpointError("Continuation start differs from the checkpoint")
    if runtime_identity() != value["runtime_hash"]:
        raise CheckpointError("Effective model/config differs from the checkpoint")
    state = value["runtime_state"]
    ctx.window_start = state["window_start"]
    ctx.exposure_log = state["exposure_log"]
    ctx.pending_signals = state["pending_signals"]
    ctx.review = [Finding(**row) for row in state["review"]]
    ctx.branch_intervention = getattr(ctx.args, "branch_intervention", None)
    ctx.branch_intervention_used = False
    ctx.branch_start = value["next_session"]
    ctx.review_lineage = (value["parent_run_id"], ctx.run_id)


def require_stable_membership(ctx, path: Path) -> None:
    """Do not seal new archive bytes for membership loaded at prefix start."""
    expected = ctx.input_identity.get("files", {}).get("sector_membership", {}).get("sha256")
    if not expected or file_hash(path) != expected:
        raise CheckpointError("Membership archive changed since the prefix started")


def capture_result(ctx, args, result: dict, *, source_hash: str, runtime_hash: str) -> dict:
    from alpha_agents.data.memory_store import _get_conn
    from alpha_agents.model_factory import SWITCHES
    if (result["errors"] or ctx.failed_learning or SWITCHES
            or len(result["equity"]) != len(result["window"])
            or len(result["window"]) != args.days
            or not result["production"]["unchanged"]
            or not result["corpus"]["access"]["read_only"]
            or result["corpus"]["changes"]):
        raise CheckpointError("Only a complete, unchanged-input prefix may create a checkpoint")
    if _get_conn().in_transaction:
        raise CheckpointError("Account has an uncommitted transaction at the checkpoint boundary")
    last = result["window"][-1]
    index = ctx.corpus.index[last]
    if index + 1 >= len(ctx.corpus.days):
        raise CheckpointError("Corpus has no session after the checkpoint")
    if runtime_identity() != runtime_hash:
        raise CheckpointError("Runtime configuration changed during the prefix")
    saved_args = {k: str(v) if isinstance(v, Path) else v
                  for k, v in vars(args).items() if not k.startswith("_")}
    for key in ("target", "out", "checkpoint_out", "continuation", "branch_intervention"):
        saved_args.pop(key, None)
    inputs = {}
    if getattr(args, "sector_membership", None):
        require_stable_membership(ctx, Path(args.sector_membership))
        inputs["sector_membership.json"] = str(args.sector_membership)
        saved_args["sector_membership"] = "sector_membership.json"
    metadata = {
        "source_hash": source_hash, "runtime_hash": runtime_hash,
        "parent_run_id": ctx.run_id, "trader": ctx.trader,
        "completed_through": last, "next_session": ctx.corpus.days[index + 1],
        "args": saved_args, "input_paths": inputs,
        "runtime_state": {"window_start": ctx.window_start,
                          "exposure_log": getattr(ctx, "exposure_log", []),
                          "pending_signals": ctx.pending_signals,
                          "review": [r.as_dict() for r in getattr(ctx, "review", [])]},
        "end_account": result["equity"][-1],
        "limitations": ["Daily/synthetic-close execution, not intraday path truth",
                        "Runner state only; not a live process/complete OS snapshot",
                        "No point-in-time certification of corpus or model training",
                        "Frozen full corpus uses disk space once per checkpoint"],
    }
    from alpha_agents.config import DATA_DIR
    return capture(Path(DATA_DIR), args.checkpoint_out, metadata=metadata)
