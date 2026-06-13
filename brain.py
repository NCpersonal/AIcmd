"""
Brain — 双环境执行器（VPC 隔离 + UPC 宿主机）+ Markdown 输出
"""

import os
os.environ["http_proxy"] = ""
os.environ["https_proxy"] = ""
os.environ["all_proxy"] = ""

import re
import json
import asyncio
import subprocess
from typing import Callable, Optional, Dict, List

# ── 命令安全围栏 ──────────────────────────────────────────

def sanitize_command(command: str) -> str:
    """清理命令：移除 ANSI 转义码和不安全字符"""
    if not command:
        return ""
    
    # 移除完整的 ANSI 转义码 (\x1b[...m)
    command = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', command)
    # 移除剩余的 ESC 字符及其后可能跟随的残缺内容
    command = re.sub(r'\x1b', '', command)
    # 移除常见的残余片段（如 0m、90m 等被截断的转义码）
    command = re.sub(r'(?<!\w)(\d+[mM])(?!\w)', '', command)
    # 移除控制字符（除了换行和制表符）
    command = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', command)
    # 只保留可打印 ASCII 和常见 Unicode 字符（命令行字符）
    command = re.sub(r'[^\x20-\x7e\n\t]', '', command)
    # 移除前后空白
    command = command.strip()
    return command

def validate_command(command: str) -> (bool, str):
    """验证命令安全性"""
    if not command or command.isspace():
        return False, "命令为空"
    
    # 检查是否有危险命令
    dangerous_patterns = [
        r'^\s*(rm\s+-rf\s+|dd\s+|mkfs\s+|fdisk\s+|format\s+)',
        r'(;|\|\||\&\&)\s*(rm\s+-rf|shutdown|reboot|poweroff)',
        r'^\s*:(){:|:&};:',  # fork bomb
    ]
    
    for pattern in dangerous_patterns:
        if re.match(pattern, command, re.IGNORECASE):
            return False, f"危险命令被阻止: {command[:30]}..."
    
    # 检查命令长度
    if len(command) > 4096:
        return False, "命令过长"
    
    return True, ""


