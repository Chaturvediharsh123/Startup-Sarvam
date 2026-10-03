import json

import pytest

from actions.errors import ActionValidationError, UnsupportedActionError
from actions.models import Action, ActionType
from actions.registry import ActionRegistry, Capability

SPEC_CAPABILITIES = {
    "open_app", "open_url", "click", "type", "press_key", "hotkey",
    "scroll", "read", "screenshot", "wait", "download", "close_app",
}


def test_registry_exposes_exactly_the_spec_capability_set():
    registry = ActionRegistry()
    assert set(registry.names) == SPEC_CAPABILITIES
    tool_names = {t["function"]["name"] for t in registry.tool_definitions()}
    assert tool_names == SPEC_CAPABILITIES


def test_no_code_execution_capability_exists():
    names = " ".join(ActionRegistry().names) + " ".join(t.value for t in ActionType)
    for forbidden in ("python", "shell", "powershell", "exec", "eval", "cmd", "run"):
        assert forbidden not in names


def test_tool_schemas_are_json_and_forbid_extra_parameters():
    for tool in ActionRegistry().tool_definitions():
        schema = tool["function"]["parameters"]
        json.dumps(schema)
        assert "parameters" in schema["properties"]


def test_disabled_capability_is_unregistered():
    registry = ActionRegistry(disabled=["download"])
    assert not registry.is_registered("download")
    with pytest.raises(UnsupportedActionError):
        registry.get("download")


def test_unknown_capability():
    with pytest.raises(UnsupportedActionError):
        ActionRegistry().get("execute_shell")


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "click"},  # needs element target
        {"type": "click", "target": {"surface": "browser"}},  # page-level only
        {"type": "open_url", "parameters": {"url": "https://x.test"}, "target": {"surface": "browser"}},
        {"type": "read", "target": {"surface": "screen"}},  # no OCR surface
        {"type": "download", "parameters": {}, "target": {"surface": "browser"}},  # neither element nor url
        {"type": "download", "parameters": {"url": "https://x.test/a.pdf"},
         "target": {"surface": "browser", "text": "Download"}},  # both
        {"type": "download", "parameters": {"url": "https://x.test/a.pdf"},
         "target": {"surface": "window", "window": "x", "name": "y"}},
    ],
)
def test_capability_shape_checks(payload):
    with pytest.raises(ActionValidationError):
        ActionRegistry().check(Action.model_validate(payload))


def test_surface_defaults_and_risk():
    registry = ActionRegistry()
    assert registry.surface_for(Action(type="open_url", parameters={"url": "https://x.test"})) == "browser"
    assert registry.surface_for(Action(type="hotkey", parameters={"keys": "ctrl+s"})) == "screen"
    click = Action(type="click", target={"surface": "window", "window": "Notepad", "name": "OK"})
    assert registry.surface_for(click) == "window"
    assert registry.effective_risk(click) == "medium"
    assert registry.effective_risk(click.model_copy(update={"risk": "high"})) == "high"


def test_action_from_tool_call():
    registry = ActionRegistry()
    action = registry.action_from_tool_call(
        "click", json.dumps({"target": {"surface": "browser", "role": "button", "name": "Download"}, "parameters": {}})
    )
    assert action.type is ActionType.CLICK
    with pytest.raises(ActionValidationError):
        registry.action_from_tool_call("click", "{not json")
    with pytest.raises(ActionValidationError):
        registry.action_from_tool_call("open_url", {"parameters": {"url": "javascript:alert(1)"}})
    with pytest.raises(UnsupportedActionError):
        registry.action_from_tool_call("execute_python", {"code": "1"})


def test_register_replaces_capability():
    registry = ActionRegistry()
    registry.register(Capability(ActionType.READ, "read", "optional", frozenset({"browser"}), "browser",
                                 idempotent=True, default_risk="low"))
    registry.check(Action(type="read"))  # target became optional
