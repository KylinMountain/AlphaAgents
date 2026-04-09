"""Agent lifecycle hooks for broadcasting tool call and reasoning events."""

import logging
import time
from typing import Callable

from agents import RunHooks

logger = logging.getLogger(__name__)


class TracingHooks(RunHooks):
    """Accumulates tool calls during an agent run for decision trace persistence.

    Usage:
        tracer = TracingHooks()
        result = await Runner.run(agent, prompt, hooks=tracer)
        tool_log = tracer.get_tool_log()  # list of {tool, input, output, duration}
    """

    def __init__(self):
        self._tool_log: list[dict] = []
        self._pending: dict[str, float] = {}  # tool_name -> start_time

    async def on_tool_start(self, context, agent, tool):
        self._pending[tool.name] = time.time()

    async def on_tool_end(self, context, agent, tool, result):
        start = self._pending.pop(tool.name, None)
        duration = time.time() - start if start else 0
        self._tool_log.append({
            "tool": tool.name,
            "output_preview": str(result)[:500] if result else "",
            "duration": round(duration, 2),
        })

    def get_tool_log(self) -> list[dict]:
        return list(self._tool_log)


class CombinedHooks(RunHooks):
    """Combines multiple RunHooks into one, dispatching events to all."""

    def __init__(self, *hooks_list: RunHooks):
        self._hooks = [h for h in hooks_list if h is not None]

    async def on_agent_start(self, context, agent):
        for h in self._hooks:
            await h.on_agent_start(context, agent)

    async def on_tool_start(self, context, agent, tool):
        for h in self._hooks:
            await h.on_tool_start(context, agent, tool)

    async def on_tool_end(self, context, agent, tool, result):
        for h in self._hooks:
            await h.on_tool_end(context, agent, tool, result)

    async def on_llm_end(self, context, agent, response):
        for h in self._hooks:
            if hasattr(h, "on_llm_end"):
                await h.on_llm_end(context, agent, response)

    async def on_handoff(self, context, from_agent, to_agent):
        for h in self._hooks:
            await h.on_handoff(context, from_agent, to_agent)


class ToolEventHooks(RunHooks):
    """Captures tool calls, LLM reasoning text, and handoff events.

    The callback signature is: callback(event_dict) where event_dict has:
      - agent: str (agent name)
      - type: "tool_start" | "tool_end" | "handoff" | "reasoning" | "agent_start"
      - tool: str (tool name, for tool events)
      - text: str (reasoning text, for reasoning events)
      - result_preview: str (truncated result, for tool_end)
      - timestamp: float
    """

    def __init__(self, callback, agent_label: str = ""):
        self._callback = callback
        self._agent_label = agent_label

    async def on_agent_start(self, context, agent):
        if self._callback:
            self._callback({
                "agent": self._agent_label or agent.name,
                "type": "agent_start",
                "text": f"{agent.name} 开始分析",
                "timestamp": time.time(),
            })

    async def on_tool_start(self, context, agent, tool):
        if self._callback:
            self._callback({
                "agent": self._agent_label or agent.name,
                "type": "tool_start",
                "tool": tool.name,
                "timestamp": time.time(),
            })

    async def on_tool_end(self, context, agent, tool, result):
        preview = str(result)[:200] if result else ""
        if self._callback:
            self._callback({
                "agent": self._agent_label or agent.name,
                "type": "tool_end",
                "tool": tool.name,
                "result_preview": preview,
                "timestamp": time.time(),
            })

    async def on_llm_end(self, context, agent, response):
        """Extract intermediate reasoning text from LLM responses."""
        if not self._callback:
            return
        try:
            from openai.types.responses import ResponseOutputMessage, ResponseOutputText
            for item in response.output:
                if isinstance(item, ResponseOutputMessage):
                    for part in (item.content or []):
                        if isinstance(part, ResponseOutputText) and part.text:
                            text = part.text.strip()
                            if text:
                                self._callback({
                                    "agent": self._agent_label or agent.name,
                                    "type": "reasoning",
                                    "text": text[:300],
                                    "timestamp": time.time(),
                                })
        except Exception:
            # Don't let parsing errors break the pipeline
            pass

    async def on_handoff(self, context, from_agent, to_agent):
        if self._callback:
            self._callback({
                "agent": self._agent_label or from_agent.name,
                "type": "handoff",
                "tool": f"handoff → {to_agent.name}",
                "text": f"移交给 {to_agent.name} 做深度分析",
                "timestamp": time.time(),
            })
