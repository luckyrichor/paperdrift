import io

import httpx
from PIL import Image

from app.fetchers.base import Candidate
from app.fetchers.danbooru import DanbooruFetcher
from app.pipeline import PREVIEW_MAX_EDGE, normalize_preview


def post(i, rating="g", tags="solo", char="lauma_(genshin_impact)", w=2000, h=1000, score=100):
    return {"id": i, "rating": rating, "file_ext": "jpg", "file_url": f"https://cdn/{i}.jpg",
            "large_file_url": f"https://cdn/s{i}.jpg", "preview_file_url": f"https://cdn/p{i}.jpg",
            "image_width": w, "image_height": h, "score": score, "fav_count": 0,
            "tag_string_general": tags, "tag_string_character": char,
            "tag_string_copyright": "genshin_impact", "tag_string_artist": "someone"}


def make(handler, **kw):
    return DanbooruFetcher(client=httpx.Client(transport=httpx.MockTransport(handler)), **kw)


def test_resolve_picks_canonical_character_tag():
    def handler(req):
        assert req.url.path == "/tags.json"
        return httpx.Response(200, json=[
            {"name": "lauma_(genshin_impact)", "post_count": 300},
            {"name": "lauma_(other_game)", "post_count": 5},
            {"name": "laumamama", "post_count": 9999},               # 前缀相同但不是同一个词，必须忽略
        ])
    assert make(handler).resolve("Lauma") == "lauma_(genshin_impact)"


def test_resolve_keeps_explicit_tags_and_falls_back_on_error():
    def boom(req):
        raise httpx.ConnectError("down")
    f = make(boom)
    assert f.resolve("lauma_(genshin_impact)") == "lauma_(genshin_impact)"
    assert f.resolve("rating:g") == "rating:g"
    assert f.resolve("raiden shogun") == "raiden_shogun"             # 解析失败：原词（空格转下划线）


def test_search_uses_resolved_tag_filters_rating_locally_and_maps_fields():
    seen = {}
    allcalls = []

    def handler(req):
        if req.url.path == "/tags.json":
            return httpx.Response(200, json=[{"name": "lauma_(genshin_impact)", "post_count": 300}])
        seen["tags"] = req.url.params["tags"]
        allcalls.append(seen["tags"])
        assert req.url.params["limit"] == "200"
        return httpx.Response(200, json=[post(1), post(2, rating="e"), post(3, w=900, h=1600)])

    out = make(handler).search(["lauma"], {"nsfw": 0}, pages=1)
    assert "lauma_(genshin_impact)" in allcalls[0] and "score:>=120" in allcalls[0]   # 先只要高分图
    assert "score" not in seen["tags"]                                  # 高分图太少（<80）：逐级放宽到不限分
    assert [c.source_id for c in out] == ["1", "3"]                   # rating=e 被本地过滤
    assert out[0].orientation == "landscape" and out[1].orientation == "portrait"
    assert out[0].preview_url == "https://cdn/s1.jpg" and out[0].full_url == "https://cdn/1.jpg"


def test_422_on_metatag_falls_back_without_it():
    calls = []

    def handler(req):
        if req.url.path == "/tags.json":
            return httpx.Response(200, json=[])
        calls.append(req.url.params["tags"])
        if "rating:g" in req.url.params["tags"]:
            return httpx.Response(422, json={"success": False, "message": "You cannot search for more than 2 tags at a time"})
        return httpx.Response(200, json=[post(1), post(2, rating="q")])

    f = make(handler)
    f.max_tags = 3                                                     # 有名额时服务器端也按分级过滤
    out = f.search(["a"], {"nsfw": 0}, pages=1)
    assert [c.source_id for c in out] == ["1"]
    assert "rating:g" in calls[0] and "rating:g" not in calls[1]


def test_extra_terms_beyond_tag_limit_are_filtered_locally():
    def handler(req):
        if req.url.path == "/tags.json":
            return httpx.Response(200, json=[])
        assert len(req.url.params["tags"].split()) <= 2               # 匿名账号最多发 2 个（含质量门槛）
        return httpx.Response(200, json=[post(1, tags="solo night"), post(2, tags="solo day")])

    out = make(handler).search(["a", "solo", "night"], {"nsfw": 1}, pages=1)
    assert [c.source_id for c in out] == ["1"]


def _png(w, h, color=(10, 120, 200)):
    b = io.BytesIO()
    Image.new("RGB", (w, h), color).save(b, "PNG")
    return b.getvalue()


def cand(w, h, kind="image"):
    return Candidate(source="x", source_id="1", kind=kind, preview_url="p", full_url="f", width=w, height=h)


def test_cropped_thumbnail_is_replaced_by_full_image():
    fetched = []

    def fetch(url, **kw):
        fetched.append(url)
        return _png(1920, 1080)

    out = Image.open(io.BytesIO(normalize_preview(_png(300, 200), cand(1920, 1080), fetch)))
    assert fetched == ["f"]                                           # 300x200（3:2）≠ 16:9，改取原图
    assert abs(out.width / out.height - 1920 / 1080) < 0.01           # 整张，不是裁剪后的
    assert max(out.size) == PREVIEW_MAX_EDGE


def test_uncropped_preview_is_kept_and_not_upscaled():
    def fetch(url, **kw):
        raise AssertionError("不该再下载原图")

    out = Image.open(io.BytesIO(normalize_preview(_png(640, 360), cand(1920, 1080), fetch)))
    assert out.size == (640, 360)


def test_video_preview_never_fetches_full_file():
    def fetch(url, **kw):
        raise AssertionError("视频不能当图片下载")

    normalize_preview(_png(300, 300), cand(1920, 1080, kind="video"), fetch)


def test_transparent_png_gets_white_background():
    b = io.BytesIO()
    Image.new("RGBA", (50, 50), (0, 0, 0, 0)).save(b, "PNG")
    out = Image.open(io.BytesIO(normalize_preview(b.getvalue(), cand(50, 50), lambda *a, **k: b""))).convert("RGB")
    assert out.getpixel((5, 5)) == (255, 255, 255)


def test_score_threshold_stays_when_enough_high_score_posts():
    calls = []

    def handler(req):
        if req.url.path == "/tags.json":
            return httpx.Response(200, json=[])
        calls.append(req.url.params["tags"])
        return httpx.Response(200, json=[post(i) for i in range(100)])

    make(handler).search(["a"], {"nsfw": 1}, pages=2)
    assert calls == ["a score:>=120", "a score:>=120"]


def test_threshold_relaxes_when_high_score_posts_are_all_filtered_out_locally():
    calls = []

    def handler(req):
        if req.url.path == "/tags.json":
            return httpx.Response(200, json=[])
        t = req.url.params["tags"]
        calls.append(t)
        if "score:>=120" in t:                                        # 高分图全是非 SFW：本地过滤后为 0
            return httpx.Response(200, json=[post(i, rating="e") for i in range(100)])
        return httpx.Response(200, json=[post(i) for i in range(100)])

    out = make(handler).search(["a"], {"nsfw": 0}, pages=1)
    assert len(out) == 100 and "score:>=120" in calls[0] and "score:>=50" in calls[1]
