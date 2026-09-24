"""Read ``.env`` into ``os.environ``, before anything freezes a constant.

This lives in its own module rather than in ``config`` for one reason, and it
is an import-order reason: ``config`` evaluates every provider constant at
import time, so importing ``config`` to reach a loader defined inside it
freezes those constants *first* and the loader runs too late to matter.

The failure that made this concrete is D41. A walk-forward launched with a
command that had worked an hour earlier died on

    openai.OpenAIError: Missing credentials. Please pass an `api_key` ...

because ``main.py`` loads ``.env`` and the scripts under ``scripts/`` did not.
The runner could not reach a provider at all, and the command line said
nothing about it.

So the rule is: **call :func:`load_env` before importing ``config``.** A
caller that imports ``config`` first gets the constants from whatever the
ambient environment held, which is the defect this module exists to prevent.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def default_path() -> Path:
    """``$ALPHAAGENTS_ENV_FILE``, else ``<repo>/.env``.

    The override exists for containers: docker's ``env_file`` injects values
    but leaves no file behind, so a settings page saving to ``/app/.env``
    would write a file no process reads. Mount the host's ``.env`` and point
    this at it, and the scheduler and the page read and write the same one.
    """
    raw = os.environ.get("ALPHAAGENTS_ENV_FILE")
    return Path(raw) if raw else PROJECT_ROOT / ".env"


def load_env(env_path: Path | None = None) -> int:
    """Load ``.env`` into ``os.environ``. Returns how many keys it set.

    ``setdefault`` rather than assignment: an exported variable is a fresher
    statement than a file, and a caller who exported a key to point one run at
    a different provider must win over the file's value.

    A missing file returns 0 and is not an error — a deployment that injects
    its environment has no ``.env`` and is not misconfigured.
    """
    path = env_path if env_path is not None else default_path()
    if not path.exists():
        return 0
    set_count = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = value.strip()
        set_count += 1
    if set_count:
        logger.debug("Loaded %d key(s) from %s", set_count, path)
    return set_count


def _key_of(line: str) -> str | None:
    s = line.strip()
    if not s or s.startswith("#") or "=" not in s:
        return None
    return s.partition("=")[0].strip() or None


def read_env(env_path: Path | None = None) -> dict[str, str]:
    """The file's active ``KEY=value`` lines. The first wins, as in :func:`load_env`."""
    path = env_path if env_path is not None else default_path()
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key = _key_of(line)
        if key and key not in out:
            out[key] = line.partition("=")[2].strip()
    return out


#: Heading written above keys the settings page adds to a file that lacked them.
APPENDED_HEADER = "# ── 由设置页写入 ──"


def update_env(updates: dict[str, str | None], env_path: Path | None = None) -> Path:
    """Rewrite ``.env`` in place: set, add or (``None``) remove keys.

    Every other line — comments, blank lines, order — is kept as it was. A
    key's first active line is replaced; any later active line for the same
    key is dropped, because :func:`load_env` never reads it and leaving it
    would show a value that is not the one in effect. New keys go at the end
    under :data:`APPENDED_HEADER`.

    The previous file is kept as ``.env.bak-settings`` and the file's mode is
    preserved (it holds secrets; ``chmod 600`` must survive a save).
    """
    path = env_path if env_path is not None else default_path()
    for key, value in updates.items():
        if not key or not key.replace("_", "").isalnum() or not key[0].isalpha():
            raise ValueError(f"not an environment variable name: {key!r}")
        if value is not None and ("\n" in value or "\r" in value):
            raise ValueError(f"{key}: a value cannot span lines")
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    lines, seen = [], set()
    for line in old.splitlines():
        key = _key_of(line)
        if key in updates:
            if key in seen:
                continue
            seen.add(key)
            if updates[key] is not None:
                lines.append(f"{key}={updates[key]}")
            continue
        lines.append(line)
    new = [k for k, v in updates.items() if k not in seen and v is not None]
    if new:
        if APPENDED_HEADER not in lines:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append(APPENDED_HEADER)
        lines += [f"{k}={updates[k]}" for k in new]
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    if path.exists():
        backup = path.with_name(path.name + ".bak-settings")
        backup.write_text(old, encoding="utf-8")
        os.chmod(backup, mode)
    tmp = path.with_name(path.name + ".tmp-settings")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(tmp, mode)
    os.replace(tmp, path)
    logger.info("Settings page updated %d key(s) in %s", len(updates), path)
    return path
