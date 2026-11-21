"""EVE LP 兑换规划器 - 桌面版打包脚本（PyInstaller）。

用法（在仓库根目录）：
    .venv-build\\Scripts\\python.exe tools\\build_exe.py

产物：dist\\EVE商人助手.exe（单文件，双击即用）。
可写数据（config.json / token.json / plans.json / lp_costs.json）放在 exe 同目录，
首次运行会自动生成，不会被写进 exe 内部。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"
EXE_NAME = "EVE商人助手"


def main() -> int:
    if not (ROOT / "static" / "index.html").exists():
        print("找不到 static/index.html，请在仓库根目录执行")
        return 1

    # 默认 onedir：启动快（不需要每次解包到 %TEMP%），也不容易被安全软件拦。
    # 传 --onefile 可以生成单文件版，但本机实测 PyInstaller 单文件模式报
    # “Could not create temporary directory!”（安全软件拦截临时目录），故不用。
    onefile = "--onefile" in sys.argv
    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onefile" if onefile else "--onedir",
        "--windowed",
        "--name", EXE_NAME,
        "--icon", str(ROOT / "assets" / "eve-lp.ico"),
        "--add-data", f"{ROOT / 'static'};static",
        "--add-data", f"{ROOT / 'assets'};assets",
        # uvicorn 的协议实现是按字符串动态导入的，得显式带上
        "--hidden-import", "uvicorn.logging",
        "--hidden-import", "uvicorn.loops.auto",
        "--hidden-import", "uvicorn.loops.asyncio",
        "--hidden-import", "uvicorn.protocols.http.auto",
        "--hidden-import", "uvicorn.protocols.websockets.auto",
        "--hidden-import", "uvicorn.lifespan.on",
        "--hidden-import", "webview.platforms.edgechromium",
        "--collect-submodules", "webview",
        "--distpath", str(DIST),
        "--workpath", str(BUILD),
        "--specpath", str(BUILD),
        str(ROOT / "desktop.py"),
    ]
    print(" ".join(args))
    rc = subprocess.call(args, cwd=str(ROOT))
    if rc != 0:
        print("打包失败，退出码", rc)
        return rc

    exe = DIST / f"{EXE_NAME}.exe" if onefile else DIST / EXE_NAME / f"{EXE_NAME}.exe"
    if not exe.exists():
        print("没有生成 exe：", exe)
        return 1

    out_dir = exe.parent
    # 把随包文件放到 exe 旁边（存在才拷，避免覆盖旧数据）
    for name in ("config.example.json", "README.md"):
        src = ROOT / name
        if src.exists():
            shutil.copy2(src, out_dir / name)

    size_mb = exe.stat().st_size / 1e6
    print(f"\n完成：{exe}  ({size_mb:.1f} MB)")
    print("双击运行；数据文件（config.json / plans.json / lp_costs.json）会生成在同目录。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
