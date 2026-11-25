"""Google Colab CLI 零人工全自動調度引擎（自動授權、自動搶卡 G4→A100→L4、自動部署 ComfyUI、自動關機省算力）。"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from importlib import resources
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

LogCallback = Callable[[str, str], None]  # (message, level)

# 用於本機網頁端完成一次性 OAuth2 授權的暫存 flow 狀態
_oauth_lock = threading.Lock()
_pending_oauth_flow: Optional[Any] = None


def find_colab_cli_binary() -> Optional[str]:
    """定位當前虛擬環境或系統中的官方 `colab` CLI 執行檔（不追蹤 python symlink 以保留 .venv/bin）。"""
    venv_bin = Path(sys.executable).parent / "colab"
    if venv_bin.is_file() and os.access(venv_bin, os.X_OK):
        return str(venv_bin)
    project_venv_bin = Path(__file__).resolve().parent.parent / ".venv" / "bin" / "colab"
    if project_venv_bin.is_file() and os.access(project_venv_bin, os.X_OK):
        return str(project_venv_bin)
    found = shutil.which("colab")
    return str(Path(found)) if found else None


def _load_colab_oauth_client_config() -> Dict[str, Any]:
    user_cfg = Path.home() / ".colab-cli-oauth-config.json"
    if user_cfg.is_file():
        return json.loads(user_cfg.read_text(encoding="utf-8"))
    cfg_res = resources.files("colab_cli").joinpath("oauth_config.json")
    return json.loads(cfg_res.read_text(encoding="utf-8"))


def get_colab_auth_status() -> Dict[str, Any]:
    """
    無阻塞檢查官方 google-colab-cli 是否已安裝及是否已完成 OAuth2 授權（檢查 OS Keychain）。
    不會觸發互動式 stdin 阻塞。
    """
    cli_bin = find_colab_cli_binary()
    if not cli_bin:
        return {
            "cli_installed": False,
            "cli_path": "",
            "authorized": False,
            "message": "尚未安裝 google-colab-cli，請執行 pip install google-colab-cli",
        }

    authorized = False
    detail = "尚未授權 Google Colab 帳號（首次使用請點「一鍵授權 Google Colab」）"
    try:
        from colab_cli import auth as colab_auth
        from google.oauth2.credentials import Credentials

        token_json: Optional[str] = None
        try:
            token_json = colab_auth._get_token_from_keyring()
        except Exception:
            token_json = None

        if not token_json and os.path.exists(colab_auth.LEGACY_TOKEN_PATH):
            token_json = Path(colab_auth.LEGACY_TOKEN_PATH).read_text(encoding="utf-8")

        if token_json:
            creds = Credentials.from_authorized_user_info(
                json.loads(token_json), colab_auth.PUBLIC_SCOPES
            )
            if creds and (creds.valid or bool(creds.refresh_token)):
                authorized = True
                detail = "已完成 Google Colab OAuth 授權（支援全自動開機與關機）"
    except Exception:
        # 尚未建立鑰匙圈項目或尚未授權時，回傳友善中文指引
        detail = "尚未授權 Google Colab 帳號（首次使用請點下方第 1 步完成一次性授權）"

    return {
        "cli_installed": True,
        "cli_path": cli_bin,
        "authorized": authorized,
        "message": detail,
    }


def start_colab_oauth_flow() -> Dict[str, str]:
    """產生官方 Google OAuth2 授權網址，供使用者在瀏覽器點擊同意後複製授權碼貼回。"""
    global _pending_oauth_flow
    from colab_cli import auth as colab_auth
    from google_auth_oauthlib.flow import InstalledAppFlow

    client_config = _load_colab_oauth_client_config()
    flow = InstalledAppFlow.from_client_config(client_config, colab_auth.PUBLIC_SCOPES)
    flow.redirect_uri = colab_auth.REMOTE_REDIRECT_URI
    auth_url, _ = flow.authorization_url(prompt="consent", token_usage="remote")

    with _oauth_lock:
        _pending_oauth_flow = flow

    return {
        "auth_url": auth_url,
        "instruction": "請在新分頁登入你的 Google Colab Pro 帳號並點選允許，將頁面顯示的授權碼複製貼回下方輸入框即可（只需做這一次）。",
    }


def complete_colab_oauth_flow(auth_code: str) -> Dict[str, Any]:
    """用使用者貼回的一次性授權碼交換憑證，永久安全寫入 macOS Keychain 供 colab-cli 免重複登入。"""
    global _pending_oauth_flow
    from colab_cli import auth as colab_auth

    clean_code = (auth_code or "").strip()
    if not clean_code:
        raise ValueError("請貼上 Google 頁面顯示的授權碼（Authorization Code）。")

    with _oauth_lock:
        flow = _pending_oauth_flow
        if flow is None:
            raise ValueError("請先點擊「開啟 Google 授權頁面」，取得授權碼後再點完成。")

    flow.fetch_token(code=clean_code)
    creds = flow.credentials
    token_json = creds.to_json()
    try:
        colab_auth._save_token_to_keyring(token_json)
        colab_auth._remove_legacy_token()
    except Exception:
        # Headless Linux / Docker 環境無系統鑰匙圈時，安全寫入 ~/.config/colab-cli/token.json（0600 權限）
        legacy_path = Path(colab_auth.LEGACY_TOKEN_PATH)
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.write_text(token_json, encoding="utf-8")
        try:
            legacy_path.chmod(0o600)
        except OSError:
            pass

    with _oauth_lock:
        _pending_oauth_flow = None

    return get_colab_auth_status()


# -------------------------
# 雲端 ComfyUI + MiniMax H3 + Cloudflare Tunnel 一鍵非阻塞部署腳本
# -------------------------
REMOTE_BOOTSTRAP_SCRIPT = r'''
import os, re, shutil, subprocess, sys, threading, time, urllib.request
from pathlib import Path

_hb_stop = threading.Event()
_hb_state = {"stage": "初始化雲端環境", "t0": time.time()}

def _heartbeat_loop():
    while not _hb_stop.wait(20):
        elapsed = int(time.time() - _hb_state["t0"])
        print(f"HEARTBEAT: {_hb_state['stage']}（已耗時 {elapsed} 秒，持續運作中）", flush=True)

threading.Thread(target=_heartbeat_loop, daemon=True).start()

try:
    _hb_state["stage"] = "[1/4] 檢查雲端 GPU 與環境"
    print("=== [1/4] 檢查雲端 GPU 與環境 ===", flush=True)
    gpu_info = subprocess.run(
        ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
        capture_output=True, text=True, check=True
    ).stdout.strip()
    mem_mb = int(subprocess.run(
        ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, check=True
    ).stdout.strip().splitlines()[0])
    print(f"GPU: {gpu_info} ({mem_mb/1024:.1f} GB VRAM)", flush=True)

    COMFY_DIR = Path("/content/ComfyUI")
    if not COMFY_DIR.exists():
        _hb_state["stage"] = "[2/4] 安裝最新官方 ComfyUI 與相依套件"
        print("=== [2/4] 安裝最新官方 ComfyUI 與 comfy-kitchen ===", flush=True)
        subprocess.run(["git", "clone", "--depth", "1", "https://github.com/Comfy-Org/ComfyUI.git", str(COMFY_DIR)], check=True)
        subprocess.run(["pip", "install", "-q", "-r", str(COMFY_DIR / "requirements.txt")], check=True)
        subprocess.run(["pip", "install", "-q", "--upgrade", "comfy-kitchen", "huggingface_hub[hf_transfer]", "imageio-ffmpeg", "opencv-python-headless"], check=True)
    else:
        print("=== [2/4] ComfyUI 已安裝，略過重裝 ===", flush=True)

    vhs_dir = COMFY_DIR / "custom_nodes" / "ComfyUI-VideoHelperSuite"
    if not vhs_dir.exists():
        _hb_state["stage"] = "[2/4] 安裝 VideoHelperSuite 節點"
        subprocess.run(["git", "clone", "--depth", "1", "https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite.git", str(vhs_dir)], check=True)
        subprocess.run(["pip", "install", "-q", "-r", str(vhs_dir / "requirements.txt")], check=False)

    upscale_dir = COMFY_DIR / "models" / "upscale_models"
    upscale_dir.mkdir(parents=True, exist_ok=True)
    esrgan_path = upscale_dir / "RealESRGAN_x2plus.pth"
    if not esrgan_path.exists():
        subprocess.run([
            "curl", "-sL", "-o", str(esrgan_path),
            "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.1/RealESRGAN_x2plus.pth"
        ], check=False)

    _hb_state["stage"] = "[3/4] 下載官方開源 MiniMax H3 權重"
    print("=== [3/4] 下載官方開源 MiniMax H3 權重（自動適配 GPU 架構） ===", flush=True)
    os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "1"
    from huggingface_hub import hf_hub_download

    is_blackwell = any(k in gpu_info.upper() for k in ("PRO 6000", "5090", "B200", "GB", "BLACKWELL"))
    text_enc_primary = (
        "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"
        if is_blackwell else
        "text_encoders/qwen3vl_32b_minimax_h3_int8_convrot.safetensors"
    )
    text_enc_fallback = "text_encoders/qwen3vl_32b_minimax_h3_int8_convrot.safetensors"
    COMFY_MODELS = COMFY_DIR / "models"
    FILES = [
        ("diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors", COMFY_MODELS / "diffusion_models", None),
        (text_enc_primary, COMFY_MODELS / "text_encoders", text_enc_fallback if text_enc_primary != text_enc_fallback else None),
        ("vae/minimax_h3_video_vae_fp16.safetensors", COMFY_MODELS / "vae", None),
        ("vae/minimax_h3_audio_vae_fp32.safetensors", COMFY_MODELS / "vae", None),
    ]
    for hf_path, target_dir, fallback_path in FILES:
        target_dir.mkdir(parents=True, exist_ok=True)
        fname = Path(hf_path).name
        fpath = target_dir / fname
        if fpath.exists() and fpath.stat().st_size > 10_000_000:
            print(f"SKIP_EXISTS: {fname}", flush=True)
            continue
        _hb_state["stage"] = f"[3/4] 下載權重 {fname}"
        print(f"DOWNLOADING: {hf_path}", flush=True)
        try:
            hf_hub_download(repo_id="Comfy-Org/MiniMax-H3", filename=hf_path, local_dir=str(COMFY_MODELS))
            print(f"DOWNLOADED: {fname}", flush=True)
        except Exception as dl_err:
            if fallback_path:
                fb_name = Path(fallback_path).name
                print(f"FALLBACK_DOWNLOAD: {hf_path} 不可用，改下載 {fallback_path}", flush=True)
                _hb_state["stage"] = f"[3/4] 改下載備援權重 {fb_name}"
                hf_hub_download(repo_id="Comfy-Org/MiniMax-H3", filename=fallback_path, local_dir=str(COMFY_MODELS))
                print(f"DOWNLOADED: {fb_name}", flush=True)
            else:
                raise dl_err

    _hb_state["stage"] = "[4/4] 啟動 ComfyUI 與 Cloudflare 安全通道"
    print("=== [4/4] 背景啟動 ComfyUI 與 Cloudflare 安全通道 ===", flush=True)
    cf_bin = Path("/usr/local/bin/cloudflared")
    if not cf_bin.exists():
        subprocess.run([
            "curl", "-sL", "-o", str(cf_bin),
            "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"
        ], check=True)
        cf_bin.chmod(0o755)

    subprocess.run(["pkill", "-f", "ComfyUI/main.py"], check=False)
    subprocess.run(["pkill", "-f", "cloudflared"], check=False)
    time.sleep(1)

    comfy_cmd = ["python", "/content/ComfyUI/main.py", "--listen", "0.0.0.0", "--port", "8188", "--preview-method", "none"]
    if mem_mb >= 70000:
        comfy_cmd.append("--highvram")

    comfy_log = open("/content/comfyui.log", "w")
    subprocess.Popen(comfy_cmd, cwd="/content/ComfyUI", stdout=comfy_log, stderr=subprocess.STDOUT, start_new_session=True)

    ready = False
    for _ in range(90):
        try:
            with urllib.request.urlopen("http://127.0.0.1:8188/system_stats", timeout=2) as resp:
                if resp.status == 200:
                    ready = True
                    break
        except Exception:
            time.sleep(1)

    if not ready:
        print("COMFYUI_BOOT_FAILED:", Path("/content/comfyui.log").read_text()[-1500:], flush=True)
        sys.exit(1)

    cf_log_path = Path("/content/cloudflared.log")
    cf_log = open(cf_log_path, "w")
    subprocess.Popen(
        [str(cf_bin), "tunnel", "--url", "http://127.0.0.1:8188", "--no-autoupdate"],
        stdout=cf_log, stderr=subprocess.STDOUT, start_new_session=True
    )

    public_url = None
    tunnel_registered = False
    t0 = time.time()
    while time.time() - t0 < 45:
        if cf_log_path.exists():
            txt = cf_log_path.read_text(errors="ignore")
            if not public_url:
                m = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", txt)
                if m:
                    public_url = m.group(0)
            if public_url and ("Registered tunnel connection" in txt or (time.time() - t0 > 12)):
                tunnel_registered = True
                break
        time.sleep(0.8)

    if not public_url:
        print("TUNNEL_FAILED:", cf_log_path.read_text(errors="ignore")[-1000:], flush=True)
        sys.exit(1)

    # 在雲端先行確認 Cloudflare Tunnel 外網連線與 DNS 皆已就緒，避免本機過早查詢觸發負向 DNS 快取
    _hb_state["stage"] = "[4/4] 等待 Cloudflare 安全通道全球生效"
    edge_ready = False
    for _ in range(30):
        try:
            with urllib.request.urlopen(f"{public_url}/system_stats", timeout=4) as resp:
                if resp.status == 200:
                    edge_ready = True
                    break
        except Exception:
            time.sleep(2)
    if not edge_ready:
        time.sleep(4)
    print(f"DIRECTOR_COMFYUI_URL={public_url}", flush=True)
finally:
    _hb_stop.set()
'''


class ColabCLIManager:
    """
    封裝官方 `google-colab-cli` 的全生命週期管理：
    1. provision_gpu_with_fallback：依優先序自動搶卡（例如 G4 -> A100 -> L4），單卡超時或無卡自動降級。
    2. bootstrap_comfyui：遠端執行部署腳本並自動擷取 `DIRECTOR_COMFYUI_URL`。
    3. stop_session：保證釋放 Colab Runtime，絕不白扣計算單元。
    """

    def __init__(self, auth_mode: str = "oauth2") -> None:
        self.cli_bin = find_colab_cli_binary()
        if not self.cli_bin:
            raise RuntimeError("找不到官方 `colab` CLI，請先安裝 google-colab-cli。")
        self.auth_mode = auth_mode

    def _base_cmd(self) -> List[str]:
        return [self.cli_bin, f"--auth={self.auth_mode}"]  # type: ignore[list-item]

    def list_sessions_raw(self) -> str:
        proc = subprocess.run(
            [*self._base_cmd(), "sessions"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return (proc.stdout or "") + "\n" + (proc.stderr or "")

    def keepalive_ping(self, session_name: str) -> bool:
        """
        每隔數分鐘透過 `colab exec` 送出輕量心跳，防止 Google Colab 在背景生片期間
        誤判 Jupyter 閒置而強制回收雲端 GPU 機器。
        """
        if not session_name:
            return False
        try:
            proc = subprocess.run(
                [*self._base_cmd(), "exec", "-s", session_name, "--timeout", "30"],
                input="print('keepalive', flush=True)\n",
                capture_output=True,
                text=True,
                timeout=45,
                check=False,
            )
            return proc.returncode == 0
        except Exception:
            return False

    def stop_session(self, session_name: str, log_cb: Optional[LogCallback] = None) -> bool:
        """徹底釋放並刪除指定的 Colab Runtime（等同手動點 Disconnect and delete runtime）。"""
        if not session_name:
            return False
        try:
            proc = subprocess.run(
                [*self._base_cmd(), "stop", "-s", session_name],
                capture_output=True,
                text=True,
                timeout=45,
                check=False,
            )
            ok = proc.returncode == 0
            if log_cb:
                if ok:
                    log_cb(
                        f"🛑 已自動執行 `colab stop -s {session_name}` 徹底關閉並釋放雲端 GPU，停止扣除計算單元！",
                        "success",
                    )
                else:
                    out = ((proc.stdout or "") + " " + (proc.stderr or "")).strip()[:180]
                    log_cb(f"⚠️ 自動釋放工作階段回應：{out}", "warning")
            return ok
        except Exception as exc:
            if log_cb:
                log_cb(f"⚠️ 自動關閉 Colab 工作階段時發生例外：{exc}", "warning")
            return False

    def provision_gpu_with_fallback(
        self,
        session_name: str,
        gpu_priority: Optional[List[str]] = None,
        per_gpu_timeout_sec: int = 75,
        stop_event: Optional[threading.Event] = None,
        log_cb: Optional[LogCallback] = None,
    ) -> str:
        """
        依序自動嘗試向 Colab 申請顯卡（預設 G4 -> A100 -> L4）。
        若某級別顯卡缺貨報錯或逾時，自動清理殘留並無縫切換下一順位顯卡！
        """
        candidates = [g.upper() for g in (gpu_priority or ["G4", "A100", "L4"]) if g.strip()]
        if not candidates:
            candidates = ["G4", "A100", "L4"]

        last_err = ""
        for gpu in candidates:
            if stop_event and stop_event.is_set():
                raise InterruptedError("使用者已取消自動開機流程。")

            if log_cb:
                log_cb(f"☁️ 正在自動向 Google Colab 申請 **{gpu} GPU**（會話：{session_name}）...", "info")

            try:
                proc = subprocess.run(
                    [*self._base_cmd(), "new", "-s", session_name, "--gpu", gpu],
                    capture_output=True,
                    text=True,
                    timeout=per_gpu_timeout_sec,
                    check=False,
                )
                combined = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
                if proc.returncode == 0:
                    if log_cb:
                        log_cb(f"🟢 自動搶卡成功！已分配到 Colab **{gpu} GPU** 雲端機器。", "success")
                    return gpu

                last_err = combined[-260:] if combined else f"exit code {proc.returncode}"
                if "401" in combined or "403" in combined or "auth" in combined.lower():
                    raise RuntimeError(f"Colab 授權已過期或未授權，請先點「一鍵授權 Google Colab」：{last_err}")

                if log_cb:
                    log_cb(f"⚠️ {gpu} GPU 目前暫時無可用配額或連線失敗，自動切換下一順位顯卡...", "warning")
                self.stop_session(session_name)

            except subprocess.TimeoutExpired:
                last_err = f"{gpu} GPU 排隊超過 {per_gpu_timeout_sec} 秒未分配"
                if log_cb:
                    log_cb(f"⚠️ {gpu} GPU 排隊逾時（{per_gpu_timeout_sec}秒），自動放棄並改搶下一順位顯卡...", "warning")
                self.stop_session(session_name)

        raise RuntimeError(
            f"嘗試所有指定顯卡（{' → '.join(candidates)}）皆無法分配：{last_err}"
        )

    def bootstrap_comfyui(
        self,
        session_name: str,
        stop_event: Optional[threading.Event] = None,
        log_cb: Optional[LogCallback] = None,
    ) -> str:
        """
        透過 `colab exec -s <session> -f <script>` 在剛開好的雲端 GPU 上全自動部署 ComfyUI、
        下載 MiniMax H3 權重、啟動 Cloudflare Tunnel，並即時擷取 HTTPS 連線網址！
        """
        tmp_fd, tmp_script_str = tempfile.mkstemp(prefix="colab_bootstrap_", suffix=".py")
        tmp_script = Path(tmp_script_str)
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                f.write(REMOTE_BOOTSTRAP_SCRIPT)

            if log_cb:
                log_cb("📦 已自動推送部署腳本至 Colab GPU（安裝 ComfyUI ＋ 高速下載 MiniMax H3 權重約 3～5 分鐘）...", "info")

            proc = subprocess.Popen(
                [*self._base_cmd(), "exec", "-s", session_name, "--timeout", "900", "-f", str(tmp_script)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )

            captured_url: Optional[str] = None
            tail_lines: List[str] = []

            assert proc.stdout is not None
            for raw_line in proc.stdout:
                if stop_event and stop_event.is_set():
                    proc.terminate()
                    raise InterruptedError("使用者已手動中止雲端部署。")

                line = raw_line.strip()
                if not line:
                    continue
                tail_lines.append(line)
                if len(tail_lines) > 30:
                    tail_lines.pop(0)

                m_url = re.search(r"DIRECTOR_COMFYUI_URL=(https://[a-zA-Z0-9-]+\.trycloudflare\.com)", line)
                if m_url:
                    captured_url = m_url.group(1)

                # 把關鍵中文／下載進度與心跳轉播給前端即時 Log
                if log_cb and (
                    line.startswith("===")
                    or line.startswith("GPU:")
                    or line.startswith("DOWNLOADING:")
                    or line.startswith("DOWNLOADED:")
                    or line.startswith("FALLBACK_DOWNLOAD:")
                    or line.startswith("SKIP_EXISTS:")
                    or line.startswith("HEARTBEAT:")
                    or "DIRECTOR_COMFYUI_URL=" in line
                ):
                    clean_line = line.replace("HEARTBEAT: ", "⏳ ")
                    log_cb(f"☁️ [Colab 雲端] {clean_line}", "info")

            ret = proc.wait(timeout=30)
            if ret != 0 or not captured_url:
                err_tail = "\n".join(tail_lines[-10:])
                raise RuntimeError(f"Colab 雲端部署 ComfyUI 失敗（exit {ret}）：{err_tail}")

            if log_cb:
                log_cb(f"🌐 已自動擷取 Colab 雲端 ComfyUI 通道網址：{captured_url}，無需手動複製！", "success")
            return captured_url

        finally:
            if tmp_script.exists():
                tmp_script.unlink(missing_ok=True)
