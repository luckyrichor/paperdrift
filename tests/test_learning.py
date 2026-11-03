import numpy as np

from app import pipeline
from app.expand import expansion_tags
from app.taste import mmr_order
from conftest import FakeEmbedder, FakeFetcher, png_bytes


def test_expansion_picks_tags_common_in_likes_but_not_in_everything():
    liked = [["genshin_impact", "solo", "artist_a", "blue_sky"], ["genshin_impact", "solo", "artist_a"], ["genshin_impact", "solo", "artist_a", "night"]]
    rejected = [["genshin_impact", "solo", "artist_b", "blue_sky"]] * 6 + [["genshin_impact", "solo", "night"]] * 3
    out = expansion_tags(liked, liked + rejected, exclude={"genshin_impact", "solo"})
    assert out[0] == "artist_a" and "solo" not in out and "blue_sky" not in out


def test_expansion_needs_at_least_two_likes():
    assert expansion_tags([["a"]], [["a"], ["b"]], set()) == []


def test_mmr_spreads_the_batch_over_clusters():
    rng = np.random.default_rng(0)
    centers = np.eye(4, 16)
    vecs = np.vstack([c + 0.02 * rng.normal(size=(10, 16)) for c in centers]).astype(np.float32)
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
    scores = np.linspace(1, 0, 40).astype(np.float32)               # 分数最高的全在第一个簇
    plain = np.argsort(-scores)[:4]
    div = mmr_order(vecs, scores, lam=1.0, limit=4)[:4]
    assert len({i // 10 for i in plain}) == 1 and len({i // 10 for i in div}) == 4
    assert sorted(mmr_order(vecs, scores, 0.5, 4)) == list(range(40))   # 其余的按分数补在后面，不丢


class Expandable(FakeFetcher):
    expandable = True

    def __init__(self):
        super().__init__(n=40)
        self.calls = []

    def search(self, terms, profile, pages=2, **kw):
        self.calls.append(list(terms))
        return super().search(terms, profile, pages)


def fetch(url, **kw):
    return png_bytes(int(url.rsplit("/", 1)[-1]))


def test_likes_drive_expansion_searches_and_marked_candidates(tmp_path):
    from app.db import DB
    db = DB(tmp_path / "p.db")
    pid = db.create_profile("t", "x", batch_size=10)
    pv = tmp_path / "pv"; pv.mkdir()
    f = Expandable()
    prof = db.get_profile(pid)
    pipeline.run_profile(db, prof, [f], pv, fetch_bytes=fetch)
    assert len(f.calls) == 1                                           # 还没有喜欢：不扩展
    items = db.list_items(pid, "new", limit=100)
    for i in items:                                                    # 喜欢 girl/purple_hair 的，拒绝 boy/night 的
        db.set_decision(i["id"], "liked" if "purple_hair" in i["tags"] else "rejected", None)
    stats = pipeline.run_profile(db, prof, [f], pv, fetch_bytes=fetch)
    assert stats.get("expanded") and f.calls[-1][0] in ("purple_hair", "girl") and f.calls[-1][1:] == ["x"]
    assert "按喜欢的图扩展搜索" in pipeline.summarize(stats, prof)


def test_clip_makes_the_candidate_pool_bigger(tmp_path):
    from app.db import DB
    db = DB(tmp_path / "p.db")
    pid = db.create_profile("t", "x", batch_size=5)
    pv = tmp_path / "pv"; pv.mkdir()
    n = {"c": 0}

    def counting(url, **kw):
        n["c"] += 1
        return fetch(url)
    pipeline.run_profile(db, db.get_profile(pid), [FakeFetcher(n=100)], pv, fetch_bytes=counting, embedder=FakeEmbedder(True))
    with_clip = n["c"]
    db2 = DB(tmp_path / "p2.db")
    pid2 = db2.create_profile("t", "x", batch_size=5)
    n["c"] = 0
    pipeline.run_profile(db2, db2.get_profile(pid2), [FakeFetcher(n=100)], pv, fetch_bytes=counting)
    assert with_clip > n["c"]


def test_artists_are_prefixed_prioritised_and_searched_by_raw_name():
    liked = [["solo", "gold_trim", "artist:foo"], ["solo", "gold_trim", "artist:foo"], ["solo", "gold_trim", "artist:bar"]]
    decided = liked + [["solo", "gold_trim", "artist:baz"]] * 6
    out = expansion_tags(liked, decided, exclude={"solo"})
    assert out[0] == "artist:foo"


def test_danbooru_marks_artist_tags_and_still_filters_extras():
    import httpx
    from app.fetchers.danbooru import DanbooruFetcher
    row = {"id": 1, "rating": "g", "file_url": "https://c/1.jpg", "file_ext": "jpg", "large_file_url": "https://c/s1.jpg",
           "image_width": 1920, "image_height": 1080, "score": 50, "fav_count": 9,
           "tag_string_general": "solo", "tag_string_character": "", "tag_string_copyright": "genshin_impact", "tag_string_artist": "foo"}
    t = httpx.MockTransport(lambda r: httpx.Response(200, json=[] if r.url.path == "/tags.json" else [row]))
    out = DanbooruFetcher(client=httpx.Client(transport=t)).search(["foo", "genshin_impact", "solo"], {"nsfw": 0}, pages=1)
    assert out and "artist:foo" in out[0].tags


def test_low_resolution_candidates_are_dropped(tmp_path):
    from app.db import DB
    from app.fetchers.base import Candidate

    class F(FakeFetcher):
        def search(self, terms, profile, pages=2):
            mk = lambda i, w, h: Candidate(source="fake", source_id=str(i), kind="image", preview_url=f"fake://{i}",
                                           full_url=f"fake://full/{i}", width=w, height=h, tags=["a"], prior=0.5)
            return [mk(0, 800, 600), mk(1, 1920, 1080), mk(2, 1080, 1920), mk(3, None, None), mk(4, 1280, 500)]

    db = DB(tmp_path / "p.db")
    pid = db.create_profile("t", "x", batch_size=10)
    pv = tmp_path / "pv"; pv.mkdir()
    stats = pipeline.run_profile(db, db.get_profile(pid), [F()], pv, fetch_bytes=fetch)
    ids = {i["source_id"] for i in db.list_items(pid, "new", limit=50)}
    assert ids == {"1", "2", "3"} and stats["small"] == 2


def test_repeatedly_rejected_artist_is_blocked_but_liked_artist_is_not():
    from app.expand import blocked_artists
    rej = [["artist:bad"], ["artist:bad", "x"], ["artist:bad"], ["artist:bad"], ["artist:mixed"] * 1] + [["artist:mixed"]] * 5
    liked = [["artist:mixed"]]
    assert blocked_artists(liked, rej) == {"artist:bad"}
