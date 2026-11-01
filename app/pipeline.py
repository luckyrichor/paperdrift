"""一次抓取流程：搜索 → 过滤 → 补标签 → 取缩略图(哈希+向量) → 打分 → 选出（20% 探索）→ 规整预览 → 入待审队列。

进度通过 progress(stage, done, total, detail) 回调汇报；should_stop() 为真时尽快收尾并保留已入库的部分。
"""
import io
import logging
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx
import numpy as np
from PIL import Image, ImageOps

from . import phash
from .embed import from_bytes, to_bytes
from .fetchers.base import UA
from .scoring import score
from .taste import taste_scores

log = logging.getLogger("paperdrift.pipeline")

EXPLORE_RATIO = 0.2
POOL_FACTOR = 3            # 只对热度先验靠前的 batch_size*3 张补标签、取缩略图、算向量
TASTE_WEIGHT = 0.7         # 口味分（z-score）相对标签分的权重
# 各阶段在总进度条里占的区间（%）；阶段名与下面 report() 里的一致
STAGE_RANGE = {"搜索各来源": (0, 8), "读取标签": (8, 40), "下载缩略图": (40, 70), "打分排序": (70, 72), "保存入选的图": (72, 100)}
ENRICH_BUDGET = 240        # 补标签最多花这么多秒（Wallhaven 逐张查详情很慢），超时的候选按热度先验参与排序

PREVIEW_MAX_EDGE = 1280
ASPECT_TOL = 0.06                  # 缩略图比例与原图相差超过 6% 就认为被站点裁剪过
FULL_MAX_BYTES = 40 * 1024 * 1024


def parse_terms(query: str) -> list[str]:
    return [t.strip() for t in re.split(r"[,，;；\n]+", query) if t.strip()]


def download_bytes(url: str, max_bytes: int = 20 * 1024 * 1024) -> bytes:
    with httpx.stream("GET", url, headers={"User-Agent": UA}, timeout=30, follow_redirects=True) as r:
        r.raise_for_status()
        buf = bytearray()
        for chunk in r.iter_bytes():
            buf += chunk
            if len(buf) > max_bytes:
                raise ValueError("图片过大")
        return bytes(buf)


def normalize_preview_ex(data: bytes, cand, fetch_bytes) -> tuple[bytes, bool]:
    """得到「整张、不被裁剪」的预览：站点给的缩略图常被裁成固定比例（如 Wallhaven 的 300x200），
    这会让审图看不全，也会让 CLIP 向量失真。比例对不上就改取原图；最后统一缩到最长边 1280 的 JPEG。
    返回 (JPEG 字节, 是否换成了原图)。"""
    img = Image.open(io.BytesIO(data))
    img.load()
    replaced = False
    if cand.width and cand.height and cand.kind != "video":
        want, got = cand.width / cand.height, img.width / img.height
        if abs(got - want) / want > ASPECT_TOL:
            img = Image.open(io.BytesIO(fetch_bytes(cand.full_url, max_bytes=FULL_MAX_BYTES)))
            img.draft("RGB", (PREVIEW_MAX_EDGE * 2, PREVIEW_MAX_EDGE * 2))      # JPEG 解码时直接缩小，省内存
            img.load()
            replaced = True
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.getchannel("A"))
        img = bg
    else:
        img = img.convert("RGB")
    img.thumbnail((PREVIEW_MAX_EDGE, PREVIEW_MAX_EDGE), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=88, optimize=True)
    return out.getvalue(), replaced


def normalize_preview(data: bytes, cand, fetch_bytes) -> bytes:
    return normalize_preview_ex(data, cand, fetch_bytes)[0]


