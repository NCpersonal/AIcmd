#!/usr/bin/env python3
"""AI Virtual PC — Dual Environment Server"""

import os
os.environ["http_proxy"] = ""
os.environ["https_proxy"] = ""
os.environ["all_proxy"] = ""

import sys
import json
import asyncio
import uuid
from pathlib import Path
from dataclasses import dataclass, asdict

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from terminal import DualTerminal
from brain import Brain, DualExecutor

CONFIG_FILE = Path(__file__).parent / "settings.json"

@dataclass
class Settings:
    api_key: str  = ""
    base_url: str = "https://api.openai.com/v1"
    model: str    = "gpt-4o"
    port: int     = 8000

    @classmethod
    def load(cls):
        s = cls()
        if CONFIG_FILE.exists():
            try:
                for k, v in json.loads(CONFIG_FILE.read_text()).items():
                    if hasattr(s, k) and v:
                        setattr(s, k, v)
            except: pass
        for env, attr in [("OPENAI_API_KEY","api_key"),("OPENAI_BASE_URL","base_url"),("OPENAI_MODEL","model"),("PORT","port")]:
            val = os.getenv(env, "")
            if val:
                setattr(s, attr, int(val) if attr == "port" else val)
        return s

    def save(self):
        d = asdict(self); d.pop("port")
        CONFIG_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2))

settings = Settings.load()

app = FastAPI()
STATIC = Path(__file__).parent / "static"
if STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

def _mask(k):
    return (k[:4] + "*"*(len(k)-8) + k[-4:]) if k and len(k)>=12 else ("****" if k else "")

@app.get("/")
async def index():
    return FileResponse(str(STATIC / "index.html"))

@app.get("/api/settings")
async def get_settings():
    return {"api_key": _mask(settings.api_key), "api_key_set": bool(settings.api_key), "base_url": settings.base_url, "model": settings.model}

@app.post("/api/settings")
async def post_settings(req: Request):
    d = await req.json()
    if d.get("api_key"): settings.api_key = d["api_key"].strip()
    if d.get("base_url"): settings.base_url = d["base_url"].strip().rstrip("/")
    if d.get("model"): settings.model = d["model"].strip()
    settings.save()
    return {"ok": True, "api_key": _mask(settings.api_key), "base_url": settings.base_url, "model": settings.model}

@app.get("/api/models")
async def list_models():
    if not settings.api_key:
        return {"ok": False, "error": "未设置 API Key", "models": []}
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10, proxy=None) as c:
            r = await c.get(f"{settings.base_url}/models", headers={"Authorization": f"Bearer {settings.api_key}"})
            if r.status_code == 200:
                return {"ok": True, "models": sorted(m["id"] for m in r.json().get("data", []))}
            return {"ok": False, "error": f"HTTP {r.status_code}", "models": []}
    except Exception as e:
        return {"ok": False, "error": str(e), "models": []}


