import atexit
import io
import os
import re
import time
import threading
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_file, render_template

from timelapse import TimelapseManager

app = Flask(__name__)

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


def _camera_loop():
    global _latest_frame, _cam_error
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
        result = tl.start(name, interval_sec)
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
    return jsonify(tl.status())


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
    path = TIMELAPSES_DIR / _safe_id(tl_id) / "frames" / "frame_00001.jpg"
    if not path.exists():
        return jsonify({"error": "Thumbnail not found"}), 404
    return send_file(path, mimetype="image/jpeg")


@app.route("/api/timelapses/<tl_id>", methods=["DELETE"])
def delete_timelapse(tl_id: str):
    try:
        tl.delete(tl_id)
        return jsonify({"ok": True})
    except (ValueError, FileNotFoundError) as exc:
        return jsonify({"error": str(exc)}), 409


if __name__ == "__main__":
    start_camera()
    app.run(host="0.0.0.0", port=5000, threaded=True, use_reloader=False)
