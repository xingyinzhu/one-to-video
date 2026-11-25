"""系統路徑設定與 macOS FFmpeg/FFprobe 自動偵測模組。"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional, Tuple

BASE_DIR: Path = Path(__file__).resolve().parent.parent
FRONTEND_DIR: Path = BASE_DIR / "frontend"


def get_data_dir() -> Path:
    return Path(os.environ.get("STUDIO_DATA_DIR", str(BASE_DIR / "data"))).expanduser().resolve()


def get_db_path() -> Path:
    return get_data_dir() / "studio.db"


def get_projects_dir() -> Path:
    return get_data_dir() / "projects"


def get_default_downloads_dir() -> Path:
    env_val = os.environ.get("STUDIO_DOWNLOADS_DIR")
    if env_val:
        return Path(env_val).expanduser().resolve()
    return (Path.home() / "Downloads").resolve()


def get_default_desktop_output_dir() -> Path:
    env_val = os.environ.get("STUDIO_DESKTOP_OUTPUT_DIR")
    if env_val:
        return Path(env_val).expanduser().resolve()
    return (Path.home() / "Desktop" / "AutoVideoOutputs").resolve()


SERVER_HOST: str = os.environ.get("STUDIO_HOST", "127.0.0.1")
SERVER_PORT: int = int(os.environ.get("STUDIO_PORT", "8787"))


def ensure_base_dirs() -> None:
    """確保資料庫與專案儲存目錄存在。"""
    get_data_dir().mkdir(parents=True, exist_ok=True)
    get_projects_dir().mkdir(parents=True, exist_ok=True)


def _find_exec(binary_name: str, env_key: str) -> Optional[str]:
    """依序檢查環境變數、macOS Homebrew 常見路徑與系統 PATH。"""
    env_override = os.environ.get(env_key)
    if env_override and Path(env_override).is_file() and os.access(env_override, os.X_OK):
        return str(Path(env_override).resolve())

    candidate_dirs = [
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
        Path("/opt/local/bin"),
    ]
    for d in candidate_dirs:
        candidate = d / binary_name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())

    which_found = shutil.which(binary_name)
    if which_found:
        return str(Path(which_found).resolve())

    return None


def detect_ffmpeg_binaries(allow_static_download: bool = False) -> Tuple[Optional[str], Optional[str]]:
    """
    偵測本機可用的 ffmpeg 與 ffprobe 執行檔路徑。
    優先順序：
    1. FFMPEG_BINARY / FFPROBE_BINARY 環境變數
    2. /opt/homebrew/bin (Apple Silicon Homebrew)
    3. /usr/local/bin (Intel Homebrew)
    4. 系統 PATH
    5. static-ffmpeg 內建執行檔備援（避免從 Finder 雙擊時環境缺少 brew）
    """
    ffmpeg_path = _find_exec("ffmpeg", "FFMPEG_BINARY")
    ffprobe_path = _find_exec("ffprobe", "FFPROBE_BINARY")

    if ffmpeg_path and ffprobe_path:
        return ffmpeg_path, ffprobe_path

    # 檢查 static_ffmpeg 快取或視需要下載
    try:
        import static_ffmpeg
        import static_ffmpeg.run as sf_run

        platform_dir = Path(sf_run.get_platform_dir())
        cached_ffmpeg = platform_dir / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
        cached_ffprobe = platform_dir / ("ffprobe.exe" if os.name == "nt" else "ffprobe")

        if cached_ffmpeg.is_file() and cached_ffprobe.is_file():
            return str(cached_ffmpeg.resolve()), str(cached_ffprobe.resolve())

        if allow_static_download:
            fp_ffmpeg, fp_ffprobe = sf_run.get_or_fetch_platform_executables_else_raise()
            if fp_ffmpeg and fp_ffprobe:
                return str(Path(fp_ffmpeg).resolve()), str(Path(fp_ffprobe).resolve())
    except Exception:
        pass

    return ffmpeg_path, ffprobe_path
