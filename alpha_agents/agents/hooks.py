"""Agent lifecycle hooks for broadcasting tool call events."""

import logging
import time

from agents import RunHooks

logger = logging.getLogger(__name__)


class ToolEventHooks(RunHooks):
    """Captures tool_start/tool_end events and forwards them via a callback.

    The callback signature is: callback(event_dict) where event_dict has:
      - agent: str (agent name)
      - tool: str (tool function name)
      - status: "start" | "end"
      - arguments: str (JSON args, only on start)
      - result_preview: str (truncated result, only on end)
      - timestamp: float
    """

    def __init__(self, callback, agent_label: str = ""):
        self._callback = callback
        self._agent_label = agent_label

    async def on_tool_start(self, context, agent, tool):
        if self._callback:
            self._callback({
                "agent": self._agent_label or agent.name,
                "tool": tool.name,
                "status": "start",
                "timestamp": time.time(),
            })

    async def on_tool_end(self, context, agent, tool, result):
        preview = str(result)[:200] if result else ""
        if self._callback:
            self._callback({
                "agent": self._agent_label or agent.name,
                "tool": tool.name,
                "status": "end",
                "result_preview": preview,
                "timestamp": time.time(),
            })

    async def on_handoff(self, context, from_agent, to_agent):
        if self._callback:
            self._callback({
                "agent": self._agent_label or from_agent.name,
                "tool": f"handoff → {to_agent.name}",
                "status": "handoff",
                "timestamp": time.time(),
            })
