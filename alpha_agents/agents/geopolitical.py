from agents import Agent

from alpha_agents.config import PROMPTS_DIR


def create_geopolitical_agent(model) -> Agent:
    """Create the geopolitical deep-analysis sub-agent."""
    prompt = (PROMPTS_DIR / "geopolitical.md").read_text(encoding="utf-8")
    return Agent(
        name="geopolitical",
        instructions=prompt,
        model=model,
        tools=[],  # Pure analysis, no tools needed
    )
