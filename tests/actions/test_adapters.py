"""Adapter unit tests (no real browser/desktop) plus opt-in integration tests."""

from __future__ import annotations

import ast
import http.server
import socketserver
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from actions.base import ExecutionContext
from actions.browser import _safe_filename, _unique
from actions.config import ActionSettings
from actions.errors import ActionPermissionError
from actions.models import Action
from actions.screen import VisualAdapter
from actions.windows import escape_send_keys, hotkey_sequence, key_sequence

ACTIONS_DIR = Path(__file__).resolve().parents[2] / "actions"
ADAPTER_MODULES = ["browser.py", "windows.py", "screen.py", "base.py"]


# --------------------------------------------------------------------------- isolation

@pytest.mark.parametrize("module", ADAPTER_MODULES)
def test_adapters_do_not_import_memory_planning_or_llm_code(module):
    tree = ast.parse((ACTIONS_DIR / module).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not imported & {"memory", "planner", "language", "openai", "anthropic", "requests"}


def test_action_block_never_imports_memory_block():
    for path in ACTIONS_DIR.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "from memory" not in text and "import memory" not in text, path.name


# --------------------------------------------------------------------------- helpers

def test_send_keys_escaping():
    assert escape_send_keys("a+b (c) {d} 50% ~") == "a{+}b {(}c{)} {{}d{}} 50{%} {~}"
    assert escape_send_keys("नमस्ते") == "नमस्ते"


def test_key_and_hotkey_sequences():
    assert key_sequence("enter") == "{ENTER}"
    assert key_sequence("enter", 3) == "{ENTER 3}"
    assert key_sequence("a", 2) == "aa"
    assert key_sequence("plus") == "{+}"
    assert hotkey_sequence(["ctrl", "s"]) == "^s"
    assert hotkey_sequence(["ctrl", "shift", "t"]) == "^+t"
    assert hotkey_sequence(["alt", "left"]) == "%{LEFT}"
    assert hotkey_sequence(["win", "d"]) == "{VK_LWIN down}d{VK_LWIN up}"


def test_download_filenames_are_sanitised(tmp_path):
    assert _safe_filename('..\\..\\evil:"x".pdf') == "_.._evil__x_.pdf"
    assert _safe_filename("...") == "download"
    (tmp_path / "bill.pdf").write_text("x")
    assert _unique(tmp_path / "bill.pdf").name == "bill (1).pdf"


# --------------------------------------------------------------------------- visual adapter with a fake pyautogui

class FakePyAutoGUI:
    class FailSafeException(Exception):
        pass

    def __init__(self, fail: bool = False):
        self.log: list[tuple] = []
        self.fail = fail

    def __getattr__(self, name):
        def record(*args, **kwargs):
            if self.fail:
                raise FakePyAutoGUI.FailSafeException()
            self.log.append((name, args, kwargs))
        return record


@pytest.fixture
def visual(tmp_path, monkeypatch):
    settings = ActionSettings(evidence_dir=tmp_path, visual_evidence=False, browser_user_data_dir=None, _env_file=None)
    adapter = VisualAdapter(settings)
    fake = FakePyAutoGUI()
    monkeypatch.setattr(VisualAdapter, "_pyautogui", staticmethod(lambda: fake))
    yield adapter, fake, settings
    adapter._runner.shutdown()


async def test_visual_click_and_hotkey(visual):
    adapter, fake, settings = visual
    ctx = ExecutionContext(settings, 5)
    await adapter.execute(Action(type="click", target={"surface": "screen", "point": {"x": 5, "y": 6}}), ctx)
    await adapter.execute(Action(type="hotkey", parameters={"keys": "ctrl+plus"}), ctx)
    await adapter.execute(Action(type="type", parameters={"text": "hello", "submit": True}), ctx)
    names = [entry[0] for entry in fake.log]
    assert names == ["click", "hotkey", "write", "press"]
    assert fake.log[0][1] == (5, 6)
    assert fake.log[1][1] == ("ctrl", "+")


async def test_visual_failsafe_maps_to_permission_error(visual, monkeypatch):
    adapter, _, settings = visual
    monkeypatch.setattr(VisualAdapter, "_pyautogui", staticmethod(lambda: FakePyAutoGUI(fail=True)))
    with pytest.raises(ActionPermissionError):
        await adapter.execute(Action(type="press_key", parameters={"key": "enter"}), ExecutionContext(settings, 5))


# --------------------------------------------------------------------------- integration (opt-in)

PAGE = b"""<!doctype html><html><head><title>Bills</title></head><body>
<h1>Your bills</h1>
<label>Account <input id="acct"></label>
<button onclick="document.getElementById('msg').textContent='Clicked!'">Show</button>
<p id="msg"></p>
<a href="/bill.pdf" download>Download latest bill</a>
</body></html>"""


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body, ctype = (PAGE, "text/html") if self.path == "/" else (b"%PDF-1.4 test", "application/pdf")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        if self.path != "/":
            self.send_header("Content-Disposition", 'attachment; filename="bill.pdf"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def local_site():
    server = socketserver.TCPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.mark.integration
async def test_playwright_end_to_end(local_site, tmp_path):
    from actions.executor import build_default_executor

    settings = ActionSettings(evidence_dir=tmp_path / "ev", download_dir=tmp_path / "dl", browser_headless=True,
                              browser_user_data_dir=None, _env_file=None)
    async with build_default_executor(settings) as ex:
        r = await ex.execute({"type": "open_url", "parameters": {"url": local_site},
                              "expected_outcomes": [{"kind": "title_contains", "value": "bills"}]})
        assert r.success and r.expected_outcomes_met, r
        r = await ex.execute({"type": "type", "target": {"surface": "browser", "label": "Account"},
                              "parameters": {"text": "JVVNL-123", "clear_first": True}})
        assert r.success, r
        r = await ex.execute({"type": "click", "target": {"surface": "browser", "role": "button", "name": "Show"},
                              "expected_outcomes": [{"kind": "text_present", "value": "Clicked!", "wait_s": 2}]})
        assert r.success and r.expected_outcomes_met, r
        r = await ex.execute({"type": "read", "target": {"surface": "browser", "text": "Your bills"}})
        assert r.output["text"] == "Your bills"
        r = await ex.execute({"type": "click", "target": {"surface": "browser", "role": "button", "name": "Nope"},
                              "timeout_s": 2, "retry_policy": {"max_attempts": 1}})
        assert r.error.error_type == "TargetNotFoundError"
        r = await ex.execute({"type": "download", "target": {"surface": "browser", "role": "link", "name": "Download latest bill"},
                              "expected_outcomes": [{"kind": "download_completed"}]})
        assert r.success and r.expected_outcomes_met, r
        assert Path(r.output["path"]).read_bytes().startswith(b"%PDF")
        r = await ex.execute({"type": "screenshot", "target": {"surface": "browser"}})
        assert Path(r.output["path"]).exists()


@pytest.mark.integration
@pytest.mark.skipif(sys.platform != "win32", reason="Windows UI Automation")
async def test_windows_notepad_round_trip(tmp_path):
    from actions.config import AppEntry
    from actions.executor import build_default_executor

    # Open a dedicated empty file and match only its window. Windows 11 Notepad restores old
    # tabs, so a generic ".*Notepad.*" match could type into one of the user's own documents.
    scratch = tmp_path / "sarvam_ui_test.txt"
    scratch.write_text("", encoding="utf-8")
    title_re = ".*sarvam_ui_test.*"
    settings = ActionSettings(evidence_dir=tmp_path, browser_user_data_dir=None, _env_file=None,
                              apps={"notepad": AppEntry(command=["notepad.exe", str(scratch)], window_title_re=title_re)})
    async with build_default_executor(settings) as ex:
        r = await ex.execute({"type": "open_app", "parameters": {"app": "notepad"}})
        assert r.success, r
        title = r.output["window_title"]
        assert "sarvam_ui_test" in title, title
        win = {"surface": "window", "window": title_re}
        try:
            r = await ex.execute({"type": "type", "target": {**win, "control_type": "Document"},
                                  "parameters": {"text": "नमस्ते hello (test) 100%"}})
            assert r.success, r
            r = await ex.execute({"type": "read", "target": {**win, "control_type": "Document"}})
            assert "नमस्ते hello (test) 100%" in r.output["text"], r.output
        finally:
            # Only control-targeted cleanup: blind keystrokes could land in another window.
            r = await ex.execute({"type": "close_app", "parameters": {"app": "notepad"}})
            if not r.output or not r.output.get("closed"):
                await ex.execute({"type": "click", "target": {**win, "name": "Don't save"}, "timeout_s": 3})
        assert title
