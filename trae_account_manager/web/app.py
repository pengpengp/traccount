"""TAM local web dashboard (FastAPI)."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from pydantic import BaseModel

from .. import db, vault
from ..config import get_trae_data_dir
from ..models import Account
from ..process_ctl import get_trae_exe_path, set_trae_exe_path
from ..switcher import Switcher

log = logging.getLogger(__name__)

WEB_ROOT = Path(__file__).parent
STATIC_DIR = WEB_ROOT / "static"


app = FastAPI(
    title="Trae Account Manager",
    description="Local dashboard for account registration & switching.",
    version="1.0.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
class ConnectionManager:
    """Track websocket clients and broadcast log/progress events."""

    def __init__(self) -> None:
        self.active: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.active.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self.active.discard(ws)

    async def broadcast(self, msg: dict) -> None:
        dead: list[WebSocket] = []
        for ws in list(self.active):
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.active.discard(ws)


manager = ConnectionManager()


# ---------------------------------------------------------------------------
# request / response models
# ---------------------------------------------------------------------------
class AddAccountReq(BaseModel):
    email: str
    token: str
    refresh_token: str = ""
    user_id: str = ""
    region: str = "SG"
    name: str = ""


class RegisterReq(BaseModel):
    count: int = 1
    concurrency: int = 2
    headless: bool = True
    persist: bool = True


class SetPathReq(BaseModel):
    path: str


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    """Serve the single-page dashboard."""
    html = STATIC_DIR / "index.html"
    if not html.exists():
        return HTMLResponse(
            "<h1>Dashboard not built</h1>"
            "<p>Run <code>npm run build</code> in <code>web/</code> to populate static/.</p>"
        )
    return HTMLResponse(html.read_text(encoding="utf-8"))


@app.get("/api/accounts")
async def api_list_accounts(only_active: bool = False) -> dict:
    accs = db.list_accounts(only_active=only_active)
    cur_id = db.get_current_account_id()
    return {
        "current_id": cur_id,
        "accounts": [_account_brief(a, a.id == cur_id) for a in accs],
    }


@app.get("/api/accounts/{account_id}")
async def api_get_account(account_id: str) -> dict:
    a = db.get_account(account_id)
    if a is None:
        raise HTTPException(404, "account not found")
    return _account_brief(a, a.id == db.get_current_account_id())


@app.post("/api/accounts")
async def api_add_account(req: AddAccountReq) -> dict:
    from ..config import host_for_region
    acc = Account(
        email=req.email,
        name=req.name or req.email.split("@", 1)[0],
        user_id=req.user_id,
        region=req.region,
    )
    secrets = {
        "jwt_token": req.token,
        "refresh_token": req.refresh_token,
        "login_info": {
            "token": req.token,
            "refresh_token": req.refresh_token,
            "user_id": req.user_id,
            "email": req.email,
            "username": req.name,
            "host": host_for_region(req.region),
            "region": req.region,
        },
    }
    acc.secrets_blob = vault.encrypt_obj(secrets)
    acc = db.upsert_account(acc)
    return {"id": acc.id, "email": acc.email}


@app.delete("/api/accounts/{account_id}")
async def api_delete_account(account_id: str) -> dict:
    ok = db.delete_account(account_id)
    if not ok:
        raise HTTPException(404, "account not found")
    return {"deleted": True}


@app.post("/api/switch/{account_id}")
async def api_switch(account_id: str, launch: bool = True) -> dict:
    a = db.get_account(account_id)
    if a is None:
        raise HTTPException(404, "account not found")
    sw = Switcher()
    res = await asyncio.to_thread(
        sw.switch_to_account, a, launch=launch
    )
    await manager.broadcast({"type": "switch", "result": res})
    return res


@app.post("/api/capture")
async def api_capture(name: str = "", email: str = "") -> dict:
    sw = Switcher()
    acc = await asyncio.to_thread(sw.capture_current, name, email)
    if acc is None:
        raise HTTPException(400, "no live Trae session found")
    return {"id": acc.id, "email": acc.email}


@app.post("/api/clear")
async def api_clear(launch: bool = False) -> dict:
    sw = Switcher()
    res = await asyncio.to_thread(sw.clear_login_state)
    return res


@app.post("/api/register")
async def api_register(req: RegisterReq) -> dict:
    """Trigger a registration batch (runs in background; events on /ws)."""
    from ..register import register_batch

    async def _run() -> None:
        try:
            await manager.broadcast({"type": "register_start", "count": req.count})
            results = await register_batch(
                req.count, req.concurrency,
                headless=req.headless, persist=req.persist,
            )
            ok = sum(1 for r in results if r.success)
            await manager.broadcast(
                {"type": "register_done", "ok": ok, "total": len(results)}
            )
            for r in results:
                await manager.broadcast({
                    "type": "register_result",
                    "success": r.success,
                    "email": r.email,
                    "error": r.error,
                })
        except Exception as e:
            log.exception("register batch failed")
            await manager.broadcast({"type": "register_error", "error": str(e)})

    asyncio.create_task(_run())
    return {"started": True, "count": req.count}


@app.get("/api/usage/{account_id}")
async def api_usage(account_id: str) -> dict:
    from ..trae_api import TraeApiClient
    a = db.get_account(account_id)
    if a is None:
        raise HTTPException(404, "account not found")
    async with TraeApiClient.for_account(a) as client:
        try:
            summary = await client.get_usage_summary_by_token()
        except Exception as e:
            raise HTTPException(502, f"Trae API error: {e}") from e
    return summary.to_dict()


@app.get("/api/path")
async def api_get_path() -> dict:
    return {"path": get_trae_exe_path()}


@app.post("/api/path")
async def api_set_path(req: SetPathReq) -> dict:
    set_trae_exe_path(req.path)
    return {"path": req.path}


@app.get("/api/info")
async def api_info() -> dict:
    from .. import __version__
    return {
        "version": __version__,
        "trae_data_dir": str(get_trae_data_dir()),
        "trae_exe": get_trae_exe_path(),
        "current_account_id": db.get_current_account_id(),
    }


# ---------------------------------------------------------------------------
@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    await manager.connect(websocket)
    try:
        await websocket.send_json({"type": "hello"})
        while True:
            msg = await websocket.receive_json()
            if msg.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        manager.disconnect(websocket)


# ---------------------------------------------------------------------------
def _account_brief(a: Account, is_current: bool) -> dict:
    return {
        "id": a.id,
        "email": a.email,
        "name": a.name,
        "region": a.region,
        "plan_type": a.plan_type,
        "status": a.status,
        "machine_id": a.machine_id,
        "is_current": is_current,
        "user_id": a.user_id,
        "created_at": a.created_at,
        "last_used_at": a.last_used_at,
    }
