"""运行期路径解析：同时支持源码运行与 PyInstaller 打包后的 exe。

- 打包后（sys.frozen）：
    只读资源（static/）在 sys._MEIPASS 里；
    可写数据（config.json / token.json / plans.json ...）放在 exe 同目录，
    这样把 exe 拷到任何地方都能直接跑，不会写进临时解包目录。
- 源码运行：两者都是本文件所在目录。
"""
from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_dir() -> Path:
    """只读资源目录（打包后为解包临时目录）。"""
    meipass = getattr(sys, "_MEIPASS", None)
    if is_frozen() and meipass:
        return Path(meipass)
    return Path(__file__).resolve().parent


def data_dir() -> Path:
    """可写数据目录（打包后为 exe 所在目录）。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def data_path(name: str) -> Path:
    return data_dir() / name


def resource_path(*parts: str) -> Path:
    return resource_dir().joinpath(*parts)
