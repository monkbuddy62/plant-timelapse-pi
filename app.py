import atexit
import io
import os
import re
import time
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from flask import Flask, Response, jsonify, request, send_file, render_template

from timelapse import TimelapseManager

app = Flask(__name__)

BUILD_ID = "9"

TIMELAPSES_DIR = Path(os.environ.get("TIMELAPSES_DIR", Path(__file__).parent / "timelapses"))
TIMELAPSES_DIR.mkdir(exist_ok=True)

STREAM_WIDTH = int(os.environ.get("STREAM_WIDTH", 1280))
STREAM_HEIGHT = int(os.environ.get("STREAM_HEIGHT", 720))
STREAM_FPS = int(os.environ.get("STREAM_FPS", 10))

# ── Camera ─────────────────────────────────────────────────────────────────────
_latest_frame: bytes = b""
_frame_lock = threading.Lock()
_cam_stop = threading.Event()
_cam_thread: threading.Thread | None = None
_cam_error: str = ""

# ── Camera controls ────────────────────────────────────────────────────────────
_cam_controls: dict = {}
_picam2_lock = threading.Lock()
_picam2_ref = None


def update_camera_controls(controls: dict):
    global _cam_controls, _picam2_ref
    _cam_controls = dict(controls)
    with _picam2_lock:
        if _picam2_ref is not None:
            try:
                _picam2_ref.set_controls(controls)
            except Exception as exc:
                app.logger.warning("set_controls failed: %s", exc)


def _camera_loop():
    global _latest_frame, _cam_error, _picam2_ref
    try:
        from picamera2 import Picamera2
        from PIL import Image

        picam2 = Picamera2()
        config = picam2.create_video_configuration(
            main={"size": (STREAM_WIDTH, STREAM_HEIGHT), "format": "RGB888"},
        )
        picam2.configure(config)
        picam2.start()
        time.sleep(1.0)

        with _picam2_lock:
            _picam2_ref = picam2
            if _cam_controls:
                picam2.set_controls(_cam_controls)

        interval = 1.0 / STREAM_FPS
        try:
            while not _cam_stop.is_set():
                t0 = time.monotonic()
                arr = picam2.capture_array()
                img = Image.fromarray(arr)
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=85)
                with _frame_lock:
                    _latest_frame = buf.getvalue()
                elapsed = time.monotonic() - t0
                wait = interval - elapsed
                if wait > 0:
                    time.sleep(wait)
        finally:
            with _picam2_lock:
                _picam2_ref = None
            picam2.stop()
            picam2.close()
    except Exception as exc:
        _cam_error = str(exc)
        app.logger.error("Camera thread died: %s", exc)


def get_frame() -> bytes:
    with _frame_lock:
        return _latest_frame


def start_camera():
    global _cam_thread
    _cam_stop.clear()
    _cam_thread = threading.Thread(target=_camera_loop, daemon=True, name="camera")
    _cam_thread.start()
    for _ in range(120):
        if get_frame():
            return
        time.sleep(0.1)
    app.logger.warning("Camera did not produce a frame within 12 s")


def stop_camera():
    _cam_stop.set()
    if _cam_thread:
        _cam_thread.join(timeout=5)


atexit.register(stop_camera)

# ── Timelapse manager ──────────────────────────────────────────────────────────
tl = TimelapseManager(TIMELAPSES_DIR, get_frame)


# ── Schedule ───────────────────────────────────────────────────────────────────
@dataclass
class ScheduleEntry:
    name: str
    interval_sec: int
    start_ts: float
    stop_ts: Optional[float] = None
    started: bool = False


_schedule: Optional[ScheduleEntry] = None
_schedule_lock = threading.Lock()
_sched_stop = threading.Event()


def _scheduler_loop():
    global _schedule
    while not _sched_stop.is_set():
        now = time.time()
        with _schedule_lock:
            sched = _schedule

        if sched and not sched.started and now >= sched.start_ts:
            if not tl.active:
                try:
                    tl.start(sched.name, sched.interval_sec)
                    app.logger.info("Scheduled timelapse started: %s", sched.name)
                except ValueError:
                    pass
            with _schedule_lock:
                if _schedule is sched:
                    _schedule.started = True

        if sched and sched.started and sched.stop_ts and now >= sched.stop_ts:
            if tl.active:
                try:
                    tl.stop()
                    app.logger.info("Scheduled timelapse stopped: %s", sched.name)
                except ValueError:
                    pass
            with _schedule_lock:
                if _schedule is sched:
                    _schedule = None

        if tl.revive_dead_thread():
            app.logger.warning("Capture thread was dead; restarted automatically")

        _sched_stop.wait(timeout=30)


