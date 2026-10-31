import asyncio
import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import pipeline
from .auth import LoginLimiter, check_token, make_token
from .config import Settings, load_settings
from .db import DB, now
from .fetchers import make_fetchers
from .fetchers.base import UA
from .scheduler import scheduler_loop

log = logging.getLogger("paperdrift")
STATIC = Path(__file__).parent / "static"
REASONS = {"style", "character", "composition", "resolution", "other"}
LIKE_DELTA = 1.0
REJECT_DELTA = -1.0


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    db = DB(settings.db_path)
    limiter = LoginLimiter()
    running: set[int] = set()
    run_lock = threading.Lock()

    def run_profile(pid: int) -> dict | None:
        with run_lock:
            if pid in running:
                return None
            running.add(pid)
        try:
            p = db.get_profile(pid)
            if not p:
                return None
            stats = pipeline.run_profile(db, p, make_fetchers(settings), settings.previews_dir)
            msg = f"新增 {stats['inserted']}，重复 {stats['dup']}，抓到 {stats['fetched']}"
            if stats["errors"]:
                msg += "；错误：" + " | ".join(stats["errors"])[:300]
            db.update_profile(pid, last_run=now(), last_status=msg)
            return stats
        except Exception as e:
            log.exception("run_profile failed")
            db.update_profile(pid, last_run=now(), last_status=f"失败：{e}"[:300])
            return None
        finally:
            with run_lock:
                running.discard(pid)

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(scheduler_loop(db, run_profile))
        yield
        task.cancel()

    app = FastAPI(title="paperdrift", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    # ---------- 认证 ----------
    def require_auth(request: Request):
        token = request.cookies.get("pd_token")
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
        if not check_token(settings.secret, token):
            raise HTTPException(401, "未登录")

    class Login(BaseModel):
        password: str

    @app.post("/api/login")
    def login(body: Login, request: Request, response: Response):
        ip = request.client.host if request.client else "?"
        if limiter.locked(ip):
            raise HTTPException(429, "失败次数过多，请 15 分钟后再试")
        import hmac
        if not hmac.compare_digest(body.password.encode(), settings.password.encode()):
            limiter.fail(ip)
            raise HTTPException(401, "密码错误")
        limiter.ok(ip)
        token = make_token(settings.secret)
        response.set_cookie("pd_token", token, httponly=True, samesite="lax",
                            secure=settings.cookie_secure, max_age=30 * 24 * 3600)
        return {"token": token}       # apk 用这个放进 Authorization: Bearer

    @app.post("/api/logout")
    def logout(response: Response):
        response.delete_cookie("pd_token")
        return {"ok": True}

    @app.get("/api/me", dependencies=[Depends(require_auth)])
    def me():
        return {"ok": True}

    # ---------- 需求档案 ----------
    class ProfileIn(BaseModel):
        name: str = Field(min_length=1, max_length=80)
        query: str = Field(min_length=1, max_length=500)
        target: str = Field(default="any", pattern="^(any|landscape|portrait)$")
        interval_hours: float = Field(default=24, ge=1, le=24 * 30)
        batch_size: int = Field(default=30, ge=5, le=100)
        nsfw: bool = False

    class ProfilePatch(BaseModel):
        name: str | None = Field(default=None, min_length=1, max_length=80)
        query: str | None = Field(default=None, min_length=1, max_length=500)
        target: str | None = Field(default=None, pattern="^(any|landscape|portrait)$")
        interval_hours: float | None = Field(default=None, ge=1, le=24 * 30)
        batch_size: int | None = Field(default=None, ge=5, le=100)
        nsfw: bool | None = None
        enabled: bool | None = None

    @app.get("/api/profiles", dependencies=[Depends(require_auth)])
    def list_profiles():
        return db.list_profiles()

    @app.post("/api/profiles", dependencies=[Depends(require_auth)])
    def create_profile(p: ProfileIn):
        pid = db.create_profile(p.name, p.query, p.target, p.interval_hours, p.batch_size, p.nsfw)
        return db.get_profile(pid)

    @app.patch("/api/profiles/{pid}", dependencies=[Depends(require_auth)])
    def patch_profile(pid: int, p: ProfilePatch):
        if not db.get_profile(pid):
            raise HTTPException(404)
        data = p.model_dump(exclude_none=True)
        for k in ("nsfw", "enabled"):
            if k in data:
                data[k] = int(data[k])
        db.update_profile(pid, **data)
        return db.get_profile(pid)

    @app.delete("/api/profiles/{pid}", dependencies=[Depends(require_auth)])
    def delete_profile(pid: int):
        db.delete_profile(pid)
        return {"ok": True}

    @app.post("/api/profiles/{pid}/run", dependencies=[Depends(require_auth)])
    def run_now(pid: int, background: BackgroundTasks):
        if not db.get_profile(pid):
            raise HTTPException(404)
        if pid in running:
            return {"started": False, "reason": "正在运行"}
        db.update_profile(pid, last_status="运行中…")
        background.add_task(run_profile, pid)
        return {"started": True}

    # ---------- 图片条目 ----------
    @app.get("/api/items", dependencies=[Depends(require_auth)])
    def list_items(profile_id: int, status: str = "new", kind: str | None = None,
                   orientation: str | None = None, limit: int = 50, offset: int = 0):
        if status not in ("new", "liked", "rejected"):
            raise HTTPException(400, "status 无效")
        return db.list_items(profile_id, status, kind, orientation, min(limit, 200), offset)

    class Decision(BaseModel):
        decision: str = Field(pattern="^(like|reject|undo)$")
        reason: str | None = None

    @app.post("/api/items/{iid}/decision")
    def decide(iid: int, d: Decision, background: BackgroundTasks, _=Depends(require_auth)):
        item = db.get_item(iid)
        if not item:
            raise HTTPException(404)
        if d.reason and d.reason not in REASONS:
            raise HTTPException(400, "reason 无效")
        # 先撤销旧决定对权重的影响，再应用新决定（改主意也安全）
        old = {"liked": LIKE_DELTA, "rejected": REJECT_DELTA}.get(item["status"])
        if old:
            db.bump_weights(item["profile_id"], item["tags"], -old)
        new_status = {"like": "liked", "reject": "rejected", "undo": "new"}[d.decision]
        if new_status != "new":
            db.bump_weights(item["profile_id"], item["tags"], LIKE_DELTA if new_status == "liked" else REJECT_DELTA)
        db.set_decision(iid, new_status, d.reason if new_status == "rejected" else None)
        if new_status == "liked" and not item["local_path"]:
            background.add_task(download_original, iid)
        return {"ok": True, "status": new_status}

    def download_original(iid: int):
        item = db.get_item(iid)
        if not item or item["local_path"]:
            return
        ext = Path(item["full_url"].split("?")[0]).suffix or ".bin"
        dest = settings.library_dir / str(item["profile_id"]) / f"{item['source']}_{item['source_id']}{ext}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        limit = settings.max_download_mb * 1024 * 1024
        tmp = dest.with_suffix(dest.suffix + ".part")
        try:
            size = 0
            with httpx.stream("GET", item["full_url"], headers={"User-Agent": UA},
                              timeout=60, follow_redirects=True) as r:
                r.raise_for_status()
                with open(tmp, "wb") as f:
                    for chunk in r.iter_bytes():
                        size += len(chunk)
                        if size > limit:
                            raise ValueError("文件超过大小上限")
                        f.write(chunk)
            tmp.rename(dest)
            db.set_local_path(iid, str(dest))
        except Exception:
            log.exception("download original failed: %s", item["full_url"])
            tmp.unlink(missing_ok=True)

    @app.get("/api/items/{iid}/preview", dependencies=[Depends(require_auth)])
    def preview(iid: int):
        item = db.get_item(iid)
        if not item or not item["preview_path"] or not Path(item["preview_path"]).exists():
            raise HTTPException(404)
        return FileResponse(item["preview_path"], headers={"Cache-Control": "private, max-age=86400"})

    @app.get("/api/items/{iid}/file", dependencies=[Depends(require_auth)])
    def original(iid: int, background: BackgroundTasks):
        item = db.get_item(iid)
        if not item or item["status"] != "liked":
            raise HTTPException(404, "只有已喜欢的条目可以下载原图")
        if not item["local_path"] or not Path(item["local_path"]).exists():
            background.add_task(download_original, iid)
            raise HTTPException(409, "原图正在下载，请稍后重试")
        return FileResponse(item["local_path"], filename=Path(item["local_path"]).name)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    app.state.db = db
    app.state.settings = settings
    app.state.run_profile = run_profile
    return app
