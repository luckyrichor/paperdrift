import asyncio
import logging
import threading
import time
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
from .embed import Embedder, to_bytes
from .llm import Translator, TranslateError, has_cjk
from .fetchers import make_fetchers
from .fetchers.base import UA
from .scheduler import scheduler_loop

log = logging.getLogger("paperdrift")
STATIC = Path(__file__).parent / "static"
REASONS = {"style", "character", "composition", "resolution", "other"}
LIKE_DELTA = 1.0
REJECT_DELTA = -1.0


def create_app(settings: Settings | None = None, embedder=None, translator=None) -> FastAPI:
    settings = settings or load_settings()
    db = DB(settings.db_path)
    embedder = embedder or Embedder(settings.data_dir / "models")
    if translator is None and settings.llm_key:
        translator = Translator(settings.llm_key, settings.llm_model, settings.llm_base)
    can_translate = bool(translator and translator.available())
    limiter = LoginLimiter()
    running: set[int] = set()
    progress: dict[int, dict] = {}
    cancels: dict[int, threading.Event] = {}
    run_lock = threading.Lock()
    RUN_DEADLINE = 15 * 60          # 一次抓取最长 15 分钟，到点自动收尾（保留已入库的部分）
    db.reset_stale_runs()           # 服务重启前还标着「运行中」的，其实已被打断

    def claim(pid: int) -> bool:
        """原子地占下「正在抓取」的名额；已有同一需求在抓取就返回 False（不会并发跑第二次）。"""
        with run_lock:
            if pid in running:
                return False
            running.add(pid)
            cancels[pid] = threading.Event()
            progress[pid] = {"stage": "排队中", "done": 0, "total": 0, "detail": "", "pct": 0.0, "t0": time.monotonic()}
            return True

    def release(pid: int) -> None:
        with run_lock:
            running.discard(pid)
            progress.pop(pid, None)
            cancels.pop(pid, None)

    def reporter(pid: int):
        def report(stage, done=0, total=0, detail=""):
            lo, hi = pipeline.STAGE_RANGE.get(stage, (0, 0))
            pct = lo + (hi - lo) * (done / total if total else 0.0)
            with run_lock:
                if pid in progress:
                    progress[pid].update(stage=stage, done=done, total=total, detail=detail, pct=min(pct, 100.0))
        return report

    def ensure_terms(pid: int, p: dict) -> str | None:
        """描述含中文、配置了翻译、且还没针对当前描述翻译过 → 翻译并缓存。失败不阻断抓取，返回错误说明。"""
        if not can_translate or not has_cjk(p["query"]):
            return None
        st = p.get("search_terms")
        if st and st.get("for") == p["query"]:
            return None
        reporter(pid)("翻译关键词", 0, 1)
        try:
            st = translator.translate(p["query"])
        except TranslateError as e:
            log.warning("translate failed: %s", e)
            return f"关键词翻译失败：{e}"
        except Exception:
            log.exception("translate crashed")
            return "关键词翻译失败：内部错误"
        db.update_profile(pid, search_terms=st)
        p["search_terms"] = st
        reporter(pid)("翻译关键词", 1, 1)
        return None

    def execute(pid: int, claimed: bool = False) -> dict | None:
        if not claimed and not claim(pid):
            return None
        try:
            p = db.get_profile(pid)
            if not p:
                return None
            cancel = cancels[pid]
            terr = ensure_terms(pid, p)
            deadline = time.monotonic() + RUN_DEADLINE
            stats = pipeline.run_profile(
                db, p, make_fetchers(settings), settings.previews_dir, embedder=embedder,
                progress=reporter(pid), should_stop=lambda: cancel.is_set() or time.monotonic() > deadline)
            timed_out = (not cancel.is_set()) and time.monotonic() > deadline
            if terr:
                stats["errors"].insert(0, terr)
            db.update_profile(pid, last_run=now(), last_status=pipeline.summarize(stats, p, timed_out, can_translate))
            return stats
        except Exception as e:
            log.exception("run_profile failed")
            db.update_profile(pid, last_run=now(), last_status=f"失败：{e}"[:300])
            return None
        finally:
            release(pid)

    def run_profile(pid: int) -> dict | None:       # 定时任务入口
        return execute(pid)

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
        return {"ok": True, "clip": embedder.available(), "translate": can_translate}

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
        out = db.list_profiles()
        with run_lock:
            for p in out:
                pr = progress.get(p["id"])
                p["running"] = None if not pr else {
                    "stage": pr["stage"], "done": pr["done"], "total": pr["total"], "detail": pr["detail"],
                    "pct": round(pr["pct"]), "elapsed": int(time.monotonic() - pr["t0"]),
                    "cancelling": cancels[p["id"]].is_set()}
        return out

    @app.post("/api/profiles", dependencies=[Depends(require_auth)])
    def create_profile(p: ProfileIn):
        pid = db.create_profile(p.name, p.query, p.target, p.interval_hours, p.batch_size, p.nsfw)
        return db.get_profile(pid)

    @app.patch("/api/profiles/{pid}", dependencies=[Depends(require_auth)])
    def patch_profile(pid: int, p: ProfilePatch):
        if not db.get_profile(pid):
            raise HTTPException(404)
        data = p.model_dump(exclude_none=True)
        if "query" in data and data["query"] != db.get_profile(pid)["query"]:
            data["search_terms"] = None                    # 描述改了：旧的翻译作废，下次抓取重新翻译
        for k in ("nsfw", "enabled"):
            if k in data:
                data[k] = int(data[k])
        db.update_profile(pid, **data)
        return db.get_profile(pid)

    @app.delete("/api/profiles/{pid}", dependencies=[Depends(require_auth)])
    def delete_profile(pid: int):
        import shutil
        db.delete_profile(pid)
        shutil.rmtree(settings.data_dir / "refs" / str(pid), ignore_errors=True)
        return {"ok": True}

    @app.post("/api/profiles/{pid}/run", dependencies=[Depends(require_auth)])
    def run_now(pid: int, background: BackgroundTasks):
        """已经在抓取时再点不会重复开始，也不会排队；抓完之后再点才是新的一轮。"""
        if not db.get_profile(pid):
            raise HTTPException(404)
        if not claim(pid):
            return {"started": False, "reason": "已经在抓取中，不会重复开始"}
        background.add_task(execute, pid, True)
        return {"started": True}

    @app.post("/api/profiles/{pid}/cancel", dependencies=[Depends(require_auth)])
    def cancel_run(pid: int):
        with run_lock:
            ev = cancels.get(pid)
        if ev is None:
            return {"cancelled": False, "reason": "当前没有在抓取"}
        ev.set()
        return {"cancelled": True}

    # ---------- 参考图 ----------
    MAX_REF_BYTES = 10 * 1024 * 1024
    REF_EXT = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp", "GIF": ".gif"}

    @app.post("/api/profiles/{pid}/refs", dependencies=[Depends(require_auth)])
    async def add_ref(pid: int, request: Request):
        """请求体直接是图片的原始字节（Content-Type: image/*），网页和 apk 都好调用。"""
        if not db.get_profile(pid):
            raise HTTPException(404)
        data = await request.body()
        if not data or len(data) > MAX_REF_BYTES:
            raise HTTPException(413 if data else 400, "图片为空或超过 10MB")
        try:
            from PIL import Image
            import io
            im = Image.open(io.BytesIO(data))
            fmt = im.format
            im.verify()
        except Exception:
            raise HTTPException(400, "不是有效的图片")
        if fmt not in REF_EXT:
            raise HTTPException(400, "只支持 PNG / JPEG / WEBP / GIF")
        import uuid
        d = settings.data_dir / "refs" / str(pid)
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{uuid.uuid4().hex}{REF_EXT[fmt]}"
        path.write_bytes(data)
        v = await asyncio.to_thread(embedder.embed, data)       # CLIP 没装时为 None，之后装上会自动补算
        rid = db.add_ref(pid, path, to_bytes(v) if v is not None else None)
        return {"id": rid, "embedded": v is not None}

    @app.get("/api/profiles/{pid}/refs", dependencies=[Depends(require_auth)])
    def list_refs(pid: int):
        return [{"id": r["id"], "embedded": r["embedding"] is not None} for r in db.list_refs(pid)]

    @app.get("/api/refs/{rid}/image", dependencies=[Depends(require_auth)])
    def ref_image(rid: int):
        r = db.get_ref(rid)
        if not r or not Path(r["path"]).exists():
            raise HTTPException(404)
        return FileResponse(r["path"], headers={"Cache-Control": "private, max-age=86400"})

    @app.delete("/api/refs/{rid}", dependencies=[Depends(require_auth)])
    def delete_ref(rid: int):
        r = db.get_ref(rid)
        if r:
            Path(r["path"]).unlink(missing_ok=True)
            db.delete_ref(rid)
        return {"ok": True}

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
    app.state.running, app.state.progress, app.state.cancels = running, progress, cancels
    app.state.claim = claim
    return app
