import io
import json
import re
import shutil
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from astral import LocationInfo
from astral.sun import sun as astral_sun
from PIL import Image, ImageStat


@dataclass
class TimelapseSession:
    id: str
    name: str
    interval_sec: int
    segment_hours: float
    start_time: float
    frame_count: int = 0
    skipped_dark: int = 0
    segment_index: int = 1       # segment currently being captured (1-based)
    segments_compiled: int = 0   # fully compiled segments on disk
    skip_dark: bool = True
    dark_threshold: int = 30     # avg pixel value 0-255; frames below this are skipped
    daylight_only: bool = False
    latitude: float = 0.0
    longitude: float = 0.0
    sunrise_offset_min: int = 0  # minutes after sunrise to start capturing
    sunset_offset_min: int = 0   # minutes before sunset to stop capturing
    tz_offset_min: int = 0       # browser UTC offset at start (e.g. -420 for PDT); used so daylight window uses user's local date, not Pi's system date
    status: str = "running"      # running | compiling | done | error
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
        self._seg_threads: list[threading.Thread] = []

    # ── Public API ────────────────────────────────────────────────────────────

    def revive_dead_thread(self) -> bool:
        """Restart the capture thread if it died while the session is still 'running'.
        Called periodically by the scheduler as a watchdog."""
        with self._lock:
            if self._session is None or self._session.status != "running":
                return False
            if self._stop_event.is_set():
                return False  # stop() is in progress — don't fight it
            if self._thread is not None and self._thread.is_alive():
                return False
            session = self._session
            session_dir = self.base_dir / session.id

        seg_frames_dir = session_dir / f"seg_{session.segment_index:03d}" / "frames"
        seg_frames_dir.mkdir(parents=True, exist_ok=True)
        seg_frame_count = len(list(seg_frames_dir.glob("frame_*.jpg")))

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._capture_loop,
            args=(session_dir, seg_frame_count),
            daemon=True,
            name=f"timelapse-{session.id}-revived",
        )
        self._thread.start()
        return True

    def resume_interrupted(self) -> Optional[str]:
        """On startup, find any session left in 'running' state and resume it."""
        for meta_file in self.base_dir.glob("*/meta.json"):
            try:
                data = json.loads(meta_file.read_text())
                if data.get("status") != "running":
                    continue
                known = set(TimelapseSession.__dataclass_fields__)
                session = TimelapseSession(**{k: v for k, v in data.items() if k in known})
                session_dir = self.base_dir / session.id

                seg_frames_dir = session_dir / f"seg_{session.segment_index:03d}" / "frames"
                seg_frames_dir.mkdir(parents=True, exist_ok=True)
                seg_frame_count = len(list(seg_frames_dir.glob("frame_*.jpg")))

                with self._lock:
                    self._session = session

                self._stop_event.clear()
                self._thread = threading.Thread(
                    target=self._capture_loop,
                    args=(session_dir, seg_frame_count),
                    daemon=True,
                    name=f"timelapse-{session.id}",
                )
                self._thread.start()
                return session.id
            except Exception:
                pass
        return None

    @property
    def active(self) -> bool:
        with self._lock:
            return self._session is not None and self._session.status == "running"

    def start(self, name: str, interval_sec: int, segment_hours: float = 2.0,
              skip_dark: bool = True, dark_threshold: int = 30,
              daylight_only: bool = False, latitude: float = 0.0, longitude: float = 0.0,
              sunrise_offset_min: int = 0, sunset_offset_min: int = 0,
              tz_offset_min: int = 0) -> dict:
        with self._lock:
            if self._session is not None and self._session.status == "running":
                raise ValueError("A timelapse is already running")

            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            safe_name = re.sub(r"[^a-zA-Z0-9_-]", "-", name.strip()) or "timelapse"
            tl_id = f"{ts}_{safe_name}"
            session_dir = self.base_dir / tl_id
            (session_dir / "seg_001" / "frames").mkdir(parents=True)

            self._session = TimelapseSession(
                id=tl_id,
                name=name,
                interval_sec=interval_sec,
                segment_hours=segment_hours,
                skip_dark=skip_dark,
                dark_threshold=dark_threshold,
                daylight_only=daylight_only,
                latitude=latitude,
                longitude=longitude,
                sunrise_offset_min=sunrise_offset_min,
                sunset_offset_min=sunset_offset_min,
                tz_offset_min=tz_offset_min,
                start_time=time.time(),
            )
            self._write_meta(session_dir, self._session)
            result = asdict(self._session)

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._capture_loop,
            args=(session_dir, 0),
            daemon=True,
            name=f"timelapse-{tl_id}",
        )
        self._thread.start()
        return result

    def stop(self) -> dict:
        with self._lock:
            if self._session is None or self._session.status != "running":
                raise ValueError("No timelapse is currently running")
            session = self._session
            session_dir = self.base_dir / session.id

        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=30)

        for t in list(self._seg_threads):
            t.join(timeout=120)
        self._seg_threads.clear()

        with self._lock:
            session.end_time = time.time()
            session.status = "compiling"
            self._write_meta(session_dir, session)

        threading.Thread(
            target=self._finalize,
            args=(session_dir, session),
            daemon=True,
            name=f"finalize-{session.id}",
        ).start()

        return asdict(session)

    def status(self) -> dict:
        with self._lock:
            if self._session is None:
                return {"active": False}
            s = self._session
            now = s.end_time if s.end_time else time.time()
            result = {
                "active": s.status == "running",
                "id": s.id,
                "name": s.name,
                "status": s.status,
                "frame_count": s.frame_count,
                "elapsed_sec": int(now - s.start_time),
                "interval_sec": s.interval_sec,
                "segment_hours": s.segment_hours,
                "segment_index": s.segment_index,
                "segments_compiled": s.segments_compiled,
                "skip_dark": s.skip_dark,
                "dark_threshold": s.dark_threshold,
                "skipped_dark": s.skipped_dark,
                "daylight_only": s.daylight_only,
                "latitude": s.latitude,
                "longitude": s.longitude,
                "sunrise_offset_min": s.sunrise_offset_min,
                "sunset_offset_min": s.sunset_offset_min,
                "tz_offset_min": s.tz_offset_min,
                "capture_alive": self._thread is not None and self._thread.is_alive(),
            }

        # Compute daylight window outside the lock so an astral exception
        # doesn't poison the status endpoint and leave the UI frozen.
        if result["daylight_only"] and result["status"] == "running":
            try:
                dl_start, dl_end = _daylight_window(
                    result["latitude"], result["longitude"],
                    result["sunrise_offset_min"], result["sunset_offset_min"],
                    result["tz_offset_min"],
                )
                now_utc = datetime.now(timezone.utc)
                result["daylight_paused"] = not (dl_start <= now_utc <= dl_end)
                result["daylight_resume_ts"] = dl_start.timestamp()
                result["daylight_pause_ts"] = dl_end.timestamp()
            except Exception as exc:
                result["daylight_error"] = str(exc)

        return result

    def list_timelapses(self) -> list:
        results = []
        for meta_file in sorted(self.base_dir.glob("*/meta.json"), reverse=True):
            try:
                meta = json.loads(meta_file.read_text())
                tl_id = meta["id"]
                tl_dir = self.base_dir / tl_id
                # Support both new seg_001/frames/ layout and legacy frames/ layout
                meta["has_thumbnail"] = (
                    (tl_dir / "seg_001" / "frames" / "frame_00001.jpg").exists()
                    or (tl_dir / "frames" / "frame_00001.jpg").exists()
                )
                meta["has_video"] = (tl_dir / "output.mp4").exists()
                meta["can_preview"] = bool(list(tl_dir.glob("seg_*/seg_*.mp4")))
                results.append(meta)
            except Exception:
                pass
        return results

    def delete(self, tl_id: str):
        with self._lock:
            if self._session and self._session.id == tl_id and self._session.status in ("running", "compiling"):
                raise ValueError("Cannot delete an active timelapse; stop it first")
        safe = re.sub(r"[^a-zA-Z0-9_-]", "", tl_id)
        target = self.base_dir / safe
        if not target.exists():
            raise FileNotFoundError(f"Timelapse '{tl_id}' not found")
        shutil.rmtree(target)

    def build_preview(self, tl_id: str) -> Optional[Path]:
        """Lossless concat of all compiled segments → preview.mp4. Fast (~1s)."""
        safe = re.sub(r"[^a-zA-Z0-9_-]", "", tl_id)
        session_dir = self.base_dir / safe
        if not session_dir.exists():
            return None

        seg_files = sorted(session_dir.glob("seg_*/seg_*.mp4"))
        if not seg_files:
            return None

        preview_path = session_dir / "preview.mp4"

        if len(seg_files) == 1:
            shutil.copy2(seg_files[0], preview_path)
            return preview_path

        concat_list = session_dir / "concat.txt"
        concat_list.write_text("\n".join(f"file '{f.resolve()}'" for f in seg_files))
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                 "-i", str(concat_list), "-c", "copy", str(preview_path)],
                check=True, capture_output=True, timeout=120,
            )
            return preview_path
        except Exception:
            return None
        finally:
            concat_list.unlink(missing_ok=True)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _capture_loop(self, session_dir: Path, initial_seg_frames: int):
        with self._lock:
            if self._session is None:
                return
            interval = self._session.interval_sec
            segment_secs = self._session.segment_hours * 3600
            seg_idx = self._session.segment_index
            skip_dark = self._session.skip_dark
            dark_threshold = self._session.dark_threshold
            daylight_only = self._session.daylight_only
            latitude = self._session.latitude
            longitude = self._session.longitude
            sunrise_offset_min = self._session.sunrise_offset_min
            sunset_offset_min = self._session.sunset_offset_min
            tz_offset_min = self._session.tz_offset_min

        seg_frame_count = initial_seg_frames
        seg_start = time.time()
        _dl_date: Optional[date] = None
        _dl_start: Optional[datetime] = None
        _dl_end: Optional[datetime] = None

        while not self._stop_event.is_set():
            # Daylight window check — recalculate once per day (keyed on user's local date)
            if daylight_only:
                user_tz = timezone(timedelta(minutes=tz_offset_min))
                today = datetime.now(user_tz).date()
                if today != _dl_date:
                    try:
                        _dl_start, _dl_end = _daylight_window(
                            latitude, longitude, sunrise_offset_min, sunset_offset_min,
                            tz_offset_min,
                        )
                        _dl_date = today
                    except Exception:
                        # If astral fails, skip the window check and capture anyway
                        _dl_start = _dl_end = None
                if _dl_start and _dl_end and not (_dl_start <= datetime.now(timezone.utc) <= _dl_end):
                    self._stop_event.wait(timeout=60)
                    continue

            frame = self._get_frame()
            if frame:
                if skip_dark and _avg_brightness(frame) < dark_threshold:
                    with self._lock:
                        if self._session:
                            self._session.skipped_dark += 1
                else:
                    seg_frame_count += 1
                    seg_frames_dir = session_dir / f"seg_{seg_idx:03d}" / "frames"
                    (seg_frames_dir / f"frame_{seg_frame_count:05d}.jpg").write_bytes(frame)
                    with self._lock:
                        if self._session:
                            self._session.frame_count += 1
                            if self._session.frame_count % 30 == 0:
                                self._write_meta(session_dir, self._session)

            if time.time() - seg_start >= segment_secs:
                seg_idx, seg_frame_count = self._rotate_segment(session_dir, seg_idx)
                seg_start = time.time()

            self._stop_event.wait(timeout=interval)

    def _rotate_segment(self, session_dir: Path, completed_idx: int) -> tuple:
        next_idx = completed_idx + 1
        (session_dir / f"seg_{next_idx:03d}" / "frames").mkdir(parents=True, exist_ok=True)

        with self._lock:
            if self._session:
                self._session.segment_index = next_idx
                self._write_meta(session_dir, self._session)

        # Prune dead compile threads before appending
        self._seg_threads = [t for t in self._seg_threads if t.is_alive()]

        t = threading.Thread(
            target=self._compile_segment,
            args=(session_dir, completed_idx, True),
            daemon=True,
            name=f"seg-{session_dir.name}-{completed_idx:03d}",
        )
        t.start()
        self._seg_threads.append(t)

        return next_idx, 0

    def _compile_segment(self, session_dir: Path, seg_idx: int, update_count: bool = False) -> bool:
        seg_dir = session_dir / f"seg_{seg_idx:03d}"
        output = seg_dir / f"seg_{seg_idx:03d}.mp4"
        frames_dir = seg_dir / "frames"

        if not frames_dir.exists() or not list(frames_dir.glob("frame_*.jpg")):
            return False

        try:
            subprocess.run(
                ["ffmpeg", "-y", "-r", "24",
                 "-i", str(frames_dir / "frame_%05d.jpg"),
                 "-c:v", "libx264", "-pix_fmt", "yuv420p",
                 "-movflags", "+faststart", str(output)],
                check=True, capture_output=True, timeout=600,
            )
            if update_count:
                with self._lock:
                    if self._session and self._session.id == session_dir.name:
                        self._session.segments_compiled += 1
                        self._write_meta(session_dir, self._session)
            return True
        except Exception:
            return False

    def _finalize(self, session_dir: Path, session: TimelapseSession):
        # Compile last partial segment (synchronous — capture loop is already stopped)
        self._compile_segment(session_dir, session.segment_index)

        seg_files = sorted(session_dir.glob("seg_*/seg_*.mp4"))
        output = session_dir / "output.mp4"

        try:
            if not seg_files:
                raise ValueError("No compiled segments found")

            if len(seg_files) == 1:
                shutil.copy2(seg_files[0], output)
            else:
                concat_list = session_dir / "concat.txt"
                concat_list.write_text(
                    "\n".join(f"file '{f.resolve()}'" for f in seg_files)
                )
                try:
                    subprocess.run(
                        ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                         "-i", str(concat_list), "-c", "copy", str(output)],
                        check=True, capture_output=True, timeout=300,
                    )
                finally:
                    concat_list.unlink(missing_ok=True)

            session.status = "done"
        except Exception as e:
            session.status = "error"
            session.error = str(e)[-500:]
        finally:
            session.end_time = time.time()
            self._write_meta(session_dir, session)
            with self._lock:
                if self._session and self._session.id == session.id:
                    self._session.status = session.status

    @staticmethod
    def _write_meta(session_dir: Path, session: "TimelapseSession"):
        (session_dir / "meta.json").write_text(json.dumps(asdict(session), indent=2))


def _avg_brightness(jpeg_bytes: bytes) -> float:
    img = Image.open(io.BytesIO(jpeg_bytes)).convert("L")
    return ImageStat.Stat(img).mean[0]


def _daylight_window(lat: float, lon: float, rise_offset_min: int, set_offset_min: int,
                     tz_offset_min: int = 0):
    """Return (start_utc, end_utc) for today's capture window.

    Uses tz_offset_min (browser UTC offset, e.g. -420 for PDT) to derive
    the user's local calendar date. This makes the window correct regardless
    of what timezone the Pi's OS is set to.
    """
    user_tz = timezone(timedelta(minutes=tz_offset_min))
    local_date = datetime.now(user_tz).date()
    loc = LocationInfo(latitude=lat, longitude=lon)
    s = astral_sun(loc.observer, date=local_date)
    start = s["sunrise"] + timedelta(minutes=rise_offset_min)
    end = s["sunset"] - timedelta(minutes=set_offset_min)
    return start, end
