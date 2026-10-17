"""Claude agents: tool-using loops that make their own decisions.

- story_agent: researches the catalog, picks what to write, drafts a story and
  revises it until it passes the catalog's checks.
- social_agent: writes a post per platform for a published story, checks
  recent posts so it doesn't repeat itself, and publishes them.
"""
