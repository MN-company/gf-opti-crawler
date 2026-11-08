"""Shared, configurable HTTP identity for the lightweight client and Playwright."""
import os

DEFAULT_USER_AGENT = 'RecipeArchiveResearchBot/0.8.1'
USER_AGENT_ENV = 'RECIPE_SCRAPER_USER_AGENT'


def resolve_user_agent(value=None):
    agent = value if value is not None else os.environ.get(USER_AGENT_ENV, DEFAULT_USER_AGENT)
    if not isinstance(agent, str) or not agent.strip() or len(agent) > 256:
        raise ValueError('User-Agent must be non-empty and at most 256 characters.')
    if any(ord(char) < 32 or ord(char) == 127 for char in agent):
        raise ValueError('Invalid control character in User-Agent.')
    return agent.strip()
