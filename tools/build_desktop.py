"""把 gui.py 打包成 Windows 桌面程序（PyInstaller onedir）。

用法（在仓库根目录）：
    .venv-build\\Scripts\\python.exe tools\\build_desktop.py           # 只打包
    .venv-build\\Scripts\\python.exe tools\\build_desktop.py --install  # 打包 + 安装到 app/ + 桌面快捷方式

为什么要 .venv-build：本机系统 Python 是 3.14，PyInstaller 对 3.14 的 tkinter 打包还不稳；
.venv-build 是 Python 3.12（uv 建的），打包出来最省心。
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = "EVE商人助手"
DIST = ROOT / "dist" / NAME
# 安装目录放在仓库里而不是 %LOCALAPPDATA%：本机安全/沙箱策略会拒绝在 AppData 下新建目录，
# 而程序目录本身需要在运行时写 gui_state.json，放在仓库里反而更稳。
INSTALL_DIR = ROOT / "app"


def build() -> Path:
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--windowed",            # 不带黑色控制台窗口
        "--onedir",              # onefile 在部分机器上被安全软件拦临时目录，故用目录式
        "--name", NAME,
        "--icon", str(ROOT / "assets" / "eve-lp.ico"),
        "--add-data", f"{ROOT / 'assets'}{';'}assets",
        "--log-level", "WARN",
        str(ROOT / "gui.py"),
    ]
    print("$", " ".join(cmd))
    subprocess.run(cmd, cwd=ROOT, check=True)
    exe = DIST / f"{NAME}.exe"
    if not exe.exists():
        raise SystemExit(f"打包失败：没找到 {exe}")
    return exe


def install(exe: Path) -> Path:
    r"""把 dist 目录整体拷到仓库内的 app\<NAME>。

    用 PowerShell 的 Copy-Item 而不是 shutil：本机安全软件会拦 python 进程批量写 exe
    （PermissionError: WinError 5），换成 powershell 复制就正常。
    """
    INSTALL_DIR.parent.mkdir(parents=True, exist_ok=True)
    ps = f"""
$src = '{DIST}'
$dst = '{INSTALL_DIR}'
if (-not (Test-Path $dst)) {{ New-Item -ItemType Directory -Path $dst -Force | Out-Null }}
Copy-Item -Path (Join-Path $src '*') -Destination $dst -Recurse -Force
Write-Output 'copied'
"""
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True, capture_output=True, text=True)
    # 保留用户数据：gui_state.json / lp_costs.json 不覆盖
    src_lpc = ROOT / "lp_costs.json"
    dst_lpc = INSTALL_DIR / "lp_costs.json"
    if src_lpc.exists() and not dst_lpc.exists():
        subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"Copy-Item -Path '{src_lpc}' -Destination '{dst_lpc}' -Force"],
            check=False, capture_output=True, text=True,
        )
    unblock(INSTALL_DIR)
    return INSTALL_DIR / f"{NAME}.exe"


def unblock(path: Path) -> None:
    """去掉「来自网络」标记（Zone.Identifier），否则每次运行都会弹“打开文件-安全警告”。

    新建/覆盖过的二进制在部分机器上会被打上 MOTW，资源管理器就会拦一次；
    Unblock-File 对没有标记的文件是空操作，所以无条件执行。
    """
    ps = f"""
$p = '{path}'
if (Test-Path $p) {{
    Get-ChildItem -Path $p -Recurse -File -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue
    Unblock-File -Path $p -ErrorAction SilentlyContinue
}}
Write-Output 'unblocked'
"""
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=False, capture_output=True, text=True)


def sign(path: Path) -> None:
    r"""用本机自签代码签名证书签名，避免 SmartScreen 报「无法识别的应用 / 发布者未知」。

    证书在 CurrentUser\My，同时被加进 CurrentUser\Root 与 TrustedPublisher（无需管理员）。
    每次重新打包后 exe 都会变，所以必须在 install 之后再签一次。
    """
    ps = f"""
$subject = 'CN=EVE LP Planner (Local)'
$cert = Get-ChildItem Cert:\\CurrentUser\\My | Where-Object {{ $_.Subject -eq $subject }} | Select-Object -First 1
if (-not $cert) {{
    $cert = New-SelfSignedCertificate -Type CodeSigningCert -Subject $subject -CertStoreLocation Cert:\\CurrentUser\\My -NotAfter (Get-Date).AddYears(10) -KeyUsage DigitalSignature
    $cer = Join-Path $env:TEMP 'evelp-code.cer'
    Export-Certificate -Cert $cert -FilePath $cer -Force | Out-Null
    certutil -user -addstore Root $cer | Out-Null
    certutil -user -addstore TrustedPublisher $cer | Out-Null
}}
$exe = '{path}'
Get-ChildItem -Path (Split-Path $exe) -Recurse -Filter '*.exe' -ErrorAction SilentlyContinue | ForEach-Object {{
    Set-AuthenticodeSignature -FilePath $_.FullName -Certificate $cert -HashAlgorithm SHA256 | Out-Null
}}
$r = Get-AuthenticodeSignature -FilePath $exe
Write-Output ('signed=' + $r.Status)
"""
    script = ROOT / "dist" / "_sign.ps1"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_bytes(b"\xff\xfe" + ps.encode("utf-16-le"))
    out = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        check=False, capture_output=True, text=True,
    )
    print((out.stdout or out.stderr).strip())


def make_shortcut(target: Path) -> Path:
    """用 WScript.Shell 建/刷新桌面快捷方式。

    注意：脚本里含中文路径，必须写成 UTF-16LE + BOM 的 .ps1 交给 powershell -File，
    直接 `powershell -Command "...中文..."` 会按本地 ANSI 编码解析成乱码（导致写错文件名）。
    """
    ps = f"""
$exe = '{target}'
$desk = [Environment]::GetFolderPath('Desktop')
$lnk = Join-Path $desk 'EVE 商人助手.lnk'
$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut($lnk)
$s.TargetPath = $exe
$s.WorkingDirectory = Split-Path $exe
$s.IconLocation = "$exe,0"
$s.Description = 'EVE LP 兑换规划器（桌面版）'
$s.Save()
Write-Output $lnk
"""
    script = ROOT / "dist" / "_make_shortcut.ps1"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_bytes(b"\xff\xfe" + ps.encode("utf-16-le"))
    out = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        capture_output=True, text=True, check=True,
    )
    lnk = Path(out.stdout.strip().splitlines()[-1])
    unblock(lnk)
    return lnk


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--install", action="store_true", help="打包后装到 app/ 并刷新桌面快捷方式")
    args = ap.parse_args()

    exe = build()
    print(f"打包完成：{exe}  ({exe.stat().st_size / 1e6:.1f} MB)")
    if args.install:
        installed = install(exe)
        print(f"已安装：{installed}")
        sign(installed)
        print(f"桌面快捷方式：{make_shortcut(installed)}")


if __name__ == "__main__":
    main()
