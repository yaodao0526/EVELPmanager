"""EVE LP 兑换规划器 - 桌面版入口（打包成 exe 使用）。

启动流程：
1. 在后台线程里跑本地 FastAPI 服务（只监听 127.0.0.1，不对外）
2. 等服务就绪后，用一个原生窗口（WebView2）打开界面 —— 不经过浏览器标签页
3. 关掉窗口就退出程序

`--floater` 参数用于悬浮窗：后端会以 `EVE商人助手.exe --floater` 的方式拉起它。
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
import traceback
from pathlib import Path

import paths

PREFERRED_PORT = 8000
APP_TITLE = "EVE LP 兑换规划器"


def _log(msg: str) -> None:
    try:
        log_dir = paths.data_dir() / "logs"
        log_dir.mkdir(exist_ok=True)
        with open(log_dir / "gui.log", "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


def _pick_port() -> int:
    """优先 8000（ESI 回调地址写死了这个端口），被占用时顺延。"""
    for port in range(PREFERRED_PORT, PREFERRED_PORT + 20):
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", port))
            return port
        except OSError:
            continue
        finally:
            probe.close()
    return PREFERRED_PORT


def _probe(url: str, timeout: float = 0.6) -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _start_server(port: int) -> "object":
    import uvicorn

    from app import app

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
        lifespan="on",
        # 无控制台窗口时 sys.stdout 是 None，uvicorn 默认的日志格式化器会在
        # sys.stdout.isatty() 上崩掉，所以这里完全绕开它的 logging 配置
        log_config=None,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="uvicorn", daemon=True)
    thread.start()
    return server


def main() -> int:
    # 打包成 --windowed 后没有控制台，sys.stdout/stderr 为 None：
    # 任何 print / 第三方库写日志都会炸，这里统一兜到一个空写入器
    if sys.stdout is None or sys.stderr is None:
        import io

        sink = io.StringIO()
        sys.stdout = sys.stdout or sink
        sys.stderr = sys.stderr or sink

    if "--floater" in sys.argv:
        import floater

        floater.main()
        return 0

    # 已有实例在跑（端口 8000 返回我们的 /api/meta）就直接复用，不重复起服务
    server = None
    port = PREFERRED_PORT
    if not _probe(f"http://127.0.0.1:{port}/api/meta"):
        port = _pick_port()
        try:
            server = _start_server(port)
        except Exception:
            _log("服务启动失败:\n" + traceback.format_exc())
            raise

    url = f"http://127.0.0.1:{port}"
    for _ in range(200):  # 最多等 20 秒
        if _probe(f"{url}/api/meta"):
            break
        time.sleep(0.1)
    else:
        _log("服务 20 秒内没有就绪")
        raise RuntimeError("本地服务启动超时，见 logs/gui.log")

    _run_window(url)

    if server is not None:
        server.should_exit = True
        time.sleep(0.3)
    return 0


def _find_edge() -> str | None:
    """找本机的 Microsoft Edge（WebView2 同源，免装额外运行时）。"""
    for p in (
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ):
        if Path(p).exists():
            return p
    return None


def _edge_app_window(url: str) -> int:
    """用 Edge 的 --app 模式开一个独立窗口：没有地址栏/标签页，任务栏里是应用窗口。

    独立的 user-data-dir 有两个作用：一眼能看出这是我们自己的窗口，
    以及 localStorage（税费、档位、LP 选择）落在程序目录里、跨次启动保留。
    """
    import subprocess

    exe = _find_edge()
    if not exe:
        raise RuntimeError("没找到 Microsoft Edge，无法打开界面窗口")

    # 独立的 profile 目录：窗口进程与用户自己的 Edge 隔离，
    # 而且 localStorage（税费、档位、LP 选择）跟着程序目录走、跨次启动保留
    profile = paths.data_dir() / ".window-profile"
    profile.mkdir(parents=True, exist_ok=True)

    log_dir = paths.data_dir() / "logs"
    log_dir.mkdir(exist_ok=True)
    # TEMP/TMP 指到程序目录下：浏览器的缓存/渲染进程要用能写的临时目录，
    # 受限环境里系统 TEMP 可能不可写，结果就是一个空白窗口
    tmp = paths.data_dir() / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["TEMP"] = str(tmp)
    env["TMP"] = str(tmp)
    with open(log_dir / "edge.log", "ab") as fh:
        proc = subprocess.Popen(
            [
                exe,
                f"--app={url}",
                f"--user-data-dir={profile}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-background-mode",
                "--window-size=1600,1000",
                "--start-maximized",
            ],
            cwd=str(profile.parent),
            stdout=fh,
            stderr=fh,
            env=env,
        )
    _log(f"已启动 Edge 窗口 pid={proc.pid} url={url}")
    # 关掉窗口 = Edge 进程结束；主进程跟着退出
    while proc.poll() is None:
        time.sleep(0.5)
    _log(f"Edge 窗口已退出 code={proc.returncode}")
    return proc.returncode or 0


def _webview_window(url: str) -> int | None:
    """源码运行时优先用 pywebview 的原生窗口（打包后 pythonnet 不可靠，会返回 None）。"""
    if paths.is_frozen():
        return None
    try:
        import webview
    except Exception:
        _log("pywebview 不可用，改用 Edge 窗口:\n" + traceback.format_exc())
        return None

    storage = paths.data_dir() / ".webview"
    storage.mkdir(parents=True, exist_ok=True)
    try:
        webview.create_window(
            APP_TITLE,
            url,
            width=1560,
            height=980,
            min_size=(1180, 760),
            background_color="#0b1018",
            maximized=True,
        )
        webview.start(private_mode=False, storage_path=str(storage), debug=False)
    except Exception:
        _log("pywebview 启动失败，改用 Edge 窗口:\n" + traceback.format_exc())
        return None
    return 0


def _run_window(url: str) -> int:
    rc = _webview_window(url)
    if rc is not None:
        return rc
    return _edge_app_window(url)


if __name__ == "__main__":
    # 打包成无控制台窗口时，任何异常都只能写日志
    try:
        sys.exit(main())
    except Exception:
        _log("未捕获异常:\n" + traceback.format_exc())
        raise
