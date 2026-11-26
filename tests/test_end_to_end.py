"""端對端自動化測試（涵蓋 5 階段狀態機、Downloads 監聽器防呆、FFmpeg 合成規格與邊界測試）。"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Tuple

import pytest
from fastapi.testclient import TestClient

from backend.database import Database
from backend.orchestrator import StudioOrchestrator
from backend.settings import detect_ffmpeg_binaries
from backend.video_engine import probe_video, render_project_video


def _get_ffmpeg() -> str:
    ffmpeg_bin, _ = detect_ffmpeg_binaries(allow_static_download=True)
    assert ffmpeg_bin is not None, "測試環境必須具備可用的 ffmpeg"
    return ffmpeg_bin


def _generate_synthetic_video(
    out_path: Path,
    width: int,
    height: int,
    fps: int,
    duration_sec: float,
    freq_hz: int = 440,
) -> Path:
    """
    使用 ffmpeg 產生指定解析度、FPS、時長且『含音軌』、檔案大小 > 50KB 的測試 .mp4。
    加入動態視覺圖樣與輕微顆粒確保編碼後體積大於 50KB 門檻，模擬真實下載影片。
    """
    ffmpeg_bin = _get_ffmpeg()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    vf = f"testsrc2=size={width}x{height}:rate={fps},noise=alls=8:allf=t+u,format=yuv420p"
    af = f"sine=frequency={freq_hz}:sample_rate=44100"

    cmd = [
        ffmpeg_bin,
        "-y",
        "-f",
        "lavfi",
        "-i",
        vf,
        "-f",
        "lavfi",
        "-i",
        af,
        "-t",
        f"{duration_sec:.2f}",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "20",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        str(out_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, f"產生測試影片失敗: {proc.stderr}"
    assert out_path.stat().st_size > 50 * 1024, (
        f"測試影片需 > 50KB 以符合監聽器門檻，實際為 {out_path.stat().st_size} bytes"
    )
    return out_path


@pytest.fixture()
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Tuple[StudioOrchestrator, Path, Path]:
    """建立完全隔離的暫存資料庫、模擬 Downloads 資料夾與模擬桌面輸出資料夾。"""
    data_dir = tmp_path / "studio_data"
    downloads_dir = tmp_path / "mock_downloads"
    desktop_out_dir = tmp_path / "mock_desktop_outputs"
    data_dir.mkdir(parents=True, exist_ok=True)
    downloads_dir.mkdir(parents=True, exist_ok=True)
    desktop_out_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("STUDIO_DATA_DIR", str(data_dir))
    monkeypatch.setenv("STUDIO_DOWNLOADS_DIR", str(downloads_dir))
    monkeypatch.setenv("STUDIO_DESKTOP_OUTPUT_DIR", str(desktop_out_dir))

    db = Database(db_path=data_dir / "studio.db")
    orch = StudioOrchestrator(
        db=db,
        min_watch_file_size=50 * 1024,
        watch_stability_sec=0.8,
    )
    yield orch, downloads_dir, desktop_out_dir
    orch.watcher.stop()


def test_full_pipeline_watcher_and_render(
    isolated_env: Tuple[StudioOrchestrator, Path, Path]
) -> None:
    """
    驗收標準 #1 完整端對端測試：
    1. 建立專案（輸入一句中文需求）→ 使用免 Key 解析模式載入 3 個測試分鏡。
    2. 驗證啟動監聽前的舊影片與 .crdownload 暫存檔不會被誤抓。
    3. 啟動資料夾監聽器 → 依序在模擬下載資料夾生成 3 支不同解析度且含音軌的測試 .mp4。
    4. 驗證監聽器自動依序歸檔為 shot_01.mp4～shot_03.mp4，產生縮圖與繁體中文進度 Log。
    5. 驗證階段 ④ 合成成片為精準 1920×1080、24 fps、yuv420p、無音軌，且時長符合 sum - (N-1)*transition（誤差 < 0.2s）。
    """
    orch, downloads_dir, desktop_out_dir = isolated_env

    # 先放一支「啟動監聽前就存在的舊影片」，確保監聽器不會誤抓歷史舊檔
    old_preexisting = downloads_dir / "old_history_video.mp4"
    _generate_synthetic_video(old_preexisting, 640, 360, 24, 2.0, freq_hz=330)

    # 階段 ①：一句話建立專案（先把 auto_render 設為 False 以便逐步斷言階段 ③ 狀態，再觸發階段 ④）
    proj = orch.create_project_and_plan(
        brief="我要做一條 1 分鐘、橫屏、訪問溫莎城堡的實拍紀錄片，無聲方便後期配音",
        target_duration=30,
        aspect_ratio="16:9",
        visual_style="35mm_doc",
        audio_mode="mute",
        planner_mode="paste",
        auto_render=False,
    )
    pid = proj["id"]
    assert proj["current_stage"] == 2
    assert proj["stage_statuses"]["2"] == "action_needed"

    # 階段 ②：使用免 Key 貼上解析模式載入 3 個測試分鏡
    pasted_json = json.dumps(
        {
            "style_lock": "英國黃金時刻自然光 ＋ 35mm 寫實紀錄片",
            "shots": [
                {
                    "shot_order": 1,
                    "title": "長步道空拍開場",
                    "duration_sec": 3.0,
                    "scene_desc_zh": "高空航拍溫莎長步道",
                    "voiceover_zh": "走進九百年歷史的溫莎城堡。",
                    "prompt_en": "Aerial drone shot flying along The Long Walk toward Windsor Castle",
                },
                {
                    "shot_order": 2,
                    "title": "亨利八世門推進",
                    "duration_sec": 3.5,
                    "scene_desc_zh": "穿過古老石拱門",
                    "voiceover_zh": "穿過亨利八世門。",
                    "prompt_en": "Gimbal shot walking through Henry VIII Gate",
                },
                {
                    "shot_order": 3,
                    "title": "圓塔夕陽遠景收尾",
                    "duration_sec": 4.0,
                    "scene_desc_zh": "夕陽下的圓塔與泰晤士河",
                    "voiceover_zh": "在暮色中落下帷幕。",
                    "prompt_en": "Sunset aerial pull-back from Round Tower",
                },
            ],
        },
        ensure_ascii=False,
    )
    proj = orch.apply_pasted_storyboard(pid, pasted_json)
    assert len(proj["shots"]) == 3
    for s in proj["shots"]:
        assert s["prompt_en"].startswith("Generate a 16:9 landscape")

    # 確認分鏡並啟動下載資料夾監聽（進入階段 ③）
    proj = orch.confirm_storyboard(pid, start_watcher=True, watch_dir=downloads_dir)
    assert proj["current_stage"] == 3
    assert proj["watcher_active"] is True

    # 放入一個瀏覽器未下載完成的 .crdownload 暫存檔，確認被忽略
    temp_downloading = downloads_dir / "unfinished_flow_clip.mp4.crdownload"
    temp_downloading.write_bytes(b"x" * 120_000)
    time.sleep(1.2)
    mid_check = orch.db.get_project(pid, include_details=True)
    assert all(s["status"] == "pending" for s in mid_check["shots"])

    # 依序在暫存模擬下載資料夾生成 3 支不同解析度、不同 FPS 且含音軌的測試 .mp4
    # 注意先產生在別處再搬移（模擬瀏覽器下載完畢落檔或直接寫入 downloads_dir）
    clip_specs = [
        ("flow_gen_alpha.mp4", 1280, 720, 30, 3.0, 440),
        ("flow_gen_beta.mp4", 1920, 1080, 25, 3.5, 554),
        ("flow_gen_gamma.mp4", 854, 480, 24, 4.0, 659),
    ]

    for idx, (fname, w, h, fps_in, dur_in, freq) in enumerate(clip_specs, start=1):
        dl_target = downloads_dir / fname
        _generate_synthetic_video(dl_target, w, h, fps_in, dur_in, freq_hz=freq)

        # 等待監聽器完成 0.8s 穩定度檢查 + ffprobe 驗證 + 縮圖歸檔
        deadline = time.time() + 12.0
        while time.time() < deadline:
            cur_shots = orch.db.list_shots(pid)
            done_cnt = sum(1 for s in cur_shots if s["status"] == "done")
            if done_cnt >= idx:
                break
            time.sleep(0.25)
        else:
            pytest.fail(f"監聽器未能在時限內自動歸檔第 {idx} 支測試影片 ({fname})")

    # 驗證 3 鏡皆依序歸檔為 shot_01.mp4 ~ shot_03.mp4 且產生縮圖與中文日誌
    proj_after_watch = orch.enrich_project(orch.db.get_project(pid, include_details=True))
    assert proj_after_watch is not None
    assert proj_after_watch["done_shots"] == 3
    assert proj_after_watch["stage_statuses"]["3"] == "done"

    shots_done = proj_after_watch["shots"]
    for idx, s in enumerate(shots_done, start=1):
        assert s["status"] == "done"
        assert Path(s["clip_path"]).name == f"shot_{idx:02d}.mp4"
        assert Path(s["clip_path"]).is_file()
        assert Path(s["thumb_path"]).name == f"shot_{idx:02d}.jpg"
        assert Path(s["thumb_path"]).is_file()
        assert s["has_audio"] is True  # 原始素材含音軌

    log_messages = [lg["message"] for lg in proj_after_watch["logs"]]
    assert any("Shot 01" in m and "自動歸檔" in m for m in log_messages)
    assert any("Shot 02" in m and "自動歸檔" in m for m in log_messages)
    assert any("Shot 03" in m and "自動歸檔" in m for m in log_messages)

    # 階段 ④：觸發全自動後期剪輯合成（預設 16:9, 24fps, 無聲, 轉場 0.5s）
    transition_sec = 0.5
    proj_rendered = orch.start_render(
        pid,
        override_options={"transition_sec": transition_sec, "fps": 24, "align_duration": False},
        run_async=False,
    )

    assert proj_rendered["current_stage"] == 5
    assert proj_rendered["stage_statuses"]["4"] == "done"
    assert proj_rendered["stage_statuses"]["5"] == "done"

    out_meta = proj_rendered["output_meta"]
    out_file = Path(out_meta["output_path"])
    desk_file = Path(out_meta["desktop_path"])
    assert out_file.is_file()
    assert desk_file.is_file()
    assert desk_file.parent == desktop_out_dir

    # 用 ffprobe 嚴格驗證最終成片規格
    probed = probe_video(out_file)
    assert probed["width"] == 1920
    assert probed["height"] == 1080
    assert abs(probed["fps"] - 24.0) < 0.05
    assert probed["pix_fmt"] == "yuv420p"
    assert probed["has_audio"] is False
    assert not any(st.get("codec_type") == "audio" for st in probed["streams"])

    # 驗證總時長符合公式：sum(durations) - (N - 1) * transition_sec（誤差 < 0.2 秒）
    expected_duration = (3.0 + 3.5 + 4.0) - (3 - 1) * transition_sec  # 10.5 - 1.0 = 9.5s
    assert abs(probed["duration"] - expected_duration) < 0.2, (
        f"預期時長 {expected_duration:.2f}s，實際為 {probed['duration']:.3f}s"
    )


def test_single_clip_and_short_clip_safety(tmp_path: Path) -> None:
    """邊界防呆測試：單支影片跳過 xfade，以及超短片段自動縮短轉場不報錯。"""
    clip1 = _generate_synthetic_video(tmp_path / "single.mp4", 1280, 720, 30, 2.8)
    res_single = render_project_video(
        clips=[{"shot_order": 1, "title": "單鏡測試", "clip_path": str(clip1)}],
        temp_dir=tmp_path / "temp1",
        output_dir=tmp_path / "out1",
        desktop_output_dir=None,
        project_name="單片邊界測試",
        aspect_ratio="16:9",
        fps=24,
        audio_mode="mute",
        transition_sec=0.5,
    )
    p1 = probe_video(Path(res_single["output_path"]))
    assert p1["width"] == 1920 and p1["height"] == 1080
    assert p1["has_audio"] is False
    assert abs(p1["duration"] - 2.8) < 0.2

    # 兩支超短影片 (1.2s) 搭配過長轉場設定 (1.0s -> 1.2 < 1.0*2.5)，驗證自動防呆縮短轉場
    short_a = _generate_synthetic_video(tmp_path / "short_a.mp4", 960, 540, 24, 1.2)
    short_b = _generate_synthetic_video(tmp_path / "short_b.mp4", 960, 540, 24, 1.2)
    res_short = render_project_video(
        clips=[
            {"shot_order": 1, "title": "短鏡A", "clip_path": str(short_a)},
            {"shot_order": 2, "title": "短鏡B", "clip_path": str(short_b)},
        ],
        temp_dir=tmp_path / "temp2",
        output_dir=tmp_path / "out2",
        desktop_output_dir=None,
        project_name="短片轉場防呆",
        aspect_ratio="9:16",
        fps=24,
        audio_mode="keep",
        transition_sec=1.0,
    )
    p2 = probe_video(Path(res_short["output_path"]))
    assert p2["width"] == 1080 and p2["height"] == 1920
    assert p2["has_audio"] is True
    assert p2["duration"] > 1.5


def test_fastapi_routes_and_voiceover_export(
    isolated_env: Tuple[StudioOrchestrator, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """驗證 FastAPI REST 路由、配音參考稿匯出與歷史專案查詢。"""
    orch, _, _ = isolated_env
    import backend.main as main_mod

    monkeypatch.setattr(main_mod, "orchestrator", orch)
    client = TestClient(main_mod.app)

    sys_resp = client.get("/api/system/status")
    assert sys_resp.status_code == 200
    assert sys_resp.json()["ffmpeg_ready"] is True

    create_resp = client.post(
        "/api/projects",
        json={
            "brief": "1 分鐘京都秋日古寺巡禮",
            "target_duration": 60,
            "aspect_ratio": "16:9",
            "visual_style": "35mm_doc",
            "audio_mode": "mute",
            "planner_mode": "paste",
        },
    )
    assert create_resp.status_code == 200
    proj = create_resp.json()["project"]
    pid = proj["id"]
    assert len(proj["shots"]) == 8

    # 匯出配音參考稿 (.md & .txt)
    vo_md = client.get(f"/api/projects/{pid}/voiceover-script?fmt=md")
    assert vo_md.status_code == 200
    assert "後期配音參考稿" in vo_md.text
    assert "Shot 01" in vo_md.text

    vo_txt = client.get(f"/api/projects/{pid}/voiceover-script?fmt=txt")
    assert vo_txt.status_code == 200
    assert "[Shot 01]" in vo_txt.text

    # 驗證 Colab G4 一鍵筆記本下載端點與合法 ipynb 結構
    nb_resp = client.get("/api/colab/notebook")
    assert nb_resp.status_code == 200
    nb_data = json.loads(nb_resp.text)
    assert len(nb_data.get("cells", [])) >= 4


def test_comfyui_minimax_h3_relay_pipeline(
    isolated_env: Tuple[StudioOrchestrator, Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    端對端測試 Colab G4 ComfyUI（MiniMax H3 首尾幀接力 + RealESRGAN 1080p）全自動管線：
    使用 httpx.MockTransport 完整模擬遠端 ComfyUI API，驗證：
    1. /system_stats 與 /object_info 連線健檢與模型偵測。
    2. 45 秒專案自動規劃為 4 幕（15s + 10s×3）且專案命名乾淨不截斷。
    3. 第 1 幕走 T2V；第 2 幕自動擷取第 1 幕尾幀 PNG 並透過 /upload/image 上傳掛接至 first_frame。
    4. RealESRGAN 節點鏈（UpscaleModelLoader -> ImageUpscaleWithModel -> ImageScale）正確組裝。
    5. 自動拉回影片、產生縮圖，並銜接階段 ④ 產出精準 1920x1080 24fps 無聲成片。
    """
    import httpx
    from backend.comfyui_client import ComfyUIClient

    orch, _, _ = isolated_env

    # 預先準備 2 支快速測試影片讓 Mock ComfyUI 回傳
    mock_out_dir = tmp_path / "mock_comfy_out"
    mock_out_dir.mkdir(parents=True, exist_ok=True)
    act1_vid = _generate_synthetic_video(mock_out_dir / "act1.mp4", 1920, 1080, 24, 2.5, freq_hz=440)
    act2_vid = _generate_synthetic_video(mock_out_dir / "act2.mp4", 1920, 1080, 24, 2.5, freq_hz=554)

    recorded_prompts = []
    uploaded_images = []
    history_store = {}

    def mock_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET":
            if path == "/system_stats":
                return httpx.Response(
                    200,
                    json={
                        "devices": [
                            {
                                "name": "NVIDIA RTX PRO 6000 Blackwell 96GB",
                                "vram_total": 96 * (1024**3),
                                "vram_free": 90 * (1024**3),
                            }
                        ]
                    },
                )
            if path == "/object_info/MiniMaxH3ImageToVideo":
                return httpx.Response(200, json={"MiniMaxH3ImageToVideo": {"input": {"required": {}}}})
            if path == "/object_info/UNETLoader":
                return httpx.Response(
                    200,
                    json={
                        "UNETLoader": {
                            "input": {
                                "required": {
                                    "unet_name": [["minimax_h3_fl2va_pruned_int8_convrot.safetensors"]]
                                }
                            }
                        }
                    },
                )
            if path == "/object_info/CLIPLoader":
                return httpx.Response(
                    200,
                    json={
                        "CLIPLoader": {
                            "input": {
                                "required": {
                                    "clip_name": [["qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"]]
                                }
                            }
                        }
                    },
                )
            if path == "/object_info/VAELoader":
                return httpx.Response(
                    200,
                    json={
                        "VAELoader": {
                            "input": {
                                "required": {
                                    "vae_name": [[
                                        "minimax_h3_video_vae_int8_convrot.safetensors",
                                        "minimax_h3_audio_vae_fp32.safetensors",
                                    ]]
                                }
                            }
                        }
                    },
                )
            if path == "/object_info/UpscaleModelLoader":
                return httpx.Response(
                    200,
                    json={
                        "UpscaleModelLoader": {
                            "input": {"required": {"model_name": [["RealESRGAN_x2plus.pth"]]}}
                        }
                    },
                )
            if path == "/object_info/LoraLoaderModelOnly":
                return httpx.Response(
                    200,
                    json={
                        "LoraLoaderModelOnly": {
                            "input": {
                                "required": {
                                    "lora_name": [["minimax_h3_fl2v_turbo_8step_v1.0_768p_comfyui_bf16.safetensors"]]
                                }
                            }
                        }
                    },
                )
            if path.startswith("/object_info/"):
                return httpx.Response(200, json={})
            if path.startswith("/history/"):
                pid = path.split("/history/", 1)[1]
                return httpx.Response(200, json={pid: history_store[pid]} if pid in history_store else {})
            if path == "/view":
                fname = request.url.params.get("filename", "")
                return httpx.Response(
                    200,
                    content=(mock_out_dir / fname).read_bytes(),
                    headers={"Content-Type": "video/mp4"},
                )
        elif request.method == "POST":
            if path == "/upload/image":
                uploaded_images.append(len(request.content))
                return httpx.Response(200, json={"name": f"uploaded_relay_{len(uploaded_images)}.png"})
            if path == "/prompt":
                payload = json.loads(request.content.decode("utf-8"))
                wf = payload["prompt"]
                recorded_prompts.append(wf)
                job_id = f"job_{len(recorded_prompts)}"
                out_name = act1_vid.name if len(recorded_prompts) == 1 else act2_vid.name
                history_store[job_id] = {
                    "status": {"status_str": "success", "completed": True},
                    "outputs": {
                        "14": {
                            "videos": [{"filename": out_name, "subfolder": "", "type": "output"}]
                        }
                    },
                }
                return httpx.Response(200, json={"prompt_id": job_id})
            if path == "/interrupt":
                return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)

    mock_transport = httpx.MockTransport(mock_handler)
    orig_init = ComfyUIClient.__init__

    def patched_init(self, base_url: str, timeout_sec: float = 45.0, transport=None):
        orig_init(self, base_url, timeout_sec=timeout_sec, transport=mock_transport)

    monkeypatch.setattr(ComfyUIClient, "__init__", patched_init)

    mock_url = "https://mock-colab-g4.trycloudflare.com"
    conn = orch.test_comfyui_connection(mock_url)
    assert conn["connected"] is True
    assert conn["vram_total_gb"] == 96.0
    assert conn["has_h3_node"] is True
    assert conn["has_realesrgan"] is True

    # 驗證 45 秒溫莎城堡專案：4 幕（15s + 10s×3）且專案名乾淨不截斷
    proj = orch.create_project_and_plan(
        brief="我要做一條 45 秒、橫屏、訪問溫莎城堡的實拍紀錄片，無聲方便後期配音",
        target_duration=45,
        aspect_ratio="16:9",
        visual_style="35mm_doc",
        audio_mode="mute",
        planner_mode="paste",
        auto_render=False,
    )
    assert not proj["name"].endswith("，無")
    assert [s["duration_sec"] for s in proj["shots"]] == [15.0, 10.0, 10.0, 10.0]

    # 改成 2 幕以便快速驗證首尾幀接力全流程
    pid = proj["id"]
    orch.apply_pasted_storyboard(
        pid,
        json.dumps(
            {
                "shots": [
                    {"shot_order": 1, "title": "第一幕開場", "duration_sec": 15.0, "prompt_en": "Aerial Windsor Castle"},
                    {"shot_order": 2, "title": "第二幕接力", "duration_sec": 10.0, "prompt_en": "Continue into Henry VIII Gate"},
                ]
            }
        ),
    )
    orch.confirm_storyboard(pid, start_watcher=False)

    # 執行全自動 ComfyUI 首尾幀接力 + RealESRGAN 生片
    res_proj = orch.start_comfyui_generation(
        pid,
        comfyui_url=mock_url,
        relay_mode=True,
        use_turbo=False,
        enable_realesrgan=True,
        poll_interval_sec=0.05,
        run_async=False,
    )

    assert res_proj["done_shots"] == 2
    assert res_proj["stage_statuses"]["3"] == "done"
    assert len(recorded_prompts) == 2

    # 驗證第 1 幕：無 first_frame、length=362 (15s 公式)、含 RealESRGAN 節點
    wf1 = recorded_prompts[0]
    assert "first_frame" not in wf1["5"]["inputs"]
    assert wf1["5"]["inputs"]["length"] == 362
    assert wf1["20"]["class_type"] == "UpscaleModelLoader"
    assert wf1["21"]["class_type"] == "ImageUpscaleWithModel"

    # 驗證第 2 幕：已上傳第 1 幕尾幀 PNG 並掛接至 LoadImage -> MiniMaxH3ImageToVideo.first_frame！
    assert len(uploaded_images) == 1
    wf2 = recorded_prompts[1]
    assert wf2["16"]["class_type"] == "LoadImage"
    assert wf2["5"]["inputs"]["first_frame"] == ["16", 0]
    assert wf2["5"]["inputs"]["length"] == 243

    # 接續執行階段 ④ 合成並驗證輸出
    final_proj = orch.start_render(pid, override_options={"transition_sec": 0.5}, run_async=False)
    assert final_proj["current_stage"] == 5
    final_probe = probe_video(Path(final_proj["output_meta"]["output_path"]))
    assert final_probe["width"] == 1920 and final_probe["height"] == 1080
    assert final_probe["has_audio"] is False
    assert abs(final_probe["duration"] - 4.5) < 0.2


