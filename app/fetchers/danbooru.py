import math

import httpx

from .base import UA, Candidate, Fetcher

PAGE_LIMIT = 200          # Danbooru 单页上限
ARTIST = "artist:"
MAX_PAGES = 8                    # 一次最多翻这么多页（每页 200 条）
SCORE_STEPS = (120, 50, 15, 0)   # 质量门槛：从高到低逐级放宽，直到第一页够多
ENOUGH = 60                      # 本地过滤（分级/其余词）之后至少要有这么多条，否则放宽质量门槛


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

    def _convert(self, rows: list[dict], nsfw: bool, extra: list[str]) -> list[Candidate]:
        out: list[Candidate] = []
        for p in rows:
            if not nsfw and p.get("rating") != "g":
                continue
            full, ext = p.get("file_url"), p.get("file_ext", "")
            if not full or ext in ("zip", "swf"):                 # zip=ugoira，暂不支持
                continue
            taglist = " ".join(p.get(k, "") for k in (
                "tag_string_general", "tag_string_character", "tag_string_copyright")).split()
            taglist += [ARTIST + a for a in p.get("tag_string_artist", "").split()]    # 画师单独标出来：画风偏好主要由画师决定，学习和扩展搜索都重点用它
            if extra and not set(extra) <= {t.removeprefix(ARTIST) for t in taglist}:
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

    expandable = True                # 支持「按喜欢的图的标签扩展搜索」

    def search(self, terms, profile, pages=MAX_PAGES, want=None):
        resolved = [self.resolve(t) for t in terms if t.strip()]
        server, extra = resolved[: self.max_tags], resolved[self.max_tags:]
        nsfw = bool(profile.get("nsfw"))
        # 匿名时服务器不按质量排序，只会返回「最新的」，里面大多是低分图。
        # 所以留一个名额给 score:>=N 元标签，让服务器只返回高分图；其余词放到本地过滤。
        if len(resolved) > self.max_tags - 1:
            server, extra = resolved[: self.max_tags - 1], resolved[self.max_tags - 1:]
        use_rating = not nsfw and len(server) + 1 < self.max_tags
        out: list[Candidate] = []
        steps = list(SCORE_STEPS)
        # 候选池要够大：本地过滤（分级/单人等）之后只剩一小部分，池子太小就没有「挑」的余地，学习也就无从谈起。
        want = want or int(profile.get("batch_size", 30)) * 10
        for page in range(1, pages + 1):
            while True:
                thr = steps[0]
                meta = ([f"score:>={thr}"] if thr else []) + (["rating:g"] if use_rating else [])
                try:
                    rows = self._page(server + meta, page)
                except _TagLimit:
                    if not use_rating:
                        raise
                    use_rating = False
                    continue
                cands = self._convert(rows, nsfw, extra)
                if page == 1 and len(cands) < ENOUGH and len(steps) > 1:
                    steps.pop(0)                                     # 过滤后的高分图太少（冷门角色/高分图多半不是 SFW）：放宽门槛重来
                    continue
                break
            if not rows:
                break
            out.extend(cands)
            if len(out) >= want:
                break
        return out
