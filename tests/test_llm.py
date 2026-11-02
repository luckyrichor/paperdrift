import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.llm import TranslateError, Translator, clean_terms
from app.main import create_app
from conftest import FakeEmbedder, FakeFetcher, png_bytes
from app import pipeline

KEY = "test-key-not-real"


def mock_translator(handler, key=KEY):
    return Translator(key, client=httpx.Client(transport=httpx.MockTransport(handler)))


def ok_response(payload):
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(payload)}}]})


def test_clean_terms_drops_dangerous_and_dupes():
    raw = ["Genshin Impact", "rating:explicit", "-solo", "~x", "order:rank", "solo", "solo", "1girl", 5, "a;b"]
    assert clean_terms(raw, 6, snake=True) == ["genshin_impact", "solo", "1girl"]
    assert clean_terms(["genshin_impact", "solo"], 4, snake=False) == ["genshin impact", "solo"]
    assert clean_terms("nope", 3, True) == []


def test_translate_ok_sends_auth_json_mode_and_never_leaks_key():
    seen = {}

    def h(req):
        seen["auth"] = req.headers["authorization"]
        seen["body"] = json.loads(req.content)
        return ok_response({"danbooru": ["genshin_impact", "solo", "1girl"], "wallhaven": ["genshin impact"], "notes": "ok"})

    r = mock_translator(h).translate("原神女角色，单人")
    assert r["danbooru"][0] == "genshin_impact" and r["for"] == "原神女角色，单人"
    assert seen["auth"] == f"Bearer {KEY}"
    assert seen["body"]["response_format"] == {"type": "json_object"} and seen["body"]["model"] == "deepseek-flash"
    assert "json" in seen["body"]["messages"][0]["content"].lower()


def test_translate_retries_without_thinking_on_400():
    calls = []

    def h(req):
        body = json.loads(req.content)
        calls.append("thinking" in body)
        return httpx.Response(400, json={}) if "thinking" in body else ok_response({"danbooru": ["solo"], "wallhaven": []})

    assert mock_translator(h).translate("x")["danbooru"] == ["solo"]
    assert calls == [True, False]


@pytest.mark.parametrize("resp,msg", [
    (httpx.Response(401), "拒绝"), (httpx.Response(402), "余额"), (httpx.Response(429), "频繁"),
    (httpx.Response(500), "500"),
    (httpx.Response(200, json={"choices": [{"message": {"content": ""}}]}), "无法解析"),
    (httpx.Response(200, json={"choices": [{"message": {"content": "not json"}}]}), "无法解析"),
])
def test_translate_errors_are_friendly(resp, msg):
    with pytest.raises(TranslateError) as e:
        mock_translator(lambda req: resp).translate("x")
    assert msg in str(e.value) and KEY not in str(e.value)


def test_translate_empty_result_and_network_error():
    with pytest.raises(TranslateError, match="为空"):
        mock_translator(lambda req: ok_response({"danbooru": ["rating:x"], "wallhaven": []})).translate("x")

    def boom(req):
        raise httpx.ConnectError("dns")
    with pytest.raises(TranslateError, match="连不上"):
        mock_translator(boom).translate("x")


class Recording(FakeFetcher):
    name = "danbooru"

    def __init__(self):
        super().__init__(n=10)
        self.terms = []

    def search(self, terms, profile, pages=2):
        self.terms.append(list(terms))
        return super().search(terms, profile, pages)


def make(settings, monkeypatch, translator, fetcher):
    monkeypatch.setattr("app.main.make_fetchers", lambda s: [fetcher])
    monkeypatch.setattr(pipeline, "download_bytes", lambda url, **kw: png_bytes(int(url.rsplit("/", 1)[-1])))
    app = create_app(settings, embedder=FakeEmbedder(on=False), translator=translator)
    c = TestClient(app)
    c.__enter__()
    assert c.post("/api/login", json={"password": "correct horse battery"}).status_code == 200
    return c


def test_run_translates_once_caches_and_resets_on_edit(settings, monkeypatch):
    n = {"calls": 0}

    def h(req):
        n["calls"] += 1
        return ok_response({"danbooru": ["genshin_impact", "solo"], "wallhaven": ["genshin impact"], "notes": "n"})

    f = Recording()
    c = make(settings, monkeypatch, mock_translator(h), f)
    assert c.get("/api/me").json()["translate"] is True
    pid = c.post("/api/profiles", json={"name": "t", "query": "原神女角色"}).json()["id"]
    c.post(f"/api/profiles/{pid}/run")
    assert f.terms[-1] == ["genshin_impact", "solo"] and n["calls"] == 1
    assert c.get("/api/profiles").json()[0]["search_terms"]["danbooru"][0] == "genshin_impact"
    c.post(f"/api/profiles/{pid}/run")
    assert n["calls"] == 1                                   # 缓存
    c.patch(f"/api/profiles/{pid}", json={"query": "崩坏星穹铁道"})
    assert c.get("/api/profiles").json()[0]["search_terms"] is None
    c.post(f"/api/profiles/{pid}/run")
    assert n["calls"] == 2


def test_english_query_skips_translation(settings, monkeypatch):
    f = Recording()
    c = make(settings, monkeypatch, mock_translator(lambda r: pytest.fail("should not call")), f)
    pid = c.post("/api/profiles", json={"name": "t", "query": "genshin impact, solo"}).json()["id"]
    c.post(f"/api/profiles/{pid}/run")
    assert f.terms and f.terms[-1] == pipeline.parse_terms("genshin impact, solo")


def test_translation_failure_does_not_block_and_is_reported(settings, monkeypatch):
    f = Recording()
    c = make(settings, monkeypatch, mock_translator(lambda r: httpx.Response(401)), f)
    pid = c.post("/api/profiles", json={"name": "t", "query": "原神"}).json()["id"]
    c.post(f"/api/profiles/{pid}/run")
    st = c.get("/api/profiles").json()[0]["last_status"]
    assert "关键词翻译失败" in st and KEY not in st and f.terms          # 仍然照常抓取


def test_no_key_means_no_translate_flag(authed):
    assert authed.get("/api/me").json()["translate"] is False


def test_old_prompt_version_cache_is_retranslated(settings, monkeypatch):
    n = {"c": 0}

    def h(req):
        n["c"] += 1
        return ok_response({"danbooru": ["solo"], "wallhaven": ["x"]})

    c = make(settings, monkeypatch, mock_translator(h), Recording())
    pid = c.post("/api/profiles", json={"name": "t", "query": "原神"}).json()["id"]
    c.app.state.db.update_profile(pid, search_terms={"for": "原神", "danbooru": ["old"], "wallhaven": ["old"]})   # 无版本号 = 旧版
    c.post(f"/api/profiles/{pid}/run")
    assert n["c"] == 1 and c.get("/api/profiles").json()[0]["search_terms"]["danbooru"] == ["solo"]
