from pathlib import Path
import json

# This script runs only in the isolated audit checkout, never in production.
def change(path, old, new):
    p = Path(path)
    text = p.read_text(encoding='utf-8')
    assert text.count(old) == 1, (path, old[:100], text.count(old))
    p.write_text(text.replace(old, new, 1), encoding='utf-8')

p = Path('alpha_agents/agents/t1_decider.py')
s = p.read_text(encoding='utf-8')
s = s.replace('from alpha_agents.config import PROMPTS_DIR', '''from alpha_agents.config import PROMPTS_DIR
from alpha_agents.data.decision_frame import (
    DecisionFrame, FrameError, TraderState, fingerprint,
)
from alpha_agents.data.decision_capture import Capture''', 1)
s = s.replace('                  research_budget=None, research_packet: dict | None = None) -> dict:', '''                  research_budget=None, research_packet: dict | None = None,
                  trader_id: str | None = None, run_id: str | None = None,
                  origin: str = "t1_decider") -> dict:''', 1)
s = s.replace('    message = build_message(\n', '    chosen_template = template if template is not None else load_prompt()\n    message = build_message(\n', 1)
s = s.replace('        template=template if template is not None else load_prompt(),', '        template=chosen_template,', 1)
start = s.index('    agent = Agent(name=f"t1_decider:{DECIDER_NAME}",', s.index('async def propose('))
end = s.index('\n\ndef propose_sync(', start)
body = s[start:end]
body = body.replace('Agent(name=f"t1_decider:{DECIDER_NAME}",', 'Agent(name=request["agent_name"],', 1)
body = body.replace('instructions=SYSTEM_INSTRUCTIONS, model=model,', 'instructions=request["instructions"], model=model,', 1)
body = body.replace('max_turns=max_turns,\n                                         label="t1_decide"', '**call_options')
body = body.replace('parse_orders(raw, {row["code"] for row in panel})', 'parse_orders(raw, set(value["panel_codes"]))')
assert 'max_turns=max_turns' not in body
capture = '''    from alpha_agents.data.trader_session import namespace
    frame = make_frame(
        day=day, phase=phase, message=message,
        state=TraderState.create(book=book or "", knowledge=knowledge or "",
                                 trader_note=trader_note or ""),
        panel_codes=[row["code"] for row in panel], model=model,
        template=chosen_template, max_turns=max_turns, tools=tools or [],
        trader_id=trader_id or "unbound", run_id=namespace(run_id), origin=origin)
    with Capture(frame, enabled=trader_id is not None) as captured:
        parsed = await _run_frame(frame, model=model, tools=tools or [], budget=budget)
        parsed["frame_hash"] = frame.frame_hash
        parsed["decision_id"] = captured.invocation_id
        captured.output = parsed
        return parsed


'''
s = s[:start] + capture + Path('.audit/m2-seam.txt').read_text(encoding='utf-8') + body + s[end:]
p.write_text(s, encoding='utf-8')

p = Path('scripts/walk_forward.py')
s = p.read_text(encoding='utf-8')
for anchor in ('    return t1_decider.propose_sync(\n', '        verdict = t1_decider.propose_sync(\n'):
    assert s.count(anchor) == 1
    indent = '        ' if anchor.startswith('    return') else '            '
    s = s.replace(anchor, anchor + indent + 'trader_id=ctx.trader, run_id=ctx.run_id, origin="walk_forward",\n')
s = s.replace('                "decision_explanation": {', '                "frame_hash": verdict.get("frame_hash"),\n                "decision_id": verdict.get("decision_id"),\n                "decision_explanation": {', 1)
p.write_text(s, encoding='utf-8')

change('alpha_agents/model_factory.py',
       '                 role: str = llm_roles.AGENT) -> OpenAIChatCompletionsModel:',
       '                 role: str = llm_roles.AGENT, allow_failover: bool = True,\n                 max_retries: int | None = None) -> OpenAIChatCompletionsModel:')
change('alpha_agents/model_factory.py',
       '    client = AsyncOpenAI(api_key=api_key, base_url=base_url, **extra)',
       '    if max_retries is not None:\n        extra["max_retries"] = max_retries\n    client = AsyncOpenAI(api_key=api_key, base_url=base_url, **extra)')