@app.websocket("/ws")
async def ws_handler(ws: WebSocket):
    await ws.accept()
    sid = uuid.uuid4().hex[:8]
    workspace = str(Path("./workspaces").resolve() / sid)
    os.makedirs(workspace, exist_ok=True)
    print(f"[{sid}] 工作区: {workspace}")

    # 创建双终端
    dt = DualTerminal(workspace)

    vpc_queue: asyncio.Queue = asyncio.Queue()
    upc_queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_event_loop()

    def on_vpc(data: bytes):
        loop.call_soon_threadsafe(vpc_queue.put_nowait, data)
    def on_upc(data: bytes):
        loop.call_soon_threadsafe(upc_queue.put_nowait, data)

    dt.vpc.on_output(on_vpc)
    dt.upc.on_output(on_upc)
    dt.start()

    async def fwd_vpc():
        try:
            while True:
                data = await vpc_queue.get()
                await ws.send_json({"t": "vpc_term", "d": data.decode("utf-8", errors="replace")})
        except Exception as e:
            print(f"[{sid}] VPC 终端转发异常: {e}")
            pass
    async def fwd_upc():
        try:
            while True:
                data = await upc_queue.get()
                await ws.send_json({"t": "upc_term", "d": data.decode("utf-8", errors="replace")})
        except Exception as e:
            print(f"[{sid}] UPC 终端转发异常: {e}")
            pass

    fwd_v = asyncio.create_task(fwd_vpc())
    fwd_u = asyncio.create_task(fwd_upc())

    async def send(msg_type, data):
        try: await ws.send_json({"t": msg_type, "d": data})
        except Exception as e:
            print(f"[{sid}] 发送 {msg_type} 失败: {e}")
            pass

    # 终端显示队列 — AI 命令输出通过此通道回显到终端（不经过 bash）
    ai_vpc_q: asyncio.Queue = asyncio.Queue()
    ai_upc_q: asyncio.Queue = asyncio.Queue()
    loop2 = asyncio.get_event_loop()

    def _term_display(which, text):
        q = ai_vpc_q if which == "vpc" else ai_upc_q
        loop2.call_soon_threadsafe(q.put_nowait, text)

    async def _fwd_ai_vpc():
        try:
            while True:
                data = await ai_vpc_q.get()
                await ws.send_json({"t": "vpc_term", "d": data})
        except Exception:
            pass
    async def _fwd_ai_upc():
        try:
            while True:
                data = await ai_upc_q.get()
                await ws.send_json({"t": "upc_term", "d": data})
        except Exception:
            pass

    asyncio.create_task(_fwd_ai_vpc())
    asyncio.create_task(_fwd_ai_upc())

    executor = DualExecutor(dt.vpc, dt.upc, workspace, display_cb=_term_display)

    # 发送终端初始化上下文（显示在终端里的欢迎信息）
    import platform
    _term_display("vpc", f"\x1b[2m── VPC · 隔离环境 · {workspace} ──\x1b[0m\r\n")
    _term_display("vpc", f"\x1b[2m{platform.platform()} · Python {platform.python_version()}\x1b[0m\r\n")
    _term_display("vpc", f"\x1b[2mAI 执行的命令和输出将显示在此处，也可手动操作 shell\x1b[0m\r\n")
    _term_display("vpc", "\r\n")
    _term_display("upc", f"\x1b[2m── UPC · 宿主机 · {os.path.expanduser('~')} ──\x1b[0m\r\n")
    _term_display("upc", f"\x1b[2m{platform.platform()} · 拥有完整系统权限\x1b[0m\r\n")
    _term_display("upc", f"\x1b[2mAI 执行的系统管理命令和输出将显示在此处\x1b[0m\r\n")
    _term_display("upc", "\r\n")

    async def term_switch(which):
        await send("term_switch", which)

    def make_brain():
        return Brain(
            executor=executor, send=send,
            model=settings.model, api_key=settings.api_key, base_url=settings.base_url,
            term_switch=term_switch,
        )

    brain = None  # 延迟创建，确保在同一 WebSocket 会话中复用
    brain_task = None

    try:
        while True:
            raw = await ws.receive_json()
            t = raw.get("t")

            if t == "task":
                if brain_task and not brain_task.done():
                    await send("error", "上一个任务还在执行")
                    continue
                # 停止旧任务（如果在跑），但复用同一个 Brain 实例以保留对话历史
                if brain:
                    brain.stop()
                else:
                    brain = make_brain()
                brain_task = asyncio.create_task(brain.run_task(raw["d"]))

            elif t == "answer":
                brain.answer_question(raw["d"])
            elif t == "chat":
                brain.inject_message(raw["d"])
            elif t == "vpc_input":
                dt.vpc.write(raw["d"])
            elif t == "upc_input":
                dt.upc.write(raw["d"])
            elif t == "switch_term":
                dt.switch_to(raw["d"])
            elif t == "takeover":
                brain.pause()
                brain.inject_message("[用户接管终端]")
                await send("status", "uc")
            elif t == "release":
                brain.resume()
                brain.inject_message("[用户释放终端，请检查状态后继续]")
                await send("status", "working")
            elif t == "stop":
                brain.stop()
                if brain_task and not brain_task.done():
                    brain_task.cancel()
                await send("status", "idle")
            elif t == "resize":
                r = raw.get("d", {})
                dt.resize(r.get("rows", 24), r.get("cols", 80))

    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[{sid}] {e}")
    finally:
        brain.stop()
        fwd_v.cancel()
        fwd_u.cancel()
        dt.close()


if __name__ == "__main__":
    import uvicorn
    print(f"\n  AI Virtual PC (Dual) | {settings.model} | http://localhost:{settings.port}")
    print(f"  🟢 VPC: 隔离虚拟环境   🟠 UPC: 宿主机\n")
    if not settings.api_key:
        print("  ⚠  未设置 API Key，在网页 ⚙ 中配置\n")
    uvicorn.run(app, host="0.0.0.0", port=settings.port, log_level="warning")
