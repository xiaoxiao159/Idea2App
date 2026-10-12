"""原生目录选择器:浏览器为安全拿不到绝对路径,supervisor 与项目目录同机,
故由 supervisor 弹系统原生目录对话框,把绝对路径报回前端(手输兜底)。

Windows 用 PowerShell + System.Windows.Forms.FolderBrowserDialog(系统自带、零依赖);
其它平台 zenity / kdialog。返回所选绝对路径或 None(取消/不可用)。
"""
from __future__ import annotations

import subprocess
import sys


def pick_dir() -> str | None:
    if sys.platform == "win32":
        return _pick_windows()
    for cmd in (("zenity", "--file-selection", "--directory", "--title=选择项目目录"),
                ("kdialog", "--getexistingdirectory", "选择项目目录")):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if out.returncode != 0:
                continue
            path = out.stdout.strip()
            if path:
                return path
        except Exception:
            continue
    return None


def _pick_windows() -> str | None:
    script = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
        "$d.Description = '选择项目目录';"
        "$d.ShowNewFolderButton = $true;"
        "if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { $d.SelectedPath }"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", script],
            capture_output=True, text=True, timeout=600)
        if out.returncode != 0:
            return None
        path = out.stdout.strip()
        return path or None
    except Exception:
        return None