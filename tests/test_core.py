import random

from app import phash
from app.auth import check_token, make_token
from app.pipeline import parse_terms
from app.scheduler import is_due
from app.scoring import score
from conftest import png_bytes


def test_parse_terms():
    assert parse_terms("raiden shogun, genshin impact，night\n") == ["raiden shogun", "genshin impact", "night"]


def test_phash_same_image_resized_is_duplicate_and_different_is_not():
    import io
    from PIL import Image
    a = png_bytes(1)
    im = Image.open(io.BytesIO(a)).resize((32, 24))
    buf = io.BytesIO(); im.save(buf, "PNG")
    assert phash.is_duplicate(phash.dhash(buf.getvalue()), [phash.dhash(a)])
    assert not phash.is_duplicate(phash.dhash(png_bytes(2)), [phash.dhash(a)])


def test_score_prefers_liked_tags():
    w = {"purple_hair": 2.0, "night": -2.0}
    assert score(["purple_hair"], w) > score(["night"], w)
    assert score([], {}, prior=1.0) > score([], {}, prior=0.0)


def test_token_roundtrip_and_tamper():
    t = make_token("k" * 32)
    assert check_token("k" * 32, t)
    assert not check_token("x" * 32, t)
    assert not check_token("k" * 32, t[:-2] + "aa")
    assert not check_token("k" * 32, make_token("k" * 32, ttl=-1))


def test_is_due():
    base = {"enabled": 1, "interval_hours": 24, "last_run": None}
    assert is_due(base)
    from datetime import datetime, timedelta, timezone
    recent = dict(base, last_run=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat())
    old = dict(base, last_run=(datetime.now(timezone.utc) - timedelta(hours=25)).isoformat())
    assert not is_due(recent) and is_due(old)
    assert not is_due(dict(old, enabled=0))


def test_all_api_requires_auth(client):
    for path in ("/api/profiles", "/api/items?profile_id=1", "/api/me", "/api/items/1/preview"):
        assert client.get(path).status_code == 401
    assert client.post("/api/profiles", json={"name": "a", "query": "b"}).status_code == 401


def test_login_wrong_password_and_lockout(client):
    for _ in range(5):
        assert client.post("/api/login", json={"password": "nope"}).status_code == 401
    assert client.post("/api/login", json={"password": "correct horse battery"}).status_code == 429


def test_bearer_token_works_for_apk(client):
    tok = client.post("/api/login", json={"password": "correct horse battery"}).json()["token"]
    client.cookies.clear()
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/me", headers={"Authorization": f"Bearer {tok}"}).status_code == 200


def test_full_flow_run_review_learn(authed):
    c = authed
    p = c.post("/api/profiles", json={"name": "t", "query": "girl, purple hair", "batch_size": 10}).json()
    pid = p["id"]
    assert c.post(f"/api/profiles/{pid}/run").json()["started"]
    items = c.get(f"/api/items?profile_id={pid}").json()
    assert len(items) == 10
    assert c.get(f"/api/items/{items[0]['id']}/preview").status_code == 200

    # 第二次抓取：已推过的绝不重复
    c.post(f"/api/profiles/{pid}/run")
    items2 = c.get(f"/api/items?profile_id={pid}&limit=200").json()
    keys = [(i["source"], i["source_id"]) for i in items2]
    assert len(keys) == len(set(keys)) == 20

    # 喜欢 girl/purple_hair，不要 boy/night → 之后新图里 girl 的分数更高
    for i in items2:
        if "girl" in i["tags"]:
            c.post(f"/api/items/{i['id']}/decision", json={"decision": "like"})
        else:
            c.post(f"/api/items/{i['id']}/decision", json={"decision": "reject", "reason": "style"})
    w = c.app.state.db.weights(pid)
    assert w["girl"] > 0 > w["night"]

    # 撤销应还原权重
    liked = c.get(f"/api/items?profile_id={pid}&status=liked").json()
    before = c.app.state.db.weights(pid)["girl"]
    c.post(f"/api/items/{liked[0]['id']}/decision", json={"decision": "undo"})
    assert c.app.state.db.weights(pid)["girl"] == before - 1


def test_target_filter_and_bad_reason(authed):
    c = authed
    pid = c.post("/api/profiles", json={"name": "t", "query": "x", "target": "portrait", "batch_size": 10}).json()["id"]
    c.post(f"/api/profiles/{pid}/run")
    items = c.get(f"/api/items?profile_id={pid}").json()
    assert items and all(i["orientation"] == "portrait" for i in items)
    r = c.post(f"/api/items/{items[0]['id']}/decision", json={"decision": "reject", "reason": "<script>"})
    assert r.status_code == 400


def test_original_download_only_for_liked(authed):
    c = authed
    pid = c.post("/api/profiles", json={"name": "t", "query": "x", "batch_size": 5}).json()["id"]
    c.post(f"/api/profiles/{pid}/run")
    i = c.get(f"/api/items?profile_id={pid}").json()[0]
    assert c.get(f"/api/items/{i['id']}/file").status_code == 404


def test_learning_changes_next_batch(authed):
    c = authed
    pid = c.post("/api/profiles", json={"name": "t", "query": "x", "batch_size": 10}).json()["id"]
    c.post(f"/api/profiles/{pid}/run")
    for i in c.get(f"/api/items?profile_id={pid}").json():
        d = "like" if "girl" in i["tags"] else "reject"
        c.post(f"/api/items/{i['id']}/decision", json={"decision": d})
    c.post(f"/api/profiles/{pid}/run")
    new = c.get(f"/api/items?profile_id={pid}").json()
    assert len(new) == 10
    assert sum("girl" in i["tags"] for i in new) >= 8     # 80% 按口味，20% 探索
