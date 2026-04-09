"""Lightweight decision tracing for pipeline tasks.

Wraps agent runs to capture input context, tool calls, output,
and duration — then persists to decision_traces table.
"""

import logging
import time
from datetime import datetime

from alpha_agents.agents.hooks import TracingHooks, CombinedHooks
from alpha_agents.data.report_store import save_decision_trace

logger = logging.getLogger(__name__)


def trace_agent_run(report_type: str, agent_name: str, user_message: str):
    """Create a tracing context manager for an agent run.

    Usage:
        ctx = trace_agent_run("morning", "morning_analyst", user_message)
        tracer = ctx.hooks(existing_hooks)  # CombinedHooks
        result = await Runner.run(agent, user_message, hooks=tracer)
        ctx.save(result.final_output)
    """
    return _TraceContext(report_type, agent_name, user_message)


class _TraceContext:
    def __init__(self, report_type: str, agent_name: str, user_message: str):
        self.report_type = report_type
        self.agent_name = agent_name
        self.user_message = user_message
        self._tracer = TracingHooks()
        self._start = time.time()

    def hooks(self, existing_hooks=None):
        """Return a combined hooks object that includes tracing."""
        if existing_hooks:
            return CombinedHooks(existing_hooks, self._tracer)
        return self._tracer

    def save(self, final_output: str) -> None:
        """Persist the trace to database. Call after agent completes."""
        duration = time.time() - self._start
        today = datetime.now().strftime("%Y-%m-%d")
        try:
            trace_id = save_decision_trace(
                date=today,
                report_type=self.report_type,
                agent_name=self.agent_name,
                input_context=self.user_message[:5000],
                tool_calls=self._tracer.get_tool_log(),
                final_output=final_output[:10000],
                duration_seconds=round(duration, 1),
            )
            tool_count = len(self._tracer.get_tool_log())
            logger.info("Saved decision trace #%d (%s, %d tools, %.1fs)",
                        trace_id, self.report_type, tool_count, duration)
        except Exception as e:
            logger.debug("Failed to save decision trace: %s", e)
