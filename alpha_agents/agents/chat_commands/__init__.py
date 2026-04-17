"""Slash-command dispatcher for the interactive chat mode.

Input starting with ``/`` is a deterministic local command (no LLM roundtrip);
anything else is forwarded by the caller to the chat analyst agent.

Public API:
    ChatContext  — mutable state shared across handlers
    Command      — single registered command (name, aliases, handler, ...)
    REGISTRY     — tuple[Command, ...] of every registered command
    dispatch     — async (user_input, ctx) → None; routes to handler
    parse_slash  — pure helper exposed for tests
"""

from alpha_agents.agents.chat_commands.context import ChatContext, Command
from alpha_agents.agents.chat_commands.handlers import REGISTRY, dispatch, parse_slash

__all__ = ["ChatContext", "Command", "REGISTRY", "dispatch", "parse_slash"]
