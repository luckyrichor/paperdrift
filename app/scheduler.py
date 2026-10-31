import asyncio
import logging
from datetime import datetime, timedelta, timezone

log = logging.getLogger("paperdrift.scheduler")


def is_due(profile: dict, now: datetime | None = None) -> bool:
    if not profile["enabled"]:
        return False
    if not profile["last_run"]:
        return True
    now = now or datetime.now(timezone.utc)
    last = datetime.fromisoformat(profile["last_run"])
    return now - last >= timedelta(hours=profile["interval_hours"])


async def scheduler_loop(db, run_fn, tick: int = 60):
    """每分钟检查一次到期的需求档案；服务器重启后错过的任务会自动补跑。"""
    while True:
        try:
            for p in db.list_profiles():
                if is_due(p):
                    await asyncio.to_thread(run_fn, p["id"])
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("scheduler tick failed")
        await asyncio.sleep(tick)
