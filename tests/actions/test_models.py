import pytest
from pydantic import ValidationError

from actions.models import (
    Action,
    ActionResult,
    ActionType,
    Condition,
    ConditionCheck,
    HotkeyParams,
    Target,
    TypeParams,
    utcnow,
)


def test_parameters_are_typed_per_action_type():
    action = Action(type="type", target={"surface": "browser", "label": "Email"}, parameters={"text": "hi"})
    assert isinstance(action.parameters, TypeParams)
    assert action.action_id.startswith("act_")


def test_unknown_fields_are_rejected():
    with pytest.raises(ValidationError):
        Action(type="click", target={"surface": "browser", "text": "OK"}, parameters={}, exec="rm -rf /")
    with pytest.raises(ValidationError):
        Action(type="click", target={"surface": "browser", "text": "OK"}, parameters={"code": "print(1)"})


def test_unknown_action_type_is_rejected():
    with pytest.raises(ValidationError):
        Action(type="execute_python", parameters={"code": "print(1)"})


@pytest.mark.parametrize("url", ["javascript:alert(1)", "file:///C:/Windows/win.ini", "ftp://x.test", "example.com"])
def test_open_url_only_allows_http(url):
    with pytest.raises(ValidationError):
        Action(type="open_url", parameters={"url": url})


def test_hotkey_normalisation():
    assert HotkeyParams(keys="Control+S").keys == ["ctrl", "s"]
    assert HotkeyParams(keys=["s", "shift", "ctrl"]).combo == "ctrl+shift+s"
    for bad in ("s", "ctrl+shift", "ctrl+a+b", "ctrl+ctrl+s"):
        with pytest.raises(ValidationError):
            HotkeyParams(keys=bad)


def test_press_key_rejects_bare_modifier_and_normalises_alias():
    assert Action(type="press_key", parameters={"key": "Esc"}).parameters.key == "escape"
    with pytest.raises(ValidationError):
        Action(type="press_key", parameters={"key": "ctrl"})


def test_target_surface_rules():
    with pytest.raises(ValidationError):
        Target(surface="browser", window="Notepad")
    with pytest.raises(ValidationError):
        Target(surface="window", name="OK")  # window regex required
    with pytest.raises(ValidationError):
        Target(surface="screen", selector="#ok")
    with pytest.raises(ValidationError):
        Target(surface="screen", text="OK", point={"x": 1, "y": 2})  # one way to locate
    with pytest.raises(ValidationError):
        Target(surface="browser", name="Download")  # name needs role
    with pytest.raises(ValidationError):
        Target(surface="browser", role="button", allow_visual_fallback=True)  # needs a point or visible text
    assert Target(surface="screen", text="OK").is_element  # located by OCR
    assert Target(surface="browser", role="button", name="Pay", allow_visual_fallback=True).fallback_text == "Pay"
    assert Target(surface="browser", role="button", name="Download").is_element
    assert not Target(surface="browser").is_element
    assert not Target(surface="window", window=".*Notepad").is_element


def test_condition_shapes():
    with pytest.raises(ValidationError):
        Condition(kind="element_visible")
    with pytest.raises(ValidationError):
        Condition(kind="url_contains")
    with pytest.raises(ValidationError):
        Condition(kind="window_exists", target={"surface": "browser"})
    with pytest.raises(ValidationError):
        Condition(kind="custom")
    assert Condition(kind="url_contains", value="/bills").surface == "browser"


def test_wait_needs_exactly_one_mode():
    with pytest.raises(ValidationError):
        Action(type="wait", parameters={})
    with pytest.raises(ValidationError):
        Action(type="wait", parameters={"seconds": 1, "until": {"kind": "url_contains", "value": "x"}})
    with pytest.raises(ValidationError):
        Action(type="wait", parameters={"seconds": 61})


@pytest.mark.parametrize("name", ["../evil.pdf", "C:\\x.pdf", "a/b.pdf", "..", "x:y"])
def test_download_save_as_must_be_plain_filename(name):
    with pytest.raises(ValidationError):
        Action(type="download", parameters={"url": "https://x.test/f.pdf", "save_as": name})


def test_timeouts_and_retries_are_bounded():
    with pytest.raises(ValidationError):
        Action(type="read", target={"surface": "browser"}, timeout_s=0)
    with pytest.raises(ValidationError):
        Action(type="read", target={"surface": "browser"}, retry_policy={"max_attempts": 50})


def test_log_view_masks_sensitive_text():
    action = Action(type="type", parameters={"text": "hunter2", "sensitive": True})
    assert "hunter2" not in str(action.log_view())
    long = Action(type="type", parameters={"text": "x" * 500})
    assert len(long.log_view()["parameters"]["text"]) < 100


def test_expected_outcomes_met():
    cond = Condition(kind="custom", description="PDF download begins")
    now = utcnow()

    def result(*statuses):
        return ActionResult(success=True, action_id="a", duration_ms=1, started_at=now, finished_at=now,
                            outcome_checks=[ConditionCheck(condition=cond, status=s) for s in statuses])

    assert result().expected_outcomes_met is None
    assert result("satisfied", "satisfied").expected_outcomes_met is True
    assert result("satisfied", "unverified").expected_outcomes_met is None
    assert result("satisfied", "unsatisfied").expected_outcomes_met is False


def test_action_round_trips_through_json():
    action = Action(type="hotkey", parameters={"keys": "ctrl+s"}, expected_outcomes=[{"kind": "title_contains", "value": "Saved"}])
    again = Action.model_validate_json(action.model_dump_json())
    assert again == action
    assert again.type is ActionType.HOTKEY
