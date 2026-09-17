"""Does a connection pooled on one event loop break when used from another?

This measures the premise behind ``Context.loop`` in ``scripts/walk_forward.py``
(D40 in ``docs/exec-plans/tech-debt-tracker.md``). It is a probe, not a test,
and it does not wire into anything. Safe to run anytime — it binds loopback and
talks to nothing but itself.

Why it is not a test: ``tests/conftest.py`` installs an audit hook that refuses
``socket.bind`` and ``socket.connect`` on ``AF_INET`` outright, so a loopback
endpoint cannot exist inside the suite. The contract it supports *is* tested
(``tests/test_t1_decider.py::TestTheCallingLoopOutlivesTheClient``); the
transport measurement lives here.

What it runs, against a local OpenAI-shaped endpoint and with the same httpx
the SDK uses:

1. one client, ``asyncio.run`` per call  — the shape the runner had, one loop
   per simulated day;
2. one client, one loop for every call  — the shape it has now.

Usage:
  .venv/bin/python scripts/probe_loop_binding.py
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

_BODY = json.dumps({
    "id": "local", "object": "chat.completion", "created": 0, "model": "local",
    "choices": [{"index": 0, "finish_reason": "stop",
                 "message": {"role": "assistant", "content": "ok"}}],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}).encode()

_CALLS = 4


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(_BODY)))
        self.end_headers()
        self.wfile.write(_BODY)

    def log_message(self, *args):
        pass


class _Server(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        # A client that dies mid-request is the measurement, not a fault. The
        # default would print a ConnectionResetError traceback over the result.
        pass


def _call(client, url):
    return client.post(url, json={"model": "local", "messages": []})


def _per_call_loops(url) -> list[str]:
    """One client, ``asyncio.run`` per call — what the runner used to do."""
    client = httpx.AsyncClient(timeout=5)
    outcomes = []
    for _ in range(_CALLS):
        try:
            outcomes.append(str(asyncio.run(_call(client, url)).status_code))
        except Exception as exc:                      # noqa: BLE001
            outcomes.append(f"{type(exc).__name__}: {exc}")
    return outcomes


def _one_loop(url) -> list[str]:
    """One client, one loop for every call — what the runner does now."""
    loop = asyncio.new_event_loop()
    client = httpx.AsyncClient(timeout=5)
    outcomes = []
    try:
        for _ in range(_CALLS):
            try:
                outcomes.append(
                    str(loop.run_until_complete(_call(client, url)).status_code))
            except Exception as exc:                  # noqa: BLE001
                outcomes.append(f"{type(exc).__name__}: {exc}")
        loop.run_until_complete(client.aclose())
    finally:
        loop.close()
    return outcomes


def main() -> int:
    server = _Server(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = (f"http://127.0.0.1:{server.server_address[1]}/v1/chat/completions")
    try:
        print(f"endpoint {url}")
        print(f"httpx {httpx.__version__}\n")

        per_call = _per_call_loops(url)
        print(f"a loop per call (the defect), {_CALLS} calls:")
        for i, outcome in enumerate(per_call, 1):
            print(f"  call {i}: {outcome}")

        one = _one_loop(url)
        print(f"\none loop for every call (the fix), {_CALLS} calls:")
        for i, outcome in enumerate(one, 1):
            print(f"  call {i}: {outcome}")

        broken = sum(1 for o in per_call if o != "200")
        print(f"\na loop per call: {broken}/{_CALLS} failed")
        print(f"one loop:        {sum(1 for o in one if o != '200')}/{_CALLS} failed")
        print("\nNote the shape of the defect: not every call fails, but every")
        print("*other* one. A failure evicts the stale connection, so the next")
        print("call finds an empty pool and opens a fresh one. That is why the")
        print("120-day run needed the SDK's retry to fail once per day rather")
        print("than once per client — each day ended holding a connection bound")
        print("to its own, soon-to-be-closed loop.")
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
