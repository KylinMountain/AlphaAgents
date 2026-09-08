"""The event linker must survive a model that wraps its JSON in prose.

Observed live: "Event linking failed: Expecting value: line 1 column 1
(char 0)" on every cycle, so the causality graph showed 14 events and 0
links. A strict json.loads on the whole response fails if the model adds
so much as a leading 好的，.
"""

import pytest

from alpha_agents.pipeline.event_linker import _extract_json_array


class TestExtractJsonArray:
    def test_bare_array(self):
        assert _extract_json_array('[{"source": 0, "target": 1}]') == [
            {"source": 0, "target": 1}
        ]

    def test_array_wrapped_in_prose(self):
        text = '好的，以下是识别到的关联：\n[{"source": 0}]\n希望有帮助。'
        assert _extract_json_array(text) == [{"source": 0}]

    def test_markdown_fence(self):
        assert _extract_json_array('```json\n[{"source": 2}]\n```') == [
            {"source": 2}
        ]

    def test_empty_array_is_a_valid_answer(self):
        """"No links" is a result, not a failure — it must not read as None."""
        assert _extract_json_array("[]") == []

    @pytest.mark.parametrize("text", ["", None, "没有发现关联", '{"source": 0}'])
    def test_no_array_returns_none(self, text):
        assert _extract_json_array(text) is None

    def test_nested_arrays_take_the_outer_one(self):
        got = _extract_json_array('[{"a": [1, 2]}, {"b": [3]}]')
        assert got == [{"a": [1, 2]}, {"b": [3]}]
