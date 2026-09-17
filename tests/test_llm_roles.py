"""One table for every LLM purpose, and the inheritance that makes it usable.

The defect this pins is not "the names were ugly". It is that five schemes
existed, two of them (``MIMO_*``, and the ``DIGEST_*`` fallback inside VPA)
were read by nothing, and pointing a call site at a cheaper model meant
guessing which name it read and getting no error when the guess was wrong.
"""

import pytest

from alpha_agents import llm_roles


class TestTheLegacyNamesStillResolve:
    """``AGENT_*`` is part of a frozen policy fingerprint.

    ``model_factory.model_identity()`` feeds ``policy_sources``, so a rename
    that stopped honouring the old names would silently change which model
    every frozen version claims to have run on.
    """

    def test_a_caller_supplied_triple_is_used_when_nothing_overrides_it(
            self, monkeypatch):
        for name in ("AGENT_API_KEY", "AGENT_BASE_URL", "AGENT_MODEL"):
            monkeypatch.delenv(name, raising=False)
        got = llm_roles.resolve(
            llm_roles.AGENT, legacy=("k", "https://legacy/v1", "legacy-model"))
        assert got == ("k", "https://legacy/v1", "legacy-model")

    def test_a_caller_supplied_triple_is_authoritative(self, monkeypatch):
        """Not merely a fallback: when a caller hands over its configuration,
        that configuration is the answer.

        This is what makes the module globals patchable. ``digest.py``
        imports ``DIGEST_MODEL`` into its own namespace, and a test replaces
        *that* name; if this resolver re-read ``os.environ`` on top, the
        patch would be ignored and the test would silently exercise the
        developer's ``.env`` instead of its own fixture. The suite caught
        exactly that — ``test_digest_news_calls_api_correctly`` asserted
        ``test-model`` and got the ``.env`` value.
        """
        monkeypatch.setenv("AGENT_MODEL", "env-model")
        _, _, model = llm_roles.resolve(
            llm_roles.AGENT, legacy=("k", "https://legacy/v1", "legacy-model"))
        assert model == "legacy-model"

    def test_the_config_module_is_read_when_no_caller_triple_is_given(
            self, monkeypatch):
        """``DIGEST_*`` from ``config`` is the fallback for callers with no
        module global of their own to pass."""
        monkeypatch.setenv("DIGEST_API_KEY", "config-key")
        monkeypatch.setenv("DIGEST_BASE_URL", "https://config/v1")
        monkeypatch.setenv("DIGEST_MODEL", "config-model")
        got = llm_roles.resolve(llm_roles.DIGEST)
        assert got == ("config-key", "https://config/v1", "config-model")


class TestAPartialOverrideKeepsTheRest:
    """Setting only ``SUMMARY_MODEL`` is the common case.

    A resolver that treated a partial override as a complete one would blank
    the endpoint and the key, turning "use a cheaper model" into "stop
    calling the provider" — and the failure would look like a network fault.
    """

    def test_only_the_model_changes(self, monkeypatch):
        monkeypatch.setenv("DIGEST_API_KEY", "dk")
        monkeypatch.setenv("DIGEST_BASE_URL", "https://digest/v1")
        monkeypatch.setenv("DIGEST_MODEL", "digest-model")
        monkeypatch.setenv("SUMMARY_MODEL", "cheap-model")
        monkeypatch.delenv("SUMMARY_API_KEY", raising=False)
        monkeypatch.delenv("SUMMARY_BASE_URL", raising=False)

        key, base, model = llm_roles.resolve(llm_roles.SUMMARY)

        assert (key, base, model) == ("dk", "https://digest/v1", "cheap-model")

    def test_no_override_means_the_parent_verbatim(self, monkeypatch):
        monkeypatch.setenv("DIGEST_API_KEY", "dk")
        monkeypatch.setenv("DIGEST_BASE_URL", "https://digest/v1")
        monkeypatch.setenv("DIGEST_MODEL", "digest-model")
        for name in ("SUMMARY_API_KEY", "SUMMARY_BASE_URL", "SUMMARY_MODEL"):
            monkeypatch.delenv(name, raising=False)

        assert llm_roles.resolve(llm_roles.SUMMARY) == (llm_roles.resolve(
            llm_roles.DIGEST))