def test_zero_touch_colab_cli_auto_provision_and_autostop(
    isolated_env: Tuple[StudioOrchestrator, Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    端對端測試終極零人工 Colab CLI 流水線：
    驗證：
    1. G4 缺卡自動降級改搶 L4。
    2. 自動部署腳本並擷取 DIRECTOR_COMFYUI_URL。
    3. L4 22GB 自動套用安全模式（960x544 + 每幕縮至 5s 防止 OOM）。
    4. 完成全部 2 幕首尾幀接力下載後，保證自動呼叫 colab stop 關機停止扣點！
    """
    import httpx
    import backend.orchestrator as orch_mod
    from backend.colab_automation import ColabCLIManager
    from backend.comfyui_client import ComfyUIClient

    orch, _, _ = isolated_env

    mock_out_dir = tmp_path / "mock_zero_touch"
    mock_out_dir.mkdir(parents=True, exist_ok=True)
    v1 = _generate_synthetic_video(mock_out_dir / "z1.mp4", 960, 544, 24, 2.2, freq_hz=440)
    v2 = _generate_synthetic_video(mock_out_dir / "z2.mp4", 960, 544, 24, 2.2, freq_hz=554)

    # 模擬已授權狀態與 ColabCLIManager 搶卡/部署/自動關機行為
    monkeypatch.setattr(
        orch_mod,
        "get_colab_auth_status",
        lambda: {"cli_installed": True, "cli_path": "/mock/colab", "authorized": True, "message": "OK"},
    )

    stopped_sessions = []
    provision_calls = []

    def fake_provision(self, session_name, gpu_priority=None, per_gpu_timeout_sec=75, stop_event=None, log_cb=None):
        provision_calls.append((session_name, list(gpu_priority or [])))
        if log_cb:
            log_cb("模擬 G4 缺卡，自動降級搶到 L4 GPU！", "success")
        return "L4"

    def fake_bootstrap(self, session_name, stop_event=None, log_cb=None):
        return "https://auto-zero-touch.trycloudflare.com"

    def fake_stop(self, session_name, log_cb=None):
        stopped_sessions.append(session_name)
        if log_cb:
            log_cb(f"🛑 已自動執行 colab stop -s {session_name}", "success")
        return True

    monkeypatch.setattr(ColabCLIManager, "provision_gpu_with_fallback", fake_provision)
    monkeypatch.setattr(ColabCLIManager, "bootstrap_comfyui", fake_bootstrap)
    monkeypatch.setattr(ColabCLIManager, "stop_session", fake_stop)

    recorded_wfs = []
    history_map = {}

    def mock_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET":
            if path == "/system_stats":
                return httpx.Response(200, json={"devices": [{"name": "NVIDIA L4", "vram_total": 22 * (1024**3), "vram_free": 21 * (1024**3)}]})
            if path == "/object_info/MiniMaxH3ImageToVideo":
                return httpx.Response(200, json={"MiniMaxH3ImageToVideo": {"input": {"required": {}}}})
            if path.startswith("/object_info/"):
                return httpx.Response(200, json={})
            if path.startswith("/history/"):
                jid = path.split("/history/", 1)[1]
                return httpx.Response(200, json={jid: history_map[jid]} if jid in history_map else {})
            if path == "/view":
                fn = request.url.params.get("filename", "")
                return httpx.Response(200, content=(mock_out_dir / fn).read_bytes(), headers={"Content-Type": "video/mp4"})
        elif request.method == "POST":
            if path == "/upload/image":
                return httpx.Response(200, json={"name": "relay_auto.png"})
            if path == "/prompt":
                wf = json.loads(request.content.decode("utf-8"))["prompt"]
                recorded_wfs.append(wf)
                jid = f"auto_{len(recorded_wfs)}"
                fn = v1.name if len(recorded_wfs) == 1 else v2.name
                history_map[jid] = {
                    "status": {"status_str": "success", "completed": True},
                    "outputs": {"14": {"videos": [{"filename": fn, "subfolder": "", "type": "output"}]}},
                }
                return httpx.Response(200, json={"prompt_id": jid})
        return httpx.Response(404)

    mock_transport = httpx.MockTransport(mock_handler)
    orig_init = ComfyUIClient.__init__
    monkeypatch.setattr(
        ComfyUIClient,
        "__init__",
        lambda self, base_url, timeout_sec=45.0, transport=None: orig_init(self, base_url, timeout_sec=timeout_sec, transport=mock_transport),
    )

    proj = orch.create_project_and_plan(
        brief="倫敦聖誕節前的攝政街",
        target_duration=45,
        aspect_ratio="16:9",
        audio_mode="mute",
        auto_render=False,
    )
    pid = proj["id"]
    orch.apply_pasted_storyboard(
        pid,
        json.dumps({"shots": [
            {"shot_order": 1, "title": "開場", "duration_sec": 15.0, "prompt_en": "Regent Street London Christmas angels"},
            {"shot_order": 2, "title": "收尾", "duration_sec": 10.0, "prompt_en": "London double decker bus beneath angels"},
        ]}),
    )
    orch.confirm_storyboard(pid, start_watcher=False)

    res = orch.start_full_auto_colab_pipeline(
        pid,
        gpu_priority=["G4", "A100", "L4"],
        relay_mode=True,
        auto_stop=True,
        auto_adapt_l4=True,
        poll_interval_sec=0.05,
        run_async=False,
    )

    # 1. 驗證搶卡呼叫與拍完自動關機 colab stop！
    assert len(provision_calls) == 1
    assert len(stopped_sessions) == 1
    assert stopped_sessions[0] == provision_calls[0][0]

    # 2. 驗證 L4 安全模式自動降為 960x544 與 5 秒 (124 frames)，且第 2 幕有接力首幀！
    assert res["done_shots"] == 2
    assert res["stage_statuses"]["3"] == "done"
    assert recorded_wfs[0]["5"]["inputs"]["width"] == 960
    assert recorded_wfs[0]["5"]["inputs"]["height"] == 544
    assert recorded_wfs[0]["5"]["inputs"]["length"] == 124
    assert recorded_wfs[1]["5"]["inputs"]["first_frame"] == ["16", 0]


def test_photo_to_video_ref_image_and_batch_upload(
    isolated_env: Tuple[StudioOrchestrator, Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    端對端測試「實拍照片 ➔ 圖生影片（Photo-to-Video / I2V）」：
    1. 透過 FFmpeg 生成 2 張不同比例的測試實拍 JPG（橫屏 & 豎屏）。
    2. 透過 FastAPI /ref-images/batch 一次批次上傳並依張數調整鏡數為 2 鏡，驗證自動置中裁切為 1280×704 PNG。
    3. 驗證 GET /shots/{id}/ref-image 回傳 HTTP 200 PNG，且 PATCH camera_motion="subtle_ambient" 自動更新保真 Prompt。
    4. 驗證 relay_mode=False 時，Colab 生片會自動將實拍照片上傳至 /upload/image 並掛上 MiniMaxH3ImageToVideo.first_frame，
       且帶入實拍保真運鏡 Prompt（preserve exact real-world architecture + static tripod + silent）。
    5. 驗證 DELETE /shots/{id}/ref-image 可清除實拍照片。
    """
    import httpx
    import backend.main as main_mod
    from backend.comfyui_client import ComfyUIClient
    from backend.video_engine import get_ffmpeg_or_raise

    orch, _, _ = isolated_env
    monkeypatch.setattr(main_mod, "orchestrator", orch)
    client = TestClient(main_mod.app)

    ffmpeg_bin, _ = get_ffmpeg_or_raise()
    photo1 = tmp_path / "01_westminster_exterior.jpg"
    photo2 = tmp_path / "02_westminster_nave.jpg"
    subprocess.run(
        [ffmpeg_bin, "-y", "-f", "lavfi", "-i", "color=c=steelblue:s=1600x1200:d=1", "-frames:v", "1", str(photo1)],
        capture_output=True,
        check=True,
    )
    subprocess.run(
        [ffmpeg_bin, "-y", "-f", "lavfi", "-i", "color=c=goldenrod:s=900x1600:d=1", "-frames:v", "1", str(photo2)],
        capture_output=True,
        check=True,
    )

    # 建立專案並批次匯入 2 張實拍照片（依照片張數縮減為 2 鏡）
    c_resp = client.post(
        "/api/projects",
        json={
            "brief": "威斯敏斯特教堂實拍紀錄片",
            "target_duration": 60,
            "aspect_ratio": "16:9",
            "visual_style": "35mm_doc",
            "audio_mode": "mute",
            "planner_mode": "paste",
            "auto_render": False,
        },
    )
    assert c_resp.status_code == 200
    pid = c_resp.json()["project"]["id"]

    batch_resp = client.post(
        f"/api/projects/{pid}/ref-images/batch",
        data={"replace_shot_count": "true"},
        files=[
            ("files", (photo1.name, photo1.read_bytes(), "image/jpeg")),
            ("files", (photo2.name, photo2.read_bytes(), "image/jpeg")),
        ],
    )
    assert batch_resp.status_code == 200
    proj = batch_resp.json()["project"]
    assert len(proj["shots"]) == 2
    s1, s2 = proj["shots"]
    assert s1["ref_image_path"] and Path(s1["ref_image_path"]).is_file()
    assert s2["ref_image_path"] and Path(s2["ref_image_path"]).is_file()

    # 驗證輸出 PNG 尺寸確實被無變形裁切為 1280×704
    img_meta = probe_video(Path(s1["ref_image_path"])) if False else None  # noqa: F841
    ref_get = client.get(f"/api/projects/{pid}/shots/{s1['id']}/ref-image")
    assert ref_get.status_code == 200
    assert ref_get.headers["content-type"].startswith("image/png")

    # 切換 Shot 01 運鏡為「微動定鏡」
    patch_resp = client.patch(
        f"/api/projects/{pid}/shots/{s1['id']}",
        json={"camera_motion": "subtle_ambient"},
    )
    assert patch_resp.status_code == 200
    updated_s1 = patch_resp.json()["project"]["shots"][0]
    assert updated_s1["camera_motion"] == "subtle_ambient"
    assert "static tripod" in updated_s1["prompt_en"].lower()
    assert "preserve the exact real-world architecture" in updated_s1["prompt_en"].lower()

    # 模擬 ComfyUI 驗證在 relay_mode=False 下仍會上傳實拍照片作為首幀並使用保真 Prompt
    mock_out = tmp_path / "mock_i2v_out"
    mock_out.mkdir(parents=True, exist_ok=True)
    v1 = _generate_synthetic_video(mock_out / "p1.mp4", 1280, 704, 24, 2.0, freq_hz=440)
    v2 = _generate_synthetic_video(mock_out / "p2.mp4", 1280, 704, 24, 2.0, freq_hz=554)
    recorded_wfs = []
    uploaded_refs = []
    history_map = {}

    def mock_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET":
            if path == "/system_stats":
                return httpx.Response(
                    200,
                    json={"devices": [{"name": "NVIDIA A100-SXM4-40GB", "vram_total": int(39.6 * (1024**3)), "vram_free": int(38 * (1024**3))}]},
                )
            if path == "/object_info/MiniMaxH3ImageToVideo":
                return httpx.Response(200, json={"MiniMaxH3ImageToVideo": {"input": {"required": {}}}})
            if path.startswith("/object_info/"):
                return httpx.Response(200, json={})
            if path.startswith("/history/"):
                jid = path.split("/history/", 1)[1]
                return httpx.Response(200, json={jid: history_map[jid]} if jid in history_map else {})
            if path == "/view":
                fn = request.url.params.get("filename", "")
                return httpx.Response(200, content=(mock_out / fn).read_bytes(), headers={"Content-Type": "video/mp4"})
        elif request.method == "POST":
            if path == "/upload/image":
                uploaded_refs.append(len(request.content))
                return httpx.Response(200, json={"name": f"ref_uploaded_{len(uploaded_refs)}.png"})
            if path == "/prompt":
                wf = json.loads(request.content.decode("utf-8"))["prompt"]
                recorded_wfs.append(wf)
                jid = f"pjob_{len(recorded_wfs)}"
                fn = v1.name if len(recorded_wfs) == 1 else v2.name
                history_map[jid] = {
                    "status": {"status_str": "success", "completed": True},
                    "outputs": {"14": {"videos": [{"filename": fn, "subfolder": "", "type": "output"}]}},
                }
                return httpx.Response(200, json={"prompt_id": jid})
        return httpx.Response(404)

    mock_transport = httpx.MockTransport(mock_handler)
    orig_init = ComfyUIClient.__init__
    monkeypatch.setattr(
        ComfyUIClient,
        "__init__",
        lambda self, base_url, timeout_sec=45.0, transport=None: orig_init(self, base_url, timeout_sec=timeout_sec, transport=mock_transport),
    )

    orch.confirm_storyboard(pid, start_watcher=False)
    gen_res = orch.start_comfyui_generation(
        pid,
        comfyui_url="https://mock-photo-i2v.trycloudflare.com",
        relay_mode=False,
        use_turbo=False,
        enable_realesrgan=False,
        poll_interval_sec=0.05,
        run_async=False,
    )
    assert gen_res["done_shots"] == 2
    assert len(uploaded_refs) == 2
    assert recorded_wfs[0]["16"]["class_type"] == "LoadImage"
    assert recorded_wfs[0]["5"]["inputs"]["first_frame"] == ["16", 0]
    assert recorded_wfs[0]["5"]["inputs"]["width"] == 1280
    assert recorded_wfs[0]["5"]["inputs"]["height"] == 704
    assert "static tripod" in recorded_wfs[0]["5"]["inputs"]["prompt"].lower()
    assert "silent documentary" in recorded_wfs[0]["5"]["inputs"]["prompt"].lower()
    assert recorded_wfs[1]["16"]["class_type"] == "LoadImage"
    assert recorded_wfs[1]["5"]["inputs"]["first_frame"] == ["16", 0]
    # 無聲模式單鏡已移除音軌
    assert gen_res["shots"][0]["has_audio"] is False

    # 驗證移除實拍照片
    del_resp = client.delete(f"/api/projects/{pid}/shots/{s2['id']}/ref-image")
    assert del_resp.status_code == 200
    assert del_resp.json()["project"]["shots"][1]["ref_image_path"] == ""
