"""一次抓取流程：搜索 → 过滤 → 补标签 → 预取预览(哈希+向量) → 打分 → 选出（20% 探索）→ 哈希去重 → 入待审队列。"""
import logging
import random
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

import httpx
import numpy as np

from . import phash
from .embed import from_bytes, to_bytes
from .fetchers.base import UA
from .scoring import score
from .taste import taste_scores

log = logging.getLogger("paperdrift.pipeline")

EXPLORE_RATIO = 0.2
POOL_FACTOR = 3            # 只对热度先验靠前的 batch_size*3 张补标签、取预览、算向量
TASTE_WEIGHT = 0.7         # 口味分（z-score）相对标签分的权重


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


def _vectors(db, rows, embedder, save):
    """取出一批记录的向量；缺失的用预览文件补算并回写（比如 CLIP 是后来才装上的）。"""
    out = []
    for r in rows:
        emb = r["embedding"]
        if emb is None and embedder is not None:
            try:
                v = embedder.embed(Path(r.get("preview_path") or r.get("path")).read_bytes())
            except Exception:
                v = None
            if v is not None:
                emb = to_bytes(v)
                save(r["id"], emb)
        if emb is not None:
            out.append(from_bytes(emb))
    return np.stack(out) if out else None


def run_profile(db, profile: dict, fetchers, previews_dir: Path, rng: random.Random | None = None,
                fetch_bytes=None, embedder=None) -> dict:
    rng = rng or random.Random()
    fetch_bytes = fetch_bytes or download_bytes
    use_clip = embedder is not None and embedder.available()
    pid = profile["id"]
    terms = parse_terms(profile["query"])
    stats = {"fetched": 0, "inserted": 0, "dup": 0, "clip": False, "errors": []}
    if not terms:
        stats["errors"].append("需求描述为空")
        return stats

    # 1. 搜索（一个来源挂了不影响其他来源）
    cands, by_source = [], {}
    for f in fetchers:
        try:
            cands += f.search(terms, profile)
            by_source[f.name] = f
        except Exception as e:
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

    # 3. 只保留热度先验靠前的一部分，补标签
    batch = int(profile.get("batch_size", 30))
    pool.sort(key=lambda c: c.prior, reverse=True)
    pool = pool[: batch * POOL_FACTOR]
    for c in pool:
        if not c.tags:
            try:
                by_source[c.source].enrich(c)
            except Exception as e:
                log.warning("enrich %s/%s failed: %s", c.source, c.source_id, e)

    # 4. 并行预取预览：算感知哈希，有 CLIP 就算向量
    def prefetch(c):
        try:
            data = fetch_bytes(c.preview_url)
            h = phash.dhash(data)
            v = embedder.embed(data) if use_clip else None
            return c, data, h, v
        except Exception as e:
            log.warning("preview %s failed: %s", c.preview_url, e)
            return None

    with ThreadPoolExecutor(max_workers=6) as ex:
        fetched = [x for x in ex.map(prefetch, pool) if x]
    if not fetched:
        return stats

    # 5. 打分：标签分 + 先验 + 口味分
    weights = db.weights(pid)
    base = np.array([score(c.tags, weights, c.prior) for c, *_ in fetched], dtype=np.float32)
    taste_on = False
    if use_clip and all(x[3] is not None for x in fetched):
        pos_rows = db.decided_vectors(pid, "liked")
        pos = _vectors(db, pos_rows, embedder, db.set_item_embedding)
        refs = _vectors(db, db.list_refs(pid), embedder, db.set_ref_embedding)
        if refs is not None:
            pos = refs if pos is None else np.vstack([pos, refs])
        neg = _vectors(db, db.decided_vectors(pid, "rejected"), embedder, db.set_item_embedding)
        if pos is not None or neg is not None:
            vecs = np.stack([x[3] for x in fetched])
            base = base + TASTE_WEIGHT * taste_scores(vecs, pos, neg)
            taste_on = True
    stats["clip"] = taste_on

    scored = sorted(zip(base.tolist(), fetched), key=lambda x: x[0], reverse=True)

    # 6. 选择：80% 按分数，20% 从剩余里随机探索（还没有任何反馈信号时不探索）
    explore_n = round(batch * EXPLORE_RATIO) if (weights or taste_on) else 0
    exploit_n = batch - explore_n
    explore_list = scored[exploit_n:]
    rng.shuffle(explore_list)

    known_hashes = db.hashes(pid)
    used: set = set()

    def fill(items, n):
        done = 0
        for sc, (c, data, h, v) in items:
            if done >= n:
                break
            key = (c.source, c.source_id)
            if key in used:
                continue
            used.add(key)
            if phash.is_duplicate(h, known_hashes):
                stats["dup"] += 1
                continue
            path = previews_dir / f"{pid}_{c.source}_{c.source_id}{_ext(c.preview_url)}"
            path.write_bytes(data)
            db.insert_item(pid, c, path, h, sc, to_bytes(v) if v is not None else None)
            known_hashes.append(h)
            done += 1
        return done

    stats["inserted"] = fill(scored, exploit_n) + (fill(explore_list, explore_n) if explore_n else 0)
    return stats
