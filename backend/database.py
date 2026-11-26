"""SQLite 狀態持久化模組（管理 projects, shots, logs, app_settings）。"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

from .settings import ensure_base_dirs, get_db_path, get_projects_dir

_db_lock = threading.RLock()

DEFAULT_STAGE_STATUSES = {
    "1": "done",
    "2": "running",
    "3": "pending",
    "4": "pending",
    "5": "pending",
}

DEFAULT_RENDER_OPTIONS = {
    "fps": 24,
    "transition_sec": 0.5,
    "fade_in_sec": 0.8,
    "fade_out_sec": 1.0,
    "align_duration": False,
}

DEFAULT_RENDER_PROGRESS = {
    "percent": 0,
    "substep": "等待開始",
    "detail": "",
}

DEFAULT_OUTPUT_META = {
    "output_path": "",
    "desktop_path": "",
    "duration": 0.0,
    "width": 0,
    "height": 0,
    "fps": 0.0,
    "file_size": 0,
    "has_audio": False,
    "rendered_at": "",
}


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def now_time_str() -> str:
    return datetime.now().strftime("%H:%M:%S")


class Database:
    """輕量執行緒安全的 SQLite 存取封裝。"""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        ensure_base_dirs()
        self.db_path = Path(db_path) if db_path else get_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def connect(self) -> Generator[sqlite3.Connection, None, None]:
        with _db_lock:
            conn = sqlite3.connect(str(self.db_path), timeout=30.0, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    brief TEXT NOT NULL,
                    target_duration INTEGER NOT NULL DEFAULT 60,
                    aspect_ratio TEXT NOT NULL DEFAULT '16:9',
                    visual_style TEXT NOT NULL DEFAULT '35mm_doc',
                    audio_mode TEXT NOT NULL DEFAULT 'mute',
                    planner_mode TEXT NOT NULL DEFAULT 'paste',
                    style_lock TEXT NOT NULL DEFAULT '',
                    negative_prompt TEXT NOT NULL DEFAULT '',
                    current_stage INTEGER NOT NULL DEFAULT 1,
                    stage_statuses_json TEXT NOT NULL,
                    watcher_enabled INTEGER NOT NULL DEFAULT 0,
                    auto_render INTEGER NOT NULL DEFAULT 1,
                    render_options_json TEXT NOT NULL,
                    render_progress_json TEXT NOT NULL,
                    output_meta_json TEXT NOT NULL,
                    error_message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS shots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL,
                    shot_order INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    duration_sec REAL NOT NULL DEFAULT 8.0,
                    scene_desc_zh TEXT NOT NULL DEFAULT '',
                    voiceover_zh TEXT NOT NULL DEFAULT '',
                    prompt_en TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    clip_path TEXT NOT NULL DEFAULT '',
                    thumb_path TEXT NOT NULL DEFAULT '',
                    ref_image_path TEXT NOT NULL DEFAULT '',
                    camera_motion TEXT NOT NULL DEFAULT 'slow_push_in',
                    source_filename TEXT NOT NULL DEFAULT '',
                    actual_duration REAL NOT NULL DEFAULT 0.0,
                    width INTEGER NOT NULL DEFAULT 0,
                    height INTEGER NOT NULL DEFAULT 0,
                    has_audio INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_shots_project_order
                ON shots(project_id, shot_order);

                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL,
                    level TEXT NOT NULL DEFAULT 'info',
                    stage INTEGER NOT NULL DEFAULT 0,
                    time_str TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_logs_project_id
                ON logs(project_id, id);

                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            # 平滑升級既有的 studio.db（零資料遺失）
            existing_cols = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(shots)").fetchall()
            }
            if "ref_image_path" not in existing_cols:
                conn.execute(
                    "ALTER TABLE shots ADD COLUMN ref_image_path TEXT NOT NULL DEFAULT ''"
                )
            if "camera_motion" not in existing_cols:
                conn.execute(
                    "ALTER TABLE shots ADD COLUMN camera_motion TEXT NOT NULL DEFAULT 'slow_push_in'"
                )

    # -------------------------
    # 專案資料夾輔助
    # -------------------------
    def get_project_dirs(self, project_id: str) -> Dict[str, Path]:
        root = get_projects_dir() / project_id
        dirs = {
            "root": root,
            "clips": root / "clips",
            "thumbs": root / "thumbs",
            "refs": root / "refs",
            "temp": root / "temp",
            "output": root / "output",
        }
        for p in dirs.values():
            p.mkdir(parents=True, exist_ok=True)
        return dirs

    # -------------------------
    # 系統設定 CRUD
    # -------------------------
    def get_settings(self) -> Dict[str, str]:
        defaults = {
            "gemini_api_key": "",
            "gemini_model": "gemini-2.5-flash",
            "ollama_base_url": "http://127.0.0.1:11434",
            "ollama_model": "qwen2.5:7b",
            "downloads_dir": "",
            "desktop_output_dir": "",
            "video_api_provider": "comfyui_colab",
            "video_api_key": "",
            "comfyui_url": "",
            "comfyui_use_turbo": "false",
            "comfyui_enable_realesrgan": "true",
            "comfyui_relay_mode": "false",
        }
        with self.connect() as conn:
            rows = conn.execute("SELECT key, value FROM app_settings").fetchall()
            for r in rows:
                defaults[r["key"]] = r["value"]
        return defaults

    def update_settings(self, updates: Dict[str, Any]) -> Dict[str, str]:
        with self.connect() as conn:
            for k, v in updates.items():
                if v is None:
                    continue
                conn.execute(
                    """
                    INSERT INTO app_settings (key, value) VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (str(k), str(v)),
                )
        return self.get_settings()

    # -------------------------
    # Projects CRUD
    # -------------------------
    def _deserialize_project(self, row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        d["stage_statuses"] = json.loads(d.pop("stage_statuses_json") or "{}")
        d["render_options"] = json.loads(d.pop("render_options_json") or "{}")
        d["render_progress"] = json.loads(d.pop("render_progress_json") or "{}")
        d["output_meta"] = json.loads(d.pop("output_meta_json") or "{}")
        d["watcher_enabled"] = bool(d["watcher_enabled"])
        d["auto_render"] = bool(d["auto_render"])
        return d

    def create_project(
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
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        short_uid = uuid.uuid4().hex[:6]
        project_id = f"proj_{ts}_{short_uid}"
        clean_brief = brief.strip()
        if not name or not name.strip():
            # 智慧擷取簡潔專案標題（移除常見口語與格式尾句，避免硬切斷字）
            cleaned = clean_brief
            for noise_phrase in (
                "我要做一條",
                "我要做一支",
                "幫我做一條",
                "幫我做一支",
                "無聲方便後期配音",
                "純畫面無聲",
                "方便後期自己配音",
                "方便後期配音",
                "無聲方便後期配樂",
            ):
                cleaned = cleaned.replace(noise_phrase, "")
            # 取首個完整子句，移除開頭多餘標點
            parts = [p.strip(" ，。、！？,.!?") for p in cleaned.replace("，", ",").replace("。", ",").split(",") if p.strip(" ，。、！？,.!?")]
            candidate = parts[0] if parts else clean_brief
            if len(candidate) > 20:
                candidate = candidate[:20].rstrip(" ，。、！？,.!?的與和")
            name = candidate if candidate else f"紀錄片專案_{short_uid}"

        now = now_iso()
        render_opts = dict(DEFAULT_RENDER_OPTIONS)
        stage_statuses = dict(DEFAULT_STAGE_STATUSES)

        self.get_project_dirs(project_id)

        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO projects (
                    id, name, brief, target_duration, aspect_ratio, visual_style,
                    audio_mode, planner_mode, style_lock, negative_prompt,
                    current_stage, stage_statuses_json, watcher_enabled, auto_render,
                    render_options_json, render_progress_json, output_meta_json,
                    error_message, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', '', 2, ?, 0, ?, ?, ?, ?, '', ?, ?)
                """,
                (
                    project_id,
                    name.strip(),
                    clean_brief,
                    int(target_duration),
                    aspect_ratio,
                    visual_style,
                    audio_mode,
                    planner_mode,
                    json.dumps(stage_statuses, ensure_ascii=False),
                    1 if auto_render else 0,
                    json.dumps(render_opts, ensure_ascii=False),
                    json.dumps(DEFAULT_RENDER_PROGRESS, ensure_ascii=False),
                    json.dumps(DEFAULT_OUTPUT_META, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        return self.get_project(project_id, include_details=True)  # type: ignore[return-value]

    def list_projects(self) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM projects ORDER BY updated_at DESC, created_at DESC"
            ).fetchall()
            result = []
            for r in rows:
                p = self._deserialize_project(r)
                counts = conn.execute(
                    """
                    SELECT COUNT(*) AS total,
                           SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) AS done_cnt
                    FROM shots WHERE project_id = ?
                    """,
                    (p["id"],),
                ).fetchone()
                p["total_shots"] = int(counts["total"] or 0)
                p["done_shots"] = int(counts["done_cnt"] or 0)
                result.append(p)
            return result

    def get_project(self, project_id: str, include_details: bool = True) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
            if not row:
                return None
            proj = self._deserialize_project(row)
            if include_details:
                proj["shots"] = self.list_shots(project_id, conn=conn)
                proj["logs"] = self.list_logs(project_id, limit=200, conn=conn)
            return proj

    def update_project(self, project_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
        if not fields:
            return self.get_project(project_id, include_details=True)

        allowed_direct = {
            "name",
            "brief",
            "target_duration",
            "aspect_ratio",
            "visual_style",
            "audio_mode",
            "planner_mode",
            "style_lock",
            "negative_prompt",
            "current_stage",
            "error_message",
        }
        assignments: List[str] = []
        values: List[Any] = []

        for k, v in fields.items():
            if k in allowed_direct:
                assignments.append(f"{k} = ?")
                values.append(v)
            elif k == "watcher_enabled":
                assignments.append("watcher_enabled = ?")
                values.append(1 if v else 0)
            elif k == "auto_render":
                assignments.append("auto_render = ?")
                values.append(1 if v else 0)
            elif k == "stage_statuses":
                assignments.append("stage_statuses_json = ?")
                values.append(json.dumps(v, ensure_ascii=False))
            elif k == "render_options":
                assignments.append("render_options_json = ?")
                values.append(json.dumps(v, ensure_ascii=False))
            elif k == "render_progress":
                assignments.append("render_progress_json = ?")
                values.append(json.dumps(v, ensure_ascii=False))
            elif k == "output_meta":
                assignments.append("output_meta_json = ?")
                values.append(json.dumps(v, ensure_ascii=False))

        if assignments:
            assignments.append("updated_at = ?")
            values.append(now_iso())
            values.append(project_id)
            sql = f"UPDATE projects SET {', '.join(assignments)} WHERE id = ?"
            with self.connect() as conn:
                conn.execute(sql, values)

        return self.get_project(project_id, include_details=True)

    def delete_project(self, project_id: str) -> bool:
        with self.connect() as conn:
            cur = conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
            return cur.rowcount > 0

    # -------------------------
    # Shots CRUD
    # -------------------------
    def _deserialize_shot(self, row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        d["has_audio"] = bool(d["has_audio"])
        return d

    def list_shots(
        self, project_id: str, conn: Optional[sqlite3.Connection] = None
    ) -> List[Dict[str, Any]]:
        if conn is not None:
            rows = conn.execute(
                "SELECT * FROM shots WHERE project_id = ? ORDER BY shot_order ASC, id ASC",
                (project_id,),
            ).fetchall()
            return [self._deserialize_shot(r) for r in rows]
        with self.connect() as c:
            rows = c.execute(
                "SELECT * FROM shots WHERE project_id = ? ORDER BY shot_order ASC, id ASC",
                (project_id,),
            ).fetchall()
            return [self._deserialize_shot(r) for r in rows]

    def get_shot(self, shot_id: int) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM shots WHERE id = ?", (shot_id,)).fetchone()
            return self._deserialize_shot(row) if row else None

    def replace_shots(self, project_id: str, shots_data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """完整替換某專案的分鏡表（用於階段 ② 初次生成或重新解析貼上分鏡）。"""
        now = now_iso()
        with self.connect() as conn:
            conn.execute("DELETE FROM shots WHERE project_id = ?", (project_id,))
            for idx, s in enumerate(shots_data, start=1):
                conn.execute(
                    """
                    INSERT INTO shots (
                        project_id, shot_order, title, duration_sec,
                        scene_desc_zh, voiceover_zh, prompt_en,
                        status, clip_path, thumb_path, ref_image_path, camera_motion,
                        source_filename, actual_duration, width, height, has_audio, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        project_id,
                        int(s.get("shot_order", idx)),
                        str(s.get("title", f"鏡頭 {idx:02d}")).strip(),
                        float(s.get("duration_sec", 8.0)),
                        str(s.get("scene_desc_zh", "")).strip(),
                        str(s.get("voiceover_zh", "")).strip(),
                        str(s.get("prompt_en", "")).strip(),
                        str(s.get("status", "pending")),
                        str(s.get("clip_path", "")),
                        str(s.get("thumb_path", "")),
                        str(s.get("ref_image_path", "")),
                        str(s.get("camera_motion", "slow_push_in") or "slow_push_in"),
                        str(s.get("source_filename", "")),
                        float(s.get("actual_duration", 0.0)),
                        int(s.get("width", 0)),
                        int(s.get("height", 0)),
                        1 if s.get("has_audio") else 0,
                        now,
                    ),
                )
            conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (now, project_id))
            return self.list_shots(project_id, conn=conn)

    def add_shot(self, project_id: str, shot_data: Dict[str, Any]) -> Dict[str, Any]:
        now = now_iso()
        with self.connect() as conn:
            max_row = conn.execute(
                "SELECT COALESCE(MAX(shot_order), 0) AS m FROM shots WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            next_order = int(max_row["m"]) + 1
            cur = conn.execute(
                """
                INSERT INTO shots (
                    project_id, shot_order, title, duration_sec,
                    scene_desc_zh, voiceover_zh, prompt_en,
                    ref_image_path, camera_motion, status, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    project_id,
                    int(shot_data.get("shot_order", next_order)),
                    str(shot_data.get("title", f"新增鏡頭 {next_order:02d}")),
                    float(shot_data.get("duration_sec", 8.0)),
                    str(shot_data.get("scene_desc_zh", "")),
                    str(shot_data.get("voiceover_zh", "")),
                    str(shot_data.get("prompt_en", "")),
                    str(shot_data.get("ref_image_path", "")),
                    str(shot_data.get("camera_motion", "slow_push_in") or "slow_push_in"),
                    now,
                ),
            )
            conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (now, project_id))
            row = conn.execute("SELECT * FROM shots WHERE id = ?", (cur.lastrowid,)).fetchone()
            return self._deserialize_shot(row)

    def update_shot(self, shot_id: int, **fields: Any) -> Optional[Dict[str, Any]]:
        allowed = {
            "shot_order",
            "title",
            "duration_sec",
            "scene_desc_zh",
            "voiceover_zh",
            "prompt_en",
            "status",
            "clip_path",
            "thumb_path",
            "ref_image_path",
            "camera_motion",
            "source_filename",
            "actual_duration",
            "width",
            "height",
        }
        assignments: List[str] = []
        values: List[Any] = []
        for k, v in fields.items():
            if k in allowed:
                assignments.append(f"{k} = ?")
                values.append(v)
            elif k == "has_audio":
                assignments.append("has_audio = ?")
                values.append(1 if v else 0)

        if not assignments:
            return self.get_shot(shot_id)

        now = now_iso()
        assignments.append("updated_at = ?")
        values.append(now)
        values.append(shot_id)

        with self.connect() as conn:
            conn.execute(f"UPDATE shots SET {', '.join(assignments)} WHERE id = ?", values)
            row = conn.execute("SELECT * FROM shots WHERE id = ?", (shot_id,)).fetchone()
            if row:
                conn.execute(
                    "UPDATE projects SET updated_at = ? WHERE id = ?",
                    (now, row["project_id"]),
                )
                return self._deserialize_shot(row)
            return None

    def delete_shot(self, project_id: str, shot_id: int) -> List[Dict[str, Any]]:
        now = now_iso()
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM shots WHERE id = ? AND project_id = ?", (shot_id, project_id)
            )
            # 重新連續編號 shot_order
            rows = conn.execute(
                "SELECT id FROM shots WHERE project_id = ? ORDER BY shot_order ASC, id ASC",
                (project_id,),
            ).fetchall()
            for idx, r in enumerate(rows, start=1):
                conn.execute("UPDATE shots SET shot_order = ? WHERE id = ?", (idx, r["id"]))
            conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (now, project_id))
            return self.list_shots(project_id, conn=conn)

    def reorder_shots(self, project_id: str, ordered_shot_ids: List[int]) -> List[Dict[str, Any]]:
        now = now_iso()
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT id FROM shots WHERE project_id = ? ORDER BY shot_order ASC",
                (project_id,),
            ).fetchall()
            existing_ids = [r["id"] for r in existing]
            # 確保未列出的 id 仍保留在尾端
            final_order = [sid for sid in ordered_shot_ids if sid in existing_ids]
            for sid in existing_ids:
                if sid not in final_order:
                    final_order.append(sid)
            for idx, sid in enumerate(final_order, start=1):
                conn.execute(
                    "UPDATE shots SET shot_order = ?, updated_at = ? WHERE id = ? AND project_id = ?",
                    (idx, now, sid, project_id),
                )
            conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (now, project_id))
            return self.list_shots(project_id, conn=conn)

    # -------------------------
    # Logs CRUD
    # -------------------------
    def add_log(
        self,
        project_id: str,
        message: str,
        level: str = "info",
        stage: int = 0,
    ) -> Dict[str, Any]:
        t_str = now_time_str()
        c_at = now_iso()
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO logs (project_id, level, stage, time_str, message, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (project_id, level, int(stage), t_str, message, c_at),
            )
            row = conn.execute("SELECT * FROM logs WHERE id = ?", (cur.lastrowid,)).fetchone()
            return dict(row)

    def list_logs(
        self,
        project_id: str,
        limit: int = 200,
        conn: Optional[sqlite3.Connection] = None,
    ) -> List[Dict[str, Any]]:
        sql = """
            SELECT * FROM (
                SELECT * FROM logs WHERE project_id = ? ORDER BY id DESC LIMIT ?
            ) sub ORDER BY id ASC
        """
        if conn is not None:
            rows = conn.execute(sql, (project_id, limit)).fetchall()
            return [dict(r) for r in rows]
        with self.connect() as c:
            rows = c.execute(sql, (project_id, limit)).fetchall()
            return [dict(r) for r in rows]
