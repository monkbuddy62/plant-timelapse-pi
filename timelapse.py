import json
import re
import shutil
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional


@dataclass
class TimelapseSession:
    id: str
    name: str
    interval_sec: int
    start_time: float
    frame_count: int = 0
    status: str = "running"  # running | compiling | done | error
    end_time: Optional[float] = None
    error: Optional[str] = None


class TimelapseManager:
    def __init__(self, base_dir: Path, get_frame_fn: Callable[[], bytes]):
        self.base_dir = base_dir
        self._get_frame = get_frame_fn
        self._session: Optional[TimelapseSession] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

    @property
    def active(self) -> bool:
        with self._lock:
            return self._session is not None and self._session.status == "running"

    def start(self, name: str, interval_sec: int) -> dict:
        with self._lock:
            if self._session is not None and self._session.status == "running":
                raise ValueError("A timelapse is already running")

            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            safe_name = re.sub(r"[^a-zA-Z0-9_-]", "-", name.strip()) or "timelapse"
            tl_id = f"{ts}_{safe_name}"

            session_dir = self.base_dir / tl_id
            (session_dir / "frames").mkdir(parents=True)

            self._session = TimelapseSession(
                id=tl_id,
                name=name,
                interval_sec=interval_sec,
                start_time=time.time(),
            )
            self._write_meta(session_dir, self._session)

            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._capture_loop,
                args=(session_dir,),
                daemon=True,
                name=f"timelapse-{tl_id}",
            )
            self._thread.start()
            return asdict(self._session)

    def stop(self) -> dict:
        with self._lock:
            if self._session is None or self._session.status != "running":
                raise ValueError("No timelapse is currently running")
            session = self._session
            session_dir = self.base_dir / session.id

        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=30)

        with self._lock:
            session.end_time = time.time()
            session.status = "compiling"
            self._write_meta(session_dir, session)

        threading.Thread(
            target=self._compile,
            args=(session_dir, session),
            daemon=True,
            name=f"compile-{session.id}",
        ).start()

        return asdict(session)

    def status(self) -> dict:
        with self._lock:
            if self._session is None:
                return {"active": False}
            s = self._session
            now = s.end_time if s.end_time else time.time()
            return {
                "active": s.status == "running",
                "id": s.id,
                "name": s.name,
                "status": s.status,
                "frame_count": s.frame_count,
                "elapsed_sec": int(now - s.start_time),
                "interval_sec": s.interval_sec,
            }

    def list_timelapses(self) -> list:
        results = []
        for meta_file in sorted(self.base_dir.glob("*/meta.json"), reverse=True):
            try:
                meta = json.loads(meta_file.read_text())
                tl_id = meta["id"]
                meta["has_thumbnail"] = (
                    self.base_dir / tl_id / "frames" / "frame_00001.jpg"
                ).exists()
                meta["has_video"] = (self.base_dir / tl_id / "output.mp4").exists()
                results.append(meta)
            except Exception:
                pass
        return results

    def delete(self, tl_id: str):
        with self._lock:
            if self._session and self._session.id == tl_id:
                raise ValueError("Cannot delete an active timelapse; stop it first")
        safe = re.sub(r"[^a-zA-Z0-9_-]", "", tl_id)
        target = self.base_dir / safe
        if not target.exists():
            raise FileNotFoundError(f"Timelapse '{tl_id}' not found")
        shutil.rmtree(target)

    # ── internal ──────────────────────────────────────────────────────────────

    def _capture_loop(self, session_dir: Path):
        with self._lock:
            interval = self._session.interval_sec

        while not self._stop_event.is_set():
            frame = self._get_frame()
            if frame:
                with self._lock:
                    if self._session is None:
                        break
                    self._session.frame_count += 1
                    n = self._session.frame_count
                (session_dir / "frames" / f"frame_{n:05d}.jpg").write_bytes(frame)
            self._stop_event.wait(timeout=interval)

    def _compile(self, session_dir: Path, session: TimelapseSession):
        output = session_dir / "output.mp4"
        pattern = str(session_dir / "frames" / "frame_%05d.jpg")
        cmd = [
            "ffmpeg", "-y",
            "-r", "24",
            "-i", pattern,
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(output),
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=600)
            session.status = "done"
        except subprocess.CalledProcessError as e:
            session.status = "error"
            session.error = e.stderr.decode(errors="replace")[-500:]
        except Exception as e:
            session.status = "error"
            session.error = str(e)
        finally:
            session.end_time = time.time()
            self._write_meta(session_dir, session)
            with self._lock:
                if self._session and self._session.id == session.id:
                    self._session.status = session.status
                    if session.status in ("done", "error"):
                        # keep session visible for status polling, clear on next start
                        pass

    @staticmethod
    def _write_meta(session_dir: Path, session: TimelapseSession):
        (session_dir / "meta.json").write_text(json.dumps(asdict(session), indent=2))
