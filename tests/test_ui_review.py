"""审图界面的真实浏览器测试（真实键盘/鼠标事件；没装 playwright/chromium 就跳过）。"""
import io
import random
import threading
import time

import httpx
import pytest

pytest.importorskip("playwright.sync_api")
import uvicorn
from PIL import Image
from playwright.sync_api import expect, sync_playwright

from app import pipeline
from app.main import create_app
from conftest import FakeEmbedder, FakeFetcher, png_bytes

PORT = 8792
BASE = f"http://127.0.0.1:{PORT}"


@pytest.fixture
def ui(settings, monkeypatch):
    monkeypatch.setattr("app.main.make_fetchers", lambda s: [FakeFetcher()])
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
    app.state.run_profile(pid)
    yield token, pid
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
    pg.wait_for_selector("#card")
    return b, pg


def at(pg, text):
    """带自动等待的断言：点击后接口还没返回、界面还没重绘时不会误判。"""
    expect(pg.locator("#card .meta span:last-child")).to_have_text(text)


def tabs(pg, text):
    expect(pg.locator(".tabs")).to_contain_text(text)


def test_arrows_visible_reasons_and_rejudge(ui):
    token, _ = ui
    with sync_playwright() as p:
        b, pg = open_page(p, token)
        at(pg, "1 / 10")
        assert pg.is_visible("#prev") and pg.is_visible("#next") and pg.is_disabled("#prev")
        assert pg.locator(".reasons button").count() == 5 and pg.locator(".reasons button").first.is_visible()   # 无需先按键

        pg.click("#next"); at(pg, "2 / 10")                            # 屏幕箭头
        pg.keyboard.press("ArrowLeft"); at(pg, "1 / 10")               # 键盘箭头
        pg.keyboard.press("ArrowRight"); at(pg, "2 / 10")
        pg.keyboard.press("ArrowLeft"); at(pg, "1 / 10")

        pg.click(".reasons button[data-r=style]")                      # 一次点击：不要 + 原因
        at(pg, "2 / 10"); tabs(pg, "待审 9")
        pg.click("#prev")                                              # 回看刚才那张
        expect(pg.locator(".banner")).to_contain_text("已标记不要（画风不喜欢）")
        pg.click("#like")                                              # 改主意：喜欢
        tabs(pg, "已喜欢 1"); tabs(pg, "不要 0")
        pg.click("#prev")
        pg.click("#undo")                                              # 撤销
        expect(pg.locator(".banner")).to_have_count(0); tabs(pg, "待审 10")

        pg.keyboard.press("ArrowDown"); at(pg, "2 / 10")               # 键盘：↓ 不要
        pg.click(".tabs [data-tab=rejected]"); pg.wait_for_selector(".grid img")
        assert pg.eval_on_selector(".grid img", "e => getComputedStyle(e).objectFit") == "contain"   # 缩略图不裁剪
        b.close()


def test_large_reference_image_is_compressed_before_upload(ui, tmp_path):
    token, pid = ui
    rnd = random.Random(1)
    big = Image.new("RGB", (2400, 1600))
    big.putdata([(x * 255 // 2400, y * 255 // 1600, rnd.randrange(60)) for y in range(1600) for x in range(2400)])
    f = tmp_path / "big.png"
    big.save(f, "PNG")
    assert f.stat().st_size > 1024 * 1024                              # 比 nginx 默认的 1MB 上限大

    with sync_playwright() as p:
        b, pg = open_page(p, token)
        pg.click("#refp")
        with pg.expect_request(lambda r: r.method == "POST" and r.url.endswith(f"/profiles/{pid}/refs")) as req:
            pg.set_input_files("#r_file", str(f))
            pg.wait_for_selector("#r_grid .cell")
        assert int(req.value.all_headers()["content-length"]) <= 900 * 1024
        assert pg.inner_text("#r_err").strip() == ""
        b.close()
