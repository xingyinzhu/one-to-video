"""Colab G4 ComfyUI 全自動生片引擎（支援 MiniMax H3 首尾幀接力與 RealESRGAN 1080p 超分）。"""

from __future__ import annotations

import random
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx

ProgressCb = Callable[[str], None]

# 官方 Comfy-Org/MiniMax-H3 預設權重檔名
DEFAULT_UNET = "minimax_h3_fl2va_pruned_int8_convrot.safetensors"
DEFAULT_CLIP = "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"
DEFAULT_VIDEO_VAE = "minimax_h3_video_vae_fp16.safetensors"
DEFAULT_AUDIO_VAE = "minimax_h3_audio_vae_fp32.safetensors"
DEFAULT_TURBO_LORA = "minimax_h3_fl2v_turbo_8step_v1.0_768p_comfyui_bf16.safetensors"
DEFAULT_UPSCALE_MODEL = "RealESRGAN_x2plus.pth"


def normalize_comfyui_url(url: str) -> str:
    clean = (url or "").strip().rstrip("/")
    if clean and not clean.startswith(("http://", "https://")):
        clean = "https://" + clean
    return clean


def compute_minimax_h3_frames(duration_sec: float, fps: int = 24) -> int:
    """
    官方 MiniMax H3 影格數公式（17n + 5）：
    max(5, round(a * 24)) + (5 - (max(5, round(a * 24)) % 17)) % 17
    例如：5s -> 124, 8s -> 192, 10s -> 243, 15s -> 362
    """
    clamped_sec = max(2.0, min(15.0, float(duration_sec)))
    base = max(5, round(clamped_sec * fps))
    return base + (5 - (base % 17)) % 17


def compute_minimax_h3_resolution(aspect_ratio: str, vram_gb: float = 96.0) -> Tuple[int, int]:
    """
    MiniMax H3 原生 32 倍數黃金解析度：
    - G4 96GB / A100 80GB（vram >= 40GB）：1280×704（對齊帖子實測）
    - L4 24GB（vram < 40GB）：960×544（精準 16:9 且為 32 倍數，省顯存防 OOM 並提速 ~2 倍）
    """
    if vram_gb and vram_gb < 40.0:
        return (544, 960) if aspect_ratio == "9:16" else (960, 544)
    if aspect_ratio == "9:16":
        return 704, 1280
    return 1280, 704


def _pick_installed(available: List[str], preferred_substrs: List[str], fallback: str) -> str:
    if not available:
        return fallback
    for sub in preferred_substrs:
        for item in available:
            if sub.lower() in item.lower():
                return item
    return available[0]


