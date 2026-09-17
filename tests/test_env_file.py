"""D41: the provider was ambient, so the same command was not the same run.

A walk-forward launched with a command line that had worked an hour earlier
died on `Missing credentials`. `main.py` loads `.env`; the scripts under
`scripts/` did not, so the runner could not reach a provider at all and the
command said nothing about it.

These tests pin the loader and, more importantly, the import-order rule it
exists for: it must run *before* `alpha_agents.config` is imported, because
`config` freezes every provider constant at import time.
"""

from __future__ import annotations

import os

from alpha_agents import env_file


class TestItLoadsAFile:
    def test_it_sets_the_keys(self, tmp_path, monkeypatch):
        env = tmp_path / ".env"
        env.write_text("ALPHAAGENTS_TEST_KEY=hello\n", encoding="utf-8")
        monkeypatch.delenv("ALPHAAGENTS_TEST_KEY", raising=False)
        assert env_file.load_env(env) == 1
        assert os.environ["ALPHAAGENTS_TEST_KEY"] == "hello"
        monkeypatch.delenv("ALPHAAGENTS_TEST_KEY", raising=False)

    def test_an_exported_variable_wins(self, tmp_path, monkeypatch):
        """An exported value is a fresher statement than a file: a caller who
        exported a key to point one run at a different provider must win."""
        env = tmp_path / ".env"
        env.write_text("ALPHAAGENTS_TEST_KEY=from_file\n", encoding="utf-8")
        monkeypatch.setenv("ALPHAAGENTS_TEST_KEY", "from_shell")
        env_file.load_env(env)
        assert os.environ["ALPHAAGENTS_TEST_KEY"] == "from_shell"

    def test_a_missing_file_is_not_an_error(self, tmp_path):
        """A deployment that injects its environment has no `.env` and is not
        misconfigured."""
        assert env_file.load_env(tmp_path / "nope.env") == 0

    def test_comments_and_blanks_are_skipped(self, tmp_path, monkeypatch):
        env = tmp_path / ".env"
        env.write_text(
            "# a comment\n\n  \nALPHAAGENTS_TEST_KEY=v\n#ANOTHER=x\n",
            encoding="utf-8")
        monkeypatch.delenv("ALPHAAGENTS_TEST_KEY", raising=False)
        assert env_file.load_env(env) == 1
        monkeypatch.delenv("ALPHAAGENTS_TEST_KEY", raising=False)

    def test_a_line_without_an_equals_is_skipped(self, tmp_path, monkeypatch):
        env = tmp_path / ".env"
        env.write_text("NOT A KEY VALUE\nALPHAAGENTS_TEST_KEY=v\n",
                       encoding="utf-8")
        monkeypatch.delenv("ALPHAAGENTS_TEST_KEY", raising=False)
        assert env_file.load_env(env) == 1
        monkeypatch.delenv("ALPHAAGENTS_TEST_KEY", raising=False)


class TestTheLoaderDoesNotFreezeConfig:
    def test_importing_it_does_not_pull_in_config(self):
        """The whole reason it is not defined in `config`: importing the
        loader must not evaluate the constants it is meant to precede."""
        import subprocess
        import sys
        code = (
            "import sys; from alpha_agents.env_file import load_env; "
            "print('alpha_agents.config' in sys.modules)"
        )
        out = subprocess.run([sys.executable, "-c", code],
                             capture_output=True, text=True,
                             cwd=str(env_file.PROJECT_ROOT))
        assert out.stdout.strip() == "False", (
            "importing the env loader pulled in config, which freezes the "
            "provider constants before .env is read — D41")


class TestTheScriptsLoadIt:
    def test_walk_forward_loads_env_before_config(self):
        """The regression itself. A grep rather than a run, because the
        failure only appears with a real provider configured."""
        text = (env_file.PROJECT_ROOT / "scripts" / "walk_forward.py").read_text(
            encoding="utf-8")
        assert "from alpha_agents.env_file import load_env" in text
        loader_at = text.index("from alpha_agents.env_file import load_env")
        config_at = text.index("from alpha_agents.config import")
        assert loader_at < config_at, (
            "walk_forward imports config before loading .env, so the provider "
            "constants are frozen from the ambient environment — D41")
