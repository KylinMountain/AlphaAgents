"""The undefined-name rule has to be scope-aware, not file-flat.

Two NameErrors reached production before this rule existed, and a third
slipped past its first version: that version collected every ``arg`` in a
module into one set, so a parameter of any function made the name look
bound everywhere. ``morning_scan`` referenced a ``themes`` local belonging
to a different function and linted clean.
"""

import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from lint_harness import check_undefined_names  # noqa: E402


def names(source: str) -> set[str]:
    tree = ast.parse(source)
    return {
        v.problem.split("用到了 ")[1].split("，")[0]
        for v in check_undefined_names(Path("probe.py"), tree)
    }


class TestCatchesRealBugs:
    def test_name_belonging_to_another_functions_params(self):
        """The exact morning_scan bug."""
        src = (
            "def helper(themes):\n"
            "    return themes\n"
            "def caller():\n"
            "    return build(themes=themes)\n"
            "def build(themes=None):\n"
            "    return themes\n"
        )
        assert names(src) == {"themes"}

    def test_call_without_its_import(self):
        """The exact intraday_monitor bug."""
        src = "def f():\n    return build_decision_context(task='x')\n"
        assert names(src) == {"build_decision_context"}

    def test_module_level_use(self):
        assert names("VALUE = MISSING + 1\n") == {"MISSING"}


class TestNoFalsePositives:
    @pytest.mark.parametrize("src", [
        # Function defined after its use.
        "def a():\n    return b()\ndef b():\n    return 1\n",
        # Nested helper with its own params and locals.
        "import math\n"
        "def outer(rows):\n"
        "    def _safe(value, default):\n"
        "        f = float(value)\n"
        "        return default if math.isnan(f) else f\n"
        "    return [_safe(r, 0.0) for r in rows]\n",
        # Closure over an enclosing local.
        "def outer():\n    v = 1\n    def inner():\n        return v\n    return inner()\n",
        # Comprehension and lambda targets.
        "def f(rows):\n"
        "    return [k for k, v in rows], (lambda x: x + 1)(2)\n",
        # with/except binders.
        "def f(p):\n"
        "    try:\n"
        "        with open(p) as fh:\n"
        "            return fh.read()\n"
        "    except OSError as exc:\n"
        "        return str(exc)\n",
        # Class attributes and methods.
        "class T:\n"
        "    LIMIT = 3\n"
        "    def m(self, x):\n"
        "        return x + T.LIMIT\n",
        # Decorators and defaults evaluate in the enclosing scope.
        "import functools\n"
        "DEFAULT = 1\n"
        "@functools.cache\n"
        "def f(x=DEFAULT):\n"
        "    return x\n",
        # Module dunders.
        "from pathlib import Path\nROOT = Path(__file__).parent\n",
        # Walrus and augmented assignment.
        "def f(xs):\n    total = 0\n    for x in xs:\n        total += x\n    "
        "if (n := total) > 1:\n        return n\n    return 0\n",
        # global / nonlocal.
        "_state = None\n"
        "def set_it(v):\n    global _state\n    _state = v\n",
    ])
    def test_clean(self, src):
        assert names(src) == set()


class TestScopeIsolation:
    def test_sibling_locals_do_not_leak(self):
        src = (
            "def a():\n"
            "    local_thing = 1\n"
            "    return local_thing\n"
            "def b():\n"
            "    return local_thing\n"
        )
        assert names(src) == {"local_thing"}

    def test_nested_function_locals_do_not_leak_outward(self):
        src = (
            "def outer():\n"
            "    def inner():\n"
            "        deep = 1\n"
            "        return deep\n"
            "    return deep\n"
        )
        assert names(src) == {"deep"}
