"""Context and Command dataclasses shared by the slash-command dispatcher."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from rich.console import Console


@dataclass
class ChatContext:
    """Mutable state shared across command handlers.

    Handlers mutate fields directly (e.g. /refresh rebuilds ``agent``);
    the main loop reads them back each iteration.
    """

    console: Console
    agent: Any                                    # rebuilt by /refresh
    conversation_history: list = field(default_factory=list)  # cleared by /refresh
    should_exit: bool = False                     # set by /quit


@dataclass(frozen=True)
class Command:
    """Single registered slash command."""

    name: str                                     # canonical, e.g. "/lhb"
    group: str                                    # help section, e.g. "行情"
    summary: str                                  # one-line /help description
    handler: Callable[[str, ChatContext], Awaitable[None]]
    aliases: tuple[str, ...] = ()                 # e.g. ("/量价",)
    usage: str = ""                               # e.g. "/quote <6位代码>"
