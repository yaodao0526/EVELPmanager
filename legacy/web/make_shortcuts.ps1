# 重建桌面快捷方式：直接指向系统自带的 powershell.exe（Microsoft 签名），
# 不再经过 .bat 入口，避免 Windows 弹出「无法验证发布者」的安全警告。
# 本文件只含 ASCII，中文名称从 shortcut_names.json（UTF-8）读取，避免 PowerShell 5.1 的编码问题。
$ErrorActionPreference = 'Stop'

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$cfg = Get-Content -LiteralPath (Join-Path $here 'shortcut_names.json') -Encoding UTF8 | ConvertFrom-Json

$root = $cfg.root
$desktop = [Environment]::GetFolderPath('Desktop')
$ps = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$icon = Join-Path $root 'assets\eve-lp.ico'
$ws = New-Object -ComObject WScript.Shell

function New-PlannerShortcut {
    param($Path, $ScriptName, $Description)

    $script = Join-Path $root ('tools\' + $ScriptName)
    $lnk = $ws.CreateShortcut($Path)
    $lnk.TargetPath = $ps
    $lnk.Arguments = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $script + '"'
    $lnk.WorkingDirectory = $root
    $lnk.IconLocation = "$icon,0"
    $lnk.WindowStyle = 7
    $lnk.Description = $Description
    $lnk.Save()
    return $lnk
}

$startPath = Join-Path $desktop $cfg.start_name
$stopPath = Join-Path $desktop $cfg.stop_name

New-PlannerShortcut -Path $startPath -ScriptName 'launch.ps1' -Description $cfg.start_desc | Out-Null
New-PlannerShortcut -Path $stopPath -ScriptName 'stop.ps1' -Description $cfg.stop_desc | Out-Null

foreach ($p in @($startPath, $stopPath)) {
    $s = $ws.CreateShortcut($p)
    Write-Output ("{0} -> {1} {2}" -f (Split-Path $p -Leaf), (Split-Path $s.TargetPath -Leaf), $s.Arguments)
}
