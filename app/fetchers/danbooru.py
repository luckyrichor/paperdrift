import math

import httpx

from .base import UA, Candidate, Fetcher

SFW = {"g", "s"}          # general / sensitive；nsfw 打开后额外放行 q / e
NSFW = {"q", "e"}


class DanbooruFetcher(Fetcher):
    """Danbooru posts.json。免费账号一次最多 2 个标签，所以只取前两个关键词当标签，其余在本地不过滤。"""
    name = "danbooru"
    base = "https://danbooru.donmai.us"

    def __init__(self, login: str = "", api_key: str = ""):
        auth = (login, api_key) if login and api_key else None
        self.max_tags = 6 if auth else 2
        self.client = httpx.Client(headers={"User-Agent": UA}, auth=auth, timeout=30, follow_redirects=True)

    def search(self, terms, profile, pages=2):
        tags = [t.strip().lower().replace(" ", "_") for t in terms if t.strip()][: self.max_tags]
        allowed = SFW | (NSFW if profile.get("nsfw") else set())
        out: list[Candidate] = []
        for page in range(1, pages + 1):
            r = self.client.get(f"{self.base}/posts.json", params={"tags": " ".join(tags), "limit": 100, "page": page})
            r.raise_for_status()
            rows = r.json()
            if not rows:
                break
            for p in rows:
                if p.get("rating") not in allowed:
                    continue
                ext = p.get("file_ext", "")
                full = p.get("file_url")
                if not full or ext in ("zip", "swf"):      # zip=ugoira，v1 暂不支持
                    continue
                kind = "video" if ext in ("mp4", "webm") else "gif" if ext == "gif" else "image"
                large = p.get("large_file_url") or ""
                preview = large if large.rsplit(".", 1)[-1].lower() in ("jpg", "jpeg", "png", "webp") else p.get("preview_file_url")
                if not preview:
                    continue
                taglist = " ".join(p.get(k, "") for k in (
                    "tag_string_general", "tag_string_character", "tag_string_copyright", "tag_string_artist")).split()
                out.append(Candidate(
                    source=self.name, source_id=str(p["id"]), kind=kind,
                    preview_url=preview, full_url=full,
                    page_url=f"{self.base}/posts/{p['id']}",
                    width=p.get("image_width"), height=p.get("image_height"),
                    tags=taglist,
                    prior=min(1.0, math.log1p(p.get("fav_count", 0)) / math.log1p(500)),
                ))
        return out
