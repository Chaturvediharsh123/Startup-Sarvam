"""Deterministic routing of an action to an adapter.

Surface decides the adapter: ``browser`` -> Playwright, ``window`` -> Windows UI Automation,
``screen`` -> visual (PyAutoGUI). The visual adapter is only added as a fallback when the
target explicitly allows it and carries a coordinate ``point``.
"""

from __future__ import annotations

from typing import Mapping

from actions.base import Adapter
from actions.errors import UnsupportedActionError
from actions.models import Action, ActionType, Condition, Surface, Target
from actions.registry import ActionRegistry


class ActionRouter:
    def __init__(self, adapters: Mapping[Surface, Adapter], registry: ActionRegistry) -> None:
        self._adapters = dict(adapters)
        self._registry = registry

    def adapter(self, surface: Surface) -> Adapter | None:
        adapter = self._adapters.get(surface)
        return adapter if adapter is not None and adapter.available() else None

    def route(self, action: Action) -> list[Adapter]:
        """Ordered adapter chain: primary first, optional visual fallback second.
        Empty for ``wait``, which the executor handles itself."""
        if action.type is ActionType.WAIT:
            return []
        surface = self._registry.surface_for(action)
        if surface is None:
            raise UnsupportedActionError(f"cannot determine a surface for {action.type.value}")
        primary = self.adapter(surface)
        if primary is None:
            raise UnsupportedActionError(f"no adapter available for surface '{surface}' on this machine")
        if action.type not in primary.supports:
            raise UnsupportedActionError(f"adapter '{primary.name}' does not support {action.type.value}", adapter=primary.name)
        chain = [primary]
        target = action.target
        if target is not None and target.allow_visual_fallback and surface != "screen":
            visual = self.adapter("screen")
            if visual is not None and action.type in visual.supports:
                chain.append(visual)
        return chain

    def adapter_for_condition(self, condition: Condition) -> Adapter | None:
        surface = condition.surface
        return self.adapter(surface) if surface is not None else None

    @staticmethod
    def visual_fallback_action(action: Action) -> Action:
        """Same action on the screen: at the explicit point, else at the target's visible text (OCR)."""
        target = action.target
        assert target is not None
        if target.point is not None:
            screen_target = Target(surface="screen", point=target.point, description=target.description)
        else:
            screen_target = Target(surface="screen", text=target.fallback_text, description=target.description)
        return action.model_copy(update={"target": screen_target})
