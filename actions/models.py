"""Pydantic schemas for actions, conditions, evidence and results.

These models describe *what* to do on the computer, never *why*. The planner builds an
``Action``; the executor returns an ``ActionResult``. Nothing here knows about tasks,
users or business goals.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_action_id() -> str:
    return f"act_{uuid.uuid4().hex[:12]}"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ActionType(str, Enum):
    OPEN_APP = "open_app"
    OPEN_URL = "open_url"
    CLICK = "click"
    TYPE = "type"
    PRESS_KEY = "press_key"
    HOTKEY = "hotkey"
    SCROLL = "scroll"
    READ = "read"
    SCREENSHOT = "screenshot"
    WAIT = "wait"
    DOWNLOAD = "download"
    CLOSE_APP = "close_app"


Surface = Literal["browser", "window", "screen"]
Risk = Literal["low", "medium", "high"]


# --------------------------------------------------------------------------- keys

MODIFIERS: tuple[str, ...] = ("ctrl", "shift", "alt", "win")

KEY_ALIASES: dict[str, str] = {
    "control": "ctrl", "ctl": "ctrl",
    "cmd": "win", "windows": "win", "super": "win", "meta": "win",
    "option": "alt",
    "esc": "escape", "return": "enter",
    "del": "delete", "bksp": "backspace",
    "pgup": "pageup", "page_up": "pageup", "pgdn": "pagedown", "page_down": "pagedown",
    "arrowup": "up", "arrowdown": "down", "arrowleft": "left", "arrowright": "right",
    "spacebar": "space", "+": "plus", "=": "plus", "-": "minus",
}


def normalize_key(key: str) -> str:
    k = key.strip().lower().replace(" ", "")
    if not k:
        raise ValueError("key must not be empty")
    return KEY_ALIASES.get(k, k)


def normalize_hotkey(keys: list[str]) -> list[str]:
    normalized = [normalize_key(k) for k in keys]
    modifiers = [m for m in MODIFIERS if m in normalized]
    others = [k for k in normalized if k not in MODIFIERS]
    if not modifiers:
        raise ValueError("a hotkey needs at least one modifier (ctrl, shift, alt, win)")
    if len(others) != 1:
        raise ValueError("a hotkey needs exactly one non-modifier key")
    if len(set(normalized)) != len(normalized):
        raise ValueError("a hotkey must not repeat keys")
    return [*modifiers, *others]


def _check_web_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("only absolute http(s) URLs are allowed")
    return url.strip()


# --------------------------------------------------------------------------- targets

class Point(StrictModel):
    x: int = Field(ge=0)
    y: int = Field(ge=0)


_BROWSER_LOCATORS = ("role", "label", "placeholder", "text", "selector")
_WINDOW_LOCATORS = ("window", "control_type", "automation_id")


class Target(StrictModel):
    """Where an action applies.

    * ``browser``: Playwright locators. Prefer ``role`` + ``name``, ``label`` or
      ``placeholder`` over ``text``; use a CSS ``selector`` only as a last resort.
      No locator means the current page as a whole.
    * ``window``: Windows UI Automation. ``window`` is a regex on the window title;
      ``name`` / ``control_type`` / ``automation_id`` pick a control inside it.
    * ``screen``: visual fallback only (fragile). Either a coordinate ``point`` or visible
      ``text`` that the visual adapter finds with OCR.

    ``allow_visual_fallback`` lets the executor retry a browser/window action on the screen
    when its locator cannot be resolved: at ``point`` if given, otherwise at the OCR position
    of the target's visible text (``text``, ``name`` or ``label``).
    """

    surface: Surface
    role: str | None = None
    name: str | None = None
    label: str | None = None
    placeholder: str | None = None
    text: str | None = None
    selector: str | None = None
    window: str | None = None
    control_type: str | None = None
    automation_id: str | None = None
    point: Point | None = None
    allow_visual_fallback: bool = False
    description: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _check_locators(self) -> Target:
        def present(fields: tuple[str, ...]) -> list[str]:
            return [f for f in fields if getattr(self, f) is not None]

        if self.surface == "browser":
            if present(_WINDOW_LOCATORS):
                raise ValueError(f"browser target cannot use window locators {present(_WINDOW_LOCATORS)}")
            if self.name is not None and self.role is None:
                raise ValueError("browser target: 'name' requires 'role'")
        elif self.surface == "window":
            if present(_BROWSER_LOCATORS):
                raise ValueError(f"window target cannot use browser locators {present(_BROWSER_LOCATORS)}")
            if self.window is None:
                raise ValueError("window target requires 'window' (regex on the window title)")
        else:
            used = [f for f in present(_BROWSER_LOCATORS) + present(_WINDOW_LOCATORS) if f != "text"]
            if self.name:
                used.append("name")
            if used:
                raise ValueError(f"screen target only accepts 'point' or 'text', got {used}")
            if self.point is not None and self.text is not None:
                raise ValueError("screen target takes 'point' or 'text', not both")
            if self.allow_visual_fallback:
                raise ValueError("allow_visual_fallback is meaningless on a screen target")
        if self.allow_visual_fallback and self.point is None and self.fallback_text is None:
            raise ValueError("allow_visual_fallback requires 'point' or visible text ('text', 'name' or 'label')")
        return self

    @property
    def fallback_text(self) -> str | None:
        """Visible text the visual adapter can look for with OCR."""
        return self.text or self.name or self.label

    @property
    def is_element(self) -> bool:
        """True when the target names a specific element/control rather than a whole page/window."""
        if self.surface == "browser":
            return any(getattr(self, f) is not None for f in _BROWSER_LOCATORS)
        if self.surface == "window":
            return any(getattr(self, f) is not None for f in ("name", "control_type", "automation_id"))
        return self.point is not None or self.text is not None

    def summary(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True, exclude_defaults=True) | {"surface": self.surface}


# --------------------------------------------------------------------------- conditions

ConditionKind = Literal[
    "element_visible",
    "element_hidden",
    "text_present",
    "url_contains",
    "title_contains",
    "window_exists",
    "window_absent",
    "file_exists",
    "download_completed",
    "custom",
]
ConditionStatus = Literal["satisfied", "unsatisfied", "unverified"]


class Condition(StrictModel):
    """A technical, observable condition (precondition or expected outcome).

    ``custom`` conditions are free-text expectations the executor cannot check itself; they
    are passed through as ``unverified`` for the higher-level verifier.
    """

    kind: ConditionKind
    target: Target | None = None
    value: str | None = None
    description: str | None = Field(default=None, max_length=300)
    wait_s: float = Field(default=0.0, ge=0, le=60)

    @model_validator(mode="after")
    def _check_shape(self) -> Condition:
        k = self.kind
        if k in ("element_visible", "element_hidden") and (self.target is None or not self.target.is_element):
            raise ValueError(f"{k} requires an element-level target")
        if k in ("text_present", "url_contains", "title_contains", "file_exists") and not self.value:
            raise ValueError(f"{k} requires 'value'")
        if k in ("window_exists", "window_absent") and (self.target is None or self.target.surface != "window"):
            raise ValueError(f"{k} requires a window target")
        if k == "custom" and not self.description:
            raise ValueError("custom condition requires 'description'")
        return self

    @property
    def surface(self) -> Surface | None:
        if self.target is not None:
            return self.target.surface
        if self.kind in ("text_present", "url_contains", "title_contains"):
            return "browser"
        return None


class ConditionCheck(StrictModel):
    condition: Condition
    status: ConditionStatus
    observed: str | None = None
    detail: str | None = None


class RetryPolicy(StrictModel):
    """Upper bound on attempts. The executor only retries failures that are both transient
    and safe to repeat (see ``ActionExecutor``); it never retries a non-idempotent action
    whose first attempt may already have had an effect."""

    max_attempts: int = Field(default=2, ge=1, le=5)
    backoff_s: float = Field(default=0.5, ge=0, le=10)


# --------------------------------------------------------------------------- parameters

class OpenAppParams(StrictModel):
    app: str = Field(min_length=1, max_length=64, description="Name from the configured app allow-list")

    @field_validator("app")
    @classmethod
    def _norm(cls, v: str) -> str:
        return v.strip().lower()


class CloseAppParams(OpenAppParams):
    pass


class OpenUrlParams(StrictModel):
    url: str = Field(max_length=2048)
    new_tab: bool = False

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        return _check_web_url(v)


class ClickParams(StrictModel):
    button: Literal["left", "right", "middle"] = "left"
    double: bool = False


class TypeParams(StrictModel):
    text: str = Field(min_length=1, max_length=5000)
    clear_first: bool = False
    submit: bool = Field(default=False, description="Press Enter after typing")
    sensitive: bool = Field(default=False, description="Mask the text in logs, results and evidence")


class PressKeyParams(StrictModel):
    key: str
    presses: int = Field(default=1, ge=1, le=10)

    @field_validator("key")
    @classmethod
    def _norm(cls, v: str) -> str:
        k = normalize_key(v)
        if k in MODIFIERS:
            raise ValueError("press_key cannot press a bare modifier; use hotkey")
        return k


class HotkeyParams(StrictModel):
    keys: list[str] = Field(description='Keys such as ["ctrl", "s"]; "ctrl+s" is also accepted')

    @field_validator("keys", mode="before")
    @classmethod
    def _split(cls, v: Any) -> Any:
        return v.split("+") if isinstance(v, str) else v

    @field_validator("keys")
    @classmethod
    def _norm(cls, v: list[str]) -> list[str]:
        if not 2 <= len(v) <= 4:
            raise ValueError("a hotkey has 2-4 keys")
        return normalize_hotkey(v)

    @property
    def combo(self) -> str:
        return "+".join(self.keys)


class ScrollParams(StrictModel):
    direction: Literal["up", "down", "left", "right"] = "down"
    amount: int = Field(default=3, ge=1, le=20, description="Wheel notches")


class ReadParams(StrictModel):
    max_chars: int = Field(default=4000, ge=1, le=20000)


class ScreenshotParams(StrictModel):
    full_page: bool = False


class WaitParams(StrictModel):
    seconds: float | None = Field(default=None, gt=0, le=60)
    until: Condition | None = None

    @model_validator(mode="after")
    def _one(self) -> WaitParams:
        if (self.seconds is None) == (self.until is None):
            raise ValueError("wait needs exactly one of 'seconds' or 'until'")
        return self


class DownloadParams(StrictModel):
    url: str | None = Field(default=None, max_length=2048)
    save_as: str | None = Field(default=None, max_length=128)

    @field_validator("url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        return None if v is None else _check_web_url(v)

    @field_validator("save_as")
    @classmethod
    def _filename(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if any(c in v for c in '/\\:*?"<>|') or v.strip(". ") == "" or ".." in v:
            raise ValueError("save_as must be a plain file name, not a path")
        return v


PARAMETER_MODELS: dict[ActionType, type[StrictModel]] = {
    ActionType.OPEN_APP: OpenAppParams,
    ActionType.OPEN_URL: OpenUrlParams,
    ActionType.CLICK: ClickParams,
    ActionType.TYPE: TypeParams,
    ActionType.PRESS_KEY: PressKeyParams,
    ActionType.HOTKEY: HotkeyParams,
    ActionType.SCROLL: ScrollParams,
    ActionType.READ: ReadParams,
    ActionType.SCREENSHOT: ScreenshotParams,
    ActionType.WAIT: WaitParams,
    ActionType.DOWNLOAD: DownloadParams,
    ActionType.CLOSE_APP: CloseAppParams,
}


# --------------------------------------------------------------------------- action

class Action(StrictModel):
    action_id: str = Field(default_factory=new_action_id, min_length=1, max_length=64)
    type: ActionType
    target: Target | None = None
    parameters: Any = Field(default_factory=dict)
    timeout_s: float | None = Field(default=None, gt=0, le=600, description="Defaults to the configured timeout")
    preconditions: list[Condition] = Field(default_factory=list, max_length=10)
    expected_outcomes: list[Condition] = Field(default_factory=list, max_length=10)
    risk: Risk | None = Field(default=None, description="Set by the planner/safety layer; defaults to the capability's risk")
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)

    @model_validator(mode="after")
    def _typed_parameters(self) -> Action:
        model = PARAMETER_MODELS[self.type]
        if isinstance(self.parameters, model):
            return self
        if isinstance(self.parameters, BaseModel):
            raise ValueError(f"parameters for {self.type.value} must be {model.__name__}")
        self.parameters = model.model_validate(self.parameters or {})
        return self

    @property
    def is_sensitive(self) -> bool:
        return isinstance(self.parameters, TypeParams) and self.parameters.sensitive

    def log_view(self) -> dict[str, Any]:
        """A dump safe for logs: typed text is masked when sensitive and truncated otherwise."""
        data = self.model_dump(mode="json", exclude_none=True)
        if isinstance(self.parameters, TypeParams):
            text = self.parameters.text
            data["parameters"]["text"] = "***" if self.parameters.sensitive else (text[:80] + ("…" if len(text) > 80 else ""))
        return data


# --------------------------------------------------------------------------- results

EvidenceType = Literal[
    "screenshot",
    "url",
    "page_title",
    "window_title",
    "ui_element",
    "download_path",
    "observed_text",
    "fallback",
]


class Evidence(StrictModel):
    """An observation captured during execution. It is reported, never interpreted as business truth."""

    type: EvidenceType
    value: str
    timestamp: datetime = Field(default_factory=utcnow)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ActionErrorInfo(StrictModel):
    error_type: str
    message: str
    adapter: str | None = None
    target: dict[str, Any] | None = None
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class ActionResult(StrictModel):
    """Outcome of *executing* one action.

    ``success`` means the interaction was carried out. Whether it achieved what the task
    needed is a separate question: see ``outcome_checks`` / ``expected_outcomes_met`` and
    leave the final call to the higher-level verifier.
    """

    success: bool
    action_id: str
    action_type: str | None = None
    output: Any = None
    evidence: list[Evidence] = Field(default_factory=list)
    error: ActionErrorInfo | None = None
    duration_ms: int = Field(ge=0)
    retryable: bool = False
    attempts: int = Field(default=0, ge=0)
    adapter: str | None = None
    risk: Risk | None = None
    precondition_checks: list[ConditionCheck] = Field(default_factory=list)
    outcome_checks: list[ConditionCheck] = Field(default_factory=list)
    started_at: datetime
    finished_at: datetime

    @property
    def expected_outcomes_met(self) -> bool | None:
        """True if every expected outcome was observed, False if any was observed *not* to
        hold, None if there were none or some could not be checked here."""
        if not self.outcome_checks:
            return None
        statuses = {c.status for c in self.outcome_checks}
        if "unsatisfied" in statuses:
            return False
        return True if statuses == {"satisfied"} else None