# ── Helpers ────────────────────────────────────────────────────────────────────
def _safe_id(raw: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "", raw)


# ── Routes ─────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


def _mjpeg_frames():
    interval = 1.0 / STREAM_FPS
    while True:
        frame = get_frame()
        if frame:
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n"
                + frame
                + b"\r\n"
            )
        time.sleep(interval)


@app.route("/stream")
def stream():
    return Response(
        _mjpeg_frames(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


@app.route("/api/camera/status")
def camera_status():
    frame = get_frame()
    return jsonify({
        "ready": bool(frame),
        "error": _cam_error or None,
        "width": STREAM_WIDTH,
        "height": STREAM_HEIGHT,
        "fps": STREAM_FPS,
    })


@app.route("/api/timelapse/start", methods=["POST"])
def timelapse_start():
    data = request.get_json(silent=True) or {}
    try:
        name = str(data.get("name", "timelapse"))[:64].strip() or "timelapse"
        interval_sec = max(1, int(data.get("interval_sec", 30)))
        segment_hours = max(0.1, float(data.get("segment_hours", 2.0)))
        skip_dark = bool(data.get("skip_dark", True))
        dark_threshold = max(0, min(255, int(data.get("dark_threshold", 30))))
        daylight_only = bool(data.get("daylight_only", False))
        latitude = float(data.get("latitude", 0.0))
        longitude = float(data.get("longitude", 0.0))
        sunrise_offset_min = int(data.get("sunrise_offset_min", 0))
        sunset_offset_min = int(data.get("sunset_offset_min", 0))
        tz_offset_min = int(data.get("tz_offset_min", 0))
        client_ts = float(data.get("client_ts", 0) or 0)
        result = tl.start(
            name, interval_sec, segment_hours,
            skip_dark, dark_threshold,
            daylight_only, latitude, longitude,
            sunrise_offset_min, sunset_offset_min,
            tz_offset_min, client_ts,
        )
        return jsonify(result)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409


@app.route("/api/timelapse/stop", methods=["POST"])
def timelapse_stop():
    try:
        result = tl.stop()
        return jsonify(result)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409


@app.route("/api/timelapse/status")
def timelapse_status():
    client_ts = float(request.args.get("ts", 0) or 0)
    return jsonify(tl.status(client_ts=client_ts))


@app.route("/api/debug/daylight")
def debug_daylight():
    from datetime import timezone as _tz, timedelta as _td
    from timelapse import _daylight_window
    import traceback
    out = {
        "build_id": BUILD_ID,
        "pi_time_utc": datetime.now(_tz.utc).isoformat(),
        "pi_unix": time.time(),
        "clock_offset_sec": tl._clock_offset_sec,
        "corrected_time_utc": tl._corrected_utc_now().isoformat(),
    }
    s = tl.status()
    out["session"] = {k: s.get(k) for k in (
        "active", "status", "daylight_only", "daylight_paused",
        "daylight_resume_ts", "daylight_pause_ts", "daylight_error",
        "latitude", "longitude", "sunrise_offset_min", "sunset_offset_min",
        "tz_offset_min", "capture_alive",
    )}
    if s.get("daylight_only") and s.get("latitude") is not None:
        try:
            dl_start, dl_end = _daylight_window(
                s["latitude"], s["longitude"],
                s["sunrise_offset_min"], s["sunset_offset_min"],
                s.get("tz_offset_min", 0),
            )
            out["window_start_utc"] = dl_start.isoformat()
            out["window_end_utc"] = dl_end.isoformat()
            out["window_start_unix"] = dl_start.timestamp()
            out["window_end_unix"] = dl_end.timestamp()
            out["in_window"] = (dl_start <= tl._corrected_utc_now() <= dl_end)
        except Exception:
            out["window_error"] = traceback.format_exc()
    return jsonify(out)


@app.route("/api/timelapses")
def list_timelapses():
    return jsonify(tl.list_timelapses())


@app.route("/api/timelapses/<tl_id>/video")
def timelapse_video(tl_id: str):
    path = TIMELAPSES_DIR / _safe_id(tl_id) / "output.mp4"
    if not path.exists():
        return jsonify({"error": "Video not ready yet"}), 404
    return send_file(path, mimetype="video/mp4", conditional=True)


@app.route("/api/timelapses/<tl_id>/thumbnail")
def timelapse_thumbnail(tl_id: str):
    tl_dir = TIMELAPSES_DIR / _safe_id(tl_id)
    # Support new seg_001/frames/ layout and legacy frames/ layout
    for candidate in [
        tl_dir / "seg_001" / "frames" / "frame_00001.jpg",
        tl_dir / "frames" / "frame_00001.jpg",
    ]:
        if candidate.exists():
            return send_file(candidate, mimetype="image/jpeg")
    return jsonify({"error": "Thumbnail not found"}), 404


@app.route("/api/timelapses/<tl_id>/preview")
def timelapse_preview(tl_id: str):
    path = tl.build_preview(_safe_id(tl_id))
    if path is None:
        return jsonify({"error": "No compiled segments available yet"}), 404
    return send_file(path, mimetype="video/mp4", conditional=True)


@app.route("/api/timelapses/<tl_id>", methods=["DELETE"])
def delete_timelapse(tl_id: str):
    try:
        tl.delete(tl_id)
        return jsonify({"ok": True})
    except (ValueError, FileNotFoundError) as exc:
        return jsonify({"error": str(exc)}), 409


# ── Camera controls routes ─────────────────────────────────────────────────────
@app.route("/api/camera/controls", methods=["GET"])
def get_camera_controls():
    return jsonify(_cam_controls)


@app.route("/api/camera/controls", methods=["POST"])
def set_camera_controls():
    data = request.get_json(silent=True) or {}
    controls = {}
    if "Brightness" in data:
        controls["Brightness"] = max(-1.0, min(1.0, float(data["Brightness"])))
    if "Contrast" in data:
        controls["Contrast"] = max(0.0, min(32.0, float(data["Contrast"])))
    if "Saturation" in data:
        controls["Saturation"] = max(0.0, min(32.0, float(data["Saturation"])))
    if "Sharpness" in data:
        controls["Sharpness"] = max(0.0, min(16.0, float(data["Sharpness"])))
    if "AwbMode" in data:
        awb = int(data["AwbMode"])
        if 0 <= awb <= 5:
            controls["AwbMode"] = awb
    update_camera_controls(controls)
    return jsonify({"ok": True, "controls": controls})


# ── Schedule routes ────────────────────────────────────────────────────────────
@app.route("/api/schedule", methods=["GET"])
def get_schedule():
    with _schedule_lock:
        s = _schedule
    if s is None:
        return jsonify({"active": False})
    return jsonify({
        "active": True,
        "name": s.name,
        "interval_sec": s.interval_sec,
        "start_ts": s.start_ts,
        "stop_ts": s.stop_ts,
        "started": s.started,
    })


@app.route("/api/schedule", methods=["POST"])
def set_schedule():
    global _schedule
    data = request.get_json(silent=True) or {}
    try:
        name = str(data.get("name", "timelapse"))[:64].strip() or "timelapse"
        interval_sec = max(1, int(data.get("interval_sec", 30)))
        start_ts = float(data["start_ts"])
        stop_ts = float(data["stop_ts"]) if data.get("stop_ts") else None
    except (KeyError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    if stop_ts and stop_ts <= start_ts:
        return jsonify({"error": "Stop time must be after start time"}), 400
    with _schedule_lock:
        _schedule = ScheduleEntry(
            name=name,
            interval_sec=interval_sec,
            start_ts=start_ts,
            stop_ts=stop_ts,
        )
    return jsonify({"ok": True})


@app.route("/api/schedule", methods=["DELETE"])
def clear_schedule():
    global _schedule
    with _schedule_lock:
        _schedule = None
    return jsonify({"ok": True})


if __name__ == "__main__":
    start_camera()
    resumed = tl.resume_interrupted()
    if resumed:
        app.logger.info("Resumed interrupted timelapse: %s", resumed)
    sched_thread = threading.Thread(target=_scheduler_loop, daemon=True, name="scheduler")
    sched_thread.start()
    app.run(host="0.0.0.0", port=5000, threaded=True, use_reloader=False)
