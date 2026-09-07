"""The first-run build lock.

The scheduler and the web UI are separate containers sharing data/, and
both call _ensure_index on startup. Deployed together on an empty volume
they raced: two processes ran build_index at once and the loser died on
"database is locked", restarted, and raced again. Observed on the box.
"""

import multiprocessing
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


def _hold_lock(data_dir, order_file, tag, hold_seconds):
    """Take the lock, record entry and exit, so overlap is detectable."""
    import main
    from alpha_agents import config

    config.DATA_DIR = Path(data_dir)
    main.DATA_DIR = Path(data_dir)

    with main._build_lock():
        with open(order_file, "a") as fh:
            fh.write(f"{tag}-enter\n")
        time.sleep(hold_seconds)
        with open(order_file, "a") as fh:
            fh.write(f"{tag}-exit\n")


@pytest.mark.skipif(sys.platform == "win32", reason="flock is POSIX-only")
class TestBuildLock:
    def test_two_processes_do_not_overlap(self, tmp_path):
        """The whole point: the second waits rather than racing."""
        order = tmp_path / "order.txt"
        procs = [
            multiprocessing.Process(
                target=_hold_lock, args=(str(tmp_path), str(order), tag, 0.4),
            )
            for tag in ("A", "B")
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=15)

        events = order.read_text().split()
        assert len(events) == 4

        # Whoever went first must have exited before the other entered.
        first = events[0].split("-")[0]
        assert events[1] == f"{first}-exit", (
            f"锁未生效，出现交错: {events}"
        )

    def test_lock_is_released_after_the_block(self, tmp_path):
        import main
        from alpha_agents import config

        config.DATA_DIR = tmp_path
        main.DATA_DIR = tmp_path

        for _ in range(3):
            with main._build_lock():
                pass          # a stuck lock would hang the second pass

    def test_lock_file_lives_in_the_data_dir(self, tmp_path):
        import main
        from alpha_agents import config

        config.DATA_DIR = tmp_path
        main.DATA_DIR = tmp_path

        with main._build_lock():
            pass
        assert (tmp_path / ".index-build.lock").exists()

    def test_missing_data_dir_is_created(self, tmp_path):
        import main
        from alpha_agents import config

        target = tmp_path / "not-yet"
        config.DATA_DIR = target
        main.DATA_DIR = target

        with main._build_lock():
            pass
        assert target.exists()
