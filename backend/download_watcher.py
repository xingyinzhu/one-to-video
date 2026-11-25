"""macOS 下載資料夾自動監聽器（含暫存檔過濾、0.8s 寫入穩定度雙重檢查與 ffprobe 驗證）。"""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Set, Tuple

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .video_engine import probe_video

ALLOWED_VIDEO_EXTS = {".mp4", ".mov", ".webm"}
IGNORED_TEMP_SUFFIXES = (
    ".crdownload",
    ".download",
    ".part",
    ".tmp",
    ".partial",
)

OnVideoDetectedCallback = Callable[[Path, Dict[str, Any]], None]


def is_candidate_video_path(path: Path) -> bool:
    name = path.name
    if not name or name.startswith("."):
        return False
    lower_name = name.lower()
    if any(lower_name.endswith(suf) for suf in IGNORED_TEMP_SUFFIXES):
        return False
    return path.suffix.lower() in ALLOWED_VIDEO_EXTS


class _DownloadEventHandler(FileSystemEventHandler):
    def __init__(self, enqueue_fn: Callable[[Path], None]) -> None:
        super().__init__()
        self._enqueue = enqueue_fn

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._enqueue(Path(os_fsdecode(event.src_path)))

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory and hasattr(event, "dest_path"):
            self._enqueue(Path(os_fsdecode(event.dest_path)))

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._enqueue(Path(os_fsdecode(event.src_path)))


def os_fsdecode(p: Any) -> str:
    if isinstance(p, bytes):
        return p.decode("utf-8", errors="replace")
    return str(p)