def summarize(stats: dict, profile: dict, timed_out: bool = False) -> str:
    """把一次抓取的结果写成一句人话，显示在页面的「上次：」那一行。"""
    src = "、".join(f"{k} {v}" for k, v in stats.get("by_source", {}).items())
    head = f"新增 {stats['inserted']}，重复 {stats['dup']}，抓到 {stats['fetched']}" + (f"（{src}）" if src else "") \
        + f"，CLIP {'✓' if stats['clip'] else '—'}"
    if stats.get("cancelled"):
        head = ("已超时收尾；" if timed_out else "已取消；") + head
    notes = []
    if stats["fetched"] == 0 and not stats["errors"]:
        if re.search(r"[\u4e00-\u9fff]", profile.get("query", "")):
            notes.append("没有搜到任何结果：关键词里有中文，目前只能搜英文关键词或站点标签（如 genshin impact, solo, 1girl），中文自动翻译还在开发中")
        else:
            notes.append("没有搜到任何结果：换几个更常见的英文关键词试试")
    elif stats["fetched"] > 0 and stats.get("fresh", 1) == 0:
        notes.append("抓到的图都已经推过或不符合终端要求（目前每个来源只翻前 2 页，翻页功能在开发中）")
    elif stats["inserted"] == 0 and not stats.get("cancelled"):
        notes.append("候选图都没能入选（可能全是重复图或下载失败，详见服务日志）")
    if stats["errors"]:
        notes.append("错误：" + " | ".join(stats["errors"])[:300])
    return "；".join([head] + notes)[:600]


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
                fetch_bytes=None, embedder=None, progress=None, should_stop=None) -> dict:
    rng = rng or random.Random()
    fetch_bytes = fetch_bytes or download_bytes
    report = progress or (lambda *a, **k: None)
    stop = should_stop or (lambda: False)
    use_clip = embedder is not None and embedder.available()
    pid = profile["id"]
    terms = parse_terms(profile["query"])
    stats = {"fetched": 0, "inserted": 0, "dup": 0, "clip": False, "cancelled": False, "by_source": {}, "errors": []}
    if not terms:
        stats["errors"].append("需求描述为空")
        return stats

    # 1. 搜索（一个来源挂了不影响其他来源）
    cands, by_source = [], {}
    report("搜索各来源", 0, len(fetchers))
    for n, f in enumerate(fetchers, 1):
        if stop():
            break
        try:
            found = f.search(terms, profile)
            by_source[f.name] = f
            stats["by_source"][f.name] = len(found)
            cands += found
        except Exception as e:
            log.warning("fetcher %s failed: %s", f.name, e)
            stats["errors"].append(f"{f.name}: {e}")
        report("搜索各来源", n, len(fetchers), f.name)
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

    stats["fresh"] = len(pool)          # 去掉已推过/不符合终端要求之后还剩多少

    # 3. 只保留热度先验靠前的一部分，补标签（有时间预算）
    batch = int(profile.get("batch_size", 30))
    pool.sort(key=lambda c: c.prior, reverse=True)
    pool = pool[: batch * POOL_FACTOR]
    need = [c for c in pool if not c.tags]
    t0 = time.monotonic()
    for n, c in enumerate(need, 1):
        if stop() or time.monotonic() - t0 > ENRICH_BUDGET:
            break
        report("读取标签", n - 1, len(need))
        try:
            by_source[c.source].enrich(c)
        except Exception as e:
            log.warning("enrich %s/%s failed: %s", c.source, c.source_id, e)
    report("读取标签", len(need), len(need))

    # 4. 并行取缩略图：算感知哈希，有 CLIP 就算向量。（此时用的是站点原样缩略图，只用来去重和打分）
    def prefetch(c):
        try:
            if stop():
                return None
            data = fetch_bytes(c.preview_url)
            h = phash.dhash(data)
            v = embedder.embed(data) if use_clip else None
            return c, data, h, v
        except Exception as e:
            log.warning("preview %s failed: %s", c.preview_url, e)
            return None

    fetched, done = [], 0
    report("下载缩略图", 0, len(pool))
    with ThreadPoolExecutor(max_workers=6) as ex:
        for fut in as_completed([ex.submit(prefetch, c) for c in pool]):
            done += 1
            r = fut.result()
            if r:
                fetched.append(r)
            report("下载缩略图", done, len(pool))
    stats["cancelled"] = stop()
    if not fetched:
        return stats

    # 5. 打分：标签分 + 先验 + 口味分
    report("打分排序", 0, 0)
    weights = db.weights(pid)
    base = np.array([score(c.tags, weights, c.prior) for c, *_ in fetched], dtype=np.float32)
    taste_on = False
    if use_clip and all(x[3] is not None for x in fetched):
        pos = _vectors(db, db.decided_vectors(pid, "liked"), embedder, db.set_item_embedding)
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
    inserted = 0
    report("保存入选的图", 0, batch)

    def fill(items, n):
        nonlocal inserted
        done = 0
        for sc, (c, data, h, v) in items:
            if done >= n or stop():
                break
            key = (c.source, c.source_id)
            if key in used:
                continue
            used.add(key)
            if phash.is_duplicate(h, known_hashes):
                stats["dup"] += 1
                continue
            try:     # 只对最终入选的才规整预览（必要时才下原图），不对整个候选池做
                img, replaced = normalize_preview_ex(data, c, fetch_bytes)
            except Exception as e:
                log.warning("normalize %s/%s failed: %s", c.source, c.source_id, e)
                continue
            if replaced and use_clip:
                nv = embedder.embed(img)              # 换成整张图后，向量以整张图为准
                v = nv if nv is not None else v
            path = previews_dir / f"{pid}_{c.source}_{c.source_id}.jpg"
            path.write_bytes(img)
            db.insert_item(pid, c, path, h, sc, to_bytes(v) if v is not None else None)
            known_hashes.append(h)
            done += 1
            inserted += 1
            report("保存入选的图", inserted, batch)
        return done

    fill(scored, exploit_n)
    if explore_n:
        fill(explore_list, explore_n)
    stats["inserted"] = inserted
    stats["cancelled"] = stop()
    return stats
