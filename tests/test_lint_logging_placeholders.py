"""A mapping placeholder needs a mapping, and only the lint can see it.

`logger.debug("... %(code)s", e)` reads as an ordinary lazy log line. It raises
`TypeError: format requires a mapping` the first time the handler runs — the one
moment nobody tests, because a handler that fires is already a bad day.

Four of these were produced by a batch rewrite of the silent `except`s (2026-09-14)
and only caught because the rewrite script checked each message's placeholders
against its arguments by hand. That check belongs somewhere it runs every time,
so it is a lint rule now, and these tests are its teeth.
"""

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from lint_harness import check_logging  # noqa: E402


def problems(source: str) -> list[str]:
    return [v.problem for v in check_logging(Path("probe.py"), ast.parse(source))]


class TestCatchesTheMismatch:
    def test_mapping_placeholder_with_a_positional_argument(self):
        """The exact shape the batch rewrite produced."""
        src = ('import logging\nlogger = logging.getLogger(__name__)\n'
               'def f(code):\n'
               '    try:\n'
               '        raise ValueError\n'
               '    except ValueError as e:\n'
               '        logger.debug("failed for %(code)s: %s", e)\n')
        assert problems(src) == ["映射式占位符 %(name)s 配的是位置参数"]

    def test_mapping_placeholder_with_no_arguments_at_all(self):
        src = ('import logging\nlogger = logging.getLogger(__name__)\n'
               'logger.info("a %(thing)s happened")\n')
        assert problems(src) == ["映射式占位符 %(name)s 配的是位置参数"]

    def test_mapping_placeholder_with_several_positional_arguments(self):
        src = ('import logging\nlogger = logging.getLogger(__name__)\n'
               'def f(code, e):\n'
               '    logger.error("failed for %(code)s: %s", code, e)\n')
        assert problems(src) == ["映射式占位符 %(name)s 配的是位置参数"]


class TestNoFalsePositives:
    def test_positional_placeholders(self):
        src = ('import logging\nlogger = logging.getLogger(__name__)\n'
               'def f(code, e):\n'
               '    logger.debug("failed for %s: %s", code, e)\n')
        assert problems(src) == []

    def test_a_dict_argument(self):
        src = ('import logging\nlogger = logging.getLogger(__name__)\n'
               'def f(code):\n'
               '    logger.debug("failed for %(code)s", {"code": code})\n')
        assert problems(src) == []

    def test_a_plain_message_with_no_placeholders(self):
        src = ('import logging\nlogger = logging.getLogger(__name__)\n'
               'logger.info("archive complete")\n')
        assert problems(src) == []

    def test_an_f_string_is_a_different_problem(self):
        """The older rule still fires, on its own message."""
        src = ('import logging\nlogger = logging.getLogger(__name__)\n'
               'def f(code):\n'
               '    logger.info(f"failed for {code}")\n')
        assert problems(src) == ["日志用了 f-string"]

    def test_a_percent_expression_outside_a_logger_call(self):
        src = 'TEMPLATE = "failed for %(code)s"\n'
        assert problems(src) == []