change('alpha_agents/model_factory.py',
       '    if role == llm_roles.AGENT and not _is_openrouter():',
       '    if allow_failover and role == llm_roles.AGENT and not _is_openrouter():')

schema = '''-- M2 is a new event stream; do not rebuild the M1 CHECK constraint.
CREATE TABLE IF NOT EXISTS decision_capture_events (
    id TEXT PRIMARY KEY,
    invocation_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    trader_id TEXT NOT NULL,
    session_day TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('decision_input','decision_output')),
    code TEXT NOT NULL DEFAULT '',
    observed_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    UNIQUE(invocation_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_decision_capture_events
    ON decision_capture_events(run_id,trader_id,session_day,kind,observed_at);
CREATE TRIGGER IF NOT EXISTS decision_capture_events_no_update
BEFORE UPDATE ON decision_capture_events BEGIN
    SELECT RAISE(ABORT, 'decision_capture_events is append-only');
END;
CREATE TRIGGER IF NOT EXISTS decision_capture_events_no_delete
BEFORE DELETE ON decision_capture_events BEGIN
    SELECT RAISE(ABORT, 'decision_capture_events is append-only');
END;

'''
change('alpha_agents/data/memory_schema.py',
       'CREATE TABLE IF NOT EXISTS evolution_metrics (',
       schema + 'CREATE TABLE IF NOT EXISTS evolution_metrics (')

change('alpha_agents/pipeline/tasks/entry_pricing.py', 'import asyncio\n',
       'import asyncio\nfrom dataclasses import asdict\nfrom pathlib import Path\n')
change('alpha_agents/pipeline/tasks/entry_pricing.py',
       'logger = logging.getLogger(__name__)', '''from alpha_agents.data.decision_frame import DecisionFrame, TraderState, fingerprint
from alpha_agents.data.decision_capture import Capture
from alpha_agents.data import trader_session

logger = logging.getLogger(__name__)
_CODE_HASH = fingerprint(Path(__file__).read_text(encoding="utf-8"))''')
change('alpha_agents/pipeline/tasks/entry_pricing.py', '''        result = await asyncio.wait_for(
            Runner.run(agent, build_context(candidates, trader), max_turns=25),
            timeout=_TIMEOUT)''', '''        message = build_context(candidates, trader)
        at = trader_session.instant()
        frame = DecisionFrame.create(
            identity={"run_id": trader_session.namespace(), "trader_id": trader.id,
                      "stage": "entry_price", "phase": "intraday",
                      "session_day": at[:10], "information_cutoff": at,
                      "information_grade": "live_tool_input", "origin": "entry_pricing"},
            # This is the displayed state, not a restorable account checkpoint.
            state=TraderState.create(book=message,
                                     trader_note=getattr(trader, "extra_prompt", "") or ""),
            request={"agent_name": agent.name, "instructions": instructions,
                     "message": message, "max_turns": 25,
                     "model": getattr(agent.model, "model", None) or "unbound",
                     "model_settings": asdict(agent.model_settings),
                     "tools": [tool.name for tool in agent.tools]},
            panel_codes=[c["code"] for c in candidates],
            producer={"contract": "entry_orders.v1", "code_hash": _CODE_HASH})
        with Capture(frame) as captured:
            result = await asyncio.wait_for(
                Runner.run(agent, message, max_turns=25), timeout=_TIMEOUT)
            captured.output = {"raw": result.final_output or ""}''')

change('tests/test_trader_tools_wiring.py',
       'tree = ast.parse(inspect.getsource(t1_decider.propose))',
       '# Normal and frozen calls now share the same exception boundary.\n        tree = ast.parse(inspect.getsource(t1_decider._run_frame))')

paths = [
 'alpha_agents/agents/t1_decider.py', 'alpha_agents/data/decision_capture.py',
 'alpha_agents/data/decision_frame.py', 'alpha_agents/data/memory_schema.py',
 'alpha_agents/model_factory.py', 'alpha_agents/pipeline/tasks/entry_pricing.py',
 'scripts/walk_forward.py', 'scripts/decision_lab.py',
 'tests/test_trader_tools_wiring.py', 'tests/test_decision_frames.py',
 'tests/test_decision_lab.py', 'docs/decision_lab.md',
 'docs/exec-plans/active/2026-09-25-trader-lifecycle-m2.md',
]
Path('/tmp/m2a-validation/changed-paths.json').write_text(json.dumps(paths))
