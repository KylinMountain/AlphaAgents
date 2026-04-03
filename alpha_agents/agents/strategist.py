import os

from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions, ResultMessage, AssistantMessage, TextBlock

from alpha_agents.config import PROMPTS_DIR, AGENT_API_KEY, AGENT_BASE_URL, AGENT_MODEL
from alpha_agents.tools.server import create_tools_server
from alpha_agents.agents.geopolitical import get_geopolitical_agent


def _build_options(system_prompt: str) -> ClaudeAgentOptions:
    tools_server = create_tools_server()
    agent_name, agent_def = get_geopolitical_agent()

    # Build env dict for the agent SDK session
    env = {}
    if AGENT_API_KEY:
        env["ANTHROPIC_API_KEY"] = AGENT_API_KEY
    if AGENT_BASE_URL:
        env["ANTHROPIC_BASE_URL"] = AGENT_BASE_URL

    opts = ClaudeAgentOptions(
        system_prompt=system_prompt,
        mcp_servers={"alpha-data": tools_server},
        allowed_tools=["Agent", "WebSearch", "WebFetch"],
        agents={agent_name: agent_def},
        env=env,
    )

    # Set model if configured (empty = SDK default)
    if AGENT_MODEL:
        opts.model = AGENT_MODEL

    return opts


async def run_analysis(prompt: str) -> str:
    system_prompt = (PROMPTS_DIR / "strategist.md").read_text(encoding="utf-8")
    options = _build_options(system_prompt)

    results = []
    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        results.append(block.text)
            elif isinstance(message, ResultMessage):
                results.append(message.result)

    return "\n".join(results)
