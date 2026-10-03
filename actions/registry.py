"""Capability registry: the closed set of actions that may execute.

The registry is also the single source for the planner's function-calling tool list, so the
LLM can only ever be offered capabilities the executor will accept. There is deliberately no
capability for running code, shell or PowerShell commands.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Literal

from pydantic import Field, ValidationError, create_model

from actions.errors import ActionValidationError, UnsupportedActionError
from actions.models import PARAMETER_MODELS, Action, ActionType, DownloadParams, Risk, Surface, Target

TargetRule = Literal["none", "optional", "required", "element"]


@dataclass(frozen=True)
class Capability:
    type: ActionType
    description: str
    target: TargetRule
    surfaces: frozenset[Surface]
    default_surface: Surface | None
    idempotent: bool
    default_risk: Risk


_ALL: frozenset[Surface] = frozenset({"browser", "window", "screen"})

DEFAULT_CAPABILITIES: tuple[Capability, ...] = (
    Capability(ActionType.OPEN_APP, "Start an allow-listed desktop application.",
               "none", frozenset(), "window", idempotent=False, default_risk="low"),
    Capability(ActionType.OPEN_URL, "Navigate the automation browser to an http(s) URL.",
               "none", frozenset(), "browser", idempotent=True, default_risk="low"),
    Capability(ActionType.CLICK, "Click a browser element, window control, or (fallback) screen point.",
               "element", _ALL, None, idempotent=False, default_risk="medium"),
    Capability(ActionType.TYPE, "Type text into a target, or into the focused element when no target is given.",
               "optional", _ALL, "screen", idempotent=False, default_risk="medium"),
    Capability(ActionType.PRESS_KEY, "Press one allow-listed key.",
               "optional", _ALL, "screen", idempotent=False, default_risk="low"),
    Capability(ActionType.HOTKEY, "Press an allow-listed key combination such as ctrl+s.",
               "optional", _ALL, "screen", idempotent=False, default_risk="medium"),
    Capability(ActionType.SCROLL, "Scroll a page, control or the screen.",
               "optional", _ALL, "screen", idempotent=False, default_risk="low"),
    Capability(ActionType.READ, "Read visible text from a page/element or window/control.",
               "required", frozenset({"browser", "window"}), None, idempotent=True, default_risk="low"),
    Capability(ActionType.SCREENSHOT, "Capture a screenshot of the screen, a window or the browser page.",
               "optional", _ALL, "screen", idempotent=True, default_risk="low"),
    Capability(ActionType.WAIT, "Wait a fixed time or until a condition holds (bounded).",
               "none", frozenset(), None, idempotent=True, default_risk="low"),
    Capability(ActionType.DOWNLOAD, "Download a file by clicking a browser element or fetching an http(s) URL.",
               "optional", frozenset({"browser"}), "browser", idempotent=False, default_risk="low"),
    Capability(ActionType.CLOSE_APP, "Close the window of an allow-listed application (graceful close, no kill).",
               "none", frozenset(), "window", idempotent=False, default_risk="medium"),
)


class ActionRegistry:
    def __init__(self, capabilities: Iterable[Capability] = DEFAULT_CAPABILITIES, disabled: Iterable[str] = ()) -> None:
        disabled_set = {d.strip().lower() for d in disabled}
        self._caps: dict[ActionType, Capability] = {
            c.type: c for c in capabilities if c.type.value not in disabled_set
        }

    # ------------------------------------------------------------------ lookup

    def register(self, capability: Capability) -> None:
        if capability.type not in PARAMETER_MODELS:
            raise ValueError(f"no parameter schema for {capability.type}")
        self._caps[capability.type] = capability

    def is_registered(self, action_type: ActionType | str) -> bool:
        try:
            return ActionType(action_type) in self._caps
        except ValueError:
            return False

    def get(self, action_type: ActionType | str) -> Capability:
        try:
            cap = self._caps.get(ActionType(action_type))
        except ValueError:
            cap = None
        if cap is None:
            raise UnsupportedActionError(f"capability '{getattr(action_type, 'value', action_type)}' is not registered")
        return cap

    @property
    def names(self) -> list[str]:
        return [t.value for t in self._caps]

    # ------------------------------------------------------------------ validation

    def check(self, action: Action) -> Capability:
        """Raise unless this action fits its capability's shape. Returns the capability."""
        cap = self.get(action.type)
        target = action.target
        if cap.target == "none" and target is not None:
            raise ActionValidationError(f"{cap.type.value} does not take a target")
        if cap.target in ("required", "element") and target is None:
            raise ActionValidationError(f"{cap.type.value} requires a target")
        if cap.target == "element" and target is not None and not target.is_element:
            raise ActionValidationError(f"{cap.type.value} requires an element-level target", target=target.summary())
        if target is not None and target.surface not in cap.surfaces:
            raise ActionValidationError(
                f"{cap.type.value} does not support surface '{target.surface}'", target=target.summary()
            )
        if action.type is ActionType.DOWNLOAD:
            params: DownloadParams = action.parameters
            has_element = target is not None and target.is_element
            if has_element == (params.url is not None):
                raise ActionValidationError("download needs exactly one of an element target or parameters.url")
        return cap

    def surface_for(self, action: Action) -> Surface | None:
        cap = self.get(action.type)
        return action.target.surface if action.target is not None else cap.default_surface

    def effective_risk(self, action: Action) -> Risk:
        return action.risk or self.get(action.type).default_risk

    # ------------------------------------------------------------------ planner integration

    def tool_definitions(self) -> list[dict[str, Any]]:
        """Function-calling tool schemas (OpenAI-compatible), one per registered capability."""
        tools = []
        for cap in self._caps.values():
            fields: dict[str, Any] = {"parameters": (PARAMETER_MODELS[cap.type], ...)}
            if cap.target in ("required", "element"):
                fields["target"] = (Target, ...)
            elif cap.target == "optional":
                fields["target"] = (Target | None, Field(default=None))
            model = create_model(f"{cap.type.value}_call", **fields)
            tools.append({
                "type": "function",
                "function": {
                    "name": cap.type.value,
                    "description": cap.description,
                    "parameters": model.model_json_schema(),
                },
            })
        return tools

    def action_from_tool_call(self, name: str, arguments: str | dict[str, Any]) -> Action:
        """Build a validated Action from an LLM tool call. Raises UnsupportedActionError /
        ActionValidationError; the caller decides what to do with them."""
        self.get(name)
        try:
            args = json.loads(arguments) if isinstance(arguments, str) else dict(arguments)
            action = Action.model_validate({**args, "type": name})
        except (ValueError, ValidationError) as exc:
            raise ActionValidationError(f"invalid arguments for {name}: {exc}") from exc
        self.check(action)
        return action
