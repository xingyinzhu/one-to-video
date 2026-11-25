"""FastAPI 入口、REST API 路由、SSE 即時推播與 macOS Finder 整合。"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional
from urllib.parse import quote

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .colab_automation import (
    complete_colab_oauth_flow,
    get_colab_auth_status,
    start_colab_oauth_flow,
)
from .orchestrator import StudioOrchestrator
from .settings import (
    BASE_DIR,
    FRONTEND_DIR,
    SERVER_HOST,
    SERVER_PORT,
    resolve_project_media_path,
)

orchestrator = StudioOrchestrator()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    yield
    orchestrator.watcher.stop()


app = FastAPI(
    title="AI 紀錄片導演工作台 (One-Sentence to Video)",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

if FRONTEND_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


# -------------------------
# Request Schemas
# -------------------------
class CreateProjectRequest(BaseModel):
    brief: str = Field(..., min_length=2, description="一句話影片需求描述")
    name: Optional[str] = None
    target_duration: int = 60
    aspect_ratio: str = "16:9"
    visual_style: str = "35mm_doc"
    audio_mode: str = "mute"
    planner_mode: str = "paste"
    auto_render: bool = True
    full_auto_colab: bool = False
    gpu_priority: Optional[List[str]] = None


class UpdateProjectRequest(BaseModel):
    name: Optional[str] = None
    brief: Optional[str] = None
    target_duration: Optional[int] = None
    aspect_ratio: Optional[str] = None
    visual_style: Optional[str] = None
    audio_mode: Optional[str] = None
    planner_mode: Optional[str] = None
    style_lock: Optional[str] = None
    negative_prompt: Optional[str] = None
    current_stage: Optional[int] = None
    auto_render: Optional[bool] = None
    render_options: Optional[Dict[str, Any]] = None


class GenerateStoryboardRequest(BaseModel):
    mode: Optional[str] = None


class ParseStoryboardRequest(BaseModel):
    raw_text: str


class ConfirmStoryboardRequest(BaseModel):
    start_watcher: bool = True


class AddShotRequest(BaseModel):
    title: str = "新增鏡頭"
    duration_sec: float = 8.0
    scene_desc_zh: str = ""
    voiceover_zh: str = ""
    prompt_en: str = ""


class UpdateShotRequest(BaseModel):
    title: Optional[str] = None
    duration_sec: Optional[float] = None
    scene_desc_zh: Optional[str] = None
    voiceover_zh: Optional[str] = None
    prompt_en: Optional[str] = None


class ReorderShotsRequest(BaseModel):
    shot_ids: List[int]


class WatcherToggleRequest(BaseModel):
    enabled: bool
    target_shot_id: Optional[int] = None


class ImportDownloadsRequest(BaseModel):
    count: Optional[int] = None


class RenderRequest(BaseModel):
    aspect_ratio: Optional[str] = None
    audio_mode: Optional[str] = None
    fps: Optional[int] = None
    transition_sec: Optional[float] = None
    fade_in_sec: Optional[float] = None
    fade_out_sec: Optional[float] = None
    align_duration: Optional[bool] = None


class ComfyUITestRequest(BaseModel):
    url: str


class ComfyUIGenerateRequest(BaseModel):
    comfyui_url: Optional[str] = None
    relay_mode: Optional[bool] = None
    use_turbo: Optional[bool] = None
    enable_realesrgan: Optional[bool] = None
    only_shot_id: Optional[int] = None


class ColabAuthCompleteRequest(BaseModel):
    code: str


class ColabAutoRunRequest(BaseModel):
    gpu_priority: Optional[List[str]] = None
    relay_mode: Optional[bool] = None
    use_turbo: Optional[bool] = None
    enable_realesrgan: Optional[bool] = None
    auto_stop: bool = True
    auto_adapt_l4: bool = True


# -------------------------
# 前端首頁
# -------------------------
@app.get("/", response_class=HTMLResponse)
async def serve_index() -> HTMLResponse:
    index_file = FRONTEND_DIR / "index.html"
    if not index_file.is_file():
        return HTMLResponse("<h1>前端檔案尚未建立</h1>", status_code=404)
    return HTMLResponse(index_file.read_text(encoding="utf-8"))


# -------------------------
# 系統狀態與設定 API
# -------------------------
@app.get("/api/system/status")
async def api_system_status() -> Dict[str, Any]:
    return orchestrator.get_system_status()


@app.get("/api/settings")
async def api_get_settings() -> Dict[str, Any]:
    cfg = orchestrator.db.get_settings()
    # 前端顯示時回傳設定（本機單人工具，方便檢視與修改）
    return {
        "settings": cfg,
        "system": orchestrator.get_system_status(),
    }


@app.put("/api/settings")
async def api_update_settings(payload: Dict[str, Any]) -> Dict[str, Any]:
    updated = orchestrator.db.update_settings(payload)
    return {
        "settings": updated,
        "system": orchestrator.get_system_status(),
    }


# -------------------------
# 專案管理 API
# -------------------------
@app.get("/api/projects")
async def api_list_projects() -> Dict[str, Any]:
    return {"projects": orchestrator.db.list_projects()}


@app.post("/api/projects")
async def api_create_project(req: CreateProjectRequest) -> Dict[str, Any]:
    try:
        proj = orchestrator.create_project_and_plan(
            brief=req.brief,
            name=req.name,
            target_duration=req.target_duration,
            aspect_ratio=req.aspect_ratio,
            visual_style=req.visual_style,
            audio_mode=req.audio_mode,
            planner_mode=req.planner_mode,
            auto_render=req.auto_render,
        )
        if req.full_auto_colab:
            orchestrator.confirm_storyboard(proj["id"], start_watcher=False)
            proj = orchestrator.start_full_auto_colab_pipeline(
                project_id=proj["id"],
                gpu_priority=req.gpu_priority,
                auto_stop=True,
                run_async=True,
            )
        return {"project": proj}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/projects/{project_id}")
async def api_get_project(project_id: str) -> Dict[str, Any]:
    proj = orchestrator.enrich_project(
        orchestrator.db.get_project(project_id, include_details=True)
    )
    if not proj:
        raise HTTPException(status_code=404, detail="找不到此專案")
    return {"project": proj}


@app.patch("/api/projects/{project_id}")
async def api_update_project(
    project_id: str, req: UpdateProjectRequest
) -> Dict[str, Any]:
    existing = orchestrator.db.get_project(project_id, include_details=False)
    if not existing:
        raise HTTPException(status_code=404, detail="找不到此專案")

    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    if "render_options" in updates:
        merged_opts = dict(existing["render_options"])
        merged_opts.update(updates["render_options"])
        updates["render_options"] = merged_opts

    orchestrator.db.update_project(project_id, **updates)
    orchestrator.broadcast_state(project_id)
    proj = orchestrator.enrich_project(
        orchestrator.db.get_project(project_id, include_details=True)
    )
    return {"project": proj}


@app.delete("/api/projects/{project_id}")
async def api_delete_project(project_id: str) -> Dict[str, Any]:
    if orchestrator.watcher.active_project_id == project_id:
        orchestrator.watcher.stop()
    dirs = orchestrator.db.get_project_dirs(project_id)
    deleted = orchestrator.db.delete_project(project_id)
    if dirs["root"].is_dir():
        shutil.rmtree(dirs["root"], ignore_errors=True)
    if not deleted:
        raise HTTPException(status_code=404, detail="找不到此專案")
    return {"ok": True}


# -------------------------
# 階段 ②：分鏡規劃與編修 API
# -------------------------
@app.post("/api/projects/{project_id}/storyboard/generate")
async def api_generate_storyboard(
    project_id: str, req: GenerateStoryboardRequest
) -> Dict[str, Any]:
    try:
        proj = orchestrator.trigger_storyboard_generation(
            project_id, mode=req.mode, run_async=(req.mode not in (None, "paste"))
        )
        return {"project": proj}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/projects/{project_id}/storyboard/parse")
async def api_parse_storyboard(
    project_id: str, req: ParseStoryboardRequest
) -> Dict[str, Any]:
    try:
        proj = orchestrator.apply_pasted_storyboard(project_id, req.raw_text)
        return {"project": proj}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/projects/{project_id}/storyboard/confirm")
async def api_confirm_storyboard(
    project_id: str, req: ConfirmStoryboardRequest
) -> Dict[str, Any]:
    try:
        proj = orchestrator.confirm_storyboard(
            project_id, start_watcher=req.start_watcher
        )
        return {"project": proj}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/projects/{project_id}/shots")
async def api_add_shot(project_id: str, req: AddShotRequest) -> Dict[str, Any]:
    proj = orchestrator.db.get_project(project_id, include_details=False)
    if not proj:
        raise HTTPException(status_code=404, detail="找不到此專案")
    orchestrator.db.add_shot(project_id, req.model_dump())
    orchestrator.log_and_broadcast(project_id, f"已新增分鏡「{req.title}」。", stage=2)
    return {
        "project": orchestrator.enrich_project(
            orchestrator.db.get_project(project_id, include_details=True)
        )
    }


@app.patch("/api/projects/{project_id}/shots/{shot_id}")
async def api_update_shot(
    project_id: str, shot_id: int, req: UpdateShotRequest
) -> Dict[str, Any]:
    shot = orchestrator.db.get_shot(shot_id)
    if not shot or shot["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="找不到此分鏡")
    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    orchestrator.db.update_shot(shot_id, **updates)
    orchestrator.broadcast_state(project_id)
    return {
        "project": orchestrator.enrich_project(
            orchestrator.db.get_project(project_id, include_details=True)
        )
    }


@app.delete("/api/projects/{project_id}/shots/{shot_id}")
async def api_delete_shot(project_id: str, shot_id: int) -> Dict[str, Any]:
    shot = orchestrator.db.get_shot(shot_id)
    if not shot or shot["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="找不到此分鏡")
    orchestrator.db.delete_shot(project_id, shot_id)
    orchestrator.log_and_broadcast(
        project_id, f"已刪除鏡頭「{shot['title']}」並重新編號。", stage=2
    )
    return {
        "project": orchestrator.enrich_project(
            orchestrator.db.get_project(project_id, include_details=True)
        )
    }


@app.post("/api/projects/{project_id}/shots/reorder")
async def api_reorder_shots(
    project_id: str, req: ReorderShotsRequest
) -> Dict[str, Any]:
    orchestrator.db.reorder_shots(project_id, req.shot_ids)
    orchestrator.log_and_broadcast(project_id, "已更新分鏡排列順序。", stage=2)
    return {
        "project": orchestrator.enrich_project(
            orchestrator.db.get_project(project_id, include_details=True)
        )
    }


# -------------------------
# 階段 ③：監聽器、手動上傳替換與重抽 API
# -------------------------
@app.post("/api/projects/{project_id}/watcher/toggle")
async def api_toggle_watcher(
    project_id: str, req: WatcherToggleRequest
) -> Dict[str, Any]:
    try:
        if req.enabled:
            proj = orchestrator.start_watcher(
                project_id, target_shot_id=req.target_shot_id
            )
        else:
            proj = orchestrator.stop_watcher(project_id)
        return {"project": proj}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/projects/{project_id}/shots/{shot_id}/upload")
async def api_upload_shot_clip(
    project_id: str, shot_id: int, file: UploadFile = File(...)
) -> Dict[str, Any]:
    orig_name = file.filename or "uploaded_clip.mp4"
    suffix = Path(orig_name).suffix.lower() or ".mp4"
    if suffix not in (".mp4", ".mov", ".webm", ".mkv"):
        raise HTTPException(status_code=400, detail="僅支援 .mp4 / .mov / .webm / .mkv 影片格式")

    tmp_fd, tmp_path_str = tempfile.mkstemp(suffix=suffix)
    tmp_path = Path(tmp_path_str)
    try:
        with open(tmp_fd, "wb") as f_out:
            while chunk := await file.read(1024 * 1024):
                f_out.write(chunk)
        proj = orchestrator.assign_uploaded_or_local_clip(
            project_id=project_id,
            shot_id=shot_id,
            src_path=tmp_path,
            original_filename=orig_name,
        )
        return {"project": proj}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


@app.post("/api/projects/{project_id}/shots/{shot_id}/reset")
async def api_reset_shot(project_id: str, shot_id: int) -> Dict[str, Any]:
    try:
        proj = orchestrator.reset_shot(project_id, shot_id)
        return {"project": proj}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/projects/{project_id}/import-downloads")
async def api_import_downloads(
    project_id: str, req: ImportDownloadsRequest
) -> Dict[str, Any]:
    try:
        proj = orchestrator.import_latest_from_downloads(
            project_id, max_count=req.count
        )
        return {"project": proj}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# -------------------------
# 階段 ③ 升級：Colab G4 ComfyUI (MiniMax H3 + RealESRGAN) 全自動生片 API
# -------------------------
@app.post("/api/comfyui/test")
async def api_test_comfyui(req: ComfyUITestRequest) -> Dict[str, Any]:
    try:
        info = orchestrator.test_comfyui_connection(req.url)
        return {"ok": True, "comfyui": info}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"無法連線至 Colab ComfyUI：{exc}") from exc


@app.post("/api/projects/{project_id}/comfyui/generate")
async def api_start_comfyui_generate(
    project_id: str, req: ComfyUIGenerateRequest
) -> Dict[str, Any]:
    try:
        proj = orchestrator.start_comfyui_generation(
            project_id=project_id,
            comfyui_url=req.comfyui_url,
            relay_mode=req.relay_mode,
            use_turbo=req.use_turbo,
            enable_realesrgan=req.enable_realesrgan,
            only_shot_id=req.only_shot_id,
            run_async=True,
        )
        return {"project": proj}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/projects/{project_id}/comfyui/stop")
async def api_stop_comfyui_generate(project_id: str) -> Dict[str, Any]:
    try:
        proj = orchestrator.stop_comfyui_generation(project_id)
        return {"project": proj}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# -------------------------
# 終極零人工全自動：Google Colab CLI 一次性網頁授權 ＋ 自動開機/生片/關機 API
# -------------------------
@app.get("/api/colab/auth/status")
async def api_colab_auth_status() -> Dict[str, Any]:
    return get_colab_auth_status()


@app.post("/api/colab/auth/start")
async def api_colab_auth_start() -> Dict[str, Any]:
    try:
        return start_colab_oauth_flow()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/colab/auth/complete")
async def api_colab_auth_complete(req: ColabAuthCompleteRequest) -> Dict[str, Any]:
    try:
        return complete_colab_oauth_flow(req.code)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"授權失敗：{exc}") from exc


@app.post("/api/projects/{project_id}/colab/auto-run")
async def api_colab_auto_run(
    project_id: str, req: ColabAutoRunRequest
) -> Dict[str, Any]:
    try:
        proj = orchestrator.start_full_auto_colab_pipeline(
            project_id=project_id,
            gpu_priority=req.gpu_priority,
            relay_mode=req.relay_mode,
            use_turbo=req.use_turbo,
            enable_realesrgan=req.enable_realesrgan,
            auto_stop=req.auto_stop,
            auto_adapt_l4=req.auto_adapt_l4,
            run_async=True,
        )
        return {"project": proj}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/colab/notebook")
async def api_download_colab_notebook() -> FileResponse:
    nb_path = BASE_DIR / "colab" / "Colab_G4_ComfyUI_MiniMaxH3.ipynb"
    if not nb_path.is_file():
        raise HTTPException(status_code=404, detail="找不到 Colab 筆記本檔案")
    return FileResponse(
        str(nb_path),
        media_type="application/x-ipynb+json",
        headers={
            "Content-Disposition": 'attachment; filename="Colab_G4_ComfyUI_MiniMaxH3.ipynb"'
        },
    )


# -------------------------
# 階段 ④ & ⑤：全自動剪輯合成、預覽、Finder 與配音稿匯出
# -------------------------
@app.post("/api/projects/{project_id}/render")
async def api_start_render(
    project_id: str, req: RenderRequest
) -> Dict[str, Any]:
    try:
        overrides = {k: v for k, v in req.model_dump().items() if v is not None}
        proj = orchestrator.start_render(
            project_id, override_options=overrides, run_async=True
        )
        return {"project": proj}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/projects/{project_id}/shots/{shot_id}/thumb")
async def api_get_shot_thumb(project_id: str, shot_id: int) -> FileResponse:
    shot = orchestrator.db.get_shot(shot_id)
    if not shot or shot["project_id"] != project_id or not shot["thumb_path"]:
        raise HTTPException(status_code=404, detail="縮圖不存在")
    p = resolve_project_media_path(shot["thumb_path"], project_id, "thumbs")
    if not p:
        raise HTTPException(status_code=404, detail="縮圖檔案不存在")
    return FileResponse(str(p), media_type="image/jpeg")


@app.get("/api/projects/{project_id}/shots/{shot_id}/clip")
async def api_get_shot_clip(project_id: str, shot_id: int) -> FileResponse:
    shot = orchestrator.db.get_shot(shot_id)
    if not shot or shot["project_id"] != project_id or not shot["clip_path"]:
        raise HTTPException(status_code=404, detail="片段不存在")
    p = resolve_project_media_path(shot["clip_path"], project_id, "clips")
    if not p:
        raise HTTPException(status_code=404, detail="片段檔案不存在")
    return FileResponse(str(p), media_type="video/mp4")


@app.get("/api/projects/{project_id}/output/video")
async def api_get_output_video(
    project_id: str, download: bool = Query(False)
) -> FileResponse:
    proj = orchestrator.db.get_project(project_id, include_details=False)
    if not proj:
        raise HTTPException(status_code=404, detail="找不到此專案")
    meta = proj.get("output_meta") or {}
    out_p = resolve_project_media_path(meta.get("output_path") or "", project_id, "output")
    if not out_p:
        desk_p = Path(meta.get("desktop_path") or "")
        if desk_p.is_file():
            out_p = desk_p
        else:
            raise HTTPException(status_code=404, detail="成片尚未生成或檔案不存在")

    if download:
        encoded = quote(out_p.name)
        return FileResponse(
            str(out_p),
            media_type="video/mp4",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded}"},
        )
    return FileResponse(str(out_p), media_type="video/mp4")


@app.get("/api/projects/{project_id}/voiceover-script")
async def api_export_voiceover_script(
    project_id: str, fmt: str = Query("md", pattern="^(md|txt)$")
) -> PlainTextResponse:
    try:
        filename, content = orchestrator.build_voiceover_script(project_id, fmt=fmt)
        encoded = quote(filename)
        media_type = "text/markdown; charset=utf-8" if fmt == "md" else "text/plain; charset=utf-8"
        return PlainTextResponse(
            content=content,
            media_type=media_type,
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded}"},
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/projects/{project_id}/reveal")
async def api_reveal_in_finder(project_id: str) -> Dict[str, Any]:
    proj = orchestrator.db.get_project(project_id, include_details=False)
    if not proj:
        raise HTTPException(status_code=404, detail="找不到此專案")
    meta = proj.get("output_meta") or {}
    target_str = meta.get("desktop_path") or meta.get("output_path") or ""
    target = Path(target_str) if target_str else None
    if not target or not target.exists():
        alt = Path(meta.get("output_path") or "")
        if alt.exists():
            target = alt
        else:
            raise HTTPException(status_code=404, detail="找不到可開啟的成片檔案")

    if sys.platform != "darwin":
        return {
            "ok": True,
            "path": str(target),
            "message": f"目前為 Linux / Docker 環境，請直接於掛載的輸出目錄查看成片：{target}",
        }

    proc = subprocess.run(["open", "-R", str(target)], capture_output=True, text=True, check=False)
    return {
        "ok": proc.returncode == 0,
        "path": str(target),
    }


# -------------------------
# SSE 全程即時監控事件流
# -------------------------
@app.get("/api/projects/{project_id}/events")
async def api_project_events(project_id: str, request: Request) -> StreamingResponse:
    proj = orchestrator.enrich_project(
        orchestrator.db.get_project(project_id, include_details=True)
    )
    if not proj:
        raise HTTPException(status_code=404, detail="找不到此專案")

    loop = asyncio.get_running_loop()
    q = orchestrator.bus.subscribe(project_id, loop)

    async def event_generator() -> AsyncGenerator[str, None]:
        try:
            init_payload = json.dumps(
                {"type": "snapshot", "project": proj}, ensure_ascii=False
            )
            yield f"data: {init_payload}\n\n"

            while True:
                if await request.is_disconnected():
                    break
                try:
                    item = await asyncio.wait_for(q.get(), timeout=15.0)
                    data_str = json.dumps(item, ensure_ascii=False)
                    yield f"data: {data_str}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            orchestrator.bus.unsubscribe(project_id, loop, q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.main:app", host=SERVER_HOST, port=SERVER_PORT, reload=False)
