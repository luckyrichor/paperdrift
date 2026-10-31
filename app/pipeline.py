"""一次抓取流程：搜索 → 去重/过滤 → 打分 → 选出（20% 探索）→ 下载预览 → 哈希去重 → 入待审队列。"""
import logging
import random
import re
from pathlib import Path
from urllib.parse import urlparse

import httpx

from . import phash
from .fetchers.base import UA
from .scoring import score

log = logging.getLogger("paperdrift.pipeline")

EXPLORE_RATIO = 0.2
ENRICH_FACTOR = 3          # 需要补标签的来源，最多补 batch_size*3 张


def parse_terms(query: str) -> list[str]:
    return [t.strip() for t in re.split(r"[,，;；\n]+", query) if t.strip()]


def download_bytes(url: str, max_bytes: int = 20 * 1024 * 1024) -> bytes:
    with httpx.stream("GET", url, headers={"User-Agent": UA}, timeout=30, follow_redirects=True) as r:
        r.raise_for_status()
        buf = bytearray()
        for chunk in r.iter_bytes():
            buf += chunk
            if len(buf) > max_bytes:
                raise ValueError("预览图过大")
        return bytes(buf)


def _ext(url: str) -> str:
    e = Path(urlparse(url).path).suffix.lower()
    return e if e in (".jpg", ".jpeg", ".png", ".webp", ".gif") else ".jpg"


def run_profile(db, profile: dict, fetchers, previews_dir: Path, rng: random.Random | None = None,
                fetch_bytes=None) -> dict:
    rng = rng or random.Random()
    fetch_bytes = fetch_bytes or download_bytes
    pid = profile["id"]
    terms = parse_terms(profile["query"])
    stats = {"fetched": 0, "inserted": 0, "dup": 0, "errors": []}
    if not terms:
        stats["errors"].append("需求描述为空")
        return stats

    # 1. 搜索
    cands = []
    by_source = {}
    for f in fetchers:
        try:
            found = f.search(terms, profile)
            by_source[f.name] = f
            cands += found
        except Exception as e:                      # 一个来源挂了不影响其他来源
            log.warning("fetcher %s failed: %s", f.name, e)
            stats["errors"].append(f"{f.name}: {e}")
    stats["fetched"] = len(cands)

    # 2. 过滤：已推过的、不符合目标终端的
    seen = db.seen_ids(pid)
    target = profile.get("target", "any")
    pool, ids = [], set()
    for c in cands:
        key = (c.source, c.source_id)
        if key in seen or key in ids:
            continue
        if target in ("landscape", "portrait") and c.orientation not in (target, "unknown"):
            continue
        ids.add(key)
        pool.append(c)

    # 3. 补标签（仅对热度先验靠前的部分，省接口额度）
    batch = int(profile.get("batch_size", 30))
    pool.sort(key=lambda c: c.prior, reverse=True)
    enriched = {}
    for c in pool[: batch * ENRICH_FACTOR]:
        if not c.tags:
            f = by_source.get(c.source)
            try:
                f.enrich(c)
            except Exception as e:
                log.warning("enrich %s/%s failed: %s", c.source, c.source_id, e)
        enriched[(c.source, c.source_id)] = c
    pool = [c for c in pool if (c.source, c.source_id) in enriched]

    # 4. 打分 + 选择
    weights = db.weights(pid)
    scored = sorted(((score(c.tags, weights, c.prior), c) for c in pool), key=lambda x: x[0], reverse=True)
    explore_n = round(batch * EXPLORE_RATIO) if weights else 0     # 没反馈时全靠先验，不需要探索
    exploit_n = batch - explore_n
    exploit_list = scored
    explore_list = scored[exploit_n:]
    rng.shuffle(explore_list)

    known_hashes = db.hashes(pid)

    def fill(source_list, n):
        done = 0
        for sc, c in source_list:
            if done >= n:
                break
            if (c.source, c.source_id) in used:
                continue
            try:
                data = fetch_bytes(c.preview_url)
                h = phash.dhash(data)
            except Exception as e:
                log.warning("preview %s failed: %s", c.preview_url, e)
                continue
            if phash.is_duplicate(h, known_hashes):
                stats["dup"] += 1
                used.add((c.source, c.source_id))
                continue
            path = previews_dir / f"{pid}_{c.source}_{c.source_id}{_ext(c.preview_url)}"
            path.write_bytes(data)
            db.insert_item(pid, c, path, h, sc)
            known_hashes.append(h)
            used.add((c.source, c.source_id))
            done += 1
        return done

    used: set = set()
    n1 = fill(exploit_list, exploit_n)
    n2 = fill(explore_list, explore_n) if explore_n else 0
    stats["inserted"] = n1 + n2
    return stats