class ComfyUIClient:
    """與 Colab 遠端 ComfyUI 伺服器溝通的無依賴 HTTP 客戶端。"""

    def __init__(
        self,
        base_url: str,
        timeout_sec: float = 45.0,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self.base_url = normalize_comfyui_url(base_url)
        if not self.base_url:
            raise ValueError("尚未設定 Colab ComfyUI 連線網址（例如 https://xxxx.trycloudflare.com）。")
        self.timeout_sec = timeout_sec
        self.transport = transport
        self.client_id = f"ai_director_{uuid.uuid4().hex[:8]}"

    def _http_client(self, timeout: Optional[float] = None) -> httpx.Client:
        kwargs: Dict[str, Any] = {
            "timeout": timeout or self.timeout_sec,
            "follow_redirects": True,
        }
        if self.transport is not None:
            kwargs["transport"] = self.transport
        return httpx.Client(**kwargs)

    def _extract_combo_list(self, obj_info: Dict[str, Any], node_name: str, field: str) -> List[str]:
        try:
            node = obj_info.get(node_name) or {}
            req = (node.get("input") or {}).get("required") or {}
            spec = req.get(field)
            if isinstance(spec, list) and spec and isinstance(spec[0], list):
                return [str(x) for x in spec[0]]
        except Exception:
            pass
        return []

    def check_connection(self) -> Dict[str, Any]:
        """檢查 Colab ComfyUI 存活狀態、GPU 顯存、MiniMax H3 節點與已安裝權重檔。"""
        with self._http_client(timeout=15.0) as client:
            r_stats = client.get(f"{self.base_url}/system_stats")
            r_stats.raise_for_status()
            stats = r_stats.json()

            devices = stats.get("devices") or []
            gpu_name = "Unknown GPU"
            vram_total_gb = 0.0
            vram_free_gb = 0.0
            if devices:
                d0 = devices[0]
                gpu_name = str(d0.get("name") or d0.get("type") or "GPU")
                vram_total_gb = round(float(d0.get("vram_total") or 0) / (1024**3), 1)
                vram_free_gb = round(float(d0.get("vram_free") or 0) / (1024**3), 1)

            # 檢查核心節點與模型清單
            detected_models = {
                "unet": DEFAULT_UNET,
                "clip": DEFAULT_CLIP,
                "video_vae": DEFAULT_VIDEO_VAE,
                "audio_vae": DEFAULT_AUDIO_VAE,
                "turbo_lora": DEFAULT_TURBO_LORA,
                "upscale_model": DEFAULT_UPSCALE_MODEL,
            }
            has_h3_node = False
            has_realesrgan = False
            has_vhs = False
            has_turbo_lora = False

            def fetch_node_info(node_type: str) -> Dict[str, Any]:
                try:
                    rr = client.get(f"{self.base_url}/object_info/{node_type}")
                    if rr.status_code == 200:
                        return rr.json()
                except Exception:
                    pass
                return {}

            h3_info = fetch_node_info("MiniMaxH3ImageToVideo")
            has_h3_node = "MiniMaxH3ImageToVideo" in h3_info

            unet_list = self._extract_combo_list(fetch_node_info("UNETLoader"), "UNETLoader", "unet_name")
            clip_list = self._extract_combo_list(fetch_node_info("CLIPLoader"), "CLIPLoader", "clip_name")
            vae_list = self._extract_combo_list(fetch_node_info("VAELoader"), "VAELoader", "vae_name")
            lora_list = self._extract_combo_list(
                fetch_node_info("LoraLoaderModelOnly"), "LoraLoaderModelOnly", "lora_name"
            )
            up_list = self._extract_combo_list(
                fetch_node_info("UpscaleModelLoader"), "UpscaleModelLoader", "model_name"
            )
            has_vhs = "VHS_LoadVideo" in fetch_node_info("VHS_LoadVideo")

            if unet_list:
                detected_models["unet"] = _pick_installed(
                    unet_list,
                    ["minimax_h3_fl2va_pruned_int8", "minimax_h3_fl2va_int8", "minimax_h3_fl2va", "minimax_h3"],
                    DEFAULT_UNET,
                )
            if clip_list:
                detected_models["clip"] = _pick_installed(
                    clip_list,
                    ["qwen3vl_32b_minimax_h3_nvfp4", "qwen3vl_32b_minimax_h3_int8", "minimax_h3", "qwen3vl"],
                    DEFAULT_CLIP,
                )
            if vae_list:
                detected_models["video_vae"] = _pick_installed(
                    vae_list,
                    ["minimax_h3_video_vae_fp16", "minimax_h3_video_vae_int8", "minimax_h3_video_vae"],
                    DEFAULT_VIDEO_VAE,
                )
                detected_models["audio_vae"] = _pick_installed(
                    vae_list,
                    ["minimax_h3_audio_vae"],
                    DEFAULT_AUDIO_VAE,
                )
            if lora_list:
                for item in lora_list:
                    if "minimax_h3" in item.lower() and "turbo" in item.lower():
                        detected_models["turbo_lora"] = item
                        has_turbo_lora = True
                        break
            if up_list:
                for item in up_list:
                    if "realesrgan" in item.lower() or "esrgan" in item.lower():
                        detected_models["upscale_model"] = item
                        has_realesrgan = True
                        break

            return {
                "connected": True,
                "url": self.base_url,
                "gpu_name": gpu_name,
                "vram_total_gb": vram_total_gb,
                "vram_free_gb": vram_free_gb,
                "has_h3_node": has_h3_node,
                "has_realesrgan": has_realesrgan,
                "has_vhs": has_vhs,
                "has_turbo_lora": has_turbo_lora,
                "models": detected_models,
            }

    def wait_until_ready(
        self,
        max_wait_sec: float = 90.0,
        retry_interval_sec: float = 3.0,
        stop_event: Optional[threading.Event] = None,
        progress_cb: Optional[ProgressCb] = None,
    ) -> Dict[str, Any]:
        """
        等待新建立的 Cloudflare Quick Tunnel 完成全球 DNS 生效並連線至 ComfyUI。
        容忍初期常見的 DNS 尚未生效（Errno -5 / nodename nor servname）、TLS 握手或 502/503/504。
        """
        deadline = time.time() + max_wait_sec
        attempt = 0
        last_exc: Optional[Exception] = None
        while True:
            if stop_event and stop_event.is_set():
                raise InterruptedError("使用者已中止連線等待。")
            attempt += 1
            try:
                return self.check_connection()
            except Exception as exc:
                last_exc = exc
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise RuntimeError(
                        f"等待 Colab ComfyUI 通道生效逾時（已重試 {attempt} 次 / {int(max_wait_sec)} 秒）：{last_exc}"
                    ) from last_exc
                if progress_cb and (attempt == 1 or attempt % 3 == 0):
                    progress_cb(
                        f"等待 Cloudflare 安全通道 DNS 生效中（第 {attempt} 次嘗試，尚餘約 {int(remaining)} 秒）..."
                    )
                sleep_sec = min(retry_interval_sec, max(0.2, remaining))
                if stop_event:
                    stop_event.wait(sleep_sec)
                else:
                    time.sleep(sleep_sec)

    def upload_image(self, image_path: Path, remote_name: Optional[str] = None) -> str:
        """上傳首幀參考圖（前一幕尾幀）至 ComfyUI input 資料夾（內建瞬斷重試）。"""
        p = Path(image_path).resolve()
        filename = remote_name or p.name
        last_exc: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                with self._http_client() as client:
                    with open(p, "rb") as f:
                        files = {"image": (filename, f, "image/png")}
                        data = {"overwrite": "true", "type": "input"}
                        resp = client.post(f"{self.base_url}/upload/image", files=files, data=data)
                        resp.raise_for_status()
                        res = resp.json()
                        return str(res.get("name") or filename)
            except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                last_exc = exc
                if attempt < 3:
                    time.sleep(2.0 * attempt)
        raise RuntimeError(f"上傳首尾幀接力圖片失敗：{last_exc}") from last_exc

    def interrupt(self) -> None:
        try:
            with self._http_client(timeout=10.0) as client:
                client.post(f"{self.base_url}/interrupt")
        except Exception:
            pass

    def build_minimax_h3_workflow(
        self,
        prompt_en: str,
        duration_sec: float = 10.0,
        aspect_ratio: str = "16:9",
        first_frame_image: Optional[str] = None,
        seed: Optional[int] = None,
        use_turbo_lora: bool = False,
        enable_realesrgan_inline: bool = False,
        models: Optional[Dict[str, str]] = None,
        filename_prefix: str = "video/director_h3",
        vram_gb: float = 96.0,
    ) -> Dict[str, Any]:
        """
        依據官方 Comfy-Org/workflow_templates 建構 100% 原生 ComfyUI API Workflow：
        - 第 1 幕（first_frame_image=None）：文字生片（帶原生立體聲）
        - 第 2..N 幕（first_frame_image="xxx.png"）：自動掛接 LoadImage -> MiniMaxH3ImageToVideo.first_frame 首尾幀接力！
        - 可選 inline RealESRGAN 超分直接輸出 1920x1080（或 1080x1920）
        """
        m = {
            "unet": DEFAULT_UNET,
            "clip": DEFAULT_CLIP,
            "video_vae": DEFAULT_VIDEO_VAE,
            "audio_vae": DEFAULT_AUDIO_VAE,
            "turbo_lora": DEFAULT_TURBO_LORA,
            "upscale_model": DEFAULT_UPSCALE_MODEL,
            **(models or {}),
        }

        width, height = compute_minimax_h3_resolution(aspect_ratio, vram_gb=vram_gb)
        target_w, target_h = (1920, 1080) if aspect_ratio == "16:9" else (1080, 1920)
        length_frames = compute_minimax_h3_frames(duration_sec, fps=24)
        use_seed = int(seed) if seed is not None else random.randint(1, 2**50)
        steps = 8 if use_turbo_lora else 20

        workflow: Dict[str, Any] = {
            "1": {
                "class_type": "UNETLoader",
                "inputs": {
                    "unet_name": m["unet"],
                    "weight_dtype": "default",
                },
            },
            "2": {
                "class_type": "CLIPLoader",
                "inputs": {
                    "clip_name": m["clip"],
                    "type": "minimax",
                    "device": "default",
                },
            },
            "3": {
                "class_type": "VAELoader",
                "inputs": {"vae_name": m["video_vae"]},
            },
            "4": {
                "class_type": "VAELoader",
                "inputs": {"vae_name": m["audio_vae"]},
            },
            "5": {
                "class_type": "MiniMaxH3ImageToVideo",
                "inputs": {
                    "prompt": prompt_en,
                    "width": width,
                    "height": height,
                    "length": length_frames,
                    "clip": ["2", 0],
                    "vae": ["3", 0],
                },
            },
            "6": {
                "class_type": "RandomNoise",
                "inputs": {"noise_seed": use_seed},
            },
            "7": {
                "class_type": "KSamplerSelect",
                "inputs": {"sampler_name": "res_multistep"},
            },
        }

        model_ref = ["1", 0]
        if use_turbo_lora:
            workflow["15"] = {
                "class_type": "LoraLoaderModelOnly",
                "inputs": {
                    "lora_name": m["turbo_lora"],
                    "strength_model": 1.0,
                    "model": ["1", 0],
                },
            }
            model_ref = ["15", 0]

        if first_frame_image:
            workflow["16"] = {
                "class_type": "LoadImage",
                "inputs": {"image": first_frame_image},
            }
            workflow["5"]["inputs"]["first_frame"] = ["16", 0]

        workflow["8"] = {
            "class_type": "BasicScheduler",
            "inputs": {
                "scheduler": "simple",
                "steps": steps,
                "denoise": 1.0,
                "model": model_ref,
            },
        }
        workflow["9"] = {
            "class_type": "BasicGuider",
            "inputs": {
                "model": model_ref,
                "conditioning": ["5", 0],
            },
        }
        workflow["10"] = {
            "class_type": "SamplerCustomAdvanced",
            "inputs": {
                "noise": ["6", 0],
                "guider": ["9", 0],
                "sampler": ["7", 0],
                "sigmas": ["8", 0],
                "latent_image": ["5", 1],
            },
        }
        workflow["11"] = {
            "class_type": "VAEDecode",
            "inputs": {
                "samples": ["10", 0],
                "vae": ["3", 0],
            },
        }
        workflow["12"] = {
            "class_type": "VAEDecodeAudio",
            "inputs": {
                "samples": ["10", 0],
                "vae": ["4", 0],
            },
        }

        images_out_ref = ["11", 0]
        if enable_realesrgan_inline:
            workflow["20"] = {
                "class_type": "UpscaleModelLoader",
                "inputs": {"model_name": m["upscale_model"]},
            }
            workflow["21"] = {
                "class_type": "ImageUpscaleWithModel",
                "inputs": {
                    "upscale_model": ["20", 0],
                    "image": ["11", 0],
                },
            }
            workflow["22"] = {
                "class_type": "ImageScale",
                "inputs": {
                    "image": ["21", 0],
                    "upscale_method": "lanczos",
                    "width": target_w,
                    "height": target_h,
                    "crop": "center",
                },
            }
            images_out_ref = ["22", 0]

        workflow["13"] = {
            "class_type": "CreateVideo",
            "inputs": {
                "fps": 24,
                "bit_depth": 8,
                "color_space": "sRGB",
                "codec": "none",
                "images": images_out_ref,
                "audio": ["12", 0],
            },
        }
        workflow["14"] = {
            "class_type": "SaveVideo",
            "inputs": {
                "filename_prefix": filename_prefix,
                "format": "auto",
                "format.codec": "auto",
                "codec": "auto",
                "video": ["13", 0],
            },
        }
        return workflow

    def queue_and_download_video(
        self,
        workflow: Dict[str, Any],
        dest_mp4_path: Path,
        stop_event: Optional[threading.Event] = None,
        poll_interval_sec: float = 3.0,
        max_wait_sec: float = 1800.0,
        progress_cb: Optional[ProgressCb] = None,
    ) -> Path:
        """
        送出 ComfyUI /prompt 任務、輪詢 /history/{prompt_id} 直到完成，
        並透過 /view 下載產出的 MP4 影片至本機 Mac。
        """
        dest = Path(dest_mp4_path).resolve()
        dest.parent.mkdir(parents=True, exist_ok=True)

        with self._http_client() as client:
            r_submit: Optional[httpx.Response] = None
            last_submit_exc: Optional[Exception] = None
            for attempt in range(1, 4):
                try:
                    r_submit = client.post(
                        f"{self.base_url}/prompt",
                        json={"prompt": workflow, "client_id": self.client_id},
                    )
                    if r_submit.status_code in (502, 503, 504) and attempt < 3:
                        time.sleep(2.0 * attempt)
                        continue
                    break
                except httpx.RequestError as exc:
                    last_submit_exc = exc
                    if attempt < 3:
                        time.sleep(2.0 * attempt)
            if r_submit is None:
                raise RuntimeError(f"送出 ComfyUI 任務連線失敗：{last_submit_exc}") from last_submit_exc
            if r_submit.status_code != 200:
                raise RuntimeError(
                    f"ComfyUI 拒絕工作流請求 (HTTP {r_submit.status_code})：{r_submit.text[:300]}"
                )
            submit_data = r_submit.json()
            prompt_id = submit_data.get("prompt_id")
            if not prompt_id:
                raise RuntimeError(f"ComfyUI 未回傳 prompt_id：{submit_data}")

            start_t = time.time()
            last_report = 0.0
            consecutive_net_errs = 0
            output_file_info: Optional[Dict[str, str]] = None

            while True:
                if stop_event and stop_event.is_set():
                    self.interrupt()
                    raise InterruptedError("使用者已手動停止雲端生片任務。")

                elapsed = time.time() - start_t
                if elapsed > max_wait_sec:
                    raise TimeoutError(f"等待 Colab ComfyUI 生成逾時（超過 {int(max_wait_sec)} 秒）。")

                try:
                    r_hist = client.get(f"{self.base_url}/history/{prompt_id}")
                    if r_hist.status_code in (502, 503, 504):
                        raise httpx.ConnectError(f"Cloudflare 暫時回應 HTTP {r_hist.status_code}")
                    consecutive_net_errs = 0
                except httpx.RequestError as net_exc:
                    consecutive_net_errs += 1
                    if consecutive_net_errs >= 10:
                        raise RuntimeError(
                            f"輪詢 Colab 進度連續失敗 10 次：{net_exc}"
                        ) from net_exc
                    if progress_cb and consecutive_net_errs in (2, 5):
                        progress_cb(f"雲端通道短暫波動，自動重連中（第 {consecutive_net_errs} 次）...")
                    time.sleep(poll_interval_sec)
                    continue

                if r_hist.status_code == 200:
                    hist_json = r_hist.json()
                    if prompt_id in hist_json:
                        job = hist_json[prompt_id]
                        status_obj = job.get("status") or {}
                        status_str = str(status_obj.get("status_str") or "")
                        if status_str == "error":
                            msgs = status_obj.get("messages") or []
                            raise RuntimeError(f"ComfyUI 雲端執行失敗：{str(msgs)[:300]}")

                        outputs = job.get("outputs") or {}
                        output_file_info = self._find_video_output(outputs)
                        if output_file_info is not None:
                            break
                        if status_obj.get("completed"):
                            raise RuntimeError("ComfyUI 任務已完成但未找到輸出的影片檔案。")

                if progress_cb and (elapsed - last_report >= 15.0):
                    last_report = elapsed
                    mins, secs = divmod(int(elapsed), 60)
                    progress_cb(f"Colab 雲端生成中... 已耗時 {mins} 分 {secs:02d} 秒")

                time.sleep(poll_interval_sec)

            # 下載產出影片（內建 3 次瞬斷重試）
            params = {
                "filename": output_file_info["filename"],
                "subfolder": output_file_info.get("subfolder", ""),
                "type": output_file_info.get("type", "output"),
            }
            if progress_cb:
                progress_cb(f"雲端生成完畢，正在下載影片「{params['filename']}」回本機...")

            last_dl_exc: Optional[Exception] = None
            for dl_attempt in range(1, 4):
                try:
                    with client.stream("GET", f"{self.base_url}/view", params=params, timeout=180.0) as dl:
                        dl.raise_for_status()
                        with open(dest, "wb") as f_out:
                            for chunk in dl.iter_bytes(chunk_size=1024 * 256):
                                f_out.write(chunk)
                    last_dl_exc = None
                    break
                except (httpx.RequestError, httpx.HTTPStatusError) as dl_exc:
                    last_dl_exc = dl_exc
                    if dl_attempt < 3:
                        time.sleep(2.5 * dl_attempt)
            if last_dl_exc is not None:
                raise RuntimeError(f"下載雲端生成影片失敗：{last_dl_exc}") from last_dl_exc

        if not dest.is_file() or dest.stat().st_size < 1024:
            raise RuntimeError("下載回來的影片檔案異常過小或空白。")
        return dest

    @staticmethod
    def _find_video_output(outputs: Dict[str, Any]) -> Optional[Dict[str, str]]:
        """相容 SaveVideo (images/videos) 與 VHS_VideoCombine (gifs/videos) 輸出結構。"""
        for _node_id, node_out in outputs.items():
            if not isinstance(node_out, dict):
                continue
            for key in ("videos", "gifs", "images"):
                items = node_out.get(key)
                if isinstance(items, list):
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        fname = str(item.get("filename") or "")
                        if fname.lower().endswith((".mp4", ".webm", ".mov", ".mkv")):
                            return {
                                "filename": fname,
                                "subfolder": str(item.get("subfolder") or ""),
                                "type": str(item.get("type") or "output"),
                            }
        return None
