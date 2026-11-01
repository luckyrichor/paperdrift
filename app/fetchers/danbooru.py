import math

import httpx

from .base import UA, Candidate, Fetcher

PAGE_LIMIT = 200          # Danbooru 单页上限


class _TagLimit(Exception):
    """HTTP 422：标签数超过账号上限。"""


class DanbooruFetcher(Fetcher):
    """Danbooru posts.json。

    经验（来自一次成功的人工/agent 搜索）：
    - 角色标签要写规范名。老角色是 `raiden_shogun`，新角色必须带作品后缀 `lauma_(genshin_impact)`，
      裸名会返回 0 条。所以先用 /tags.json 把用户写的词解析成规范标签（resolve）。
    - 匿名只能同时搜 2 个标签（有账号 6 个）。多出来的词在本地按标签过滤，不浪费请求。
    - 匿名时 order 排序无效，所以排序留给本地（用 score/fav 做先验）。
    - 单页取到 200 条；SFW 只要 rating=general。
    """
    name = "danbooru"
    base = "https://danbooru.donmai.us"

    def __init__(self, login: str = "", api_key: str = "", client: httpx.Client | None = None):
        auth = (login, api_key) if login and api_key else None
        self.max_tags = 6 if auth else 2
        self.client = client or httpx.Client(headers={"User-Agent": UA}, auth=auth, timeout=30, follow_redirects=True)
        self._resolved: dict[str, str] = {}

    # ---------- 标签解析 ----------
    def resolve(self, term: str) -> str:
        t = term.strip().lower().replace(" ", "_")
        if not t or ":" in t or "(" in t:          # 元标签或用户已写了完整标签：原样使用
            return t
        if t in self._resolved:
            return self._resolved[t]
        best = t
        try:
            r = self.client.get(f"{self.base}/tags.json", params={
                "search[name_matches]": f"{t}*", "search[order]": "count", "limit": 20})
            r.raise_for_status()
            top = (None, 0)
            for row in r.json():
                n, cnt = row.get("name", ""), row.get("post_count", 0)
                if (n == t or n.startswith(t + "_(")) and cnt > top[1]:
                    top = (n, cnt)
            if top[0]:
                best = top[0]
        except Exception:
            pass                                     # 解析失败就用原词，不影响抓取
        self._resolved[t] = best
        return best

    # ---------- 搜索 ----------
    def _page(self, tags: list[str], page: int) -> list[dict]:
        r = self.client.get(f"{self.base}/posts.json", params={"tags": " ".join(tags), "limit": PAGE_LIMIT, "page": page})
        if r.status_code == 422:
            raise _TagLimit()
        r.raise_for_status()
        return r.json()

    def search(self, terms, profile, pages=2):
        resolved = [self.resolve(t) for t in terms if t.strip()]
        server, extra = resolved[: self.max_tags], resolved[self.max_tags:]
        nsfw = bool(profile.get("nsfw"))
        use_rating = not nsfw and len(server) < self.max_tags        # 还有空位时让服务器就按分级过滤，省带宽
        out: list[Candidate] = []
        for page in range(1, pages + 1):
            try:
                rows = self._page(server + (["rating:g"] if use_rating else []), page)
            except _TagLimit:
                if not use_rating:
                    raise
                use_rating = False                                    # 元标签也算名额：退回只靠本地过滤
                rows = self._page(server, page)
            if not rows:
                break
            for p in rows:
                if not nsfw and p.get("rating") != "g":
                    continue
                full, ext = p.get("file_url"), p.get("file_ext", "")
                if not full or ext in ("zip", "swf"):                 # zip=ugoira，暂不支持
                    continue
                taglist = " ".join(p.get(k, "") for k in (
                    "tag_string_general", "tag_string_character", "tag_string_copyright", "tag_string_artist")).split()
                if extra and not set(extra) <= set(taglist):
                    continue
                large = p.get("large_file_url") or ""
                preview = large if large.rsplit(".", 1)[-1].lower() in ("jpg", "jpeg", "png", "webp") else p.get("preview_file_url")
                if not preview:
                    continue
                popular = max(p.get("score", 0) or 0, p.get("fav_count", 0) or 0)
                out.append(Candidate(
                    source=self.name, source_id=str(p["id"]),
                    kind="video" if ext in ("mp4", "webm") else "gif" if ext == "gif" else "image",
                    preview_url=preview, full_url=full, page_url=f"{self.base}/posts/{p['id']}",
                    width=p.get("image_width"), height=p.get("image_height"), tags=taglist,
                    prior=min(1.0, math.log1p(max(popular, 0)) / math.log1p(500)),
                ))
        return out
