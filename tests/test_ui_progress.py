"""抓取进度、取消、重复点击、中文关键词提示的真实浏览器测试（没装 playwright/chromium 就跳过）。"""
import threading
import time

import httpx
import pytest

pytest.importorskip("playwright.sync_api")
import uvicorn
from playwright.sync_api import expect, sync_playwright

from app import pipeline
from app.main import create_app
from conftest import FakeEmbedder, FakeFetcher, png_bytes

PORT = 8793
BASE = f"http://127.0.0.1:{PORT}"
LONG = 15000


class GatedFetcher(FakeFetcher):
    """search() 会一直卡住，直到测试放行——用来稳定地观察「正在抓取」的界面。"""
    gate = threading.Event()

    def search(self, terms, profile, pages=2):
        self.gate.wait(30)
        return super().search(terms, profile, pages)


@pytest.fixture
def ui(settings, monkeypatch):
    GatedFetcher.gate = threading.Event()
    monkeypatch.setattr("app.main.make_fetchers", lambda s: [GatedFetcher()])
    monkeypatch.setattr(pipeline, "download_bytes", lambda url, **kw: png_bytes(int(url.rsplit("/", 1)[-1])))
    app = create_app(settings, embedder=FakeEmbedder(False))
    srv = uvicorn.Server(uvicorn.Config(app, port=PORT, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(50):
        if srv.started:
            break
        time.sleep(0.1)
    token = httpx.post(f"{BASE}/api/login", json={"password": "correct horse battery"}).json()["token"]
    h = {"Authorization": f"Bearer {token}"}
    pid = httpx.post(f"{BASE}/api/profiles", headers=h, json={"name": "t", "query": "x", "batch_size": 10}).json()["id"]
    yield token, pid, h
    GatedFetcher.gate.set()
    srv.should_exit = True
    t.join(timeout=5)


def open_page(p, token):
    try:
        b = p.chromium.launch()
    except Exception as e:
        pytest.skip(f"chromium unavailable: {e}")
    ctx = b.new_context(viewport={"width": 390, "height": 900})
    ctx.add_cookies([{"name": "pd_token", "value": token, "url": BASE}])
    pg = ctx.new_page()
    pg.goto(BASE)
    pg.wait_for_selector("#runp")
    return b, pg


def test_progress_bar_double_click_protection_and_auto_load(ui):
    token, pid, h = ui
    assert httpx.post(f"{BASE}/api/profiles/{pid}/run", headers=h).json()["started"] is True
    with sync_playwright() as p:
        b, pg = open_page(p, token)
        expect(pg.locator("#prog .progress")).to_be_visible()                       # 有进度条
        expect(pg.locator("#prog")).to_contain_text("搜索各来源")                       # 说明在做什么
        expect(pg.locator("#runp")).to_be_disabled()
        expect(pg.locator("#runp")).to_have_text("抓取中…")
        r = httpx.post(f"{BASE}/api/profiles/{pid}/run", headers=h).json()          # 再点一次：不会重复开始
        assert r["started"] is False and "不会重复" in r["reason"]

        GatedFetcher.gate.set()                                                     # 放行 → 自动抓完、自动载入
        expect(pg.locator("#prog .progress")).to_have_count(0, timeout=LONG)
        expect(pg.locator(".tabs")).to_contain_text("待审 10", timeout=LONG)
        expect(pg.locator("#card")).to_be_visible(timeout=LONG)                     # 不用手动刷新
        expect(pg.locator("#runp")).to_be_enabled()
        expect(pg.locator("#prog")).to_contain_text("新增 10")
        b.close()


def test_cancel_from_the_page(ui):
    token, pid, h = ui
    httpx.post(f"{BASE}/api/profiles/{pid}/run", headers=h)
    with sync_playwright() as p:
        b, pg = open_page(p, token)
        pg.click("#cancelp")
        expect(pg.locator("#prog")).to_contain_text("正在取消")
        GatedFetcher.gate.set()
        expect(pg.locator("#prog .progress")).to_have_count(0, timeout=LONG)
        expect(pg.locator("#prog")).to_contain_text("已取消", timeout=LONG)
        expect(pg.locator("#runp")).to_be_enabled()
        b.close()


def test_chinese_keywords_get_a_warning_in_the_new_profile_dialog(ui):
    token, *_ = ui
    with sync_playwright() as p:
        b, pg = open_page(p, token)
        pg.click("#newp")
        pg.click("#n_q")
        pg.keyboard.type("genshin impact, solo")
        expect(pg.locator("#n_hint")).to_be_hidden()
        pg.fill("#n_q", "")
        pg.keyboard.type("原神女角色，只能有一个角色")
        expect(pg.locator("#n_hint")).to_be_visible()
        expect(pg.locator("#n_hint")).to_contain_text("英文")
        b.close()