class TestTheTiersAreWhatTheyClaim:
    def test_summary_is_inherited_from_the_cheap_role_not_the_smart_one(
            self, monkeypatch):
        """The point of having a separate summariser role: not configuring it
        must not silently put the summarisers on the deciders' model."""
        assert llm_roles.PARENT[llm_roles.SUMMARY] == llm_roles.DIGEST

    def test_agent_has_no_parent(self):
        assert llm_roles.AGENT not in llm_roles.PARENT

    def test_every_role_declares_a_purpose_and_a_tier(self):
        for role in llm_roles.ROLES:
            purpose, tier = llm_roles.PURPOSE[role]
            assert purpose and tier

    def test_the_table_covers_every_role(self):
        rows = llm_roles.table()
        assert [r["role"] for r in rows] == list(llm_roles.ROLES)
        assert all(r["purpose"] and r["tier"] for r in rows)


class TestAnUnknownRoleIsLoud:
    """A typo must not resolve to "no credentials" and read as an outage."""

    def test_it_raises_rather_than_returning_empty(self):
        with pytest.raises(ValueError, match="unknown LLM role"):
            llm_roles.resolve("summmary")


class TestTheFactoryRoutesByRole:
    def test_the_model_name_comes_from_the_role(self, monkeypatch):
        from alpha_agents import model_factory as mf
        monkeypatch.setenv("SUMMARY_API_KEY", "sk")
        monkeypatch.setenv("SUMMARY_BASE_URL", "https://summary/v1")
        monkeypatch.setenv("SUMMARY_MODEL", "cheap-summariser")
        assert mf.create_model(role=llm_roles.SUMMARY).model == "cheap-summariser"

    def test_the_default_role_is_still_the_agent(self, monkeypatch):
        from alpha_agents import model_factory as mf
        monkeypatch.setattr(mf, "AGENT_MODEL", "agent-model")
        monkeypatch.setattr(mf, "AGENT_BASE_URL", "https://agent/v1")
        monkeypatch.setattr(mf, "AGENT_API_KEY", "k")
        assert mf.create_model().model == "agent-model"

    def test_identity_keeps_its_shape(self, monkeypatch):
        """``policy_sources`` fingerprints these keys by name."""
        from alpha_agents import model_factory as mf
        monkeypatch.setattr(mf, "AGENT_MODEL", "agent-model")
        monkeypatch.setattr(mf, "AGENT_BASE_URL", "https://agent/v1")
        assert set(mf.model_identity()) == {
            "agent_model", "agent_base_url", "default_model", "fallbacks"}

    def test_the_openrouter_chain_is_the_agents_alone(self, monkeypatch):
        """A summariser has no tool loop, so the agent's measured fallbacks
        would be a latency claim nobody made about it."""
        from agents import ModelSettings
        from alpha_agents import model_factory as mf
        monkeypatch.setenv("AGENT_BASE_URL", "https://openrouter.ai/api/v1")
        monkeypatch.setattr(mf, "AGENT_BASE_URL", "https://openrouter.ai/api/v1")
        assert mf.create_model_settings(role=llm_roles.SUMMARY).extra_body is None
        assert isinstance(mf.create_model_settings(), ModelSettings)


class TestNoProviderNameIsLeftUnread:
    """``MIMO_*`` was configured and never read. This is the guard against the
    next one: a role the table declares must be resolvable, and the resolver
    must not depend on a name outside the scheme."""

    def test_every_role_resolves_without_raising(self):
        for role in llm_roles.ROLES:
            key, base, model = llm_roles.resolve(role)
            assert isinstance(key, str) and isinstance(base, str)
            assert isinstance(model, str)
