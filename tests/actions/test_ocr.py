"""OCR text location for the visual adapter (fake engine + opt-in real Windows OCR)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from actions import screen
from actions.base import AdapterOutcome, ExecutionContext
from actions.config import ActionSettings
from actions.errors import TargetNotFoundError, UnsupportedActionError
from actions.executor import ActionExecutor
from actions.models import Action, Condition
from actions.ocr import OcrWord, TextMatch, WindowsOcrEngine, create_ocr_engine, find_text, parse_tesseract_tsv
from actions.screen import XY, VisualAdapter

from .conftest import FakeAdapter, browser_target
from .test_adapters import FakePyAutoGUI

WORDS = [
    OcrWord("Your", 40, 46, 68, 23, line=0), OcrWord("bills", 119, 46, 52, 23, line=0),
    OcrWord("Download", 402, 126, 139, 23, line=1), OcrWord("latest", 554, 126, 75, 23, line=1),
    OcrWord("bill", 640, 126, 35, 23, line=1),
    OcrWord("डाउनलोड", 40, 200, 100, 25, line=2), OcrWord("करें", 150, 200, 50, 25, line=2),
    OcrWord("Download:", 40, 300, 120, 23, line=3),
]


def test_find_text_multiword_case_insensitive():
    matches = find_text(WORDS, "download LATEST bill")
    assert matches == [TextMatch("Download latest bill", 402, 126, 273, 23)]
    assert matches[0].center == (538, 137)


def test_find_text_single_word_reading_order_and_punctuation():
    assert [m.text for m in find_text(WORDS, "Download")] == ["Download", "Download:"]
    assert find_text(WORDS, "डाउनलोड करें")[0].left == 40
    assert find_text(WORDS, "upload") == [] and find_text(WORDS, "  ") == []
    assert find_text(WORDS, "bill")[0].text == "bills"  # partial match inside a longer word


def test_parse_tesseract_tsv():
    tsv = ("level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
           "4\t1\t1\t1\t1\t0\t10\t10\t200\t20\t-1\t\n"
           "5\t1\t1\t1\t1\t1\t10\t10\t80\t20\t96.1\tबिल\n"
           "5\t1\t1\t1\t1\t2\t95\t10\t90\t20\t91.0\tडाउनलोड\n"
           "5\t1\t1\t1\t2\t1\t10\t40\t50\t20\t-1\t \n")
    words = parse_tesseract_tsv(tsv)
    assert [w.text for w in words] == ["बिल", "डाउनलोड"] and words[0].line == words[1].line


def test_auto_engine_falls_back_to_windows_or_none(monkeypatch):
    settings = ActionSettings(tesseract_cmd="Z:/missing/tesseract.exe", browser_user_data_dir=None, _env_file=None)
    engine = create_ocr_engine(settings)
    assert engine is None or engine.name == "windows"


# --------------------------------------------------------------------------- visual adapter with fake OCR

class FakeOcr:
    name = "fake"

    def __init__(self, words: list[OcrWord]) -> None:
        self.words = words

    def available(self) -> bool:
        return True

    def recognize(self, image_path: Path) -> list[OcrWord]:
        return self.words


@pytest.fixture
def visual(tmp_path, monkeypatch):
    settings = ActionSettings(evidence_dir=tmp_path, visual_evidence=False, browser_user_data_dir=None, _env_file=None)
    fake_pg = FakePyAutoGUI()
    monkeypatch.setattr(VisualAdapter, "_pyautogui", staticmethod(lambda: fake_pg))
    # Second monitor to the left: virtual screen starts at x=-1920.
    monkeypatch.setattr(screen, "grab_screen", lambda path: (path, XY(-1920, 0)))
    adapter = VisualAdapter(settings, ocr=FakeOcr(WORDS))
    yield adapter, fake_pg, settings
    adapter._runner.shutdown()


async def test_click_on_text_uses_ocr_position_and_monitor_origin(visual):
    adapter, pg, settings = visual
    out = await adapter.execute(Action(type="click", target={"surface": "screen", "text": "Download latest bill"}),
                                ExecutionContext(settings, 5))
    assert pg.log[0][0] == "click" and pg.log[0][1] == (538 - 1920, 137)
    located = out.evidence[0]
    assert located.type == "ui_element" and located.metadata["located_by"] == "ocr:fake"


async def test_missing_text_is_target_not_found(visual):
    adapter, pg, settings = visual
    with pytest.raises(TargetNotFoundError) as info:
        await adapter.execute(Action(type="click", target={"surface": "screen", "text": "Pay now"}),
                              ExecutionContext(settings, 5))
    assert info.value.may_have_side_effects is False and info.value.evidence
    assert pg.log == []


async def test_no_ocr_engine_is_unsupported(tmp_path, monkeypatch):
    settings = ActionSettings(evidence_dir=tmp_path, browser_user_data_dir=None, _env_file=None)
    monkeypatch.setattr(VisualAdapter, "_pyautogui", staticmethod(lambda: FakePyAutoGUI()))
    adapter = VisualAdapter(settings, ocr=None)
    with pytest.raises(UnsupportedActionError):
        await adapter.execute(Action(type="click", target={"surface": "screen", "text": "OK"}), ExecutionContext(settings, 5))
    adapter._runner.shutdown()


async def test_screen_text_conditions(visual):
    adapter, _, settings = visual
    ctx = ExecutionContext(settings, 5)
    visible = Condition(kind="element_visible", target={"surface": "screen", "text": "Your bills"})
    hidden = Condition(kind="element_hidden", target={"surface": "screen", "text": "Error"})
    text = Condition(kind="text_present", target={"surface": "screen"}, value="latest bill")
    assert [(await adapter.check(c, ctx)).status for c in (visible, hidden, text)] == ["satisfied"] * 3


async def test_executor_falls_back_from_browser_to_screen_text(tmp_path):
    adapters = {"browser": FakeAdapter("browser"), "screen": FakeAdapter("visual")}
    adapters["browser"].script = [TargetNotFoundError("shadow DOM")]
    executor = ActionExecutor(adapters, settings=ActionSettings(evidence_dir=tmp_path, screenshot_on_failure=False,
                                                                browser_user_data_dir=None, _env_file=None))
    target = browser_target(role="button", name="Download", allow_visual_fallback=True)
    result = await executor.execute({"type": "click", "target": target, "retry_policy": {"max_attempts": 1}})
    assert result.success and result.adapter == "visual"
    sent = adapters["screen"].calls[0].target
    assert sent.surface == "screen" and sent.text == "Download" and sent.point is None


# --------------------------------------------------------------------------- real Windows OCR (opt-in)

@pytest.mark.integration
@pytest.mark.skipif(sys.platform != "win32", reason="Windows.Media.Ocr")
def test_windows_ocr_reads_rendered_text(tmp_path):
    from PIL import Image, ImageDraw, ImageFont

    engine = WindowsOcrEngine(["en", "hi"])
    if not engine.available():
        pytest.skip("winrt OCR packages not installed")
    image = Image.new("RGB", (900, 200), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("arial.ttf", 32)
    draw.text((40, 40), "Your bills", fill="black", font=font)
    draw.text((400, 120), "Download latest bill", fill="black", font=font)
    path = tmp_path / "ocr.png"
    image.save(path)
    match = find_text(engine.recognize(path), "Download latest bill")[0]
    assert abs(match.left - 400) < 10 and abs(match.top - 125) < 10


@pytest.mark.integration
@pytest.mark.skipif(sys.platform != "win32", reason="Windows desktop")
async def test_live_ocr_click_on_visible_browser(tmp_path):
    """Moves the real mouse: opens a visible page whose button shows a random code word, raises
    the window, finds the word on screen with OCR and clicks it. Run from your own terminal:
    pytest -m integration -k live_ocr"""
    import http.server
    import secrets
    import socketserver
    import threading

    from actions.executor import build_default_executor

    token = "Kx" + "".join(secrets.choice("ACDEFHJKLMNPRTUVWXY") for _ in range(6))  # no 0/O-style lookalikes
    page = (f"<!doctype html><title>OCR check {token}</title><body style='font:48px Arial;padding:80px'>"
            f"<button style='font:48px Arial' onclick=\"m.textContent='Clicked'\">{token}</button>"
            "<p id=m></p></body>").encode()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(page)

        def log_message(self, *args):
            pass

    server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    settings = ActionSettings(evidence_dir=tmp_path, browser_user_data_dir=None, _env_file=None)
    try:
        async with build_default_executor(settings) as ex:
            assert (await ex.execute({"type": "open_url",
                                      "parameters": {"url": f"http://127.0.0.1:{server.server_address[1]}"}})).success
            from pywinauto import Desktop

            window = Desktop(backend="uia").window(title_re=f".*OCR check {token}.*", found_index=0)
            if not window.exists(timeout=10):
                pytest.skip("automation browser window is not on this desktop (e.g. run from a non-interactive session)")
            window.wrapper_object().set_focus()
            result = await ex.execute({"type": "click", "target": {"surface": "screen", "text": token}, "timeout_s": 15,
                                       "expected_outcomes": [{"kind": "text_present", "value": "Clicked", "wait_s": 3}]})
            assert result.success, result.error
            assert result.expected_outcomes_met
    finally:
        server.shutdown()
