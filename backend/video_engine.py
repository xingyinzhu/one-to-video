"""FFmpeg / FFprobe 影片健檢、縮圖擷取、兩階段標準化與電影轉場合成引擎。"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .settings import detect_ffmpeg_binaries

ProgressCallback = Callable[[int, str, str], None]


class FFmpegNotFoundError(RuntimeError):
    """系統找不到 ffmpeg 或 ffprobe 時拋出。"""


def get_ffmpeg_or_raise() -> Tuple[str, str]:
    ffmpeg_bin, ffprobe_bin = detect_ffmpeg_binaries(allow_static_download=False)
    if not ffmpeg_bin or not ffprobe_bin:
        ffmpeg_bin, ffprobe_bin = detect_ffmpeg_binaries(allow_static_download=True)
    if not ffmpeg_bin or not ffprobe_bin:
        raise FFmpegNotFoundError(
            "找不到 ffmpeg / ffprobe 執行檔。請在終端機執行：brew install ffmpeg"
        )
    return ffmpeg_bin, ffprobe_bin


def _parse_fps(rate_str: str) -> float:
    if not rate_str:
        return 0.0
    if "/" in rate_str:
        num_s, den_s = rate_str.split("/", 1)
        try:
            num, den = float(num_s), float(den_s)
            return round(num / den, 3) if den != 0 else 0.0
        except ValueError:
            return 0.0
    try:
        return round(float(rate_str), 3)
    except ValueError:
        return 0.0


def probe_video(file_path: Path) -> Dict[str, Any]:
    """使用 ffprobe 讀取影片真實時長、解析度、FPS、像素格式與音訊串流狀態。"""
    _, ffprobe_bin = get_ffmpeg_or_raise()
    p = Path(file_path).resolve()
    if not p.is_file():
        raise ValueError(f"影片檔案不存在：{p}")

    cmd = [
        ffprobe_bin,
        "-v",
        "error",
        "-show_entries",
        "format=duration,size:stream=index,codec_type,codec_name,width,height,r_frame_rate,avg_frame_rate,pix_fmt",
        "-of",
        "json",
        str(p),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()[:200]
        raise ValueError(f"ffprobe 無法解析影片（檔案可能損毀或未下載完成）：{err}")

    try:
        info = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError("ffprobe 輸出格式異常。") from exc

    streams = info.get("streams") or []
    fmt = info.get("format") or {}

    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not video_stream:
        raise ValueError("檔案中不包含有效的視訊串流（Video Stream）。")

    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    width = int(video_stream.get("width") or 0)
    height = int(video_stream.get("height") or 0)
    if width <= 0 or height <= 0:
        raise ValueError("影片解析度無效。")

    duration = float(fmt.get("duration") or video_stream.get("duration") or 0.0)
    if duration <= 0.05:
        raise ValueError("影片時長過短或無法讀取時長。")

    fps = _parse_fps(
        video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate") or "0/1"
    )
    if fps <= 0:
        fps = _parse_fps(video_stream.get("r_frame_rate") or "24/1")

    file_size = int(fmt.get("size") or p.stat().st_size)

    return {
        "valid": True,
        "duration": round(duration, 4),
        "width": width,
        "height": height,
        "fps": fps,
        "pix_fmt": str(video_stream.get("pix_fmt") or ""),
        "codec_name": str(video_stream.get("codec_name") or ""),
        "has_audio": has_audio,
        "file_size": file_size,
        "streams": streams,
    }


def prepare_reference_image(
    src_path: Path,
    dest_png_path: Path,
    aspect_ratio: str = "16:9",
) -> Dict[str, Any]:
    """
    將使用者上傳的真實照片轉正（自動依 EXIF 方向）、無變形置中裁切為
    MiniMax H3 黃金 32 倍數比例 PNG（16:9 -> 1280×704；9:16 -> 704×1280）。
    macOS 本機額外支援 .heic / .heif 透過系統 sips 轉檔。
    """
    ffmpeg_bin, _ = get_ffmpeg_or_raise()
    src = Path(src_path).resolve()
    dest = Path(dest_png_path).resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not src.is_file():
        raise ValueError(f"找不到上傳的參考圖片：{src}")

    target_w, target_h = (704, 1280) if aspect_ratio == "9:16" else (1280, 704)
    work_src = src
    tmp_heic_png: Optional[Path] = None

    try:
        if src.suffix.lower() in (".heic", ".heif") and shutil.which("sips"):
            tmp_heic_png = dest.with_name(dest.stem + "_heic_tmp.png")
            sips_proc = subprocess.run(
                ["sips", "-s", "format", "png", str(src), "--out", str(tmp_heic_png)],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            if sips_proc.returncode == 0 and tmp_heic_png.is_file():
                work_src = tmp_heic_png

        vf = (
            f"scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
            f"crop={target_w}:{target_h},format=rgb24"
        )
        cmd = [
            ffmpeg_bin,
            "-y",
            "-i",
            str(work_src),
            "-frames:v",
            "1",
            "-vf",
            vf,
            str(dest),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=60)
        if proc.returncode != 0 or not dest.is_file() or dest.stat().st_size < 1024:
            if src.suffix.lower() in (".heic", ".heif"):
                raise ValueError(
                    "目前容器環境無法直接解碼 iPhone .HEIC 照片，請先在 Mac 預覽程式或手機匯出為 JPG / PNG 後再上傳。"
                )
            err = (proc.stderr or "").strip()[-240:]
            raise ValueError(f"無法處理此參考圖片（請確認為有效 JPG/PNG/WEBP 圖片）：{err}")

        return {
            "path": str(dest),
            "width": target_w,
            "height": target_h,
            "file_size": dest.stat().st_size,
        }
    finally:
        if tmp_heic_png and tmp_heic_png.exists():
            tmp_heic_png.unlink(missing_ok=True)


def extract_thumbnail(
    video_path: Path, thumb_path: Path, duration_hint: Optional[float] = None
) -> Path:
    """從影片擷取清晰靜態縮圖（預設取前段 25% 或 0.5 秒處避開黑場）。"""
    ffmpeg_bin, _ = get_ffmpeg_or_raise()
    v_path = Path(video_path).resolve()
    t_path = Path(thumb_path).resolve()
    t_path.parent.mkdir(parents=True, exist_ok=True)

    dur = duration_hint
    if dur is None or dur <= 0:
        try:
            dur = probe_video(v_path)["duration"]
        except Exception:
            dur = 1.0

    seek_sec = min(1.2, max(0.1, dur * 0.25))
    cmd = [
        ffmpeg_bin,
        "-y",
        "-ss",
        f"{seek_sec:.2f}",
        "-i",
        str(v_path),
        "-frames:v",
        "1",
        "-vf",
        "scale=640:-2",
        "-q:v",
        "3",
        str(t_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0 or not t_path.is_file():
        # 退回第 0 秒重試一次
        fallback_cmd = [
            ffmpeg_bin,
            "-y",
            "-i",
            str(v_path),
            "-frames:v",
            "1",
            "-vf",
            "scale=640:-2",
            "-q:v",
            "3",
            str(t_path),
        ]
        subprocess.run(fallback_cmd, capture_output=True, text=True, check=True)
    return t_path


def extract_last_frame(video_path: Path, frame_path: Path) -> Path:
    """精準擷取影片最後一幀 PNG，供 MiniMax H3 下一幕『首尾幀接力』作為 first_frame 參考圖。"""
    ffmpeg_bin, _ = get_ffmpeg_or_raise()
    v_path = Path(video_path).resolve()
    f_path = Path(frame_path).resolve()
    f_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        ffmpeg_bin,
        "-y",
        "-sseof",
        "-0.08",
        "-i",
        str(v_path),
        "-update",
        "1",
        "-frames:v",
        "1",
        str(f_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0 or not f_path.is_file():
        # 備援：以 ffprobe 時長定位最後 0.1 秒
        dur = probe_video(v_path)["duration"]
        seek_t = max(0.0, dur - 0.1)
        fallback_cmd = [
            ffmpeg_bin,
            "-y",
            "-ss",
            f"{seek_t:.3f}",
            "-i",
            str(v_path),
            "-frames:v",
            "1",
            str(f_path),
        ]
        subprocess.run(fallback_cmd, capture_output=True, text=True, check=True)
    return f_path


def _sanitize_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", (name or "").strip())
    cleaned = re.sub(r"\s+", "_", cleaned).strip("._")
    return cleaned[:50] or "documentary_output"


def render_project_video(
    clips: List[Dict[str, Any]],
    temp_dir: Path,
    output_dir: Path,
    desktop_output_dir: Optional[Path],
    project_name: str,
    aspect_ratio: str = "16:9",
    fps: int = 24,
    audio_mode: str = "mute",
    transition_sec: float = 0.5,
    fade_in_sec: float = 0.8,
    fade_out_sec: float = 1.0,
    align_duration: bool = False,
    target_duration: int = 60,
    progress_cb: Optional[ProgressCallback] = None,
) -> Dict[str, Any]:
    """
    執行完整的 4 子步驟後期剪輯管線：
    1. ffprobe 片段健檢
    2. 逐鏡標準化轉碼（統一解析度、FPS、yuv420p、timescale 24000、靜音或標準音軌、可選速度校準）
    3. 精準計算 offset 並執行 xfade 疊化轉場 ＋ 首尾淡入淡出
    4. H.264 CRF 18 +faststart 輸出封裝、鏡像至桌面輸出資料夾並清理暫存檔
    """
    if not clips:
        raise ValueError("沒有任何已就緒的鏡頭素材可供合成。")

    ffmpeg_bin, _ = get_ffmpeg_or_raise()
    temp_dir = Path(temp_dir).resolve()
    output_dir = Path(output_dir).resolve()
    temp_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    def notify(pct: int, substep: str, msg: str) -> None:
        if progress_cb:
            progress_cb(pct, substep, msg)

    keep_audio = audio_mode == "keep"
    target_w, target_h = (1920, 1080) if aspect_ratio == "16:9" else (1080, 1920)
    fps = int(fps) if int(fps) in (24, 25, 30, 60) else 24
    transition_sec = max(0.0, min(2.0, float(transition_sec)))
    fade_in_sec = max(0.0, min(3.0, float(fade_in_sec)))
    fade_out_sec = max(0.0, min(3.0, float(fade_out_sec)))

    norm_files: List[Path] = []

    try:
        # ---------------------------------------------------
        # 子步驟 1：片段健檢 (ffprobe)
        # ---------------------------------------------------
        notify(5, "片段健檢 (1/4)", f"正在使用 ffprobe 檢查 {len(clips)} 支鏡頭素材完整性...")
        probed_inputs: List[Dict[str, Any]] = []
        for idx, c in enumerate(clips, start=1):
            c_path = Path(c["clip_path"]).resolve()
            meta = probe_video(c_path)
            probed_inputs.append(
                {
                    "shot_order": c.get("shot_order", idx),
                    "title": c.get("title", f"Shot {idx:02d}"),
                    "path": c_path,
                    "meta": meta,
                }
            )

        raw_total = sum(x["meta"]["duration"] for x in probed_inputs)
        notify(
            12,
            "片段健檢完成 (1/4)",
            f"健檢通過：共 {len(probed_inputs)} 支鏡頭，原始素材總時長 {raw_total:.2f} 秒。",
        )

        # 計算是否需要均勻微幅調速對齊目標時長（安全區間 0.75x ~ 1.25x）
        speed_factor = 1.0
        if align_duration and target_duration > 0:
            est_overlap = max(0, len(probed_inputs) - 1) * transition_sec
            desired_clip_sum = float(target_duration) + est_overlap
            if desired_clip_sum > 1.0 and raw_total > 0.5:
                raw_speed = raw_total / desired_clip_sum
                speed_factor = max(0.75, min(1.25, raw_speed))
                notify(
                    15,
                    "智慧時長校準",
                    f"已啟用目標時長校準（{target_duration} 秒），套用安全均勻調速倍率 {speed_factor:.2f}x。",
                )

        pts_multiplier = 1.0 / speed_factor

        # ---------------------------------------------------
        # 子步驟 2：逐鏡標準化轉碼
        # ---------------------------------------------------
        norm_durations: List[float] = []
        total_clips = len(probed_inputs)

        for idx, item in enumerate(probed_inputs, start=1):
            norm_path = temp_dir / f"norm_{idx:03d}.mp4"
            norm_files.append(norm_path)

            vf_chain = []
            if abs(pts_multiplier - 1.0) > 0.005:
                vf_chain.append(f"setpts={pts_multiplier:.5f}*PTS")
            vf_chain.extend(
                [
                    f"scale={target_w}:{target_h}:force_original_aspect_ratio=increase",
                    f"crop={target_w}:{target_h}",
                    f"fps={fps}",
                    "setsar=1",
                    "format=yuv420p",
                ]
            )
            vf_str = ",".join(vf_chain)

            if not keep_audio:
                cmd = [
                    ffmpeg_bin,
                    "-y",
                    "-i",
                    str(item["path"]),
                    "-vf",
                    vf_str,
                    "-r",
                    str(fps),
                    "-video_track_timescale",
                    "24000",
                    "-an",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "medium",
                    "-crf",
                    "18",
                    "-pix_fmt",
                    "yuv420p",
                    str(norm_path),
                ]
            else:
                if item["meta"]["has_audio"]:
                    af_parts = []
                    if abs(speed_factor - 1.0) > 0.005:
                        af_parts.append(f"atempo={speed_factor:.5f}")
                    af_parts.append("aresample=48000")
                    af_parts.append("aformat=sample_fmts=fltp:channel_layouts=stereo")
                    cmd = [
                        ffmpeg_bin,
                        "-y",
                        "-i",
                        str(item["path"]),
                        "-vf",
                        vf_str,
                        "-af",
                        ",".join(af_parts),
                        "-r",
                        str(fps),
                        "-video_track_timescale",
                        "24000",
                        "-c:v",
                        "libx264",
                        "-preset",
                        "medium",
                        "-crf",
                        "18",
                        "-pix_fmt",
                        "yuv420p",
                        "-c:a",
                        "aac",
                        "-b:a",
                        "192k",
                        str(norm_path),
                    ]
                else:
                    # 來源無音軌但選擇保留音訊時，補上靜音音軌以利後續串接
                    est_dur = item["meta"]["duration"] * pts_multiplier
                    cmd = [
                        ffmpeg_bin,
                        "-y",
                        "-i",
                        str(item["path"]),
                        "-f",
                        "lavfi",
                        "-t",
                        f"{est_dur:.4f}",
                        "-i",
                        "anullsrc=channel_layout=stereo:sample_rate=48000",
                        "-vf",
                        vf_str,
                        "-map",
                        "0:v:0",
                        "-map",
                        "1:a:0",
                        "-shortest",
                        "-r",
                        str(fps),
                        "-video_track_timescale",
                        "24000",
                        "-c:v",
                        "libx264",
                        "-preset",
                        "medium",
                        "-crf",
                        "18",
                        "-pix_fmt",
                        "yuv420p",
                        "-c:a",
                        "aac",
                        "-b:a",
                        "192k",
                        str(norm_path),
                    ]

            proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if proc.returncode != 0:
                err_tail = (proc.stderr or "").strip()[-300:]
                raise RuntimeError(
                    f"標準化第 {idx} 鏡（{item['title']}）失敗：{err_tail}"
                )

            norm_meta = probe_video(norm_path)
            norm_durations.append(norm_meta["duration"])

            step_pct = 15 + int((idx / total_clips) * 50)
            notify(
                step_pct,
                f"標準化 {idx}/{total_clips} (2/4)",
                f"已完成標準化 {idx}/{total_clips}：Shot {int(item['shot_order']):02d}（{target_w}×{target_h}, {fps}fps, {norm_meta['duration']:.2f}秒）",
            )

        # ---------------------------------------------------
        # 子步驟 3：電影轉場合成 (xfade + 首尾淡入淡出)
        # ---------------------------------------------------
        notify(70, "電影轉場合成 (3/4)", "正在計算時間軸偏移並套用電影疊化轉場與首尾淡入淡出...")

        safe_slug = _sanitize_filename(project_name)
        final_project_mp4 = output_dir / f"{safe_slug}.mp4"

        input_args: List[str] = []
        for nf in norm_files:
            input_args.extend(["-i", str(nf)])

        n_clips = len(norm_files)
        filter_steps: List[str] = []
        actual_transitions_used: List[float] = []

        if n_clips == 1:
            # 單支片段：跳過 xfade，僅套用首尾淡入淡出
            cum_dur = norm_durations[0]
            prev_v = "[0:v]"
            prev_a = "[0:a]"
        elif transition_sec <= 0.001:
            # 0 秒硬切串接
            v_inputs = "".join(f"[{i}:v]" for i in range(n_clips))
            filter_steps.append(f"{v_inputs}concat=n={n_clips}:v=1:a=0[vcat]")
            prev_v = "[vcat]"
            if keep_audio:
                a_inputs = "".join(f"[{i}:a]" for i in range(n_clips))
                filter_steps.append(f"{a_inputs}concat=n={n_clips}:v=0:a=1[acat]")
                prev_a = "[acat]"
            else:
                prev_a = ""
            cum_dur = sum(norm_durations)
        else:
            # 多片段 xfade=transition=fade
            cum_dur = norm_durations[0]
            prev_v = "[0:v]"
            prev_a = "[0:a]"

            for i in range(1, n_clips):
                prev_seg_dur = norm_durations[i - 1]
                curr_seg_dur = norm_durations[i]
                # 安全防護：若任一段過短 (< transition * 2.5)，自動縮短該處轉場秒數
                if (
                    prev_seg_dur < transition_sec * 2.5
                    or curr_seg_dur < transition_sec * 2.5
                    or cum_dur < transition_sec * 2.5
                ):
                    eff_t = round(
                        max(
                            0.05,
                            min(
                                transition_sec,
                                prev_seg_dur * 0.3,
                                curr_seg_dur * 0.3,
                                cum_dur * 0.3,
                            ),
                        ),
                        4,
                    )
                else:
                    eff_t = round(transition_sec, 4)

                actual_transitions_used.append(eff_t)
                offset = max(0.0, round(cum_dur - eff_t, 4))
                out_v = f"[vx{i}]"
                filter_steps.append(
                    f"{prev_v}[{i}:v]xfade=transition=fade:duration={eff_t}:offset={offset}{out_v}"
                )
                prev_v = out_v

                if keep_audio:
                    out_a = f"[ax{i}]"
                    filter_steps.append(
                        f"{prev_a}[{i}:a]acrossfade=d={eff_t}:c1=tri:c2=tri{out_a}"
                    )
                    prev_a = out_a

                cum_dur = cum_dur + curr_seg_dur - eff_t

        # 套用片頭淡入與片尾淡出
        eff_fi = min(fade_in_sec, cum_dur * 0.35) if fade_in_sec > 0 else 0.0
        eff_fo = min(fade_out_sec, cum_dur * 0.35) if fade_out_sec > 0 else 0.0
        fo_start = max(0.0, round(cum_dur - eff_fo, 4))

        v_post = []
        if eff_fi > 0.01:
            v_post.append(f"fade=t=in:st=0:d={eff_fi:.3f}")
        if eff_fo > 0.01:
            v_post.append(f"fade=t=out:st={fo_start:.4f}:d={eff_fo:.3f}")
        v_post.append(f"fps={fps}")
        v_post.append("format=yuv420p")
        filter_steps.append(f"{prev_v}{','.join(v_post)}[vout]")

        if keep_audio:
            a_post = []
            if eff_fi > 0.01:
                a_post.append(f"afade=t=in:st=0:d={eff_fi:.3f}")
            if eff_fo > 0.01:
                a_post.append(f"afade=t=out:st={fo_start:.4f}:d={eff_fo:.3f}")
            if a_post:
                filter_steps.append(f"{prev_a}{','.join(a_post)}[aout]")
            else:
                filter_steps.append(f"{prev_a}anull[aout]")

        filter_complex_str = ";".join(filter_steps)

        merge_cmd = [
            ffmpeg_bin,
            "-y",
            *input_args,
            "-filter_complex",
            filter_complex_str,
            "-map",
            "[vout]",
        ]
        if keep_audio:
            merge_cmd.extend(["-map", "[aout]", "-c:a", "aac", "-b:a", "192k"])
        else:
            merge_cmd.append("-an")

        merge_cmd.extend(
            [
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "18",
                "-maxrate",
                "18M",
                "-bufsize",
                "36M",
                "-pix_fmt",
                "yuv420p",
                "-r",
                str(fps),
                "-video_track_timescale",
                "24000",
                "-movflags",
                "+faststart",
                str(final_project_mp4),
            ]
        )

        merge_proc = subprocess.run(merge_cmd, capture_output=True, text=True, check=False)
        if merge_proc.returncode != 0:
            err_tail = (merge_proc.stderr or "").strip()[-400:]
            raise RuntimeError(f"FFmpeg 轉場合成失敗：{err_tail}")

        # ---------------------------------------------------
        # 子步驟 4：輸出封裝、鏡像至桌面資料夾與暫存清理
        # ---------------------------------------------------
        notify(92, "封裝與輸出驗證 (4/4)", "正在驗證成片規格並同步輸出至成品資料夾...")

        desktop_copy_path = ""
        if desktop_output_dir is not None:
            try:
                d_dir = Path(desktop_output_dir).expanduser().resolve()
                d_dir.mkdir(parents=True, exist_ok=True)
                ts_prefix = datetime.now().strftime("%Y%m%d_%H%M")
                dest = d_dir / f"{ts_prefix}_{safe_slug}.mp4"
                shutil.copy2(final_project_mp4, dest)
                desktop_copy_path = str(dest)
            except Exception:
                # 若沙盒或權限限制無法寫入外部桌面目錄，保留專案內部輸出不中斷流程
                desktop_copy_path = ""

        final_meta = probe_video(final_project_mp4)
        result = {
            "output_path": str(final_project_mp4),
            "desktop_path": desktop_copy_path,
            "duration": round(final_meta["duration"], 3),
            "width": final_meta["width"],
            "height": final_meta["height"],
            "fps": final_meta["fps"],
            "pix_fmt": final_meta["pix_fmt"],
            "file_size": final_meta["file_size"],
            "has_audio": final_meta["has_audio"],
            "speed_factor": round(speed_factor, 4),
            "norm_durations": norm_durations,
            "transitions_used": actual_transitions_used,
            "rendered_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

        notify(
            100,
            "合成完成 (4/4)",
            f"成片渲染完成！規格：{result['width']}×{result['height']} / {result['fps']}fps / 時長 {result['duration']:.2f} 秒。",
        )
        return result

    finally:
        # 自動清理標準化暫存檔
        for nf in norm_files:
            try:
                if nf.is_file():
                    nf.unlink()
            except OSError:
                pass
