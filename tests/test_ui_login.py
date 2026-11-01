"""用真实键盘事件测登录页（需要 playwright + chromium，没装就跳过）。
历史教训：用 fill() 会绕过键盘事件，曾因此漏掉「onkeydown 返回 false 吞掉所有按键」的 bug。"""
import threading
import time

import pytest

pytest.importorskip("playwright.sync_api")
import uvicorn
from playwright.sync_api import sync_playwright

from app.main import create_app
from conftest import FakeEmbedder


@pytest.fixture
def server(settings):
    config = uvicorn.Config(create_app(settings, embedder=FakeEmbedder(False)), port=8791, log_level="warning")
    srv = uvicorn.Server(config)
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(50):
        if srv.started:
            break
        time.sleep(0.1)
    yield "http://127.0.0.1:8791/"
    srv.should_exit = True
    t.join(timeout=5)


def test_can_type_password_with_real_keystrokes_and_login(server):
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:          # 没装浏览器
            pytest.skip(f"chromium unavailable: {e}")
        pg = b.new_page()
        pg.goto(server)
        pg.wait_for_selector("#pw")
        pg.click("#pw")
        pg.keyboard.type("correct horse battery")
        assert pg.input_value("#pw") == "correct horse battery"      # 字符真的进了输入框
        pg.check("#showpw")
        assert pg.get_attribute("#pw", "type") == "text"
        pg.click("#pw")                                              # 勾选后焦点在勾选框上，回到密码框再按回车
        pg.keyboard.press("Enter")                                   # 回车也能提交
        pg.wait_for_selector("#newp")
        b.close()
