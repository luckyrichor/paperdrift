import io
import random

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import pipeline
from app.config import Settings
from app.fetchers.base import Candidate, Fetcher
from app.main import create_app


def png_bytes(seed: int, tint: str = "gray") -> bytes:
    """随机噪点图（保证感知哈希互不相同）；tint 决定主色调，供假 CLIP 区分。"""
    rnd = random.Random(seed)
    img = Image.new("RGB", (64, 48))
    mul = {"gray": (1, 1, 1), "red": (1, .15, .15), "blue": (.15, .15, 1)}[tint]
    img.putdata([tuple(int(rnd.randrange(256) * m) for m in mul) for _ in range(64 * 48)])
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


class FakeEmbedder:
    """假 CLIP：向量 = 归一化的平均 RGB。红图和蓝图在这个空间里方向明显不同。"""

    def __init__(self, on=True):
        self.on = on

    def available(self):
        return self.on

    def embed(self, data):
        if not self.on:
            return None
        import numpy as np
        a = np.asarray(Image.open(io.BytesIO(data)).convert("RGB"), dtype=np.float32).reshape(-1, 3).mean(axis=0)
        return a / np.linalg.norm(a)


class FakeFetcher(Fetcher):
    name = "fake"

    def __init__(self, n=40, same_tags=False):
        self.n = n
        self.same_tags = same_tags

    def search(self, terms, profile, pages=2):
        out = []
        for i in range(self.n):
            tags = ["girl", "purple_hair"] if (i % 2 == 0 or self.same_tags) else ["boy", "night"]
            out.append(Candidate(source="fake", source_id=str(i), kind="image", preview_url=f"fake://{i}",
                                 full_url=f"fake://full/{i}", page_url=f"http://x/{i}",
                                 width=1920 if i % 4 else 1080, height=1080 if i % 4 else 1920,
                                 tags=list(tags), prior=1 - i / self.n, file_size=3 * 1024 * 1024 + i))
        return out


@pytest.fixture
def settings(tmp_path):
    s = Settings(password="correct horse battery", secret="s" * 40, data_dir=tmp_path,
                 cookie_secure=False, max_download_mb=5, wallhaven_key="", danbooru_login="", danbooru_key="")
    s.previews_dir.mkdir(); s.library_dir.mkdir()
    return s


@pytest.fixture
def app(settings, monkeypatch):
    monkeypatch.setattr("app.main.make_fetchers", lambda s: [FakeFetcher()])
    monkeypatch.setattr(pipeline, "download_bytes", lambda url, **kw: png_bytes(int(url.rsplit("/", 1)[-1])))
    return create_app(settings, embedder=FakeEmbedder(on=False))


@pytest.fixture
def clip_app(settings, monkeypatch):
    """标签完全相同，偶数号预览是红色、奇数号是蓝色：只有 CLIP 口味分能区分它们。"""
    monkeypatch.setattr("app.main.make_fetchers", lambda s: [FakeFetcher(same_tags=True)])
    monkeypatch.setattr(pipeline, "download_bytes",
                        lambda url, **kw: png_bytes(int(url.rsplit("/", 1)[-1]), "red" if int(url.rsplit("/", 1)[-1]) % 2 == 0 else "blue"))
    return create_app(settings, embedder=FakeEmbedder(on=True))


@pytest.fixture
def clip_client(clip_app):
    with TestClient(clip_app) as c:
        assert c.post("/api/login", json={"password": "correct horse battery"}).status_code == 200
        yield c


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def authed(client):
    r = client.post("/api/login", json={"password": "correct horse battery"})
    assert r.status_code == 200
    return client