class DownloadFolderWatcher:
    """
    監聽指定下載資料夾（預設 ~/Downloads）：
    1. 僅處理啟動監聽「之後」新出現或新寫入完成的影片檔，絕不誤抓舊檔。
    2. 忽略 .crdownload / .download / .part 等瀏覽器未完成暫存檔。
    3. 偵測到 .mp4/.mov/.webm 後，連續檢查檔案大小兩次（間隔 0.8 秒）皆穩定且 > min_size_bytes（預設 50KB）。
    4. 通過 ffprobe 驗證為有效視訊檔後，依序觸發回呼進行歸檔。
    """

    def __init__(
        self,
        on_video_ready: OnVideoDetectedCallback,
        min_size_bytes: int = 50 * 1024,
        stability_interval_sec: float = 0.8,
    ) -> None:
        self.on_video_ready = on_video_ready
        self.min_size_bytes = int(min_size_bytes)
        self.stability_interval_sec = float(stability_interval_sec)

        self._lock = threading.RLock()
        self._observer: Optional[Any] = None
        self._worker_thread: Optional[threading.Thread] = None
        self._scanner_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._candidate_queue: "queue.Queue[Path]" = queue.Queue()

        self._watch_dir: Optional[Path] = None
        self._active_project_id: Optional[str] = None
        self._started_at: float = 0.0
        self._baseline_files: Dict[Path, Tuple[int, int]] = {}
        self._in_flight: Set[Path] = set()
        self._processed_signatures: Set[Tuple[Path, int, int]] = set()

    @property
    def is_running(self) -> bool:
        with self._lock:
            return (
                self._observer is not None
                and not self._stop_event.is_set()
                and self._active_project_id is not None
            )

    @property
    def active_project_id(self) -> Optional[str]:
        with self._lock:
            return self._active_project_id if not self._stop_event.is_set() else None

    @property
    def watch_dir(self) -> Optional[Path]:
        with self._lock:
            return self._watch_dir

    def start(self, project_id: str, watch_dir: Path) -> None:
        """針對指定專案啟動（或切換）資料夾監聽。"""
        target_dir = Path(watch_dir).expanduser().resolve()
        target_dir.mkdir(parents=True, exist_ok=True)

        with self._lock:
            if (
                self.is_running
                and self._active_project_id == project_id
                and self._watch_dir == target_dir
            ):
                return
            self.stop()

            self._stop_event.clear()
            self._watch_dir = target_dir
            self._active_project_id = project_id
            self._started_at = time.time()
            self._baseline_files.clear()
            self._in_flight.clear()
            self._processed_signatures.clear()
            while not self._candidate_queue.empty():
                try:
                    self._candidate_queue.get_nowait()
                except queue.Empty:
                    break

            # 記錄啟動當下已存在的所有舊檔案特徵 (mtime_ns, size)，避免誤抓歷史檔案
            for item in target_dir.iterdir():
                if item.is_file() and is_candidate_video_path(item):
                    try:
                        st = item.stat()
                        self._baseline_files[item.resolve()] = (st.st_mtime_ns, st.st_size)
                    except OSError:
                        pass

            handler = _DownloadEventHandler(self._enqueue_candidate)
            observer = Observer()
            observer.schedule(handler, str(target_dir), recursive=False)
            observer.daemon = True
            observer.start()
            self._observer = observer

            worker = threading.Thread(
                target=self._worker_loop, name="DownloadWatcherWorker", daemon=True
            )
            worker.start()
            self._worker_thread = worker

            scanner = threading.Thread(
                target=self._fallback_scan_loop, name="DownloadWatcherScanner", daemon=True
            )
            scanner.start()
            self._scanner_thread = scanner

    def stop(self) -> None:
        with self._lock:
            self._stop_event.set()
            obs = self._observer
            self._observer = None
            self._active_project_id = None

        if obs is not None:
            try:
                obs.stop()
                obs.join(timeout=2.0)
            except Exception:
                pass

    def _is_eligible_new_file(self, resolved_path: Path) -> bool:
        if not resolved_path.is_file() or not is_candidate_video_path(resolved_path):
            return False
        try:
            st = resolved_path.stat()
        except OSError:
            return False

        sig = (resolved_path, st.st_mtime_ns, st.st_size)
        with self._lock:
            if sig in self._processed_signatures:
                return False
            base_sig = self._baseline_files.get(resolved_path)
            if base_sig is not None and base_sig == (st.st_mtime_ns, st.st_size):
                return False
        return True

    def _enqueue_candidate(self, raw_path: Path) -> None:
        if self._stop_event.is_set():
            return
        try:
            resolved = raw_path.resolve()
        except OSError:
            return

        if not is_candidate_video_path(resolved):
            return

        with self._lock:
            if resolved in self._in_flight:
                return
            if not self._is_eligible_new_file(resolved):
                return
            self._in_flight.add(resolved)
            self._candidate_queue.put(resolved)

    def _fallback_scan_loop(self) -> None:
        """輔助輪詢掃描（每 0.4 秒），確保 macOS 暫存目錄快速批次產檔時依檔案時間順序不漏接。"""
        while not self._stop_event.is_set():
            w_dir = self._watch_dir
            if w_dir and w_dir.is_dir():
                try:
                    candidates = []
                    for p in w_dir.iterdir():
                        if p.is_file() and is_candidate_video_path(p):
                            rp = p.resolve()
                            if self._is_eligible_new_file(rp):
                                try:
                                    candidates.append((rp.stat().st_mtime_ns, rp.name, rp))
                                except OSError:
                                    pass
                    candidates.sort(key=lambda x: (x[0], x[1]))
                    for _, _, rp in candidates:
                        self._enqueue_candidate(rp)
                except OSError:
                    pass
            self._stop_event.wait(0.4)

    def _wait_until_stable_and_valid(self, file_path: Path) -> Optional[Dict[str, Any]]:
        """
        連續檢查檔案大小兩次（間隔 0.8 秒）皆穩定且 > min_size_bytes，
        再用 ffprobe 驗證有效才回傳 probe 資訊。
        """
        max_attempts = 25
        for _ in range(max_attempts):
            if self._stop_event.is_set():
                return None
            if not file_path.is_file():
                return None

            try:
                size1 = file_path.stat().st_size
            except OSError:
                return None

            # 睡眠指定間隔（預設 0.8s）再次讀取檔案大小
            if self._stop_event.wait(self.stability_interval_sec):
                return None

            if not file_path.is_file():
                return None

            try:
                st2 = file_path.stat()
                size2 = st2.st_size
            except OSError:
                return None

            if size1 == size2 and size2 > self.min_size_bytes:
                try:
                    meta = probe_video(file_path)
                    with self._lock:
                        self._processed_signatures.add(
                            (file_path, st2.st_mtime_ns, st2.st_size)
                        )
                    return meta
                except Exception:
                    # 可能容器檔尾 (moov atom) 仍在寫入，稍候重試
                    continue

        return None

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                candidate = self._candidate_queue.get(timeout=0.3)
            except queue.Empty:
                continue

            try:
                meta = self._wait_until_stable_and_valid(candidate)
                if meta is not None and not self._stop_event.is_set():
                    self.on_video_ready(candidate, meta)
            finally:
                with self._lock:
                    self._in_flight.discard(candidate)
                self._candidate_queue.task_done()
