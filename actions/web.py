"""Browser searches (PDF "Actions": web.py). Only fixed Google/YouTube URLs are opened."""

from __future__ import annotations

import webbrowser
from urllib.parse import quote_plus

from actions import registry
from actions.registry import ActionError, action

GOOGLE_URL = "https://www.google.com/search?q="
YOUTUBE_URL = "https://www.youtube.com/results?search_query="


def _query(query: str) -> str:
    """Trim and check a search query against the allow-list limit."""
    text = " ".join(str(query).split())
    if not text:
        raise ActionError("search text is empty")
    if len(text) > registry.limit("query_max_chars", 200):
        raise ActionError("search text is too long")
    return text


@action(
    "Search Google in the web browser.",
    {"query": {"type": "string", "minLength": 1, "maxLength": 200}},
    required=["query"],
    risk="low",
)
def web_search(query: str) -> str:
    """Open Google results for ``query``."""
    text = _query(query)
    webbrowser.open(GOOGLE_URL + quote_plus(text))
    return f"searched the web for {text}"


@action(
    "Search YouTube in the web browser, for songs, videos or bhajans.",
    {"query": {"type": "string", "minLength": 1, "maxLength": 200}},
    required=["query"],
    risk="low",
)
def youtube_search(query: str) -> str:
    """Open YouTube results for ``query``."""
    text = _query(query)
    webbrowser.open(YOUTUBE_URL + quote_plus(text))
    return f"searched youtube for {text}"
