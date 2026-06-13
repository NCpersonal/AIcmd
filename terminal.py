"""
DualTerminal — 虚拟机终端(VPC) + 宿主机终端(UPC)
"""

import os
import pty
import select
import fcntl
import struct
import signal
import threading
from typing import Callable, Optional

TIOCSWINSZ = 0x5414


class Terminal:
    """单个 PTY 终端"""

    def __init__(self, name: str, color: str):
        self.name = name
        self.color = color  # 用于终端顶部标签颜色
        self.master_fd: int = -1
        self.pid: int = -1
        self._running: bool = False
        self._callbacks: list = []
        self._lock = threading.Lock()
        self.cwd: str = os.path.expanduser("~")

    def start(self, rows: int = 24, cols: int = 80, workspace: Optional[str] = None,
              isolate: bool = False):
        """
        isolate=True: 虚拟机模式，HOME/workspace 指向隔离目录
        isolate=False: 宿主机模式，继承所有宿主机环境
        """
        master_fd, slave_fd = pty.openpty()

        if isolate:
            ws = workspace or "/tmp/vpc"
            os.makedirs(ws, exist_ok=True)
        else:
            ws = workspace or os.path.expanduser("~")

        pid = os.fork()
        if pid == 0:
            os.close(master_fd)
            os.setsid()
            os.dup2(slave_fd, 0)
            os.dup2(slave_fd, 1)
            os.dup2(slave_fd, 2)
            if slave_fd > 2:
                os.close(slave_fd)

            os.chdir(ws)

            # 清除代理
            for k in list(os.environ.keys()):
                if k.lower() in ("http_proxy", "https_proxy", "all_proxy"):
                    del os.environ[k]

            if isolate:
                # VPC 模式：隔离环境
                os.environ["HOME"] = ws
                os.environ["WORKSPACE"] = ws
                os.environ["VPC_MODE"] = "1"
                os.environ["TERM"] = "xterm-256color"

                vpc_rc = os.path.join(ws, ".vpc_rc")
                with open(vpc_rc, "w") as f:
                    f.write('export PS="\\[\\e[38;5;82m\\]VPC\\[\\e[0m\\]:\\w$ "\n')
                    f.write('export PS1="$PS"\n')
                    f.write(f'cd "{ws}"\n')
                    f.write('alias python=python3\n')
                os.execvp("/bin/bash", ["/bin/bash", "--rcfile", vpc_rc, "-i"])
            else:
                # UPC 模式：宿主机完整环境
                os.environ["UPC_MODE"] = "1"
                os.environ["TERM"] = "xterm-256color"

                real_bashrc = os.path.expanduser("~/.bashrc")
                upc_rc = os.path.join(ws, ".upc_rc")
                with open(upc_rc, "w") as f:
                    f.write(f'[ -f "{real_bashrc}" ] && source "{real_bashrc}" 2>/dev/null\n')
                    f.write('PS1="\\[\\e[38;5;214m\\]UPC\\[\\e[0m\\]:\\w$ "\n')
                    f.write('export PS1="$PS1"\n')
                os.execvp("/bin/bash", ["/bin/bash", "--rcfile", upc_rc, "-i"])

        os.close(slave_fd)
        self.master_fd = master_fd
        self.pid = pid
        self._running = True
        self.cwd = ws
        self.resize(rows, cols)
        threading.Thread(target=self._read_loop, daemon=True).start()

    def _read_loop(self):
        while self._running:
            try:
                rlist, _, _ = select.select([self.master_fd], [], [], 0.02)
                if rlist:
                    data = os.read(self.master_fd, 65536)
                    if not data:
                        break
                    for cb in self._callbacks[:]:
                        try:
                            cb(data)
                        except Exception as e:
                            print(f"Terminal._read_loop error: {e}")
                            pass
            except (OSError, IOError) as e:
                print(f"Terminal._read_loop error: {e}")
                break

    def write(self, data: str):
        if not self._running or self.master_fd < 0:
            return
        with self._lock:
            try:
                os.write(self.master_fd, data.encode("utf-8"))
            except OSError as e:
                print(f"Terminal.write error: {e}")
                pass

    def on_output(self, callback):
        self._callbacks.append(callback)

    def resize(self, rows: int, cols: int):
        if self.master_fd < 0:
            return
        try:
            fcntl.ioctl(self.master_fd, TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        except OSError as e:
            print(f"Terminal.resize error: {e}")
            pass

    def close(self):
        self._running = False
        if self.master_fd >= 0:
            try:
                os.close(self.master_fd)
            except OSError as e:
                print(f"Terminal.close error: {e}")
                pass
        if self.pid > 0:
            try:
                os.kill(self.pid, signal.SIGTERM)
                os.waitpid(self.pid, os.WNOHANG)
            except (OSError, ChildProcessError) as e:
                print(f"Terminal.close error: {e}")
                pass
        self._running = False
        self._callbacks.clear()
        self.master_fd = -1
        self.pid = -1
        self.cwd = os.path.expanduser("~")


class DualTerminal:
    """双终端管理器：VPC（虚拟机）+ UPC（宿主机）"""

    def __init__(self, workspace: str):
        self.workspace = workspace

        # 虚拟机终端
        self.vpc = Terminal("VPC", "#4ade80")
        # 宿主机终端
        self.upc = Terminal("UPC", "#f59e0b")

        self._active: str = "vpc"  # 默认显示 vpc

    @property
    def active(self) -> Terminal:
        return self.vpc if self._active == "vpc" else self.upc

    def start(self, rows: int = 24, cols: int = 80):
        # VPC：隔离环境
        self.vpc.start(rows=rows, cols=cols, workspace=self.workspace, isolate=True)
        # UPC：宿主机环境
        self.upc.start(rows=rows, cols=cols, workspace=os.path.expanduser("~"), isolate=False)

    def resize(self, rows: int, cols: int):
        self.vpc.resize(rows, cols)
        self.upc.resize(rows, cols)

    def switch_to(self, which: str):
        """切换显示的终端：'vpc' 或 'upc'"""
        if which in ("vpc", "upc"):
            self._active = which

    def close(self):
        self.vpc.close()
        self.upc.close()
