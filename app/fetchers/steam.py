import math
import re

import httpx

from .base import UA, Candidate, Fetcher

APPID = 431960                       # Wallpaper Engine
API = "https://api.steampowered.com"
BROWSE = "https://steamcommunity.com/workshop/browse/"
DETAIL_URL = "https://steamcommunity.com/sharedfiles/filedetails/?id="
RES = re.compile(r"(\d{3,5})\s*x\s*(\d{3,5})", re.I)
ITEM = re.compile(r'data-publishedfileid="(\d+)"')
IMG = re.compile(r'workshopItemPreviewImage[^>]*?src="([^"]+)"', re.S)
TITLE = re.compile(r'class="workshopItemTitle[^"]*"[^>]*>([^<]*)<')


def _norm(tag: str) -> str:
    return re.sub(r"\s+", "_", tag.strip().lower())


def candidate_from_details(d: dict) -> Candidate | None:
    """GetPublishedFileDetails / QueryFiles 返回的一条记录 → Candidate。"""
    fid = str(d.get("publishedfileid") or "")
    preview = d.get("preview_url") or ""
    if not fid or not preview:
        return None
    names = [t.get("tag", "") if isinstance(t, dict) else str(t) for t in d.get("tags", [])]
    low = [n.lower() for n in names]
    w = h = None
    for n in names:
        m = RES.search(n)
        if m:
            w, h = int(m.group(1)), int(m.group(2))
            if n.lower().startswith("portrait") and w > h:
                w, h = h, w
            break
    subs = int(d.get("lifetime_subscriptions") or d.get("subscriptions") or 0)
    votes = (d.get("vote_data") or {}).get("score") or 0
    prior = min(1.0, math.log1p(subs) / math.log1p(200_000) * 0.7 + float(votes) * 0.3)
    return Candidate(
        source="steam", source_id=fid, kind="video" if "video" in low else "scene",
        preview_url=preview, full_url="",                      # 不在服务器下载：喜欢后在 Windows 上用 Steam 订阅
        page_url=DETAIL_URL + fid, width=w, height=h, file_size=int(d.get("file_size") or 0) or None,
        tags=[_norm(n) for n in names if n], prior=prior)


class SteamWorkshopFetcher(Fetcher):
    """Wallpaper Engine 创意工坊。有 Steam Web API Key 时用 QueryFiles（直接带标签、热度）；
    没有时抓创意工坊的浏览页拿到条目 ID，再用不需要 Key 的 GetPublishedFileDetails 补标签。
    只做搜索与预览，不下载工坊文件（下载需要拥有 Wallpaper Engine 的 Steam 账号）。"""
    name = "steam"

    def __init__(self, api_key: str = "", client: httpx.Client | None = None):
        self.api_key = api_key
        self.client = client or httpx.Client(headers={"User-Agent": UA}, timeout=30, follow_redirects=True)

    # ---- 有 Key：QueryFiles ----
    def _query(self, q: str, page: int, nsfw: bool) -> list[Candidate]:
        params = {
            "key": self.api_key, "query_type": 9, "page": page, "numperpage": 50,      # 9 = 按订阅数
            "creator_appid": APPID, "appid": APPID, "search_text": q, "return_tags": "true",
            "return_vote_data": "true", "match_all_tags": "true", "filetype": 0,
        }
        if not nsfw:
            params["requiredtags[0]"] = "Everyone"
        r = self.client.get(f"{API}/IPublishedFileService/QueryFiles/v1/", params=params)
        r.raise_for_status()
        rows = r.json().get("response", {}).get("publishedfiledetails", [])
        return [c for c in map(candidate_from_details, rows) if c]

    # ---- 没 Key：浏览页 + GetPublishedFileDetails ----
    def _browse_ids(self, q: str, page: int, nsfw: bool) -> list[str]:
        params = {"appid": APPID, "searchtext": q, "browsesort": "totaluniquesubscribers", "section": "readytouseitems",
                  "actualsort": "totaluniquesubscribers", "p": page, "days": -1}
        if not nsfw:
            params["requiredtags[]"] = "Everyone"
        r = self.client.get(BROWSE, params=params)
        r.raise_for_status()
        return list(dict.fromkeys(ITEM.findall(r.text)))

    def _details(self, ids: list[str]) -> list[Candidate]:
        if not ids:
            return []
        data = {"itemcount": len(ids)}
        data.update({f"publishedfileids[{i}]": fid for i, fid in enumerate(ids)})
        r = self.client.post(f"{API}/ISteamRemoteStorage/GetPublishedFileDetails/v1/", data=data)
        r.raise_for_status()
        rows = r.json().get("response", {}).get("publishedfiledetails", [])
        return [c for c in map(candidate_from_details, [x for x in rows if x.get("result", 1) == 1]) if c]

    def search(self, terms, profile, pages=2):
        q = " ".join(terms)
        nsfw = bool(profile.get("nsfw"))
        out: list[Candidate] = []
        for page in range(1, pages + 1):
            rows = self._query(q, page, nsfw) if self.api_key else self._details(self._browse_ids(q, page, nsfw))
            if not rows:
                break
            out += rows
        return out
