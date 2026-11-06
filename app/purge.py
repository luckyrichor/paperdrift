"""被拒绝的条目：删掉磁盘上所有真实文件（预览图/动图、下载的原图/视频、Steam 的 zip 和解包目录），
只保留元数据（标签、来源 ID、尺寸、大小、dHash、CLIP 向量、拒绝原因）——这些足够去重和继续学习。"""
import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .embed import to_bytes

log = logging.getLogger("paperdrift.purge")


def _size(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.is_dir() else 0


def remove_path(p) -> int:
    """删文件或目录，返回释放的字节数；不存在就当没事。"""
    if not p:
        return 0
    p = Path(p)
    n = _size(p)
    try:
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink(missing_ok=True)
    except OSError:
        log.exception("remove failed: %s", p)
        return 0
    return n


def files_of(settings, item: dict) -> list[Path]:
    """这条记录在磁盘上可能有的所有文件。"""
    out = []
    for key in ("preview_path", "local_path"):
        if item.get(key):
            out.append(Path(item[key]))
    if item.get("local_path"):
        out.append(Path(str(item["local_path"]) + ".part"))
    if item["source"] == "steam":
        out.append(settings.steam_dir / "steamapps" / "workshop" / "content" / "431960" / item["source_id"])
    lib = settings.library_dir / str(item["profile_id"])
    out.append(lib / f"{item['source']}_{item['source_id']}.zip")
    return out


def purge_item(db, settings, item: dict, embedder=None) -> int:
    """先把还没提取的东西提取出来（CLIP 向量），再删文件。返回释放的字节数。"""
    if item.get("embedding") is None and embedder is not None and item.get("preview_path"):
        try:
            v = embedder.embed(Path(item["preview_path"]).read_bytes())
            if v is not None:
                db.set_item_embedding(item["id"], to_bytes(v))
        except Exception:
            log.warning("embed before purge failed for item %s", item["id"])
    freed = sum(remove_path(p) for p in files_of(settings, item))
    db.mark_purged(item["id"])
    return freed


def purge_rejected(db, settings, embedder=None, grace_min: float = 10, pid: int | None = None) -> dict:
    """清理所有「已拒绝、过了缓冲期、文件还在」的条目。缓冲期内保留文件，是为了能「回看上一张/撤销」。"""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=grace_min)).isoformat(timespec="seconds")
    freed = n = 0
    for item in db.list_purgeable(cutoff, pid):
        freed += purge_item(db, settings, item, embedder)
        n += 1
    if n:
        log.info("purged %d rejected items, freed %.1f MB", n, freed / 1048576)
    return {"count": n, "freed": freed}
