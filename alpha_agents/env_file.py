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


def load_env(env_path: Path | None = None) -> int:
    """Load ``.env`` into ``os.environ``. Returns how many keys it set.

    ``setdefault`` rather than assignment: an exported variable is a fresher
    statement than a file, and a caller who exported a key to point one run at
    a different provider must win over the file's value.

    A missing file returns 0 and is not an error — a deployment that injects
    its environment has no ``.env`` and is not misconfigured.
    """
    path = env_path if env_path is not None else PROJECT_ROOT / ".env"
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
