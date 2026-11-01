import math
import time

import httpx

from .base import UA, Candidate, Fetcher


class WallhavenFetcher(Fetcher):
    """Wallhaven API v1。搜索结果不含标签，enrich() 会按需查详情（API 限速 45 次/分钟）。"""
    name = "wallhaven"
    base = "https://wallhaven.cc/api/v1"

    def __init__(self, api_key: str = ""):
        headers = {"User-Agent": UA}
        if api_key:
            headers["X-API-Key"] = api_key
        self.client = httpx.Client(headers=headers, timeout=30, follow_redirects=True)
        self._last = 0.0

    def _get(self, url: str, **params):
        wait = 1.4 - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        r = self.client.get(url, params=params)
        r.raise_for_status()
        return r.json()

    def search(self, terms, profile, pages=2):
        q = " ".join(terms)
        out: list[Candidate] = []
        for page in range(1, pages + 1):
            if profile.get("nsfw"):
                purity = "111" if self.client.headers.get("X-API-Key") else "110"   # NSFW 需要 API Key
            else:
                purity = "100"
            params = dict(q=q, categories="111", purity=purity, sorting="relevance", page=page)
            data = self._get(f"{self.base}/search", **params)
            for d in data.get("data", []):
                try:
                    w, h = (int(x) for x in d["resolution"].split("x"))
                except Exception:
                    w = h = None
                ftype = d.get("file_type", "")
                out.append(Candidate(
                    source=self.name,
                    source_id=d["id"],
                    kind="gif" if ftype == "image/gif" else "image",
                    preview_url=d["thumbs"].get("original") or d["thumbs"].get("large") or d["thumbs"]["small"],   # large 是被裁成固定比例的，优先用 original；若仍被裁，pipeline 会改取原图
                    full_url=d["path"],
                    page_url=d.get("url", ""),
                    width=w, height=h,
                    tags=[],
                    prior=min(1.0, math.log1p(d.get("favorites", 0)) / math.log1p(500)),
                ))
            if page >= data.get("meta", {}).get("last_page", 1):
                break
        return out

    def enrich(self, cand):
        data = self._get(f"{self.base}/w/{cand.source_id}")["data"]
        cand.tags = [t["name"].lower().replace(" ", "_") for t in data.get("tags", [])]
