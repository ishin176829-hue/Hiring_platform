import fcntl
import hmac
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import Config, SearchSettings
from .service import Service
from .store import Store


class Empty(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BatchInput(Empty):
    job_ids: list[StrictInt] = Field(min_length=1, max_length=100)
    action: Literal["start_chat", "greet", "resume"] = "greet"
    message: str = Field(default="", max_length=1000)
    resume_name: str = Field(default="", max_length=200)
    mode: Literal["dry_run", "live"] = "dry_run"


class ApproveInput(Empty):
    digest: str = Field(min_length=64, max_length=64)
    acknowledge_live: bool = False


class ImportInput(Empty):
    jobs: list[dict] = Field(min_length=1, max_length=100)


def create_app(state_dir=None, browser=None):
    config = Config(state_dir)
    store = Store(config.state_dir / "jobs.sqlite3")
    if browser is None:
        from .browser import BrowserAdapter
        browser = BrowserAdapter(config.state_dir, headless=os.environ.get("BOSS_HEADLESS", "").lower() in {"1", "true", "yes", "on"})
    service = Service(store, browser)

    @asynccontextmanager
    async def lifespan(app):
        with (config.state_dir / "service.lock").open("a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("此数据目录已有机器人服务运行") from None
            store.recover()
            try:
                yield
            finally:
                await service.close()
                fcntl.flock(lock, fcntl.LOCK_UN)

    app = FastAPI(title="BOSS 求职工作台", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.config, app.state.store, app.state.service = config, store, service
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])

    @app.middleware("http")
    async def local_auth(request: Request, call_next):
        if request.url.path.startswith("/api/"):
            header = request.headers.get("authorization", "")
            if not hmac.compare_digest(header, "Bearer " + config.token):
                return JSONResponse({"detail": "需要本地访问令牌，请从控制台或项目命令入口访问"}, status_code=401)
            origin = request.headers.get("origin")
            expected_origin = f"{request.url.scheme}://{request.headers.get('host', '')}"
            if origin and origin != expected_origin:
                return JSONResponse({"detail": "不接受跨来源访问"}, status_code=403)
            if request.method in {"POST", "PUT", "PATCH"} and request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                return JSONResponse({"detail": "请求必须使用 JSON 格式"}, status_code=415)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(ValueError)
    async def domain_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(KeyError)
    async def not_found(request, exc):
        return JSONResponse({"detail": "清单或岗位不存在"}, status_code=404)

    @app.exception_handler(RuntimeError)
    async def runtime_error(request, exc):
        return JSONResponse({"detail": service._error(exc)}, status_code=409)

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, exc):
        errors = [".".join(str(p) for p in error["loc"][1:]) + "：" + error["msg"] for error in exc.errors()]
        return JSONResponse({"detail": "；".join(errors)}, status_code=422)

    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static, check_dir=False), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index():
        source = (static / "index.html").read_text()
        return HTMLResponse(source.replace("__BOSS_API_TOKEN__", json.dumps(config.token)))

    @app.get("/health")
    async def health():
        return {"status": "ok", "service": "boss-hiring", "version": "0.1.0"}

    @app.get("/api/status")
    async def status():
        jobs = store.list_jobs()
        active = next((task for task in store.tasks() if task["id"] == service.active_id), None)
        if not service.task or service.task.done():
            active = None
        return {"paused": store.paused(), "active_task": active, "browser": await browser.status(),
                "counts": {"jobs": len(jobs), "eligible": sum(j["eligible"] for j in jobs), "batches": len(store.batches())},
                "daily_used": store.daily_used(), "settings": store.settings()}

    @app.get("/api/settings")
    async def settings():
        return store.settings()

    @app.put("/api/settings")
    async def update_settings(body: SearchSettings):
        return store.update_settings(body.model_dump())

    @app.post("/api/browser/open")
    async def open_browser(body: Empty):
        if service.browser_opening or (service.task and not service.task.done()):
            raise ValueError("任务运行时不能切换登录页面，请先等待任务结束")
        service.browser_opening = True
        try:
            return await browser.open_login()
        finally:
            service.browser_opening = False

    @app.get("/api/browser/screenshot")
    async def screenshot():
        try:
            return Response(await browser.screenshot(), media_type="image/png")
        except RuntimeError as exc:
            return JSONResponse({"detail": service._error(exc)}, status_code=409)

    @app.post("/api/collect")
    async def collect(body: Empty):
        return {"task_id": service.start_collection()}

    @app.get("/api/jobs")
    async def jobs(city: str = "", q: str = ""):
        return store.list_jobs(city, q)

    @app.post("/api/jobs/import")
    async def import_jobs(body: ImportInput):
        return {"imported": store.upsert_jobs(body.jobs)}

    @app.get("/api/tasks")
    async def tasks():
        return store.tasks()

    @app.post("/api/batches")
    async def create_batch(body: BatchInput):
        return store.create_batch(body.job_ids, body.action, body.message, body.resume_name, body.mode)

    @app.get("/api/batches")
    async def batches():
        return store.batches()

    @app.get("/api/batches/{batch_id}")
    async def batch(batch_id: int):
        return store.batch(batch_id)

    @app.post("/api/batches/{batch_id}/approve")
    async def approve(batch_id: int, body: ApproveInput):
        return store.approve(batch_id, body.digest, body.acknowledge_live)

    @app.post("/api/batches/{batch_id}/run")
    async def run_batch(batch_id: int, body: Empty):
        return {"task_id": service.start_batch(batch_id)}

    @app.post("/api/pause")
    async def pause(body: Empty):
        service.pause()
        return {"paused": True}

    @app.post("/api/resume")
    async def resume(body: Empty):
        store.set_paused(False)
        return {"paused": False}

    @app.get("/api/events")
    async def events(limit: int = 100):
        return store.events(limit)

    return app