class DualExecutor:
    """双环境执行器"""

    def __init__(self, vpc_terminal=None, upc_terminal=None, workspace: str = ".",
                 display_cb=None):
        self.vpc_term = vpc_terminal
        self.upc_term = upc_terminal
        self.vpc_cwd = workspace          # VPC 工作目录（隔离）
        self.upc_cwd = os.path.expanduser("~")  # UPC 工作目录（宿主机）
        # display_cb(which, text) — 把文本显示到终端（不经过 bash）
        self._display_cb = display_cb
        self._current_env = "vpc"         # 当前执行环境标识（供 _run 流式显示用）

    def _display(self, which, text):
        """显示文本到终端（仅视觉显示，不输入到 bash）"""
        if self._display_cb and text:
            try:
                self._display_cb(which, text)
            except Exception:
                pass

    # ── VPC 执行（隔离环境）─────────────────────────

    def vpc_exec(self, command: str, timeout: int = 120) -> dict:
        command = sanitize_command(command)
        valid, reason = validate_command(command)
        if not valid:
            self._display("vpc", f"\x1b[31m❌ {reason}\x1b[0m\r\n")
            return {"ok": False, "error": reason}

        if command.strip().startswith("cd ") or command.strip() == "cd":
            result = self._cd("vpc", command)
            if result.get("ok"):
                self._display("vpc", f"\x1b[90m[\x1b[32mVPC\x1b[90m] \x1b[36m{self.vpc_cwd}\x1b[0m$ cd {result.get('cwd','')}\r\n")
            return result

        # 显示上下文信息：环境标识 + 工作目录 + 命令
        self._display("vpc", f"\r\n\x1b[90m[\x1b[32mVPC\x1b[90m] \x1b[36m{self.vpc_cwd}\x1b[0m\r\n")
        self._display("vpc", f"  \x1b[36m$ {command}\x1b[0m\r\n")

        self._current_env = "vpc"
        env = os.environ.copy()
        env.update({"HOME": self.vpc_cwd, "WORKSPACE": self.vpc_cwd, "VPC_MODE": "1"})
        for k in list(env.keys()):
            if k.lower() in ("http_proxy", "https_proxy", "all_proxy"):
                del env[k]
        result = self._run(command, timeout, self.vpc_cwd, env)

        # 显示执行状态
        if result.get("ok"):
            self._display("vpc", f"\x1b[90m  └─ ✓ 完成\x1b[0m\r\n")
        elif result.get("code"):
            self._display("vpc", f"\x1b[90m  └─ \x1b[31m✗ exit {result['code']}\x1b[0m\r\n")
        elif result.get("error"):
            self._display("vpc", f"\x1b[90m  └─ \x1b[31m✗ {result['error']}\x1b[0m\r\n")

        return result

    def vpc_write(self, path: str, content: str) -> dict:
        result = self._write(path, content, self.vpc_cwd)
        if result.get("ok"):
            size = len(content.encode("utf-8"))
            self._display("vpc", f"\r\n\x1b[90m[\x1b[32mVPC\x1b[90m] \x1b[36m{self.vpc_cwd}\x1b[0m\r\n")
            self._display("vpc", f"  \x1b[35m✎ write\x1b[0m {path} ({size} bytes)\r\n")
        else:
            self._display("vpc", f"\x1b[31m✗ 写入失败: {result.get('error', '')}\x1b[0m\r\n")
        return result

    def vpc_read(self, path: str) -> dict:
        return self._read(path, self.vpc_cwd)

    def vpc_ls(self, path: str = ".") -> dict:
        return self._ls(path, self.vpc_cwd)

    # ── UPC 执行（宿主机）───────────────────────────

    def upc_exec(self, command: str, timeout: int = 120) -> dict:
        command = sanitize_command(command)
        valid, reason = validate_command(command)
        if not valid:
            self._display("upc", f"\x1b[31m❌ {reason}\x1b[0m\r\n")
            return {"ok": False, "error": reason}

        if command.strip().startswith("cd ") or command.strip() == "cd":
            result = self._cd("upc", command)
            if result.get("ok"):
                self._display("upc", f"\x1b[90m[\x1b[33mUPC\x1b[90m] \x1b[36m{self.upc_cwd}\x1b[0m$ cd {result.get('cwd','')}\r\n")
            return result

        # 显示上下文信息：环境标识 + 工作目录 + 命令
        self._display("upc", f"\r\n\x1b[90m[\x1b[33mUPC\x1b[90m] \x1b[36m{self.upc_cwd}\x1b[0m\r\n")
        self._display("upc", f"  \x1b[33m$ {command}\x1b[0m\r\n")

        self._current_env = "upc"
        env = os.environ.copy()
        for k in list(env.keys()):
            if k.lower() in ("http_proxy", "https_proxy", "all_proxy"):
                del env[k]
        result = self._run(command, timeout, self.upc_cwd, env)

        # 显示执行状态
        if result.get("ok"):
            self._display("upc", f"\x1b[90m  └─ ✓ 完成\x1b[0m\r\n")
        elif result.get("code"):
            self._display("upc", f"\x1b[90m  └─ \x1b[31m✗ exit {result['code']}\x1b[0m\r\n")
        elif result.get("error"):
            self._display("upc", f"\x1b[90m  └─ \x1b[31m✗ {result['error']}\x1b[0m\r\n")

        return result

    def upc_write(self, path: str, content: str) -> dict:
        result = self._write(path, content, self.upc_cwd)
        if result.get("ok"):
            size = len(content.encode("utf-8"))
            self._display("upc", f"\r\n\x1b[90m[\x1b[33mUPC\x1b[90m] \x1b[36m{self.upc_cwd}\x1b[0m\r\n")
            self._display("upc", f"  \x1b[35m✎ write\x1b[0m {path} ({size} bytes)\r\n")
        else:
            self._display("upc", f"\x1b[31m✗ 写入失败: {result.get('error', '')}\x1b[0m\r\n")
        return result

    def upc_read(self, path: str) -> dict:
        return self._read(path, self.upc_cwd)

    def upc_ls(self, path: str = ".") -> dict:
        return self._ls(path, self.upc_cwd)

    # ── 共享内部方法 ─────────────────────────────────

    def _run(self, command, timeout, cwd, env):
        import threading as _threading
        try:
            proc = subprocess.Popen(
                command, shell=True, cwd=cwd, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
        except Exception as e:
            return {"ok": False, "error": str(e)}

        lines = []
        lock = _threading.Lock()

        def _reader():
            try:
                for line in proc.stdout:
                    with lock:
                        lines.append(line)
                    # 实时显示到终端（去掉末尾 \n，xterm 用 \r\n 换行）
                    self._display(self._current_env, line.rstrip("\n") + "\r\n")
            except Exception:
                pass

        t = _threading.Thread(target=_reader, daemon=True)
        t.start()

        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            t.join(timeout=2)
            return {"ok": False, "error": f"超时 ({timeout}s)"}

        t.join(timeout=3)
        output = "".join(lines)
        return {"ok": code == 0, "code": code, "output": output[:30000]}

    def _write(self, path, content, base, terminal=None):
        fp = os.path.join(base, path)
        os.makedirs(os.path.dirname(fp) if os.path.dirname(fp) else base, exist_ok=True)
        with open(fp, "w", encoding="utf-8") as f:
            f.write(content)
        size = len(content.encode("utf-8"))
        return {"ok": True, "path": path, "bytes": size}

    def _read(self, path, base):
        fp = os.path.join(base, path)
        if not os.path.exists(fp):
            return {"ok": False, "error": f"不存在: {path}"}
        with open(fp, "r", encoding="utf-8", errors="replace") as f:
            c = f.read()
        return {"ok": True, "content": c[:30000], "truncated": len(c) > 30000}

    def _ls(self, path, base):
        dp = os.path.join(base, path)
        if not os.path.exists(dp):
            return {"ok": False, "error": f"不存在: {path}"}
        items = []
        for n in sorted(os.listdir(dp)):
            fp = os.path.join(dp, n)
            e = {"name": n, "type": "dir" if os.path.isdir(fp) else "file"}
            if os.path.isfile(fp):
                e["size"] = os.path.getsize(fp)
            items.append(e)
        return {"ok": True, "path": path, "items": items}

    def _cd(self, env_name, cmd):
        parts = cmd.split(None, 1)
        target = parts[1].strip().strip("'\"") if len(parts) > 1 else "."
        base = self.vpc_cwd if env_name == "vpc" else self.upc_cwd
        new = os.path.normpath(os.path.join(base, target) if not os.path.isabs(target) else target)
        if os.path.isdir(new):
            if env_name == "vpc":
                self.vpc_cwd = new
            else:
                self.upc_cwd = new
            return {"ok": True, "cwd": new}
        return {"ok": False, "error": f"目录不存在: {new}"}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  工具定义
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

TOOLS = [
    # ── VPC 工具（隔离环境）───
    {
        "type": "function",
        "function": {
            "name": "vpc_exec",
            "description": (
                "在 VPC（虚拟隔离环境）中执行命令。"
                "这是一个安全的沙箱，适合：运行用户代码、测试脚本、安装实验性包。"
                "VPC 环境与宿主机隔离，不会影响宿主系统。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell 命令"},
                    "timeout": {"type": "integer", "description": "超时秒数，默认120"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "vpc_write",
            "description": "在 VPC 隔离环境中写入文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "content": {"type": "string", "description": "文件内容"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "vpc_read",
            "description": "在 VPC 中读取文件。",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "vpc_ls",
            "description": "列出 VPC 中的目录内容。",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": []},
        },
    },

    # ── UPC 工具（宿主机）───
    {
        "type": "function",
        "function": {
            "name": "upc_exec",
            "description": (
                "在 UPC（宿主机）上执行命令。拥有完整系统权限。"
                "适合：系统管理（apt/systemctl）、Docker 操作、Git 仓库管理、"
                "访问宿主机文件系统、启动/停止服务。"
                "⚠ 注意：此操作会影响真实系统，请谨慎使用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell 命令"},
                    "timeout": {"type": "integer", "description": "超时秒数，默认120"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "upc_write",
            "description": "在宿主机上写入文件。⚠ 会影响真实文件系统。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "content": {"type": "string", "description": "文件内容"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "upc_read",
            "description": "在宿主机上读取文件。",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "upc_ls",
            "description": "列出宿主机上的目录内容。",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": []},
        },
    },

    # ── 通用工具 ──
    {
        "type": "function",
        "function": {
            "name": "ask",
            "description": "向用户提问。仅在需要用户做选择时使用。",
            "parameters": {
                "type": "object",
                "properties": {"question": {"type": "string", "description": "问题"}},
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "say",
            "description": "发送 Markdown 格式消息给用户。",
            "parameters": {
                "type": "object",
                "properties": {"message": {"type": "string", "description": "Markdown 消息"}},
                "required": ["message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "任务完成。",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "Markdown 格式总结"},
                    "files": {"type": "array", "items": {"type": "string"}, "description": "产物文件"},
                },
                "required": ["summary"],
            },
        },
    },
]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

SYSTEM_PROMPT = """\
你是一个专业的 AI 开发助手，运行在一台 Linux 电脑上，拥有两个独立的执行环境：

## 双环境架构

### 🟢 VPC（Virtual PC — 虚拟隔离环境）
- 完全隔离的工作空间，与宿主机互不影响
- 适合：编写和测试代码、安装依赖、运行脚本、生成文件
- 工具前缀：`vpc_exec`、`vpc_write`、`vpc_read`、`vpc_ls`
- 默认工作目录是隔离的工作区

### 🟠 UPC（宿主机）
- 真实的宿主系统，拥有完整权限
- 适合：系统管理（apt/systemctl）、Docker、Git、访问系统文件、部署服务
- 工具前缀：`upc_exec`、`upc_write`、`upc_read`、`upc_ls`
- 默认工作目录是用户 home

## 选择原则
1. **代码开发、测试、文件生成** → 用 VPC（安全隔离）
2. **系统管理、Docker、部署、Git** → 用 UPC（需要系统权限）
3. **先在 VPC 开发测试，确认无误后再用 UPC 部署**
4. 不确定时，默认用 VPC

## 输出要求
所有文本消息使用 **Markdown 格式**：
- `**粗体**` 强调关键信息
- `` `代码` `` 标记命令/文件名
- ```代码块``` 展示代码（标注语言）
- `1. 2. 3.` 有序列表展示步骤
- `> 引用` 提示注意事项

## 工作流程
1. 分析任务，说明计划（用 say，Markdown 格式）
2. 在合适的环境（VPC/UPC）中执行
3. 验证结果
4. 完成交付（调用 finish）

## 禁止
- 不执行无意义命令（随意 ls/cat/echo）
- 不重复已成功的操作
- 不盲目重试，先分析错误
- 不安装不需要的包
"""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class Brain:
    def __init__(self, executor: DualExecutor, send: Callable,
                 model: str, api_key: str, base_url: str,
                 term_switch: Callable = None):
        from openai import AsyncOpenAI
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self.model = model
        self.executor = executor
        self._send = send
        self._term_switch = term_switch  # 告诉前端切换终端
        self.messages: List[Dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
        self.tokens = 0
        self._running = False
        self._paused = False
        self._answer_future: Optional[asyncio.Future] = None
        self._user_queue: asyncio.Queue = asyncio.Queue()
        self.iterations = 0

    async def run_task(self, task: str):
        await self._send("status", "working")
        self.messages.append({"role": "user", "content": task})
        self._running = True
        self._paused = False
        self.iterations = 0

        while self._running and self.iterations < 80:
            self.iterations += 1
            if self._paused:
                await asyncio.sleep(0.5)
                continue
            while not self._user_queue.empty():
                self.messages.append({"role": "user", "content": self._user_queue.get_nowait()})

            try:
                stream = await self.client.chat.completions.create(
                    model=self.model, messages=self.messages,
                    tools=TOOLS, tool_choice="auto", temperature=0.1,
                    stream=True, stream_options={"include_usage": True},
                )

                full_content = ""
                tc_data: Dict[int, Dict] = {}

                async for chunk in stream:
                    if chunk.usage:
                        self.tokens += chunk.usage.total_tokens
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    if delta.content:
                        full_content += delta.content
                        await self._send("ai_stream", delta.content)
                    if delta.tool_calls:
                        for tc in delta.tool_calls:
                            i = tc.index
                            if i not in tc_data:
                                tc_data[i] = {"id": tc.id or "", "name": "", "arguments": ""}
                            if tc.id:
                                tc_data[i]["id"] = tc.id
                            if tc.function:
                                if tc.function.name:
                                    tc_data[i]["name"] = tc.function.name
                                if tc.function.arguments:
                                    tc_data[i]["arguments"] += tc.function.arguments

                if full_content:
                    await self._send("ai_stream_end", None)

                if tc_data:
                    self.messages.append({
                        "role": "assistant", "content": full_content or None,
                        "tool_calls": [
                            {"id": t["id"], "type": "function",
                             "function": {"name": t["name"], "arguments": t["arguments"]}}
                            for t in tc_data.values()
                        ],
                    })
                    for t in tc_data.values():
                        result = await self._dispatch(t)
                        self.messages.append({
                            "role": "tool", "tool_call_id": t["id"],
                            "content": json.dumps(result, ensure_ascii=False, default=str),
                        })
                        while not self._user_queue.empty():
                            self.messages.append({"role": "user", "content": self._user_queue.get_nowait()})
                else:
                    break

            except asyncio.CancelledError:
                break
            except Exception as e:
                await self._send("error", f"API 错误: {str(e)[:200]}")
                await asyncio.sleep(2)

        await self._send("status", "idle")

    async def _dispatch(self, tc: dict) -> dict:
        name = tc["name"]
        try:
            # 兼容两种格式：字符串和字典
            if isinstance(tc["arguments"], dict):
                args = tc["arguments"]
            else:
                args = json.loads(tc["arguments"])
        except (json.JSONDecodeError, TypeError):
            args = {}

        await self._send("ai_action", {"name": name, "args": args})

        # VPC 工具 → 切换到 VPC 终端
        if name.startswith("vpc_") and self._term_switch:
            await self._term_switch("vpc")
        elif name.startswith("upc_") and self._term_switch:
            await self._term_switch("upc")

        loop = asyncio.get_event_loop()

        dispatch = {
            "vpc_exec":   lambda: loop.run_in_executor(None, self.executor.vpc_exec, args.get("command",""), args.get("timeout",120)),
            "vpc_write":  lambda: loop.run_in_executor(None, self.executor.vpc_write, args.get("path",""), args.get("content","")),
            "vpc_read":   lambda: loop.run_in_executor(None, self.executor.vpc_read, args.get("path","")),
            "vpc_ls":     lambda: loop.run_in_executor(None, self.executor.vpc_ls, args.get("path",".")),
            "upc_exec":   lambda: loop.run_in_executor(None, self.executor.upc_exec, args.get("command",""), args.get("timeout",120)),
            "upc_write":  lambda: loop.run_in_executor(None, self.executor.upc_write, args.get("path",""), args.get("content","")),
            "upc_read":   lambda: loop.run_in_executor(None, self.executor.upc_read, args.get("path","")),
            "upc_ls":     lambda: loop.run_in_executor(None, self.executor.upc_ls, args.get("path",".")),
        }

        if name in dispatch:
            return await dispatch[name]()
        elif name == "ask":
            return await self._ask(args.get("question", ""))
        elif name == "say":
            await self._send("ai_msg", args.get("message", ""))
            return {"ok": True}
        elif name == "finish":
            self._running = False
            await self._send("ai_done", {
                "summary": args.get("summary", ""), "files": args.get("files", []),
                "tokens": self.tokens, "iterations": self.iterations,
            })
            return {"ok": True}

        return {"ok": False, "error": f"未知: {name}"}

    async def _ask(self, question: str) -> dict:
        await self._send("status", "asking")
        await self._send("ai_ask", question)
        self._answer_future = asyncio.get_event_loop().create_future()
        try:
            answer = await self._answer_future
        except asyncio.CancelledError:
            return {"answer": "(取消)"}
        await self._send("status", "working")
        return {"answer": answer}

    def answer_question(self, answer: str):
        if self._answer_future and not self._answer_future.done():
            self._answer_future.set_result(answer)

    def inject_message(self, content: str):
        self._user_queue.put_nowait(content)

    def pause(self): self._paused = True
    def resume(self): self._paused = False
    def stop(self):
        self._running = False
        self._paused = False
        if self._answer_future and not self._answer_future.done():
            self._answer_future.cancel()
