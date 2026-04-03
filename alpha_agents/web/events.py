"""Pipeline event bus for real-time WebSocket broadcasting.

Each pipeline stage emits events that get broadcast to all connected
WebSocket clients for live visualization.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field, asdict
from enum import Enum

logger = logging.getLogger(__name__)


class StageStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"


@dataclass
class StageEvent:
    stage: str
    status: StageStatus
    message: str = ""
    data: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_json(self) -> str:
        d = asdict(self)
        d["status"] = self.status.value
        return json.dumps(d, ensure_ascii=False)


class EventBus:
    """Async event bus that broadcasts pipeline events to WebSocket clients."""

    def __init__(self):
        self._subscribers: set[asyncio.Queue] = set()
        self._stage_states: dict[str, StageEvent] = {}
        self._reports: list[dict] = []
        self._max_reports = 50

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        self._subscribers.discard(q)

    async def emit(self, event: StageEvent):
        self._stage_states[event.stage] = event
        msg = event.to_json()
        dead = []
        for q in self._subscribers:
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self._subscribers.discard(q)

    def add_report(self, report: dict):
        self._reports.append(report)
        if len(self._reports) > self._max_reports:
            self._reports = self._reports[-self._max_reports:]

    def get_snapshot(self) -> dict:
        """Current state of all pipeline stages + recent reports."""
        return {
            "stages": {k: asdict(v) for k, v in self._stage_states.items()},
            "reports": self._reports[-10:],
        }


# Global singleton
event_bus = EventBus()
