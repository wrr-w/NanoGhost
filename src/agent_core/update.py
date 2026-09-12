import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def _app_dir() -> str:
    if getattr(sys, "frozen", False):
        return str(Path(sys.executable).parent)
    return os.getcwd()


def _config_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".nanoghost", "update.json")


def _read_config() -> dict:
    path = _config_path()
    if not os.path.isfile(path):
        return {}
    try:
        return json.loads(open(path, encoding="utf-8").read())
    except Exception:
        return {}


def _check_github(repo: str, asset_prefix: str = "nanoghost") -> tuple[bool, str, str]:
    import requests
    try:
        r = requests.get(
            f"https://api.github.com/repos/{repo}/releases/latest",
            headers={"Accept": "application/vnd.github+json"},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        tag = str(data.get("tag_name", "")).strip().lstrip("v")
        if not tag:
            return False, "", "no tag_name found"

        # GitHub 自动生成的 "Source code (zip)" 不在 assets 里（它走 zipball_url），
        # 所以正常情况下 assets 里只有一个 .zip。但手滑多传一个 zip 就会命中错误
        # 的文件，所以先按文件名前缀挑，挑不到再退回"第一个 .zip"。
        assets = [
            (str(a.get("name", "")).lower(), str(a.get("browser_download_url", "")))
            for a in (data.get("assets") or [])
        ]
        prefix = (asset_prefix or "").strip().lower()
        if prefix:
            for name, url in assets:
                if url and name.endswith(".zip") and name.startswith(prefix):
                    return True, tag, url
        for name, url in assets:
            if url and name.endswith(".zip"):
                return True, tag, url
        return False, "", "no .zip asset found in release"
    except Exception as e:
        return False, "", str(e)


def check_for_updates() -> tuple[bool, str, str]:
    from agent_core.version import current_version, compare_versions

    cfg = _read_config()
    repo = (cfg.get("repo") or "").strip()
    if not repo:
        return False, "", "未配置 repo，请在 ~/.nanoghost/update.json 设置 {\"repo\": \"owner/repo\"}"

    local = current_version()
    ok, remote_version, url = _check_github(repo, cfg.get("asset_prefix") or "nanoghost")
    if not ok:
        return False, "", f"检查更新失败: {url}"
    if compare_versions(remote_version, local) > 0:
        return True, remote_version, url
    return False, remote_version, url


def update_result_path() -> str:
    """覆盖结果文件路径。外部管理器轮询这个文件判断升级是否成功。

    内容: "OK" 表示成功；"FAIL:<阶段>" 表示失败（timeout/expand/copy）。
    可用环境变量 NANOGHOST_UPDATE_RESULT 覆盖（服务账号下 %TEMP% 可能不同）。
    """
    override = (os.getenv("NANOGHOST_UPDATE_RESULT") or "").strip()
    if override:
        return override
    return os.path.join(tempfile.gettempdir(), "nanoghost_update_result.txt")


def update_log_path() -> str:
    """非交互模式下升级脚本的日志路径（交互模式下输出直接打在控制台）。"""
    override = (os.getenv("NANOGHOST_UPDATE_LOG") or "").strip()
    if override:
        return override
    return os.path.join(tempfile.gettempdir(), "nanoghost_update.log")


def _write_update_bat(
    zip_path: str,
    target_dir: str,
    exe_name: str,
    interactive: bool = True,
    restart: bool = True,
) -> str:
    """生成并写出升级脚本，返回其路径。

    四个关键点（原实现在这里连续翻车）：

    1. 不能用 [System.IO.Compression.ZipFile]::ExtractToDirectory —— .NET Framework
       版本遇到目标已存在的文件会直接抛异常且**不覆盖**。覆盖安装时目标目录里
       几乎所有文件都已存在，必然第一条就失败。改用 Expand-Archive -Force。
    2. 解压/覆盖失败时必须中止，不能删掉安装包、更不能重启旧版本 —— 否则调用方
       看到"升级完成"，跑的却还是旧代码。
    3. 等待必须用 ping 而不是 timeout —— timeout 依赖控制台输入，在
       CREATE_NO_WINDOW 下会立刻报错返回，导致 60 次循环瞬间跑完、误判超时。
    4. 文件必须写成 CRLF，且**只包含 ASCII 字符**。cmd 按字节偏移跟踪批处理位置，
       文件里的多字节字符会让偏移错位，cmd 从行中间开始执行，输出 'd_iscc' 这类
       碎片报错。CRLF 只能挡住一部分情况，彻底的做法是不放多字节字符 —— 这个脚本
       跑在无人值守路径上，宁可日志是英文也不能靠运气。
       所以下面的提示文字一律用英文，这是刻意的，不要"顺手翻译回中文"。

    流程：等进程退出 → 解压到 staging → robocopy 覆盖到安装目录 → 删包 → 写结果 → 重启。

    interactive=True:  附加控制台窗口，失败时 pause，输出打屏
    interactive=False: 无窗口，输出重定向到 update_log_path()，不 pause
    restart=False:     覆盖完不拉起程序，交给外部管理器
    """
    bat = os.path.join(tempfile.gettempdir(), "nanoghost_update.bat")
    result = update_result_path()
    log = update_log_path()

    lines = [
        "@echo off",
        "chcp 65001 >nul",
        "title NanoGhost Updater",
        "",
        f'set "ZIP={zip_path}"',
        f'set "TARGET={target_dir}"',
        f'set "EXE={exe_name}"',
        'set "STAGE=%TEMP%\\nanoghost_stage"',
        f'set "RESULT={result}"',
        "",
    ]
    if interactive:
        lines += ["call :main", "exit /b %errorlevel%"]
    else:
        lines += [f'call :main >> "{log}" 2>&1', "exit /b %errorlevel%"]
    lines += [
        "",
        ":main",
        'if exist "%RESULT%" del /f /q "%RESULT%"',
        "",
        "echo Waiting for NanoGhost to exit...",
        "set /a _n=0",
        ":waitloop",
        'tasklist /FI "IMAGENAME eq %EXE%" 2>nul | find /I "%EXE%" >nul',
        "if errorlevel 1 goto :extract",
        "set /a _n+=1",
        "if %_n% GEQ 60 goto :fail_timeout",
        "ping -n 2 127.0.0.1 >nul 2>&1",
        "goto :waitloop",
        "",
        ":extract",
        'if exist "%STAGE%" rmdir /s /q "%STAGE%"',
        "echo Extracting package...",
        'powershell -NoProfile -Command "Expand-Archive -LiteralPath $env:ZIP -DestinationPath $env:STAGE -Force"',
        "if errorlevel 1 goto :fail_expand",
        "",
        "echo Copying into install dir...",
        'robocopy "%STAGE%" "%TARGET%" /E /NFL /NDL /NJH /NJS /NP',
        "if errorlevel 8 goto :fail_copy",
        'rmdir /s /q "%STAGE%"',
        'del /f /q "%ZIP%"',
        'echo OK> "%RESULT%"',
        "echo Update complete.",
    ]
    if restart:
        lines += [
            f'start "" "{os.path.join(target_dir, exe_name)}"',
            "echo Restarted.",
        ]
    else:
        lines += ["echo Restart skipped (external manager will start it)."]
    lines += [
        "exit /b 0",
        "",
        ":fail_timeout",
        "echo [ERROR] NanoGhost is still running (wait timed out), update aborted.",
        'echo FAIL:timeout> "%RESULT%"',
        "goto :fail",
        "",
        ":fail_expand",
        "echo [ERROR] Failed to extract the package. The download may be corrupt.",
        'echo FAIL:expand> "%RESULT%"',
        "goto :fail",
        "",
        ":fail_copy",
        "echo [ERROR] Failed to write into the install dir. The exe may still be locked.",
        'echo FAIL:copy> "%RESULT%"',
        "goto :fail",
        "",
        ":fail",
        "echo.",
        "echo Nothing was changed. The package is kept at: %ZIP%",
        "echo Extract it manually over: %TARGET%",
    ]
    if interactive:
        lines += ["pause"]
    lines += ["exit /b 1", ""]

    # newline="" 关掉文本模式的换行翻译，否则显式的 \r\n 会被再翻译一次变成 \r\r\n
    with open(bat, "w", encoding="utf-8", newline="") as f:
        f.write("\r\n".join(lines) + "\r\n")
    return bat


def apply_update(
    download_url: str,
    interactive: bool = True,
    restart: bool = True,
) -> tuple[bool, str]:
    """下载安装包并启动覆盖脚本。

    返回 (True, "") 只代表**覆盖脚本已启动**，不代表覆盖已经成功 —— 覆盖在
    当前进程退出之后才发生。调用方（尤其是外部管理器）应当轮询
    update_result_path() 判断最终结果。

    本函数只负责启动；调用方需自行退出当前进程让出文件占用。
    """
    import requests

    if not download_url:
        return False, "未提供下载地址"

    try:
        tmp_zip = os.path.join(tempfile.gettempdir(), "nanoghost_update.zip")

        # 清掉上一次的结果，让调用方能靠"文件出现"判断本次是否结束
        stale = update_result_path()
        if os.path.isfile(stale):
            try:
                os.remove(stale)
            except OSError:
                pass

        if interactive:
            print(f"  下载: {download_url}")
        r = requests.get(download_url, stream=True, timeout=300)
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        with open(tmp_zip, "wb") as f:
            downloaded = 0
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
                downloaded += len(chunk)
                if interactive and total > 0:
                    pct = downloaded * 100 // total
                    print(f"\r  进度: {pct}% ({downloaded}/{total})", end="", flush=True)
        if interactive:
            print()

        app_dir = _app_dir()
        batch = _write_update_bat(
            tmp_zip, app_dir, "NanoGhost.exe",
            interactive=interactive, restart=restart,
        )
        if interactive:
            flags = subprocess.CREATE_NEW_CONSOLE | subprocess.DETACHED_PROCESS
        else:
            flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
        subprocess.Popen(["cmd", "/c", batch], creationflags=flags)
        return True, ""
    except Exception as e:
        return False, str(e)


def auto_check_and_notify() -> None:
    disabled = (os.getenv("NANOGHOST_DISABLE_AUTO_UPDATE") or "").strip().lower() in ("1", "true", "yes")
    if disabled:
        return
    cfg = _read_config()
    if not cfg.get("repo"):
        return
    has_update, version, _url = check_for_updates()
    if has_update:
        from agent_core.version import current_version
        local = current_version()
        print()
        print(f"  [INFO] 新版本可用: v{version} (当前: v{local})")
        print(f"  运行 'nanoghost update' 进行升级。")
        print()
