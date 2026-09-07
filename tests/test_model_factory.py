"""One place that builds the agent model, with provider fallbacks.

Seven modules each had their own copy — small enough to slip under the
duplication linter, big enough that adding fallbacks meant editing seven
files. A real morning scan died on "Upstream error from Nvidia: Service
temporarily overloaded" after the agent had already done its tool calls,
which is what fallbacks exist to survive.
"""

from unittest.mock import patch

from agents import ModelSettings

from alpha_agents import model_factory as mf


class TestFallbackSelection:
    def test_openrouter_gets_a_fallback_chain(self):
        with patch.object(mf, "AGENT_BASE_URL", "https://openrouter.ai/api/v1"), \
             patch.object(mf, "AGENT_MODEL", "nvidia/nemotron-3.5-lightning:free"):
            got = mf._fallback_models()
        assert got and all(m.endswith(":free") for m in got)

    def test_primary_is_not_its_own_fallback(self):
        primary = mf._OPENROUTER_FALLBACKS[0]
        with patch.object(mf, "AGENT_BASE_URL", "https://openrouter.ai/api/v1"), \
             patch.object(mf, "AGENT_MODEL", primary):
            assert primary not in mf._fallback_models()

    def test_other_providers_get_none(self):
        for base in ("https://api.deepseek.com/v1",
                     "https://dashscope.aliyuncs.com/compatible-mode/v1",
                     "https://api.siliconflow.cn/v1"):
            with patch.object(mf, "AGENT_BASE_URL", base):
                assert mf._fallback_models() == []

    def test_unset_base_url_is_not_openrouter(self):
        with patch.object(mf, "AGENT_BASE_URL", None):
            assert mf._fallback_models() == []


class TestModelSettings:
    def test_openrouter_carries_the_models_array(self):
        """extra_body is the SDK's only hook for a non-standard field."""
        with patch.object(mf, "AGENT_BASE_URL", "https://openrouter.ai/api/v1"), \
             patch.object(mf, "AGENT_MODEL", "nvidia/nemotron-3.5-lightning:free"):
            settings = mf.create_model_settings()
        assert isinstance(settings, ModelSettings)
        assert settings.extra_body["models"]
        assert "nvidia/nemotron-3.5-lightning:free" not in settings.extra_body["models"]

    def test_other_providers_send_nothing_extra(self):
        with patch.object(mf, "AGENT_BASE_URL", "https://api.deepseek.com/v1"):
            assert mf.create_model_settings().extra_body is None


class TestCreateModel:
    def test_uses_the_configured_model(self):
        with patch.object(mf, "AGENT_MODEL", "some/model:free"), \
             patch.object(mf, "AGENT_BASE_URL", "https://openrouter.ai/api/v1"), \
             patch.object(mf, "AGENT_API_KEY", "k"):
            assert mf.create_model().model == "some/model:free"

    def test_falls_back_to_a_default_when_unset(self):
        with patch.object(mf, "AGENT_MODEL", None), \
             patch.object(mf, "AGENT_BASE_URL", "https://api.deepseek.com/v1"), \
             patch.object(mf, "AGENT_API_KEY", "k"):
            assert mf.create_model().model == mf.DEFAULT_MODEL


class TestNoDuplicateFactories:
    def test_no_module_defines_its_own_create_model(self):
        """The duplication this module exists to remove."""
        import pathlib
        root = pathlib.Path(__file__).parent.parent / "alpha_agents"
        offenders = [
            str(p.relative_to(root))
            for p in root.rglob("*.py")
            if p.name != "model_factory.py"
            and "def _create_model(" in p.read_text(encoding="utf-8")
        ]
        assert offenders == [], f"这些模块又自建了 model: {offenders}"
