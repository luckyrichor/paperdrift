from datetime import datetime, timedelta, timezone

import numpy as np

from app import purge
from app.embed import from_bytes
from app.fetchers.base import Candidate
from conftest import FakeEmbedder, png_bytes


def make_item(c, settings, source="wallhaven", sid="1", with_local=True, kind="image"):
    pid = c.post("/api/profiles", json={"name": "t", "query": "x"}).json()["id"] if not c.get("/api/profiles").json() else c.get("/api/profiles").json()[0]["id"]
    db = c.app.state.db
    prev = settings.previews_dir / f"{pid}_{source}_{sid}.jpg"
    prev.write_bytes(png_bytes(int(sid)))
    cand = Candidate(source=source, source_id=sid, kind=kind, preview_url="https://x/p.jpg", full_url="https://x/f.jpg" if source != "steam" else "",
                     page_url="u", width=1920, height=1080, tags=["a", "artist:foo"], file_size=999)
    iid = db.insert_item(pid, cand, prev, "f" * 16, 0.5)
    files = [prev]
    if with_local:
        lp = settings.library_dir / str(pid) / f"{source}_{sid}.bin"
        lp.parent.mkdir(parents=True, exist_ok=True)
        lp.write_bytes(b"x" * 5000)
        db.set_local_path(iid, str(lp))
        files.append(lp)
    return iid, files


def age(db, iid, minutes):
    t = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    with db.conn() as c:
        c.execute("UPDATE items SET decided_at=? WHERE id=?", (t, iid))


def test_rejected_files_deleted_after_grace_but_metadata_and_embedding_kept(authed, settings):
    c = authed
    iid, files = make_item(c, settings)
    db = c.app.state.db
    c.post(f"/api/items/{iid}/decision", json={"decision": "reject", "reason": "style"})
    assert purge.purge_rejected(db, settings, FakeEmbedder(True), grace_min=10)["count"] == 0     # 缓冲期内不删（可回看/撤销）
    assert all(f.exists() for f in files)
    age(db, iid, 11)
    res = purge.purge_rejected(db, settings, FakeEmbedder(True), grace_min=10)
    assert res["count"] == 1 and res["freed"] >= 5000
    assert not any(f.exists() for f in files)
    it = db.get_item(iid)
    assert it["purged_at"] and it["local_path"] is None
    assert it["tags"] == ["a", "artist:foo"] and it["phash"] == "f" * 16 and it["file_size"] == 999 and it["reason"] == "style"
    emb = db.decided_vectors(it["profile_id"], "rejected")[0]["embedding"]
    assert emb is not None and from_bytes(emb).ndim == 1                                          # 删文件前已提取向量
    assert c.get(f"/api/items/{iid}/preview").status_code == 410


def test_purge_now_ignores_grace_and_liked_or_new_are_untouched(authed, settings):
    c = authed
    rej, rf = make_item(c, settings, sid="1")
    liked, lf = make_item(c, settings, sid="2")
    new, nf = make_item(c, settings, sid="3")
    c.post(f"/api/items/{rej}/decision", json={"decision": "reject"})
    c.post(f"/api/items/{liked}/decision", json={"decision": "like"})
    pid = c.app.state.db.get_item(rej)["profile_id"]
    r = c.post(f"/api/profiles/{pid}/purge").json()
    assert r["count"] == 1
    assert not any(f.exists() for f in rf) and all(f.exists() for f in lf + nf)


def test_liked_then_rejected_deletes_downloaded_original_and_steam_files(authed, settings):
    c = authed
    iid, files = make_item(c, settings, source="steam", sid="777", with_local=False)
    pid = c.app.state.db.get_item(iid)["profile_id"]
    z = settings.library_dir / str(pid) / "steam_777.zip"
    z.parent.mkdir(parents=True, exist_ok=True)
    z.write_bytes(b"zip" * 100)
    folder = settings.steam_dir / "steamapps/workshop/content/431960/777"
    folder.mkdir(parents=True)
    (folder / "video.mp4").write_bytes(b"v" * 4000)
    c.app.state.db.set_local_path(iid, str(z))
    c.post(f"/api/items/{iid}/decision", json={"decision": "reject"})
    r = c.post(f"/api/profiles/{pid}/purge").json()
    assert r["count"] == 1 and not z.exists() and not folder.exists() and not files[0].exists()


def test_undo_after_purge_restores_preview_lazily(authed, settings, monkeypatch):
    from app import pipeline
    c = authed
    iid, files = make_item(c, settings, with_local=False)
    pid = c.app.state.db.get_item(iid)["profile_id"]
    c.post(f"/api/items/{iid}/decision", json={"decision": "reject"})
    c.post(f"/api/profiles/{pid}/purge")
    assert not files[0].exists()
    c.post(f"/api/items/{iid}/decision", json={"decision": "undo"})
    monkeypatch.setattr(pipeline, "download_bytes", lambda url, **kw: png_bytes(5))
    r = c.get(f"/api/items/{iid}/preview")
    assert r.status_code == 200 and r.content[:2] == b"\xff\xd8"
    assert c.app.state.db.get_item(iid)["purged_at"] is None


def test_download_finishing_after_reject_is_discarded(authed, settings, monkeypatch):
    import httpx
    c = authed
    iid, _ = make_item(c, settings, with_local=False)
    db = c.app.state.db

    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def raise_for_status(self): pass
        def iter_bytes(self):
            db.set_decision(iid, "rejected", None)                   # 下载过程中用户改了主意
            yield b"data" * 100

    monkeypatch.setattr(httpx, "stream", lambda *a, **k: Resp())
    db.set_decision(iid, "liked", None)
    c.get(f"/api/items/{iid}/file")                                    # 触发后台下载
    import time; time.sleep(0.5)
    it = db.get_item(iid)
    assert not it["local_path"] and not list((settings.library_dir).rglob("wallhaven_1*"))
