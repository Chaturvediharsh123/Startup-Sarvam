"""Actions test isolation: module state is restored, the real Start Menu is never read."""

from __future__ import annotations

import pytest

import actions  # noqa: F401 - registers every action
from actions import apps, registry
from core.config import Settings


@pytest.fixture(autouse=True)
def _isolated_registry(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the registry at test settings; restore its globals afterwards."""
    monkeypatch.setattr(registry, "_settings", settings)
    monkeypatch.setattr(registry, "_allowlist", None)
    monkeypatch.setattr(registry, "_allowlist_path", settings.allowlist_path)
    monkeypatch.setattr(registry, "_sleep", lambda _s: None)
    monkeypatch.setattr(apps, "_shortcuts", {})  # "scanned, nothing installed"
    monkeypatch.setattr(apps, "_app_path_registered", lambda exe: False)
