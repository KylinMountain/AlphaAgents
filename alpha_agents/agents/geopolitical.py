from claude_agent_sdk import AgentDefinition

from alpha_agents.config import PROMPTS_DIR


def get_geopolitical_agent() -> tuple[str, AgentDefinition]:
    prompt = (PROMPTS_DIR / "geopolitical.md").read_text(encoding="utf-8")
    return "geopolitical", AgentDefinition(
        description="局势深度分析师。专注地缘政治事件（贸易战、制裁、冲突）对A股板块的影响分析。",
        prompt=prompt,
        tools=["WebSearch", "WebFetch"],
    )
