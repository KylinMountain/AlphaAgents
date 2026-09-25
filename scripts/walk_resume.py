"""Resume a walk-forward replay that stopped — out of credit, a crash, a kill.

A replay's state is not snapshotted; it is *re-derived*. Every model call the
stopped run made is in its journal (``llm_journal/<run>.jsonl``), so this:

1. sets the journal (and the replay's own news index) aside,
2. rebuilds the sandbox fresh with ``walk_bootstrap --force``,
3. puts them back,
4. re-runs the same command with ``ALPHAAGENTS_LLM_MODE=resume``.

The re-run answers every recorded call from the journal — checked request by
request, so the rebuilt book, handbook and reviews are the ones the run had —
and at the first call the journal cannot answer it goes back to the provider
and keeps recording. Replaying the recorded part costs no tokens and takes
minutes. A request that differs from the recording (the code or the data
changed since) stops the resume with the difference named, rather than
splicing two different runs together.

Usage
-----
    .venv/bin/python scripts/walk_resume.py --target /path/to/replay-dir
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUN_ARGS = "run-args.json"
#: What survives the rebuild: the run's recording and its own news index.
KEEP = ("llm_journal", "chroma")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would run, change nothing")
    args = parser.parse_args(argv)
    target = args.target.expanduser().resolve()

    if (target / "branch-origin.json").exists():
        print("This is an independent branch. Do not rebuild it with the parent's "
              "recording; create a new trial from the sealed checkpoint.")
        return 1
    saved = target / RUN_ARGS
    if not saved.exists():
        print(f"{saved} is missing: this directory was not run by a walk_forward "
              "that saves its arguments, so there is nothing to resume.")
        return 1
    run_argv = json.loads(saved.read_text(encoding="utf-8"))["argv"]
    journals = sorted((target / "llm_journal").glob("*.jsonl"))
    journals = [j for j in journals if ".resume-tail-" not in j.name]
    if not journals:
        print(f"No recording under {target / 'llm_journal'}; start the run again "
              "instead — there is nothing to resume from.")
        return 1
    calls = sum(1 for j in journals for line in j.open(encoding="utf-8") if line.strip())
    print(f"Resuming {target}: {calls} recorded call(s) will be answered from the "
          f"journal, then the provider takes over.")
    env = dict(os.environ, ALPHAAGENTS_DATA_DIR=str(target), ALPHAAGENTS_LLM_MODE="resume")
    cmd = [sys.executable, str(PROJECT_ROOT / "scripts" / "walk_forward.py"), *run_argv]
    if args.dry_run:
        print("would run:", " ".join(cmd))
        return 0

    with tempfile.TemporaryDirectory(prefix="walk-resume-", dir=target.parent) as tmp:
        for name in KEEP:
            if (target / name).exists():
                shutil.move(str(target / name), str(Path(tmp) / name))
        boot = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scripts" / "walk_bootstrap.py"),
             "--target", str(target), "--force"], capture_output=True, text=True)
        for name in KEEP:
            if (Path(tmp) / name).exists():
                if (target / name).exists():
                    shutil.rmtree(target / name)
                shutil.move(str(Path(tmp) / name), str(target / name))
        if boot.returncode != 0:
            print(boot.stdout, boot.stderr)
            return boot.returncode
    saved.write_text(json.dumps({"argv": run_argv}, ensure_ascii=False), encoding="utf-8")
    return subprocess.call(cmd, env=env, cwd=PROJECT_ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
