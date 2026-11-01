import threading

from fastapi.testclient import TestClient

from app import pipeline
from app.db import DB
from app.main import create_app
from app.pipeline import summarize
from conftest import FakeEmbedder, FakeFetcher, png_bytes


def fetch(url, **kw):
    return png_bytes(int(url.rsplit("/", 1)[-1]))


def mkprofile(tmp_path, **kw):
    db = DB(tmp_path / "p.db")
    pid = db.create_profile("t", kw.pop("query", "x"), batch_size=kw.pop("batch_size", 10))
    (tmp_path / "pv").mkdir()
    return db, db.get_profile(pid), tmp_path / "pv"


def test_pipeline_reports_every_stage_in_order(tmp_path):
    db, prof, pv = mkprofile(tmp_path)
    seen = []
    stats = pipeline.run_profile(db, prof, [FakeFetcher()], pv, fetch_bytes=fetch,
                                 progress=lambda stage, done=0, total=0, detail="": seen.append((stage, done, total)))
    stages = list(dict.fromkeys(s for s, *_ in seen))
    assert stages == ["搜索各来源", "读取标签", "下载缩略图", "打分排序", "保存入选的图"]
    assert seen[-1] == ("保存入选的图", stats["inserted"], 10)
    assert set(stages) <= set(pipeline.STAGE_RANGE)                    # 每个阶段都有进度条区间


def test_stop_midway_keeps_what_was_saved(tmp_path):
    db, prof, pv = mkprofile(tmp_path)
    flag = {"stop": False}

    def progress(stage, done=0, total=0, detail=""):
        if stage == "保存入选的图" and done >= 3:
            flag["stop"] = True

    stats = pipeline.run_profile(db, prof, [FakeFetcher()], pv, fetch_bytes=fetch,
                                 progress=progress, should_stop=lambda: flag["stop"])
    assert stats["cancelled"] and stats["inserted"] == 3
    assert len(db.list_items(prof["id"])) == 3                          # 已入库的保留


def test_summarize_explains_each_empty_outcome():
    base = {"fetched": 0, "inserted": 0, "dup": 0, "clip": False, "cancelled": False, "by_source": {}, "errors": []}
    cn = summarize(base, {"query": "原神女角色，只要一个角色"})
    assert "没有搜到任何结果" in cn and "中文" in cn and "genshin impact" in cn
    en = summarize(base, {"query": "raiden shogun"})
    assert "没有搜到任何结果" in en and "中文" not in en
    used_up = summarize(dict(base, fetched=80, fresh=0, by_source={"wallhaven": 80}), {"query": "x"})
    assert "已经推过" in used_up and "wallhaven 80" in used_up
    err = summarize(dict(base, errors=["danbooru: boom"]), {"query": "x"})
    assert "danbooru: boom" in err and "没有搜到任何结果" not in err
    assert summarize(dict(base, inserted=5, fetched=60, fresh=50, clip=True), {"query": "x"}).startswith("新增 5")
    assert summarize(dict(base, cancelled=True), {"query": "x"}).startswith("已取消")
    assert summarize(dict(base, cancelled=True), {"query": "x"}, timed_out=True).startswith("已超时")


def test_zero_result_run_says_why(authed, monkeypatch):
    c = authed
    monkeypatch.setattr("app.main.make_fetchers", lambda s: [FakeFetcher(n=0)])
    pid = c.post("/api/profiles", json={"name": "t", "query": "原神女角色，一张图里只有一个角色"}).json()["id"]
    c.post(f"/api/profiles/{pid}/run")
    st = c.get("/api/profiles").json()[0]["last_status"]
    assert "没有搜到任何结果" in st and "中文" in st


def test_second_run_while_running_is_refused_and_progress_is_visible(authed):
    c = authed
    pid = c.post("/api/profiles", json={"name": "t", "query": "x"}).json()["id"]
    assert c.get("/api/profiles").json()[0]["running"] is None
    assert c.app.state.claim(pid)                                       # 模拟「正在抓取」
    c.app.state.progress[pid].update(stage="读取标签", done=5, total=20, pct=15.0)
    prof = c.get("/api/profiles").json()[0]
    assert prof["running"]["stage"] == "读取标签" and prof["running"]["pct"] == 15
    r = c.post(f"/api/profiles/{pid}/run").json()
    assert r["started"] is False and "不会重复" in r["reason"]
    assert c.post(f"/api/profiles/{pid}/cancel").json() == {"cancelled": True}
    assert c.app.state.cancels[pid].is_set()
    assert c.get("/api/profiles").json()[0]["running"]["cancelling"] is True


def test_cancel_when_idle_and_auth_required(authed, client):
    c = authed
    pid = c.post("/api/profiles", json={"name": "t", "query": "x"}).json()["id"]
    assert c.post(f"/api/profiles/{pid}/cancel").json()["cancelled"] is False
    c.cookies.clear()
    assert c.post(f"/api/profiles/{pid}/cancel").status_code == 401


def test_claim_is_exclusive_and_released_after_run(authed):
    c = authed
    pid = c.post("/api/profiles", json={"name": "t", "query": "x", "batch_size": 5}).json()["id"]
    assert c.app.state.claim(pid) and not c.app.state.claim(pid)        # 同一需求不会并发跑
    c.app.state.running.discard(pid)
    c.post(f"/api/profiles/{pid}/run")
    assert pid not in c.app.state.running and c.get("/api/profiles").json()[0]["running"] is None   # 跑完自动释放


def test_stale_running_status_is_cleared_on_startup(settings, monkeypatch):
    db = DB(settings.db_path)
    pid = db.create_profile("t", "x")
    db.update_profile(pid, last_status="运行中…")
    create_app(settings, embedder=FakeEmbedder(False))
    assert "被中断" in DB(settings.db_path).get_profile(pid)["last_status"]
