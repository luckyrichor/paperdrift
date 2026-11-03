import io
import json
import subprocess
from pathlib import Path

import httpx
import pytest
from PIL import Image

from app import pipeline, steamdl
from app.db import DB
from app.fetchers.steam import SteamWorkshopFetcher, candidate_from_details
from conftest import FakeEmbedder, png_bytes

ROW = {"publishedfileid": "123", "title": "Raiden", "preview_url": "https://cdn/p123.jpg", "subscriptions": 50000,
       "vote_data": {"score": 0.95}, "tags": [{"tag": "Video"}, {"tag": "Anime"}, {"tag": "Everyone"}, {"tag": "1920 x 1080"}]}


def test_candidate_mapping_kind_resolution_and_no_download_url():
    c = candidate_from_details(ROW)
    assert c.source == "steam" and c.kind == "video" and (c.width, c.height) == (1920, 1080)
    assert c.full_url == "" and c.page_url.endswith("id=123") and "anime" in c.tags and 0 < c.prior <= 1
    scene = candidate_from_details(dict(ROW, publishedfileid="9", tags=[{"tag": "Scene"}, {"tag": "Portrait 1080 x 1920"}]))
    assert scene.kind == "scene" and (scene.width, scene.height) == (1080, 1920)
    assert candidate_from_details({"publishedfileid": "1"}) is None             # 没有预览图：丢弃


def test_search_with_api_key_uses_queryfiles_and_everyone_tag():
    seen = {}

    def h(req):
        seen["url"] = str(req.url)
        return httpx.Response(200, json={"response": {"publishedfiledetails": [ROW]}})

    f = SteamWorkshopFetcher("K", client=httpx.Client(transport=httpx.MockTransport(h)))
    out = f.search(["genshin impact"], {"nsfw": 0}, pages=1)
    assert [c.source_id for c in out] == ["123"]
    u = seen["url"]
    assert "IPublishedFileService/QueryFiles" in u and "appid=431960" in u and "requiredtags%5B0%5D=Everyone" in u and "search_text=genshin+impact" in u


def test_search_without_key_scrapes_ids_then_reads_details():
    html = '<div class="workshopItem"><a data-publishedfileid="123" class="ugc"></a></div><a data-publishedfileid="456"></a><a data-publishedfileid="123">'
    calls = []

    def h(req):
        calls.append(req.url.host)
        if req.url.host == "steamcommunity.com":
            return httpx.Response(200, text=html if req.url.params["p"] == "1" else "")
        body = req.content.decode()
        assert "itemcount=2" in body
        return httpx.Response(200, json={"response": {"publishedfiledetails": [ROW, dict(ROW, publishedfileid="456")]}})

    out = SteamWorkshopFetcher("", client=httpx.Client(transport=httpx.MockTransport(h))).search(["x"], {"nsfw": 0}, pages=3)
    assert [c.source_id for c in out] == ["123", "456"] and "api.steampowered.com" in calls


class S:
    def __init__(self, tmp, user="u"):
        self.steam_user, self.steamcmd_path, self.steam_max_mb = user, "steamcmd", 1
        self.steam_dir = tmp / "steam"
        self.library_dir = tmp / "lib"


def fake_run(files, out="Success. Downloaded item"):
    def run(cmd, **kw):
        assert "+login" in cmd and cmd[cmd.index("+login") + 1] == "u" and not any("pass" in c.lower() for c in cmd)
        assert kw["env"]["HOME"].endswith("steam/home")                                # 登录缓存放数据目录
        item = Path(cmd[cmd.index("+force_install_dir") + 1]) / "steamapps/workshop/content/431960" / cmd[cmd.index("431960") + 1]
        if files:
            item.mkdir(parents=True, exist_ok=True)
            for name, size in files.items():
                (item / name).write_bytes(b"x" * size)
        return subprocess.CompletedProcess(cmd, 0, out, "")
    return run


