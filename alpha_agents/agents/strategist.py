import os
from pathlib import Path

from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions, ResultMessage, AssistantMessage, TextBlock

from alpha_agents.config import (
    PROMPTS_DIR, AGENT_API_KEY, AGENT_BASE_URL, AGENT_MODEL,
    CC_CLI_PATH, CC_LLM_PROVIDER, CC_LLM_BASE_URL, CC_LLM_API_KEY, CC_LLM_MODEL,
)
from alpha_agents.tools.server import create_tools_server
from alpha_agents.agents.geopolitical import get_geopolitical_agent


def _build_options(system_prompt: str) -> ClaudeAgentOptions:
    tools_server = create_tools_server()
    agent_name, agent_def = get_geopolitical_agent()

    env = {}

    # Check if using open-source CC with OpenAI provider
    use_open_source_cc = CC_LLM_PROVIDER == "openai" and CC_LLM_API_KEY
    if use_open_source_cc:
        env["CC_LLM_PROVIDER"] = CC_LLM_PROVIDER
        env["CC_LLM_BASE_URL"] = CC_LLM_BASE_URL
        env["CC_LLM_API_KEY"] = CC_LLM_API_KEY
        if CC_LLM_MODEL:
            env["CC_LLM_MODEL"] = CC_LLM_MODEL
        # Open-source CC still needs a dummy ANTHROPIC_API_KEY for SDK handshake
        env["ANTHROPIC_API_KEY"] = CC_LLM_API_KEY
    else:
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
        permission_mode="acceptEdits",
    )

    # Point to open-source CC wrapper if configured
    if use_open_source_cc and Path(CC_CLI_PATH).exists():
        opts.cli_path = CC_CLI_PATH

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
