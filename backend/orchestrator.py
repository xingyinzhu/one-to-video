"""5 階段狀態機引擎與 SSE 即時事件廣播中心（Orchestrator）。"""

from __future__ import annotations

import asyncio
import shutil
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .colab_automation import (
    ColabCLIManager,
    complete_colab_oauth_flow,
    get_colab_auth_status,
    start_colab_oauth_flow,
)
from .comfyui_client import ComfyUIClient, normalize_comfyui_url
from .database import Database
from .download_watcher import DownloadFolderWatcher, is_candidate_video_path
from .llm_planner import (
    build_master_prompt_for_web,
    generate_smart_draft,
    generate_via_gemini,
    generate_via_ollama,
    parse_pasted_storyboard,
)
from .settings import (
    detect_ffmpeg_binaries,
    get_default_desktop_output_dir,
    get_default_downloads_dir,
    is_running_in_docker,
    resolve_project_media_path,
)
from .video_engine import (
    extract_last_frame,
    extract_thumbnail,
    probe_video,
    render_project_video,
)


class ProjectEventBus:
    """管理每個專案的 SSE 訂閱佇列，支援從背景執行緒零延遲推播到前端。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: Dict[str, Set[Tuple[asyncio.AbstractEventLoop, asyncio.Queue]]] = {}

    def subscribe(
        self, project_id: str, loop: asyncio.AbstractEventLoop
    ) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        with self._lock:
            self._subscribers.setdefault(project_id, set()).add((loop, q))
        return q

    def unsubscribe(
        self, project_id: str, loop: asyncio.AbstractEventLoop, q: asyncio.Queue
    ) -> None:
        with self._lock:
            subs = self._subscribers.get(project_id)
            if subs:
                subs.discard((loop, q))
                if not subs:
                    self._subscribers.pop(project_id, None)

    def publish(self, project_id: str, payload: Dict[str, Any]) -> None:
        with self._lock:
            subs = list(self._subscribers.get(project_id, set()))
        for loop, q in subs:
            try:
                loop.call_soon_threadsafe(self._safe_put, q, payload)
            except RuntimeError:
                pass

    @staticmethod
    def _safe_put(q: asyncio.Queue, payload: Dict[str, Any]) -> None:
        if q.full():
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            pass


class StudioOrchestrator:
    """統籌 5 階段狀態轉換、AI 分鏡規劃、Downloads 監聽歸檔與 FFmpeg 剪輯合成。"""

    def __init__(
        self,
        db: Optional[Database] = None,
        min_watch_file_size: int = 50 * 1024,
        watch_stability_sec: float = 0.8,
    ) -> None:
        self.db = db or Database()
        self.bus = ProjectEventBus()
        self._render_locks: Set[str] = set()
        self._gen_locks: Set[str] = set()
        self._gen_stops: Dict[str, threading.Event] = {}
        self._gen_status: Dict[str, Dict[str, Any]] = {}
        self._active_colab_sessions: Dict[str, str] = {}
        self._lock = threading.RLock()
        self._pinned_shot_id: Optional[int] = None

        self.watcher = DownloadFolderWatcher(
            on_video_ready=self._handle_watched_video,
            min_size_bytes=min_watch_file_size,
            stability_interval_sec=watch_stability_sec,
        )

        # 伺服器重啟時清理先前異常中斷留下的 watcher_enabled 標記
        for p in self.db.list_projects():
            if p.get("watcher_enabled"):
                self.db.update_project(p["id"], watcher_enabled=False)
            # 重啟後無任何背景生片執行緒，將殘留的「階段 ③ 執行中」重設為可續跑
            full = self.db.get_project(p["id"], include_details=False)
            if full and (full.get("stage_statuses") or {}).get("3") == "running":
                sts = dict(full["stage_statuses"])
                sts["3"] = "action_needed"
                self.db.update_project(p["id"], stage_statuses=sts)

    # -------------------------
    # 路徑與系統狀態輔助
    # -------------------------
    def get_effective_downloads_dir(self) -> Path:
        cfg = self.db.get_settings()
        custom = (cfg.get("downloads_dir") or "").strip()
        if custom:
            return Path(custom).expanduser().resolve()
        return get_default_downloads_dir()

    def get_effective_desktop_output_dir(self) -> Path:
        cfg = self.db.get_settings()
        custom = (cfg.get("desktop_output_dir") or "").strip()
        if custom:
            return Path(custom).expanduser().resolve()
        return get_default_desktop_output_dir()

    def get_system_status(self) -> Dict[str, Any]:
        ffmpeg_bin, ffprobe_bin = detect_ffmpeg_binaries(allow_static_download=False)
        cfg = self.db.get_settings()
        dl_dir = self.get_effective_downloads_dir()
        out_dir = self.get_effective_desktop_output_dir()
        return {
            "ffmpeg_ready": bool(ffmpeg_bin and ffprobe_bin),
            "ffmpeg_path": ffmpeg_bin or "",
            "ffprobe_path": ffprobe_bin or "",
            "install_hint": "brew install ffmpeg",
            "is_docker": is_running_in_docker(),
            "can_reveal_in_finder": (sys.platform == "darwin" and not is_running_in_docker()),
            "downloads_dir": str(dl_dir),
            "desktop_output_dir": str(out_dir),
            "has_gemini_key": bool((cfg.get("gemini_api_key") or "").strip()),
            "ollama_base_url": cfg.get("ollama_base_url", "http://127.0.0.1:11434"),
            "ollama_model": cfg.get("ollama_model", "qwen2.5:7b"),
            "video_api_provider": cfg.get("video_api_provider", "comfyui_colab"),
            "comfyui_url": cfg.get("comfyui_url", ""),
            "comfyui_use_turbo": cfg.get("comfyui_use_turbo", "false") == "true",
            "comfyui_enable_realesrgan": cfg.get("comfyui_enable_realesrgan", "true") == "true",
            "comfyui_relay_mode": cfg.get("comfyui_relay_mode", "true") == "true",
            "colab_auth": get_colab_auth_status(),
            "watcher_running": self.watcher.is_running,
            "watcher_project_id": self.watcher.active_project_id,
            "watcher_dir": str(self.watcher.watch_dir) if self.watcher.watch_dir else str(dl_dir),
        }

    def enrich_project(self, proj: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not proj:
            return None
        pid = proj["id"]
        shots = proj.get("shots") if "shots" in proj else self.db.list_shots(pid)
        done_shots = [s for s in shots if s["status"] == "done"]
        pending_shots = [s for s in shots if s["status"] != "done"]

        next_shot = None
        if self._pinned_shot_id and self.watcher.active_project_id == pid:
            next_shot = next(
                (s for s in pending_shots if s["id"] == self._pinned_shot_id), None
            )
        if next_shot is None and pending_shots:
            next_shot = pending_shots[0]

        is_watching_this = (
            self.watcher.is_running and self.watcher.active_project_id == pid
        )
        proj["watcher_active"] = is_watching_this
        proj["watcher_dir"] = (
            str(self.watcher.watch_dir)
            if is_watching_this and self.watcher.watch_dir
            else str(self.get_effective_downloads_dir())
        )
        proj["waiting_shot"] = (
            {
                "id": next_shot["id"],
                "shot_order": next_shot["shot_order"],
                "title": next_shot["title"],
            }
            if next_shot
            else None
        )
        proj["total_shots"] = len(shots)
        proj["done_shots"] = len(done_shots)
        proj["comfyui_generating"] = pid in self._gen_locks
        proj["comfyui_progress"] = self._gen_status.get(pid, {})
        proj["active_colab_session"] = self._active_colab_sessions.get(pid, "")
        proj["master_prompt"] = build_master_prompt_for_web(
            proj["brief"],
            proj["target_duration"],
            proj["aspect_ratio"],
            proj["visual_style"],
        )
        return proj

    def log_and_broadcast(
        self,
        project_id: str,
        message: str,
        level: str = "info",
        stage: int = 0,
        event_type: str = "project_updated",
    ) -> Dict[str, Any]:
        log_entry = self.db.add_log(project_id, message=message, level=level, stage=stage)
        proj = self.enrich_project(self.db.get_project(project_id, include_details=True))
        self.bus.publish(
            project_id,
            {
                "type": event_type,
                "log": log_entry,
                "project": proj,
            },
        )
        return log_entry

    def broadcast_state(self, project_id: str, event_type: str = "project_updated") -> None:
        proj = self.enrich_project(self.db.get_project(project_id, include_details=True))
        if proj:
            self.bus.publish(project_id, {"type": event_type, "project": proj})

    # -------------------------
    # 階段 ① ──▶ 階段 ②：建立專案與自動規劃分鏡
    # -------------------------
    def create_project_and_plan(
        self,
        brief: str,
        name: Optional[str] = None,
        target_duration: int = 60,
        aspect_ratio: str = "16:9",
        visual_style: str = "35mm_doc",
        audio_mode: str = "mute",
        planner_mode: str = "paste",
        auto_render: bool = True,
    ) -> Dict[str, Any]:
        proj = self.db.create_project(
            brief=brief,
            name=name,
            target_duration=target_duration,
            aspect_ratio=aspect_ratio,
            visual_style=visual_style,
            audio_mode=audio_mode,
            planner_mode=planner_mode,
            auto_render=auto_render,
        )
        pid = proj["id"]
        audio_label = "純畫面無聲" if audio_mode == "mute" else "保留原聲"
        self.db.add_log(
            pid,
            f"已建立專案「{proj['name']}」（目標 {target_duration} 秒 / {aspect_ratio} / {audio_label}）。",
            level="success",
            stage=1,
        )

        # 啟動階段 ② 分鏡規劃
        self.trigger_storyboard_generation(pid, mode=planner_mode, run_async=(planner_mode != "paste"))
        return self.enrich_project(self.db.get_project(pid, include_details=True))  # type: ignore[return-value]

    def trigger_storyboard_generation(
        self, project_id: str, mode: Optional[str] = None, run_async: bool = True
    ) -> Dict[str, Any]:
        proj = self.db.get_project(project_id, include_details=False)
        if not proj:
            raise ValueError("找不到指定專案。")

        use_mode = mode or proj["planner_mode"] or "paste"
        statuses = dict(proj["stage_statuses"])
        statuses["1"] = "done"
        statuses["2"] = "running"
        self.db.update_project(
            project_id,
            planner_mode=use_mode,
            current_stage=2,
            stage_statuses=statuses,
            error_message="",
        )

        if use_mode == "paste":
            # 免 Key 模式：0 延遲產出智慧導演初稿，同時保留 Gemini 網頁版貼上解析器
            style_lock, neg_prompt, shots = generate_smart_draft(
                proj["brief"],
                proj["target_duration"],
                proj["aspect_ratio"],
                proj["visual_style"],
            )
            self.db.replace_shots(project_id, shots)
            statuses["2"] = "action_needed"
            self.db.update_project(
                project_id,
                style_lock=style_lock,
                negative_prompt=neg_prompt,
                stage_statuses=statuses,
            )
            self.log_and_broadcast(
                project_id,
                f"已自動規劃 {len(shots)} 個導演分鏡初稿！請審核微調（或使用免 Key 貼上解析覆蓋），確認後點擊「確認分鏡，進入生片」。",
                level="action",
                stage=2,
            )
            return self.enrich_project(self.db.get_project(project_id, include_details=True))  # type: ignore[return-value]

        # Gemini 或 Ollama 非同步執行
        mode_name = "免費 Gemini API" if use_mode == "gemini" else "本機 Ollama"
        self.log_and_broadcast(
            project_id,
            f"正在透過「{mode_name}」自動撰寫分鏡腳本與英文 Prompt...",
            level="info",
            stage=2,
        )

        if run_async:
            t = threading.Thread(
                target=self._run_llm_worker,
                args=(project_id, use_mode),
                daemon=True,
            )
            t.start()
        else:
            self._run_llm_worker(project_id, use_mode)

        return self.enrich_project(self.db.get_project(project_id, include_details=True))  # type: ignore[return-value]

    def _run_llm_worker(self, project_id: str, mode: str) -> None:
        proj = self.db.get_project(project_id, include_details=False)
        if not proj:
            return
        cfg = self.db.get_settings()
        statuses = dict(proj["stage_statuses"])

        try:
            if mode == "gemini":
                style_lock, neg_prompt, shots = generate_via_gemini(
                    brief=proj["brief"],
                    api_key=cfg.get("gemini_api_key", ""),
                    model=cfg.get("gemini_model", "gemini-2.5-flash"),
                    target_duration=proj["target_duration"],
                    aspect_ratio=proj["aspect_ratio"],
                    visual_style=proj["visual_style"],
                )
            elif mode == "ollama":
                style_lock, neg_prompt, shots = generate_via_ollama(
                    brief=proj["brief"],
                    base_url=cfg.get("ollama_base_url", "http://127.0.0.1:11434"),
                    model=cfg.get("ollama_model", "qwen2.5:7b"),
                    target_duration=proj["target_duration"],
                    aspect_ratio=proj["aspect_ratio"],
                    visual_style=proj["visual_style"],
                )
            else:
                style_lock, neg_prompt, shots = generate_smart_draft(
                    proj["brief"],
                    proj["target_duration"],
                    proj["aspect_ratio"],
                    proj["visual_style"],
                )

            self.db.replace_shots(project_id, shots)
            statuses["2"] = "action_needed"
            self.db.update_project(
                project_id,
                style_lock=style_lock,
                negative_prompt=neg_prompt,
                stage_statuses=statuses,
                error_message="",
            )
            self.log_and_broadcast(
                project_id,
                f"AI 分鏡生成完成（共 {len(shots)} 鏡）！等你動手確認或微調分鏡。",
                level="action",
                stage=2,
            )
        except Exception as exc:
            statuses["2"] = "failed"
            err_msg = str(exc)
            self.db.update_project(
                project_id,
                stage_statuses=statuses,
                error_message=err_msg,
            )
            self.log_and_broadcast(
                project_id,
                f"分鏡自動生成失敗：{err_msg}（可點單步重試，或一鍵切換為免 Key 模式）",
                level="error",
                stage=2,
            )

    def apply_pasted_storyboard(self, project_id: str, raw_text: str) -> Dict[str, Any]:
        """免 Key 貼上解析模式：解析外部貼入的 JSON 或 Markdown 分鏡並覆蓋。"""
        proj = self.db.get_project(project_id, include_details=False)
        if not proj:
            raise ValueError("找不到指定專案。")

        style_lock, neg_prompt, shots = parse_pasted_storyboard(
            raw_text=raw_text,
            aspect_ratio=proj["aspect_ratio"],
            visual_style=proj["visual_style"],
        )
        self.db.replace_shots(project_id, shots)
        statuses = dict(proj["stage_statuses"])
        statuses["1"] = "done"
        statuses["2"] = "action_needed"
        statuses["3"] = "pending"
        self.db.update_project(
            project_id,
            current_stage=2,
            style_lock=style_lock,
            negative_prompt=neg_prompt,
            stage_statuses=statuses,
            error_message="",
        )
        self.log_and_broadcast(
            project_id,
            f"已成功解析貼上的分鏡腳本，共載入 {len(shots)} 個鏡頭！請確認後進入生片階段。",
            level="success",
            stage=2,
        )
        return self.enrich_project(self.db.get_project(project_id, include_details=True))  # type: ignore[return-value]

    def confirm_storyboard(
        self,
        project_id: str,
        start_watcher: bool = True,
        watch_dir: Optional[Path] = None,
    ) -> Dict[str, Any]:
        """階段 ② ──▶ 階段 ③：使用者確認分鏡，進入素材收集並啟動自動監聽。"""
        proj = self.db.get_project(project_id, include_details=True)
        if not proj:
            raise ValueError("找不到指定專案。")
        shots = proj.get("shots") or []
        if not shots:
            raise ValueError("目前沒有任何分鏡，請先建立至少 1 個鏡頭。")

        all_done = all(s["status"] == "done" for s in shots)
        statuses = dict(proj["stage_statuses"])
        statuses["1"] = "done"
        statuses["2"] = "done"
        statuses["3"] = "done" if all_done else "action_needed"

        self.db.update_project(
            project_id,
            current_stage=3,
            stage_statuses=statuses,
            error_message="",
        )
        self.log_and_broadcast(
            project_id,
            f"分鏡表已確認鎖定（共 {len(shots)} 鏡），正式進入階段 ③ 收集鏡頭素材！",
            level="success",
            stage=3,
        )

        if start_watcher and not all_done:
            self.start_watcher(project_id, custom_watch_dir=watch_dir)

        return self.enrich_project(self.db.get_project(project_id, include_details=True))  # type: ignore[return-value]

    # -------------------------
    # 階段 ③：Downloads 資料夾監聽與素材歸檔
    # -------------------------
    def start_watcher(
        self,
        project_id: str,
        custom_watch_dir: Optional[Path] = None,
        target_shot_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        proj = self.db.get_project(project_id, include_details=True)
        if not proj:
            raise ValueError("找不到指定專案。")

        w_dir = Path(custom_watch_dir).resolve() if custom_watch_dir else self.get_effective_downloads_dir()
        self._pinned_shot_id = target_shot_id

        # 關閉其他專案的 watcher_enabled 狀態
        for other in self.db.list_projects():
            if other["id"] != project_id and other.get("watcher_enabled"):
                self.db.update_project(other["id"], watcher_enabled=False)

        self.watcher.start(project_id=project_id, watch_dir=w_dir)
        self.db.update_project(project_id, watcher_enabled=True)

        enriched = self.enrich_project(self.db.get_project(project_id, include_details=True))
        waiting = enriched.get("waiting_shot") if enriched else None
        wait_desc = (
            f"正在等待 Shot {waiting['shot_order']:02d}（{waiting['title']}）"
            if waiting
            else "等待新鏡頭下載"
        )
        self.log_and_broadcast(
            project_id,
            f"📡 已開啟自動監聽資料夾（{w_dir}），{wait_desc}。去 Google Flow 點下載後將自動抓取歸檔！",
            level="action",
            stage=3,
        )
        return self.enrich_project(self.db.get_project(project_id, include_details=True))  # type: ignore[return-value]

    def stop_watcher(self, project_id: str) -> Dict[str, Any]:
        if self.watcher.active_project_id == project_id:
            self.watcher.stop()
        self._pinned_shot_id = None
        self.db.update_project(project_id, watcher_enabled=False)
        self.log_and_broadcast(
            project_id,
            "已暫停自動監聽下載資料夾。",
            level="info",
            stage=3,
        )
        return self.enrich_project(self.db.get_project(project_id, include_details=True))  # type: ignore[return-value]

    def _handle_watched_video(self, src_path: Path, meta: Dict[str, Any]) -> None:
        """當 DownloadFolderWatcher 偵測到穩定且通過 ffprobe 驗證的新下載影片時觸發。"""
        project_id = self.watcher.active_project_id
        if not project_id:
            return

        with self._lock:
            proj = self.db.get_project(project_id, include_details=True)
            if not proj:
                return
            shots = proj.get("shots") or []
            pending_shots = [s for s in shots if s["status"] != "done"]
            if not pending_shots:
                return

            target_shot = None
            if self._pinned_shot_id:
                target_shot = next(
                    (s for s in pending_shots if s["id"] == self._pinned_shot_id), None
                )
                self._pinned_shot_id = None
            if target_shot is None:
                target_shot = pending_shots[0]

            self._archive_video_to_shot(
                project_id=project_id,
                shot=target_shot,
                src_path=src_path,
                source_filename=src_path.name,
                probed_meta=meta,
                via_watcher=True,
            )

    def assign_uploaded_or_local_clip(
        self,
        project_id: str,
        shot_id: int,
        src_path: Path,
        original_filename: str,
    ) -> Dict[str, Any]:
        """手動拖曳上傳或指定檔案替換單一分鏡素材。"""
        shot = self.db.get_shot(shot_id)
        if not shot or shot["project_id"] != project_id:
            raise ValueError("找不到對應的分鏡卡片。")

        meta = probe_video(src_path)
        with self._lock:
            self._archive_video_to_shot(
                project_id=project_id,
                shot=shot,
                src_path=src_path,
                source_filename=original_filename,
                probed_meta=meta,
                via_watcher=False,
            )
        return self.enrich_project(self.db.get_project(project_id, include_details=True))  # type: ignore[return-value]

    def _archive_video_to_shot(
        self,
        project_id: str,
        shot: Dict[str, Any],
        src_path: Path,
        source_filename: str,
        probed_meta: Dict[str, Any],
        via_watcher: bool,
    ) -> None:
        dirs = self.db.get_project_dirs(project_id)
        shot_order = int(shot["shot_order"])
        shot_id = int(shot["id"])

        ext = src_path.suffix.lower() if src_path.suffix.lower() in (".mp4", ".mov", ".webm") else ".mp4"
        dest_clip = dirs["clips"] / f"shot_{shot_order:02d}{ext}"
        dest_thumb = dirs["thumbs"] / f"shot_{shot_order:02d}.jpg"

        if src_path.resolve() != dest_clip.resolve():
            shutil.copy2(src_path, dest_clip)

        extract_thumbnail(
            video_path=dest_clip,
            thumb_path=dest_thumb,
            duration_hint=probed_meta["duration"],
        )

        self.db.update_shot(
            shot_id,
            status="done",
            clip_path=str(dest_clip),
            thumb_path=str(dest_thumb),
            source_filename=source_filename,
            actual_duration=probed_meta["duration"],
            width=probed_meta["width"],
            height=probed_meta["height"],
            has_audio=probed_meta["has_audio"],
        )

        prefix_msg = "偵測到新下載影片" if via_watcher else "已手動匯入影片"
        self.log_and_broadcast(
            project_id,
            f"{prefix_msg}「{source_filename}」，已自動歸檔為 Shot {shot_order:02d}（{probed_meta['duration']:.1f}秒, {probed_meta['width']}×{probed_meta['height']}）並產生縮圖！",
            level="success",
            stage=3,
            event_type="shot_captured",
        )

        # 檢查是否所有分鏡皆已完成
        updated_proj = self.db.get_project(project_id, include_details=True)
        if not updated_proj:
            return
        all_shots = updated_proj.get("shots") or []
        remaining = [s for s in all_shots if s["status"] != "done"]

        if remaining:
            nxt = remaining[0]
            if via_watcher:
                self.log_and_broadcast(
                    project_id,
                    f"👉 焦點已自動切換至下一鏡 Shot {nxt['shot_order']:02d}：{nxt['title']}（等你複製 Prompt 生片下載）",
                    level="action",
                    stage=3,
                )
        else:
            # 全部鏡頭就緒！
            statuses = dict(updated_proj["stage_statuses"])
            statuses["3"] = "done"
            self.db.update_project(project_id, stage_statuses=statuses, watcher_enabled=False)
            if self.watcher.active_project_id == project_id:
                self.watcher.stop()

            self.log_and_broadcast(
                project_id,
                f"🎉 太棒了！全部 {len(all_shots)} 個鏡頭素材均已收集完畢！",
                level="success",
                stage=3,
            )

            if updated_proj.get("auto_render"):
                self.start_render(project_id, run_async=True)

    def reset_shot(self, project_id: str, shot_id: int) -> Dict[str, Any]:
        """重設單一鏡頭為等待中，方便重新下載或覆蓋。"""
        shot = self.db.get_shot(shot_id)
        if not shot or shot["project_id"] != project_id:
            raise ValueError("找不到指定的鏡頭。")

        self.db.update_shot(
            shot_id,
            status="pending",
            clip_path="",
            thumb_path="",
            source_filename="",
            actual_duration=0.0,
            width=0,
            height=0,
            has_audio=False,
        )
        proj = self.db.get_project(project_id, include_details=False)
        if proj:
            statuses = dict(proj["stage_statuses"])
            statuses["3"] = "action_needed"
            self.db.update_project(
                project_id,
                current_stage=3,
                stage_statuses=statuses,
            )
        self._pinned_shot_id = shot_id
        self.log_and_broadcast(
            project_id,
            f"🔄 已將 Shot {int(shot['shot_order']):02d}（{shot['title']}）重設為等待錄製狀態，下次下載將優先歸檔至此鏡。",
            level="action",
            stage=3,
        )
        return self.enrich_project(self.db.get_project(project_id, include_details=True))  # type: ignore[return-value]

    def import_latest_from_downloads(
        self, project_id: str, max_count: Optional[int] = None, custom_dir: Optional[Path] = None
    ) -> Dict[str, Any]:
        """一鍵從下載資料夾匯入最新 N 支影片（依建立/修改時間由舊到新填入未完成鏡頭）。"""
        proj = self.db.get_project(project_id, include_details=True)
        if not proj:
            raise ValueError("找不到指定專案。")
        pending = [s for s in (proj.get("shots") or []) if s["status"] != "done"]
        if not pending:
            raise ValueError("所有鏡頭皆已完成，若要替換請先在卡片上點「重新錄製這鏡」或直接拖曳檔案覆蓋。")

        dl_dir = Path(custom_dir).resolve() if custom_dir else self.get_effective_downloads_dir()
        if not dl_dir.is_dir():
            raise ValueError(f"找不到下載資料夾：{dl_dir}")

        need_n = min(len(pending), int(max_count) if max_count else len(pending))
        candidates: List[Tuple[float, Path]] = []
        for p in dl_dir.iterdir():
            if p.is_file() and is_candidate_video_path(p):
                try:
                    st = p.stat()
                    if st.st_size > 10 * 1024:
                        candidates.append((st.st_mtime, p.resolve()))
                except OSError:
                    pass

        if not candidates:
            raise ValueError(f"在 {dl_dir} 中沒有找到任何影片檔（.mp4/.mov/.webm）。")

        # 取最新 need_n 支，並由舊到新排序以符合依序下載直覺
        candidates.sort(key=lambda x: x[0], reverse=True)
        picked = list(reversed(candidates[:need_n]))

        imported_cnt = 0
        # 暫時關閉自動觸發合成，等整批匯入完再檢查
        orig_auto = proj.get("auto_render", False)
        self.db.update_project(project_id, auto_render=False)
        try:
            for (_, v_path), target_shot in zip(picked, pending):
                try:
                    meta = probe_video(v_path)
                    self._archive_video_to_shot(
                        project_id=project_id,
                        shot=target_shot,
                        src_path=v_path,
                        source_filename=v_path.name,
                        probed_meta=meta,
                        via_watcher=False,
                    )
                    imported_cnt += 1
                except Exception as exc:
                    self.log_and_broadcast(
                        project_id,
                        f"略過無效影片「{v_path.name}」：{exc}",
                        level="warning",
                        stage=3,
                    )
        finally:
            self.db.update_project(project_id, auto_render=orig_auto)

        if imported_cnt == 0:
            raise ValueError("未成功匯入任何有效影片。")

        refreshed = self.db.get_project(project_id, include_details=True)
        if refreshed and orig_auto and all(s["status"] == "done" for s in refreshed["shots"]):
            self.start_render(project_id, run_async=True)

        return self.enrich_project(self.db.get_project(project_id, include_details=True))  # type: ignore[return-value]

    # -------------------------
    # 階段 ④ ──▶ 階段 ⑤：全自動後期剪輯流水線與成品交付
    # -------------------------
    def start_render(
        self,
        project_id: str,
        override_options: Optional[Dict[str, Any]] = None,
        run_async: bool = True,
    ) -> Dict[str, Any]:
        with self._lock:
            if project_id in self._render_locks:
                return self.enrich_project(self.db.get_project(project_id, include_details=True))  # type: ignore[return-value]

            proj = self.db.get_project(project_id, include_details=True)
            if not proj:
                raise ValueError("找不到指定專案。")

            ready_shots = []
            for s in (proj.get("shots") or []):
                if s["status"] == "done" and s["clip_path"]:
                    rp = resolve_project_media_path(s["clip_path"], project_id, "clips")
                    if rp:
                        s_copy = dict(s)
                        s_copy["clip_path"] = str(rp)
                        ready_shots.append(s_copy)
            if not ready_shots:
                raise ValueError("尚無任何已完成的鏡頭素材，無法開始剪輯合成。")

            render_opts = dict(proj["render_options"])
            aspect_ratio = proj["aspect_ratio"]
            audio_mode = proj["audio_mode"]

            if override_options:
                if "aspect_ratio" in override_options and override_options["aspect_ratio"] in ("16:9", "9:16"):
                    aspect_ratio = override_options["aspect_ratio"]
                if "audio_mode" in override_options and override_options["audio_mode"] in ("mute", "keep"):
                    audio_mode = override_options["audio_mode"]
                for k in ("fps", "transition_sec", "fade_in_sec", "fade_out_sec", "align_duration"):
                    if k in override_options and override_options[k] is not None:
                        render_opts[k] = override_options[k]

            statuses = dict(proj["stage_statuses"])
            statuses["1"] = "done"
            statuses["2"] = "done"
            if all(s["status"] == "done" for s in (proj.get("shots") or [])):
                statuses["3"] = "done"
            statuses["4"] = "running"
            statuses["5"] = "pending"

            self._render_locks.add(project_id)
            self.db.update_project(
                project_id,
                aspect_ratio=aspect_ratio,
                audio_mode=audio_mode,
                render_options=render_opts,
                current_stage=4,
                stage_statuses=statuses,
                render_progress={"percent": 2, "substep": "準備啟動剪輯引擎", "detail": ""},
                error_message="",
            )

        self.log_and_broadcast(
            project_id,
            f"🎬 啟動階段 ④ 全自動後期剪輯流水線（共 {len(ready_shots)} 支鏡頭，輸出比例 {aspect_ratio}，轉場 {render_opts.get('transition_sec', 0.5)} 秒）...",
            level="info",
            stage=4,
        )

        if run_async:
            t = threading.Thread(
                target=self._render_worker,
                args=(project_id,),
                name=f"RenderWorker-{project_id}",
                daemon=True,
            )
            t.start()
        else:
            self._render_worker(project_id)

        return self.enrich_project(self.db.get_project(project_id, include_details=True))  # type: ignore[return-value]

    def _render_worker(self, project_id: str) -> None:
        try:
            proj = self.db.get_project(project_id, include_details=True)
            if not proj:
                return
            dirs = self.db.get_project_dirs(project_id)
            ready_shots = []
            for s in (proj.get("shots") or []):
                if s["status"] == "done" and s["clip_path"]:
                    rp = resolve_project_media_path(s["clip_path"], project_id, "clips")
                    if rp:
                        s_copy = dict(s)
                        s_copy["clip_path"] = str(rp)
                        ready_shots.append(s_copy)
            opts = proj["render_options"]

            def on_progress(pct: int, substep: str, detail_msg: str) -> None:
                self.db.update_project(
                    project_id,
                    render_progress={
                        "percent": pct,
                        "substep": substep,
                        "detail": detail_msg,
                    },
                )
                self.log_and_broadcast(
                    project_id,
                    detail_msg,
                    level="info" if pct < 100 else "success",
                    stage=4,
                    event_type="render_progress",
                )

            result = render_project_video(
                clips=ready_shots,
                temp_dir=dirs["temp"],
                output_dir=dirs["output"],
                desktop_output_dir=self.get_effective_desktop_output_dir(),
                project_name=proj["name"],
                aspect_ratio=proj["aspect_ratio"],
                fps=int(opts.get("fps", 24)),
                audio_mode=proj["audio_mode"],
                transition_sec=float(opts.get("transition_sec", 0.5)),
                fade_in_sec=float(opts.get("fade_in_sec", 0.8)),
                fade_out_sec=float(opts.get("fade_out_sec", 1.0)),
                align_duration=bool(opts.get("align_duration", False)),
                target_duration=int(proj["target_duration"]),
                progress_cb=on_progress,
            )

            latest_proj = self.db.get_project(project_id, include_details=False)
            statuses = dict(latest_proj["stage_statuses"]) if latest_proj else {}
            statuses["4"] = "done"
            statuses["5"] = "done"

            self.db.update_project(
                project_id,
                current_stage=5,
                stage_statuses=statuses,
                output_meta=result,
                render_progress={
                    "percent": 100,
                    "substep": "已完成全部合成工序",
                    "detail": f"輸出路徑：{result.get('desktop_path') or result['output_path']}",
                },
                error_message="",
            )
            loc_str = result.get("desktop_path") or result["output_path"]
            self.log_and_broadcast(
                project_id,
                f"✅ 階段 ⑤ 成品交付就緒！成片已儲存至：{loc_str}",
                level="success",
                stage=5,
            )

        except Exception as exc:
            latest_proj = self.db.get_project(project_id, include_details=False)
            statuses = dict(latest_proj["stage_statuses"]) if latest_proj else {}
            statuses["4"] = "failed"
            err_msg = str(exc)
            self.db.update_project(
                project_id,
                current_stage=4,
                stage_statuses=statuses,
                error_message=err_msg,
            )
            self.log_and_broadcast(
                project_id,
                f"❌ 剪輯合成發生錯誤：{err_msg}（已完成收集的鏡頭均完整保留，可修正後點「單步重試」）",
                level="error",
                stage=4,
            )
        finally:
            with self._lock:
                self._render_locks.discard(project_id)

    # -------------------------
    # 配音參考稿產生器 (.md / .txt)
    # -------------------------
    def build_voiceover_script(self, project_id: str, fmt: str = "md") -> Tuple[str, str]:
        proj = self.db.get_project(project_id, include_details=True)
        if not proj:
            raise ValueError("找不到指定專案。")
        shots = proj.get("shots") or []
        opts = proj.get("render_options") or {}
        trans = float(opts.get("transition_sec", 0.5))

        def fmt_tc(sec: float) -> str:
            sec = max(0.0, sec)
            m = int(sec // 60)
            s = sec - m * 60
            return f"{m:02d}:{s:04.1f}"

        cursor = 0.0
        lines: List[str] = []
        if fmt == "txt":
            lines.append(f"【後期配音參考稿】{proj['name']}")
            lines.append(f"原始需求：{proj['brief']}")
            lines.append(f"畫面比例：{proj['aspect_ratio']} ｜ 轉場疊化：{trans} 秒")
            lines.append("=" * 56)
            lines.append("")
            for idx, s in enumerate(shots):
                dur = float(s["actual_duration"] or s["duration_sec"] or 8.0)
                start_t = cursor
                end_t = start_t + dur
                lines.append(
                    f"[Shot {int(s['shot_order']):02d}] {fmt_tc(start_t)} - {fmt_tc(end_t)} ｜ {s['title']}"
                )
                lines.append(f"  畫面：{s['scene_desc_zh'] or '（無）'}")
                lines.append(f"  旁白：{s['voiceover_zh'] or '（請填入旁白台詞）'}")
                lines.append("")
                cursor = end_t - (trans if idx < len(shots) - 1 else 0.0)
            filename = f"{proj['name']}_配音參考稿.txt"
        else:
            lines.append(f"# 🎙️ 後期配音參考稿：{proj['name']}")
            lines.append("")
            lines.append(f"- **原始需求**：{proj['brief']}")
            lines.append(f"- **畫面比例**：`{proj['aspect_ratio']}` ｜ **轉場疊化**：`{trans} 秒`")
            lines.append("")
            lines.append("---")
            lines.append("")
            for idx, s in enumerate(shots):
                dur = float(s["actual_duration"] or s["duration_sec"] or 8.0)
                start_t = cursor
                end_t = start_t + dur
                lines.append(
                    f"## Shot {int(s['shot_order']):02d}｜`{fmt_tc(start_t)} – {fmt_tc(end_t)}`｜{s['title']}"
                )
                lines.append(f"- **畫面提示**：{s['scene_desc_zh'] or '（無）'}")
                lines.append(f"- **建議旁白**：「{s['voiceover_zh'] or '（可依畫面節奏自由發揮）'}」")
                lines.append("")
                cursor = end_t - (trans if idx < len(shots) - 1 else 0.0)
            filename = f"{proj['name']}_配音參考稿.md"

        return filename, "\n".join(lines)

    # -------------------------
    # Colab G4 ComfyUI（MiniMax H3 首尾幀接力 + RealESRGAN 1080p）全自動生片引擎
    # -------------------------
    def test_comfyui_connection(self, url: str) -> Dict[str, Any]:
        """測試 Colab ComfyUI 連線並回傳 GPU / VRAM / 模型偵測資訊；連線成功則自動保存網址。"""
        clean_url = normalize_comfyui_url(url)
        client = ComfyUIClient(clean_url)
        info = client.check_connection()
        self.db.update_settings(
            {
                "comfyui_url": clean_url,
                "video_api_provider": "comfyui_colab",
            }
        )
        return info

    def stop_comfyui_generation(self, project_id: str) -> Dict[str, Any]:
        """中斷進行中的 Colab ComfyUI 雲端生片（已完成的鏡頭均完整保留）。"""
        stop_ev = self._gen_stops.get(project_id)
        if stop_ev:
            stop_ev.set()

        cfg = self.db.get_settings()
        c_url = (cfg.get("comfyui_url") or "").strip()
        if c_url:
            try:
                ComfyUIClient(c_url).interrupt()
            except Exception:
                pass

        # 若有透過 Colab CLI 自動開啟的雲端會話，立刻主動釋放防扣點
        active_sess = self._active_colab_sessions.pop(project_id, None)
        if active_sess:
            try:
                ColabCLIManager().stop_session(
                    active_sess,
                    log_cb=lambda m, lvl: self.log_and_broadcast(project_id, m, level=lvl, stage=3),
                )
            except Exception:
                pass

        proj = self.db.get_project(project_id, include_details=False)
        if proj:
            statuses = dict(proj["stage_statuses"])
            statuses["3"] = "action_needed"
            self.db.update_project(project_id, stage_statuses=statuses)

        self._gen_status.pop(project_id, None)
        self.log_and_broadcast(
            project_id,
            "⏹️ 已發送中斷指令給 Colab ComfyUI，已生成的鏡頭素材均安全保留。",
            level="warning",
            stage=3,
        )
        return self.enrich_project(self.db.get_project(project_id, include_details=True))  # type: ignore[return-value]

    def start_comfyui_generation(
        self,
        project_id: str,
        comfyui_url: Optional[str] = None,
        relay_mode: Optional[bool] = None,
        use_turbo: Optional[bool] = None,
        enable_realesrgan: Optional[bool] = None,
        only_shot_id: Optional[int] = None,
        poll_interval_sec: float = 3.0,
        run_async: bool = True,
    ) -> Dict[str, Any]:
        """
        啟動 Colab G4 ComfyUI 全自動生片管線：
        - 支援四幕/多幕『首尾幀接力』（自動抽上一幕尾幀上傳為下一幕 first_frame）
        - 支援 RealESRGAN 即時 1080p 超解析度
        - 每完成一幕立即擷取縮圖透過 SSE 推播到前端，全數完成後自動銜接階段 ④ 剪輯與階段 ⑤ 交付！
        """
        with self._lock:
            if project_id in self._gen_locks:
                return self.enrich_project(self.db.get_project(project_id, include_details=True))  # type: ignore[return-value]

            proj = self.db.get_project(project_id, include_details=True)
            if not proj:
                raise ValueError("找不到指定專案。")

            cfg = self.db.get_settings()
            effective_url = normalize_comfyui_url(comfyui_url or cfg.get("comfyui_url", ""))
            if not effective_url:
                raise ValueError(
                    "尚未填入 Colab ComfyUI 連線網址！請先在 Colab G4 執行筆記本，將印出的 https://xxxx.trycloudflare.com 貼到連線欄位。"
                )

            eff_relay = (
                bool(relay_mode)
                if relay_mode is not None
                else (cfg.get("comfyui_relay_mode", "true") == "true")
            )
            eff_turbo = (
                bool(use_turbo)
                if use_turbo is not None
                else (cfg.get("comfyui_use_turbo", "false") == "true")
            )
            eff_esrgan = (
                bool(enable_realesrgan)
                if enable_realesrgan is not None
                else (cfg.get("comfyui_enable_realesrgan", "true") == "true")
            )

            # 記住最新連線參數
            self.db.update_settings(
                {
                    "comfyui_url": effective_url,
                    "comfyui_relay_mode": "true" if eff_relay else "false",
                    "comfyui_use_turbo": "true" if eff_turbo else "false",
                    "comfyui_enable_realesrgan": "true" if eff_esrgan else "false",
                    "video_api_provider": "comfyui_colab",
                }
            )

            # 關閉本地 Downloads 監聽避免衝突
            if self.watcher.active_project_id == project_id:
                self.watcher.stop()

            shots = proj.get("shots") or []
            if only_shot_id is not None:
                targets = [s for s in shots if s["id"] == only_shot_id]
                if not targets:
                    raise ValueError("找不到指定要重新生成的鏡頭。")
            else:
                targets = [s for s in shots if s["status"] != "done"]
                if not targets:
                    raise ValueError("所有鏡頭都已經生成完成！若想重做某一幕，請點該卡片的『重新生成這幕』。")

            stop_ev = threading.Event()
            self._gen_locks.add(project_id)
            self._gen_stops[project_id] = stop_ev
            self._gen_status[project_id] = {
                "current_shot_order": targets[0]["shot_order"],
                "total_targets": len(targets),
                "completed_targets": 0,
                "message": "正在連線 Colab G4 ComfyUI...",
            }

            statuses = dict(proj["stage_statuses"])
            statuses["1"] = "done"
            statuses["2"] = "done"
            statuses["3"] = "running"
            self.db.update_project(
                project_id,
                current_stage=3,
                stage_statuses=statuses,
                watcher_enabled=False,
                error_message="",
            )

        mode_tags = []
        if eff_relay:
            mode_tags.append("首尾幀接力")
        if eff_esrgan:
            mode_tags.append("RealESRGAN 1080p 超分")
        if eff_turbo:
            mode_tags.append("8-Step Turbo 加速")

        self.log_and_broadcast(
            project_id,
            f"🤖 已啟動 Colab G4 ComfyUI 全自動生片（MiniMax H3｜{' ＋ '.join(mode_tags) or '標準模式'}），共待生成 {len(targets)} 幕...",
            level="info",
            stage=3,
        )

        worker_args = (
            project_id,
            effective_url,
            eff_relay,
            eff_turbo,
            eff_esrgan,
            only_shot_id,
            poll_interval_sec,
            stop_ev,
        )
        if run_async:
            t = threading.Thread(
                target=self._comfyui_gen_worker,
                args=worker_args,
                name=f"ComfyGen-{project_id}",
                daemon=True,
            )
            t.start()
        else:
            self._comfyui_gen_worker(*worker_args)

        return self.enrich_project(self.db.get_project(project_id, include_details=True))  # type: ignore[return-value]

    def _comfyui_gen_worker(
        self,
        project_id: str,
        comfyui_url: str,
        relay_mode: bool,
        use_turbo: bool,
        enable_realesrgan: bool,
        only_shot_id: Optional[int],
        poll_interval_sec: float,
        stop_ev: threading.Event,
    ) -> None:
        try:
            client = ComfyUIClient(comfyui_url)
            conn_info = client.wait_until_ready(
                max_wait_sec=45.0,
                retry_interval_sec=3.0,
                stop_event=stop_ev,
                progress_cb=lambda m: self.log_and_broadcast(project_id, f"🌐 {m}", level="info", stage=3),
            )
            models = conn_info.get("models") or {}
            can_esrgan = enable_realesrgan and bool(conn_info.get("has_realesrgan"))
            can_turbo = use_turbo and bool(conn_info.get("has_turbo_lora"))

            self.log_and_broadcast(
                project_id,
                f"🟢 已成功連線 Colab 雲端 GPU：{conn_info['gpu_name']}（{conn_info['vram_total_gb']} GB VRAM）！開始自動排程生成...",
                level="success",
                stage=3,
            )

            proj = self.db.get_project(project_id, include_details=True)
            if not proj:
                return
            dirs = self.db.get_project_dirs(project_id)
            all_shots = proj.get("shots") or []

            if only_shot_id is not None:
                target_shots = [s for s in all_shots if s["id"] == only_shot_id]
            else:
                target_shots = [s for s in all_shots if s["status"] != "done"]

            for idx, shot in enumerate(target_shots, start=1):
                if stop_ev.is_set():
                    raise InterruptedError("使用者已停止生片。")

                shot_order = int(shot["shot_order"])
                shot_id = int(shot["id"])
                dur_sec = float(shot.get("duration_sec") or 10.0)

                # 檢查是否啟用『首尾幀接力』：取前一幕 (shot_order - 1) 的最後一幀上傳為 first_frame
                first_frame_remote: Optional[str] = None
                if relay_mode and shot_order > 1:
                    latest_shots = self.db.list_shots(project_id)
                    prev_shot = next(
                        (s for s in latest_shots if int(s["shot_order"]) == shot_order - 1 and s["status"] == "done"),
                        None,
                    )
                    prev_clip = (
                        resolve_project_media_path(prev_shot.get("clip_path") or "", project_id, "clips")
                        if prev_shot
                        else None
                    )
                    if prev_clip:
                        last_frame_local = dirs["temp"] / f"relay_from_shot_{shot_order - 1:02d}.png"
                        extract_last_frame(prev_clip, last_frame_local)
                        remote_fname = f"{project_id}_relay_to_shot_{shot_order:02d}.png"
                        first_frame_remote = client.upload_image(last_frame_local, remote_name=remote_fname)
                        self.log_and_broadcast(
                            project_id,
                            f"🔗 已擷取 Shot {shot_order - 1:02d} 尾幀並上傳至 Colab，準備首尾幀接力生成 Shot {shot_order:02d}！",
                            level="info",
                            stage=3,
                        )

                self._gen_status[project_id] = {
                    "current_shot_order": shot_order,
                    "current_shot_title": shot["title"],
                    "total_targets": len(target_shots),
                    "completed_targets": idx - 1,
                    "message": f"正在生成第 {idx}/{len(target_shots)} 幕：Shot {shot_order:02d}（{dur_sec:.0f}秒）...",
                }
                self.log_and_broadcast(
                    project_id,
                    f"🚀 開始雲端生成 Shot {shot_order:02d}「{shot['title']}」（預計 {dur_sec:.0f} 秒｜{'首尾幀接力' if first_frame_remote else '首幕建立'}{'｜含 RealESRGAN 1080p' if can_esrgan else ''}）...",
                    level="info",
                    stage=3,
                )

                wf = client.build_minimax_h3_workflow(
                    prompt_en=shot["prompt_en"],
                    duration_sec=dur_sec,
                    aspect_ratio=proj["aspect_ratio"],
                    first_frame_image=first_frame_remote,
                    use_turbo_lora=can_turbo,
                    enable_realesrgan_inline=can_esrgan,
                    models=models,
                    filename_prefix=f"video/{project_id}_shot_{shot_order:02d}",
                    vram_gb=float(conn_info.get("vram_total_gb") or 96.0),
                )

                dest_clip = dirs["clips"] / f"shot_{shot_order:02d}.mp4"

                def _on_poll_msg(msg: str) -> None:
                    self._gen_status[project_id] = {
                        "current_shot_order": shot_order,
                        "current_shot_title": shot["title"],
                        "total_targets": len(target_shots),
                        "completed_targets": idx - 1,
                        "message": f"Shot {shot_order:02d}：{msg}",
                    }
                    self.log_and_broadcast(
                        project_id,
                        f"⏳ Shot {shot_order:02d}：{msg}",
                        level="info",
                        stage=3,
                    )

                client.queue_and_download_video(
                    workflow=wf,
                    dest_mp4_path=dest_clip,
                    stop_event=stop_ev,
                    poll_interval_sec=poll_interval_sec,
                    progress_cb=_on_poll_msg,
                )

                # 驗證並擷取縮圖入庫
                meta = probe_video(dest_clip)
                dest_thumb = dirs["thumbs"] / f"shot_{shot_order:02d}.jpg"
                extract_thumbnail(dest_clip, dest_thumb, duration_hint=meta["duration"])

                self.db.update_shot(
                    shot_id,
                    status="done",
                    clip_path=str(dest_clip),
                    thumb_path=str(dest_thumb),
                    source_filename=f"MiniMaxH3_Shot_{shot_order:02d}.mp4",
                    actual_duration=meta["duration"],
                    width=meta["width"],
                    height=meta["height"],
                    has_audio=meta["has_audio"],
                )

                self._gen_status[project_id] = {
                    "current_shot_order": shot_order,
                    "current_shot_title": shot["title"],
                    "total_targets": len(target_shots),
                    "completed_targets": idx,
                    "message": f"已完成 {idx}/{len(target_shots)} 幕！",
                }
                self.log_and_broadcast(
                    project_id,
                    f"✅ Colab G4 完成 Shot {shot_order:02d}「{shot['title']}」（{meta['duration']:.1f}秒, {meta['width']}×{meta['height']}）並已自動拉回本機產生縮圖！",
                    level="success",
                    stage=3,
                    event_type="shot_captured",
                )

            # 檢查是否全部鏡頭都完成
            refreshed = self.db.get_project(project_id, include_details=True)
            if not refreshed:
                return
            all_done = all(s["status"] == "done" for s in (refreshed.get("shots") or []))
            statuses = dict(refreshed["stage_statuses"])
            statuses["3"] = "done" if all_done else "action_needed"
            self.db.update_project(project_id, stage_statuses=statuses, error_message="")

            if all_done:
                self.log_and_broadcast(
                    project_id,
                    "🎉 全部雲端鏡頭已透過 MiniMax H3 自動生成完畢！準備銜接階段 ④ 全自動後期剪輯...",
                    level="success",
                    stage=3,
                )
                if refreshed.get("auto_render"):
                    # 首尾幀接力時預設微短轉場 (0.25s) 銜接更流暢；保留使用者自訂設定
                    self.start_render(project_id, run_async=True)
            else:
                self.broadcast_state(project_id)

        except InterruptedError:
            pass
        except Exception as exc:
            latest = self.db.get_project(project_id, include_details=False)
            if latest:
                statuses = dict(latest["stage_statuses"])
                statuses["3"] = "failed"
                self.db.update_project(
                    project_id,
                    stage_statuses=statuses,
                    error_message=str(exc),
                )
            self.log_and_broadcast(
                project_id,
                f"❌ Colab ComfyUI 雲端生片中斷：{exc}（已生成的鏡頭均完整保留，修復連線後可點『一鍵全自動生片』從未完成處續跑）",
                level="error",
                stage=3,
            )
        finally:
            with self._lock:
                self._gen_locks.discard(project_id)
                self._gen_stops.pop(project_id, None)
                self._gen_status.pop(project_id, None)
            self.broadcast_state(project_id)

    # -------------------------
    # 終極零人工全自動流水線（Colab CLI 自動開機搶卡 -> 部署 ComfyUI -> 生片 -> 合成 -> 自動關機）
    # -------------------------
    def start_full_auto_colab_pipeline(
        self,
        project_id: str,
        gpu_priority: Optional[List[str]] = None,
        relay_mode: Optional[bool] = None,
        use_turbo: Optional[bool] = None,
        enable_realesrgan: Optional[bool] = None,
        auto_stop: bool = True,
        auto_adapt_l4: bool = True,
        poll_interval_sec: float = 3.5,
        run_async: bool = True,
    ) -> Dict[str, Any]:
        """
        真正零人工干預的完整一條龍：
        1. 檢查 Google Colab OAuth 授權狀態（首次授權一次後永久免手動）
        2. 透過官方 `colab new` 自動依序搶卡（預設 G4 -> A100 -> L4）
        3. 透過 `colab exec` 自動部署 ComfyUI + MiniMax H3 + Cloudflare Tunnel 並自動抓回網址
        4. 自動逐幕首尾幀接力生片並拉回 Mac 產生縮圖
        5. 在 `finally` 區塊保證執行 `colab stop` 秒關機停扣計算單元！
        6. 自動銜接階段 ④ 本機 FFmpeg 1080p 合成並交付至桌面！
        """
        auth_st = get_colab_auth_status()
        if not auth_st.get("cli_installed"):
            raise ValueError("尚未安裝官方 google-colab-cli，請重新啟動導演工作台自動安裝依賴。")
        if not auth_st.get("authorized"):
            raise ValueError(
                "尚未授權 Google Colab 帳號！請先點擊畫面上的「🔑 一鍵授權 Google Colab（僅需首次）」完成 1 次授權。"
            )

        with self._lock:
            if project_id in self._gen_locks:
                return self.enrich_project(self.db.get_project(project_id, include_details=True))  # type: ignore[return-value]

            proj = self.db.get_project(project_id, include_details=True)
            if not proj:
                raise ValueError("找不到指定專案。")

            shots = proj.get("shots") or []
            pending = [s for s in shots if s["status"] != "done"]
            if not pending:
                raise ValueError("所有鏡頭皆已生成完成！可直接點「立即合成」或重設特定鏡頭。")

            cfg = self.db.get_settings()
            eff_relay = (
                bool(relay_mode)
                if relay_mode is not None
                else (cfg.get("comfyui_relay_mode", "true") == "true")
            )
            eff_turbo = (
                bool(use_turbo)
                if use_turbo is not None
                else (cfg.get("comfyui_use_turbo", "false") == "true")
            )
            eff_esrgan = (
                bool(enable_realesrgan)
                if enable_realesrgan is not None
                else (cfg.get("comfyui_enable_realesrgan", "true") == "true")
            )
            eff_gpus = gpu_priority or ["G4", "A100", "L4"]

            if self.watcher.active_project_id == project_id:
                self.watcher.stop()

            session_name = f"director_{project_id[-6:]}"
            stop_ev = threading.Event()
            self._gen_locks.add(project_id)
            self._gen_stops[project_id] = stop_ev
            self._active_colab_sessions[project_id] = session_name
            self._gen_status[project_id] = {
                "current_shot_order": pending[0]["shot_order"],
                "total_targets": len(pending),
                "completed_targets": 0,
                "message": f"正在透過 Colab CLI 自動搶卡開機（順位：{' → '.join(eff_gpus)}）...",
            }

            statuses = dict(proj["stage_statuses"])
            statuses["1"] = "done"
            statuses["2"] = "done"
            statuses["3"] = "running"
            self.db.update_project(
                project_id,
                current_stage=3,
                stage_statuses=statuses,
                watcher_enabled=False,
                error_message="",
            )

        self.log_and_broadcast(
            project_id,
            f"⚡ 啟動『終極零人工全自動拍片』：自動搶卡（{' → '.join(eff_gpus)}）➔ 自動部署 MiniMax H3 ➔ 自動首尾幀接力生片 ➔ 拍完自動關機停扣點！",
            level="info",
            stage=3,
        )

        worker_args = (
            project_id,
            session_name,
            eff_gpus,
            eff_relay,
            eff_turbo,
            eff_esrgan,
            auto_stop,
            auto_adapt_l4,
            poll_interval_sec,
            stop_ev,
        )
        if run_async:
            t = threading.Thread(
                target=self._full_auto_colab_worker,
                args=worker_args,
                name=f"ColabAuto-{project_id}",
                daemon=True,
            )
            t.start()
        else:
            self._full_auto_colab_worker(*worker_args)

        return self.enrich_project(self.db.get_project(project_id, include_details=True))  # type: ignore[return-value]

    def _full_auto_colab_worker(
        self,
        project_id: str,
        session_name: str,
        gpu_priority: List[str],
        relay_mode: bool,
        use_turbo: bool,
        enable_realesrgan: bool,
        auto_stop: bool,
        auto_adapt_l4: bool,
        poll_interval_sec: float,
        stop_ev: threading.Event,
    ) -> None:
        cli_mgr = ColabCLIManager()
        provisioned_gpu: Optional[str] = None
        all_clips_ready = False
        keepalive_stop = threading.Event()

        def _colab_keepalive_loop() -> None:
            # 每 3 分鐘對 Colab Jupyter Kernel 送一次輕量保活，防止多幕生成時被 Colab 誤判閒置回收
            while not keepalive_stop.wait(180):
                if stop_ev.is_set():
                    break
                if provisioned_gpu:
                    cli_mgr.keepalive_ping(session_name)

        def _log(msg: str, lvl: str = "info") -> None:
            self._gen_status[project_id] = {
                **self._gen_status.get(project_id, {}),
                "message": msg,
            }
            self.log_and_broadcast(project_id, msg, level=lvl, stage=3)

        try:
            # 1. 自動搶卡開機（G4 -> A100 -> L4）
            provisioned_gpu = cli_mgr.provision_gpu_with_fallback(
                session_name=session_name,
                gpu_priority=gpu_priority,
                stop_event=stop_ev,
                log_cb=_log,
            )

            # 2. 自動部署 ComfyUI + MiniMax H3 權重 + 擷取 Tunnel 網址
            comfy_url = cli_mgr.bootstrap_comfyui(
                session_name=session_name,
                stop_event=stop_ev,
                log_cb=_log,
            )
            self.db.update_settings({"comfyui_url": comfy_url, "video_api_provider": "comfyui_colab"})

            # 啟動 Colab Kernel 保活執行緒（避免長時間生片時被 Google Colab 閒置回收）
            threading.Thread(
                target=_colab_keepalive_loop,
                name=f"ColabKeepAlive-{project_id}",
                daemon=True,
            ).start()

            # 3. 等待 Cloudflare DNS 生效、連線確認與依 GPU 等級自動安全調優
            client = ComfyUIClient(comfy_url)
            conn_info = client.wait_until_ready(
                max_wait_sec=90.0,
                retry_interval_sec=3.0,
                stop_event=stop_ev,
                progress_cb=lambda m: _log(f"🌐 {m}", "info"),
            )
            vram_gb = float(conn_info.get("vram_total_gb") or 24.0)
            models = conn_info.get("models") or {}

            is_low_vram = (provisioned_gpu == "L4") or (vram_gb < 40.0)
            can_esrgan = enable_realesrgan and bool(conn_info.get("has_realesrgan")) and (not is_low_vram)
            can_turbo = use_turbo and bool(conn_info.get("has_turbo_lora"))

            if is_low_vram and auto_adapt_l4:
                _log(
                    f"💡 偵測到目前分配為 {provisioned_gpu or 'L4'}（{vram_gb} GB VRAM），已自動啟用防 OOM 安全模式（960×544 ＋ 每幕上限 5 秒，本機 FFmpeg 輸出 1080p）。",
                    "info",
                )

            # 4. 全自動逐幕首尾幀接力生片並下載回 Mac
            proj = self.db.get_project(project_id, include_details=True)
            if not proj:
                return
            dirs = self.db.get_project_dirs(project_id)
            target_shots = [s for s in (proj.get("shots") or []) if s["status"] != "done"]

            for idx, shot in enumerate(target_shots, start=1):
                if stop_ev.is_set():
                    raise InterruptedError("使用者已停止全自動拍片。")

                shot_order = int(shot["shot_order"])
                shot_id = int(shot["id"])
                dur_sec = float(shot.get("duration_sec") or 10.0)
                if is_low_vram and auto_adapt_l4 and dur_sec > 6.0:
                    dur_sec = 5.0

                first_frame_remote: Optional[str] = None
                if relay_mode and shot_order > 1:
                    latest_shots = self.db.list_shots(project_id)
                    prev_shot = next(
                        (s for s in latest_shots if int(s["shot_order"]) == shot_order - 1 and s["status"] == "done"),
                        None,
                    )
                    prev_clip = (
                        resolve_project_media_path(prev_shot.get("clip_path") or "", project_id, "clips")
                        if prev_shot
                        else None
                    )
                    if prev_clip:
                        last_frame_local = dirs["temp"] / f"relay_from_shot_{shot_order - 1:02d}.png"
                        extract_last_frame(prev_clip, last_frame_local)
                        remote_fname = f"{project_id}_relay_to_shot_{shot_order:02d}.png"
                        first_frame_remote = client.upload_image(last_frame_local, remote_name=remote_fname)
                        _log(f"🔗 已擷取 Shot {shot_order - 1:02d} 尾幀並上傳，接力生成 Shot {shot_order:02d}！", "info")

                self._gen_status[project_id] = {
                    "current_shot_order": shot_order,
                    "current_shot_title": shot["title"],
                    "total_targets": len(target_shots),
                    "completed_targets": idx - 1,
                    "message": f"正在雲端生成第 {idx}/{len(target_shots)} 幕：Shot {shot_order:02d}（{dur_sec:.0f}秒）...",
                }
                _log(
                    f"🚀 [{provisioned_gpu}] 開始生成 Shot {shot_order:02d}「{shot['title']}」（{dur_sec:.0f}秒｜{'首尾幀接力' if first_frame_remote else '首幕建立'}）...",
                    "info",
                )

                wf = client.build_minimax_h3_workflow(
                    prompt_en=shot["prompt_en"],
                    duration_sec=dur_sec,
                    aspect_ratio=proj["aspect_ratio"],
                    first_frame_image=first_frame_remote,
                    use_turbo_lora=can_turbo,
                    enable_realesrgan_inline=can_esrgan,
                    models=models,
                    filename_prefix=f"video/{project_id}_shot_{shot_order:02d}",
                    vram_gb=vram_gb,
                )

                dest_clip = dirs["clips"] / f"shot_{shot_order:02d}.mp4"
                client.queue_and_download_video(
                    workflow=wf,
                    dest_mp4_path=dest_clip,
                    stop_event=stop_ev,
                    poll_interval_sec=poll_interval_sec,
                    progress_cb=lambda m: _log(f"⏳ Shot {shot_order:02d}：{m}", "info"),
                )

                meta = probe_video(dest_clip)
                dest_thumb = dirs["thumbs"] / f"shot_{shot_order:02d}.jpg"
                extract_thumbnail(dest_clip, dest_thumb, duration_hint=meta["duration"])

                self.db.update_shot(
                    shot_id,
                    status="done",
                    clip_path=str(dest_clip),
                    thumb_path=str(dest_thumb),
                    source_filename=f"Colab_{provisioned_gpu}_Shot_{shot_order:02d}.mp4",
                    actual_duration=meta["duration"],
                    width=meta["width"],
                    height=meta["height"],
                    has_audio=meta["has_audio"],
                )
                self.log_and_broadcast(
                    project_id,
                    f"✅ 完成 Shot {shot_order:02d}「{shot['title']}」（{meta['duration']:.1f}秒, {meta['width']}×{meta['height']}）並已拉回本機產生縮圖！",
                    level="success",
                    stage=3,
                    event_type="shot_captured",
                )

            refreshed = self.db.get_project(project_id, include_details=True)
            if refreshed and all(s["status"] == "done" for s in (refreshed.get("shots") or [])):
                all_clips_ready = True
                statuses = dict(refreshed["stage_statuses"])
                statuses["3"] = "done"
                self.db.update_project(project_id, stage_statuses=statuses, error_message="")
                self.log_and_broadcast(
                    project_id,
                    "🎉 全部雲端鏡頭已自動生成拉回本機！馬上自動關閉 Colab GPU 省算力並啟動本機 1080p 剪輯合成...",
                    level="success",
                    stage=3,
                )

        except InterruptedError:
            pass
        except Exception as exc:
            latest = self.db.get_project(project_id, include_details=False)
            if latest:
                statuses = dict(latest["stage_statuses"])
                statuses["3"] = "failed"
                self.db.update_project(project_id, stage_statuses=statuses, error_message=str(exc))
            self.log_and_broadcast(
                project_id,
                f"❌ 全自動雲端拍片中斷：{exc}（已完成鏡頭均完整保留，雲端機器將自動關閉避免白扣點）",
                level="error",
                stage=3,
            )
        finally:
            keepalive_stop.set()
            # 保證無論成功、失敗或手動中斷，皆自動關閉 Colab Runtime 停止扣計算單元！
            if auto_stop and provisioned_gpu:
                cli_mgr.stop_session(
                    session_name,
                    log_cb=lambda m, lvl: self.log_and_broadcast(project_id, m, level=lvl, stage=3),
                )
            with self._lock:
                self._gen_locks.discard(project_id)
                self._gen_stops.pop(project_id, None)
                self._gen_status.pop(project_id, None)
                self._active_colab_sessions.pop(project_id, None)
            self.broadcast_state(project_id)

        # 雲端已安全關機後，再啟動本機 FFmpeg 階段 ④ 剪輯合成 -> 階段 ⑤ 交付！
        if all_clips_ready:
            refreshed2 = self.db.get_project(project_id, include_details=False)
            if refreshed2 and refreshed2.get("auto_render"):
                self.start_render(project_id, run_async=True)