def test_download_item_and_pack(tmp_path):
    s = S(tmp_path)
    folder = steamdl.download_item(s, "555", run=fake_run({"project.json": 10, "a.mp4": 1000}))
    z = steamdl.pack(folder, tmp_path / "lib" / "1" / "steam_555.zip")
    import zipfile
    assert z.suffix == ".zip" and set(zipfile.ZipFile(z).namelist()) >= {"555/project.json", "555/a.mp4"}


def test_download_errors_are_friendly(tmp_path):
    s = S(tmp_path)
    with pytest.raises(steamdl.SteamDownloadError, match="STEAM_USER"):
        steamdl.download_item(S(tmp_path, user=""), "1")
    with pytest.raises(steamdl.SteamDownloadError, match="验证码"):
        steamdl.download_item(s, "1", run=fake_run({}, out="Steam Guard code required"))
    with pytest.raises(steamdl.SteamDownloadError, match="没有 Wallpaper Engine"):
        steamdl.download_item(s, "1", run=fake_run({}, out="ERROR! Download item 1 failed (No subscription)"))
    with pytest.raises(steamdl.SteamDownloadError, match="太大"):
        steamdl.download_item(s, "2", run=fake_run({"big.bin": 2 * 1024 * 1024}))
    assert not (s.steam_dir / "steamapps/workshop/content/431960/2").exists()          # 超限的清理掉
    with pytest.raises(steamdl.SteamDownloadError, match="无效"):
        steamdl.download_item(s, "../x")


def test_like_steam_item_downloads_zip_and_error_is_visible(authed, monkeypatch, settings):
    from app.fetchers.base import Candidate
    c = authed
    pid = c.post("/api/profiles", json={"name": "t", "query": "x"}).json()["id"]
    db = c.app.state.db
    cand = Candidate(source="steam", source_id="777", kind="video", preview_url="p", full_url="", page_url="u", width=1920, height=1080)
    prev = settings.previews_dir / "s.jpg"
    prev.write_bytes(png_bytes(1))
    iid = db.insert_item(pid, cand, prev, "0" * 16, 0.5)

    monkeypatch.setattr(steamdl, "download_item", lambda s, fid: (_ for _ in ()).throw(steamdl.SteamDownloadError("没有 Wallpaper Engine")))
    c.post(f"/api/items/{iid}/decision", json={"decision": "like"})
    item = db.get_item(iid)
    assert item["dl_error"] == "没有 Wallpaper Engine" and not item["local_path"]
    assert c.get(f"/api/items/{iid}/file").status_code == 409

    folder = settings.steam_dir / "steamapps/workshop/content/431960/777"
    folder.mkdir(parents=True)
    (folder / "project.json").write_text("{}")
    monkeypatch.setattr(steamdl, "download_item", lambda s, fid: folder)
    assert c.post(f"/api/items/{iid}/redownload").json() == {"ok": True}
    item = db.get_item(iid)
    assert item["local_path"].endswith("steam_777.zip") and item["dl_error"] is None
    assert c.get(f"/api/items/{iid}/file").status_code == 200


def test_animated_gif_preview_is_kept_and_static_is_converted():
    frames = [Image.new("RGB", (64, 64), (i * 40, 0, 0)) for i in range(4)]
    b = io.BytesIO()
    frames[0].save(b, "GIF", save_all=True, append_images=frames[1:], duration=80, loop=0)
    assert pipeline.is_animated_gif(b.getvalue())
    s = io.BytesIO()
    frames[0].save(s, "GIF")
    assert not pipeline.is_animated_gif(s.getvalue()) and not pipeline.is_animated_gif(png_bytes(1))


def test_translator_returns_steam_terms():
    from test_llm import mock_translator, ok_response
    r = mock_translator(lambda q: ok_response({"danbooru": ["solo"], "wallhaven": [], "steam": ["Genshin Impact", "rating:x"]})).translate("原神")
    assert r["steam"] == ["genshin impact"]


def test_file_size_is_carried_from_each_source():
    assert candidate_from_details(dict(ROW, file_size="12345678")).file_size == 12345678
    assert candidate_from_details(ROW).file_size is None
