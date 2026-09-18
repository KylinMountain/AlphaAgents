#!/usr/bin/env python
"""给一个回放账本装上它那一臂的 policy，让 ON/OFF 在账本上可区分。

## 为什么需要它

反事实配对的键之一是 `decision_snapshots.policy_ref`：同一个
`(trader, information_cutoff, code)` 下，两个**不同**的 ref 才构成一对。

回放账本里这个列**天然是 NULL**，因为 `attribution.freeze()` 默认取
`policy_registry.active_ref()`，而回放库从来没 `install` 过任何版本
（`walk_forward` 整个文件不引用 policy_registry）。实测：5 天窗口的
5 条决策快照 `policy_ref` 全是 NULL，于是
`counterfactual_changes()` 按 ref 分组只有一组，
连"两个不同 policy"这个前提都不成立。

所以 ON/OFF 两臂不是"跑两次同一个库"，而是**两个独立账本各装一个版本**
（M3 §8.6：同一 mandate、同一机会面板、同一前向窗口，账本各自独立）。
本脚本负责其中"装版本"这一步。

## 两种用法

    # OFF 臂：装上基线版本
    python scripts/replay_policy.py --data-dir /tmp/off \
        --install-baseline

    # ON 臂：把某个 candidate 构成的 variant 装进另一个账本
    python scripts/replay_policy.py --data-dir /tmp/on \
        --install-candidate 42

## 它不做什么

**不 promote。** `install` 只在"指针为空"时可用，所以每臂各自是首装——
这正是回放该有的形状：一次实验不改变生产的在效版本，也不需要 gate 证据，
因为它不声称"比谁更好"，只是"这一臂跑的是这个版本"。

生产库里指针非空，此脚本会拒绝操作——回放的政策不能写进生产。
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alpha_agents.data import policy_registry as PR  # noqa: E402
from alpha_agents.data import scoring  # noqa: E402

logger = logging.getLogger(__name__)


def _bind(data_dir: Path) -> None:
    """Point the stores at ``data_dir`` and drop cached connections.

    Late binding on purpose: every store reads `config.DATA_DIR` at *call*
    time (that is what makes `ALPHAAGENTS_DATA_DIR` work), but each also keeps
    a thread-local connection, so a previously opened one would still point at
    the old file.
    """
    import alpha_agents.config as config
    config.DATA_DIR = data_dir
    from alpha_agents.data import memory_store
    memory_store.MEMORY_DB_PATH = data_dir / "memory.db"
    memory_store._local.conn = None


def _guard_not_production(data_dir: Path) -> None:
    """Refuse to install a policy into the live book.

    A replay policy is a statement about an experiment, not about what the
    system runs. Installing it into production would move the pointer the
    trading path reads — a behaviour change no gate approved.
    """
    import alpha_agents.config as config
    live = Path(getattr(config, "PROJECT_DATA_DIR", None) or
                Path(__file__).resolve().parent.parent / "data")
    try:
        same = data_dir.resolve() == live.resolve()
    except OSError:
        same = False
    if same:
        raise SystemExit(
            f"{data_dir} is the production data directory. This script "
            "installs a policy for a replay arm; doing it to the live book "
            "would change what production trades without a gate.")


def _import_candidate(data_dir: Path, candidate_id: int,
                      from_dir: Path) -> int:
    """Copy a candidate from the production book into the replay's.

    The two arms have independent books by design, and a candidate is part of
    a book — so an ON arm asked to replay "what if candidate #4 were in force"
    needs that candidate *in its own* book before it can be built into a
    variant. Copying is explicit and audited rather than reading across books:
    a variant built from a foreign row would be a decision whose evidence
    lives somewhere else, which is the attribution failure this repository
    spends effort avoiding.

    Returns the new candidate id in ``data_dir``.
    """
    from alpha_agents.data import learning_candidates as LC

    src = sqlite3.connect(f"file:{from_dir / 'memory.db'}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    try:
        row = src.execute("SELECT * FROM learning_candidates WHERE id = ?",
                          (candidate_id,)).fetchone()
        if row is None:
            raise SystemExit(
                f"no candidate #{candidate_id} in {from_dir}")
        already = LC.get_candidate(candidate_id)
        if already is not None:
            return candidate_id
        return LC.save_candidate(
            entity_type=row["entity_type"], operation=row["operation"],
            source=f"imported from {from_dir.name}",
            source_date=row["source_date"],
            payload=json.loads(row["payload_json"] or "{}"),
            claim=row["claim"], applicable_context=row["applicable_context"],
            proposed_behavior_delta=json.loads(
                row["proposed_behavior_delta"] or "{}"),
            evidence_episode_ids=json.loads(row["evidence_episode_ids"])
            if row["evidence_episode_ids"] else None,
        )
    finally:
        src.close()


def _version_for_candidate(candidate_id: int, *, parent_version_id: int | None,
                           built_by: str, data_dir: Path,
                           from_dir: Path | None) -> int:
    """Build (or reuse) the variant a candidate proposes."""
    from alpha_agents.evolution import variant as V
    from alpha_agents.data import learning_candidates as LC

    if LC.get_candidate(candidate_id) is None:
        if from_dir is None:
            raise SystemExit(
                f"no learning candidate #{candidate_id} in {data_dir}, and no "
                "--candidate-from given. A replay arm's book is independent, "
                "so the candidate has to be in it.")
        candidate_id = _import_candidate(data_dir, candidate_id, from_dir)

    parent = parent_version_id
    if parent is None:
        active = PR.active_version()
        if active is None:
            raise SystemExit(
                "no version in force to vary, and no --parent given. Build "
                "the baseline first (--install-baseline).")
        parent = int(active["id"])
    built = V.build_variant(candidate_id, parent_version_id=parent,
                            built_by=built_by, write_file=False)
    return int(built.version_id)


def install(data_dir: Path, *, baseline: bool = False,
            candidate_id: int | None = None,
            parent_version_id: int | None = None,
            candidate_from: Path | None = None,
            actor: str = "replay", reason: str = "") -> dict:
    """Put one policy in force in a replay book, once.

    Order matters and is easy to get wrong: a variant is built **against a
    parent version**, and ``install`` refuses when a pointer already exists.
    So a candidate arm freezes its parent (a record, no pointer) and then
    installs only the variant. Installing the baseline first and building
    afterwards would leave the pointer on the parent and refuse the variant —
    the arm would silently run the incumbent and the ON/OFF pair would be two
    copies of OFF.
    """
    from alpha_agents.evolution import policy_sources as PS

    _guard_not_production(data_dir)
    _bind(data_dir)

    already = PR.active_version()
    if already is not None:
        raise SystemExit(
            f"this book already has version #{already['id']} in force. A "
            "replay arm installs once, at the start; moving the pointer "
            "mid-window would make one run two policies.")

    if baseline:
        version_id = PS.freeze_live(
            created_by=actor,
            reason=reason or "replay arm: the incumbent configuration",
        )
        parent_version_id = None
    else:
        if candidate_id is None:
            raise SystemExit("pass --install-baseline or --install-candidate N")
        # A parent to vary: the caller's, or a freshly frozen record of the
        # current configuration. **Frozen only** — freezing is a record,
        # installing is the pointer, and the pointer must go to the variant.
        parent = parent_version_id
        if parent is None:
            parent = PS.freeze_live(
                created_by=actor,
                reason="replay arm: the parent this variant varies",
            )
        version_id = _version_for_candidate(
            candidate_id, parent_version_id=parent, built_by=actor,
            data_dir=data_dir, from_dir=candidate_from)

    PR.install(version_id=version_id, actor=actor,
               reason=reason or f"replay arm: version #{version_id}")
    return {
        "version_id": version_id,
        "installed": True,
        "active_ref": PR.active_ref(),
        "decision_params": scoring.in_force_decision_params().get("theme_gate"),
        "parent_version_id": parent_version_id,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", type=Path, required=True)
    ap.add_argument("--install-baseline", action="store_true",
                    help="freeze the current configuration and install it")
    ap.add_argument("--install-candidate", type=int, default=None,
                    help="build this candidate's variant and install it")
    ap.add_argument("--parent", type=int, default=None,
                    help="the version the variant varies (default: in force)")
    ap.add_argument("--candidate-from", type=Path, default=None,
                    help="copy the candidate from this data dir first (the "
                         "arms' books are independent, so an ON arm needs its "
                         "own copy to build a variant from)")
    ap.add_argument("--actor", default="replay")
    ap.add_argument("--reason", default="")
    ap.add_argument("--show", action="store_true",
                    help="print what is in force, install nothing")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")

    _guard_not_production(args.data_dir)
    _bind(args.data_dir)

    if args.show:
        v = PR.active_version()
        print(json.dumps({
            "active_ref": PR.active_ref(),
            "version_id": int(v["id"]) if v else None,
            "theme_gate": scoring.in_force_decision_params().get("theme_gate"),
        }, ensure_ascii=False, indent=2))
        return 0

    result = install(args.data_dir, baseline=args.install_baseline,
                     candidate_id=args.install_candidate,
                     parent_version_id=args.parent,
                     candidate_from=args.candidate_from,
                     actor=args.actor, reason=args.reason)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
