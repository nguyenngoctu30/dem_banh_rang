import sys
import cv2
import numpy as np
import serial
import serial.tools.list_ports
import datetime
import base64
import json
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import firebase_admin
from firebase_admin import credentials, db as fb_db

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget,
    QVBoxLayout, QHBoxLayout,
    QLabel, QFrame, QPushButton, QSizePolicy,
    QComboBox, QMessageBox, QSlider, QTextEdit
)
from PyQt5.QtCore import (Qt, QTimer, QPropertyAnimation, QEasingCurve,
                           pyqtProperty, QThread, pyqtSignal, QMutex, QMutexLocker)
from PyQt5.QtGui import QFont, QPainter, QColor, QPen, QBrush, QLinearGradient, QImage, QPixmap

from ultralytics import YOLO


CAMERA_INDEX    = 0
API_HOST        = "0.0.0.0"
API_PORT        = 8000
MODEL_PATH      = r"C:\BANH_RANG_CONG_NGHIEP\mo_hinh_1-20260501T133616Z-3-001\run_v11\weights\best.pt"
YOLO_CONF       = 0.5
YOLO_IOU        = 0.45
YOLO_INPUT_SIZE = (640, 480)   

SERIAL_PORT  = "COM6"
SERIAL_BAUD  = 9600
USE_SERIAL   = True

FIREBASE_KEY = r"C:\BANH_RANG_CONG_NGHIEP\key.json"
FIREBASE_URL = "https://hethongnhung-a17f3-default-rtdb.asia-southeast1.firebasedatabase.app/"
USE_FIREBASE = True

LABEL_GEAR       = "gear"
LABEL_NO_BEARING = "gear_no_bearing"
LABEL_CHIPPED    = "defect_chipped"
LABEL_STAIN      = "defect_stain"

# Lệnh Arduino
CMD_CONVEYOR_STOP  = 4
CMD_CONVEYOR_START = 5
CMD_SPEED_UP       = 6
CMD_SPEED_DOWN     = 7

LINE_POSITION  = 0.5
LINE_THICKNESS = 3
CROSS_MARGIN   = 5
COUNT_COOLDOWN = 1.5


BG       = "#0D0F14"
PANEL    = "#13161E"
BORDER   = "#1E2330"
ACCENT   = "#00E5FF"
ACCENT2  = "#FF4D6D"
WARN     = "#FFB703"
OK       = "#06D6A0"
TEXT_PRI = "#E8EAF0"
TEXT_SEC = "#6B7394"
CARD_BG  = "#161A26"
LINE_CLR       = (0, 229, 255)
LINE_TRIGGERED = (255, 77, 109)

SIGNAL_MAP = {0: 0, 1: 1, 2: 2, 3: 3}


class TrackingAPIHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        window = self.server.window
        if self.path == "/api/status":
            self._send_json(window._api_status())
        elif self.path in ("/api/camera/stream", "/api/camera/yolo"):
            self._stream_frames(window, self.path.endswith("/yolo"))
        else:
            self._send_json({"error": "Not found"}, status=404)

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _stream_frames(self, window, use_yolo):
        get_frame = window._get_yolo_frame if use_yolo else window._get_camera_frame
        if get_frame() is None:
            self._send_json({"error": "Camera/YOLO frame is not available"}, status=503)
            return

        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Connection", "close")
        self.end_headers()

        try:
            while self.server.api_running:
                frame = get_frame()
                if frame is None:
                    time.sleep(0.1)
                    continue
                ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
                if not ok:
                    time.sleep(0.05)
                    continue
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n")
                self.wfile.write(encoded.tobytes())
                self.wfile.write(b"\r\n")
                self.wfile.flush()
                time.sleep(0.05)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, format, *args):
        print(f"[API] {self.address_string()} - {format % args}")


class TrackingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class CameraThread(QThread):
    def __init__(self, index: int):
        super().__init__()
        self._index   = index
        self._cap     = None
        self._frame   = None
        self._mutex   = QMutex()
        self._running = False
        self._fps_ts: deque = deque(maxlen=60)

    def run(self):
        self._cap = cv2.VideoCapture(self._index)
        if not self._cap.isOpened():
            return
        self._cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self._running = True
        while self._running:
            ok, frame = self._cap.read()
            if not ok:
                time.sleep(0.005)
                continue
            with QMutexLocker(self._mutex):
                self._frame = frame
            self._fps_ts.append(time.perf_counter())

    def stop(self):
        self._running = False
        self.wait(2000)
        if self._cap:
            self._cap.release()
            self._cap = None

    def get_latest_frame(self):
        with QMutexLocker(self._mutex):
            return self._frame if self._frame is not None else None

    def get_cam_fps(self) -> float:
        ts = list(self._fps_ts)
        if len(ts) < 2:
            return 0.0
        elapsed = ts[-1] - ts[0]
        return (len(ts) - 1) / elapsed if elapsed > 0 else 0.0

    def is_opened(self) -> bool:
        return self._cap is not None and self._cap.isOpened()


class DetectResult:
    __slots__ = ("annotated", "tracked_boxes", "orig_frame")
    def __init__(self, annotated, tracked_boxes, orig_frame):
        self.annotated     = annotated
        self.tracked_boxes = tracked_boxes
        self.orig_frame    = orig_frame


class DetectThread(QThread):
    result_ready = pyqtSignal(object)

    def __init__(self, model_path: str):
        super().__init__()
        self._model_path  = model_path
        self._model       = None
        self._pending     = None
        self._mutex       = QMutex()
        self._cond        = threading.Event()
        self._running     = False
        self._fps_ts: deque = deque(maxlen=30)
        self._name_lookup = {}

    @staticmethod
    def _extract_label(raw: str) -> str:
        s = str(raw)
        for sep in ("'", '"'):
            parts = s.split(sep)
            if len(parts) >= 3:
                candidate = parts[-2].strip()
                if candidate:
                    return candidate
        return s.strip()

    def _build_name_lookup(self):
        self._name_lookup = {}
        for idx, raw in self._model.names.items():
            self._name_lookup[int(idx)] = self._extract_label(raw)
        print(f"[DetectThread] Labels: {self._name_lookup}")

    def _cls_name(self, box) -> str:
        return self._name_lookup.get(int(box.cls[0]), "")

    def run(self):
        try:
            self._model = YOLO(self._model_path)
            self._build_name_lookup()
        except Exception as e:
            print(f"[DetectThread] Không tải được model: {e}")
            return
        self._running = True
        while self._running:
            self._cond.wait(timeout=0.5)
            self._cond.clear()
            with QMutexLocker(self._mutex):
                frame = self._pending
                self._pending = None
            if frame is None or not self._running:
                continue
            if YOLO_INPUT_SIZE is not None:
                frame_yolo = cv2.resize(frame, YOLO_INPUT_SIZE)
            else:
                frame_yolo = frame
            try:
                results = self._model.track(
                    frame_yolo, conf=YOLO_CONF, iou=YOLO_IOU,
                    persist=True, verbose=False)
            except Exception as e:
                print(f"[DetectThread] Inference error: {e}")
                continue
            annotated     = results[0].plot()
            tracked_boxes = self._parse_results(results, orig_shape=frame.shape)
            self._fps_ts.append(time.perf_counter())
            self.result_ready.emit(DetectResult(annotated, tracked_boxes, frame))

    def submit_frame(self, frame: np.ndarray):
        with QMutexLocker(self._mutex):
            self._pending = frame
        self._cond.set()

    def stop(self):
        self._running = False
        self._cond.set()
        self.wait(3000)

    def get_detect_fps(self) -> float:
        ts = list(self._fps_ts)
        if len(ts) < 2:
            return 0.0
        elapsed = ts[-1] - ts[0]
        return (len(ts) - 1) / elapsed if elapsed > 0 else 0.0

    def _parse_results(self, results, orig_shape=None) -> list:
        tracked_boxes = []
        if YOLO_INPUT_SIZE is not None and orig_shape is not None:
            scale_x = orig_shape[1] / YOLO_INPUT_SIZE[0]
            scale_y = orig_shape[0] / YOLO_INPUT_SIZE[1]
        else:
            scale_x = scale_y = 1.0
        for r in results:
            if r.boxes is None:
                continue
            all_labels = set(self._cls_name(b) for b in r.boxes)
            if LABEL_NO_BEARING in all_labels:
                result_int, anchor_label = 1, LABEL_NO_BEARING
            elif LABEL_GEAR in all_labels and LABEL_STAIN in all_labels:
                result_int, anchor_label = 3, LABEL_GEAR
            elif LABEL_GEAR in all_labels and LABEL_CHIPPED in all_labels:
                result_int, anchor_label = 2, LABEL_GEAR
            elif LABEL_GEAR in all_labels:
                result_int, anchor_label = 0, LABEL_GEAR
            else:
                continue
            for box in r.boxes:
                if self._cls_name(box) != anchor_label or box.id is None:
                    continue
                tid = int(box.id[0])
                x1, y1, x2, y2 = box.xyxy[0].tolist()
                cx = int(((x1 + x2) / 2) * scale_x)
                cy = int(((y1 + y2) / 2) * scale_y)
                tracked_boxes.append((tid, cx, cy, result_int))
                break
        return tracked_boxes


class VirtualLineTracker:
    def __init__(self, line_x_ratio: float = 0.5):
        self.line_x_ratio       = line_x_ratio
        self._tracks: dict      = {}
        self._frame_w           = 640
        self._global_last_count = 0.0

    def set_line_ratio(self, ratio: float):
        self.line_x_ratio = max(0.05, min(0.95, ratio))

    def get_line_x(self) -> int:
        return int(self._frame_w * self.line_x_ratio)

    def _side(self, cx: int, line_x: int):
        if cx < line_x - CROSS_MARGIN:
            return "left"
        if cx > line_x + CROSS_MARGIN:
            return "right"
        return None

    def update(self, frame_w: int, tracked_boxes: list) -> list:
        self._frame_w = frame_w
        line_x     = self.get_line_x()
        crossed    = []
        now        = time.time()
        active_ids = set()
        for (tid, cx, cy, result) in tracked_boxes:
            active_ids.add(tid)
            side_now = self._side(cx, line_x)
            if tid not in self._tracks:
                self._tracks[tid] = {
                    "side": side_now, "counted": False,
                    "last_count_time": 0.0, "result": result, "cx": cx
                }
                continue
            t = self._tracks[tid]
            prev_side = t["side"]
            t["result"] = result
            t["cx"]     = cx
            if t["counted"] and (now - t["last_count_time"]) > COUNT_COOLDOWN:
                t["counted"] = False
            if side_now is not None:
                side_changed = (prev_side is not None) and (side_now != prev_side)
                if side_changed and not t["counted"]:
                    if (now - self._global_last_count) >= COUNT_COOLDOWN:
                        t["counted"]            = True
                        t["last_count_time"]    = now
                        self._global_last_count = now
                        crossed.append((tid, result))
                t["side"] = side_now
        gone_ids = set(self._tracks.keys()) - active_ids
        for gid in gone_ids:
            if (now - self._tracks[gid]["last_count_time"]) > COUNT_COOLDOWN * 3:
                del self._tracks[gid]
        return crossed

    def draw_line(self, frame: np.ndarray, triggered: bool = False) -> np.ndarray:
        h, w = frame.shape[:2]
        line_x = int(w * self.line_x_ratio)
        color  = LINE_TRIGGERED if triggered else LINE_CLR
        cv2.line(frame, (line_x, 0), (line_x, h), color, LINE_THICKNESS)
        label = "[ COUNTING LINE ]"
        fs = 0.55
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, fs, 1)
        tx, ty = line_x + 10, 30
        cv2.rectangle(frame, (tx-4, ty-th-4), (tx+tw+4, ty+4), (0, 0, 0), -1)
        cv2.putText(frame, label, (tx, ty), cv2.FONT_HERSHEY_SIMPLEX, fs, color, 1, cv2.LINE_AA)
        arr_y = h // 2
        cv2.arrowedLine(frame, (line_x-18, arr_y), (line_x-2, arr_y), color, 2, tipLength=0.4)
        cv2.arrowedLine(frame, (line_x+18, arr_y), (line_x+2, arr_y), color, 2, tipLength=0.4)
        return frame

    def clear(self):
        self._tracks.clear()


class _KalmanTrack:
    """Kalman filter cho 1 đối tượng. State: [cx, cy, vx, vy]."""
    _id_counter = 0

    def __init__(self, cx: float, cy: float, result_int: int, bw: float = 80, bh: float = 80):
        _KalmanTrack._id_counter += 1
        self.track_id          = _KalmanTrack._id_counter
        self.result_int        = result_int
        self.hits              = 1   # số lần detect liên tiếp
        self.time_since_update = 0   # số frame kể từ lần detect cuối
        self.bw                = bw
        self.bh                = bh

        self.kf = cv2.KalmanFilter(4, 2)
        # Ma trận chuyển đổi trạng thái: cx+=vx, cy+=vy
        self.kf.transitionMatrix = np.array([
            [1, 0, 1, 0],
            [0, 1, 0, 1],
            [0, 0, 1, 0],
            [0, 0, 0, 1],
        ], dtype=np.float32)
        # Ma trận quan sát: chỉ đo cx, cy
        self.kf.measurementMatrix = np.array([
            [1, 0, 0, 0],
            [0, 1, 0, 0],
        ], dtype=np.float32)
        # Nhiễu quá trình (tăng nếu vật tăng/giảm tốc không đều)
        self.kf.processNoiseCov = np.eye(4, dtype=np.float32) * 0.03
        self.kf.processNoiseCov[2, 2] = 0.5
        self.kf.processNoiseCov[3, 3] = 0.5
        # Nhiễu đo
        self.kf.measurementNoiseCov = np.eye(2, dtype=np.float32) * 2.0
        self.kf.errorCovPost        = np.eye(4, dtype=np.float32) * 10.0
        self.kf.statePost = np.array([[cx], [cy], [0.0], [0.0]], dtype=np.float32)

    def predict(self):
        self.kf.predict()
        self.time_since_update += 1

    def update(self, cx: float, cy: float, result_int: int, bw: float, bh: float):
        self.kf.correct(np.array([[cx], [cy]], dtype=np.float32))
        self.result_int        = result_int
        self.time_since_update = 0
        self.hits             += 1
        self.bw, self.bh       = bw, bh

    def get_cx_cy(self):
        s = self.kf.statePost
        # Trỏ chính xác vào dòng 0 cột 0, và dòng 1 cột 0 để lấy giá trị số thuần túy
        return float(s[0, 0]), float(s[1, 0])

    def get_bbox(self):
        cx, cy = self.get_cx_cy()
        return (cx - self.bw/2, cy - self.bh/2, cx + self.bw/2, cy + self.bh/2)

    @staticmethod
    def reset_id():
        _KalmanTrack._id_counter = 0


def _iou_boxes(b1, b2):
    ix1 = max(b1[0], b2[0]); iy1 = max(b1[1], b2[1])
    ix2 = min(b1[2], b2[2]); iy2 = min(b1[3], b2[3])
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    if inter == 0:
        return 0.0
    a1 = (b1[2]-b1[0]) * (b1[3]-b1[1])
    a2 = (b2[2]-b2[0]) * (b2[3]-b2[1])
    return inter / (a1 + a2 - inter + 1e-6)


class KalmanTracker:
    """
    Quản lý nhiều _KalmanTrack.

    Cách dùng — thay thế tracked_boxes YOLO bằng kết quả Kalman:
        stable = kalman_tracker.update(raw_detections_from_yolo)
        # stable có cùng format (tid, cx, cy, result_int) như tracked_boxes cũ

    Tham số:
        max_age   : số frame không có detection → xóa track  (mặc định 8)
        min_hits  : detect liên tiếp trước khi track được dùng (mặc định 2)
        iou_thresh: ngưỡng IoU để ghép detection với track   (mặc định 0.25)
    """

    def __init__(self, max_age: int = 8, min_hits: int = 2, iou_thresh: float = 0.25):
        self.max_age    = max_age
        self.min_hits   = min_hits
        self.iou_thresh = iou_thresh
        self._tracks: list = []

    # ------------------------------------------------------------------
    def update(self, detections: list) -> list:
        """
        detections : [(tid_yolo, cx, cy, result_int), ...]  ← format từ _parse_results
        Trả về     : [(track_id, cx_kalman, cy_kalman, result_int), ...]
                     chỉ gồm track ổn định (hits >= min_hits)
        """
        # Chuyển sang format nội bộ (cx, cy, bw=80, bh=80, result_int)
        dets = [(cx, cy, 80.0, 80.0, res) for (_, cx, cy, res) in detections]

        # 1. Predict tất cả track
        for t in self._tracks:
            t.predict()

        # 2. Greedy IoU matching
        matched, unmatched_dets = self._match(dets)

        # 3. Update track được match
        for d_i, t_i in matched:
            cx, cy, bw, bh, res = dets[d_i]
            self._tracks[t_i].update(cx, cy, res, bw, bh)

        # 4. Tạo track mới
        for d_i in unmatched_dets:
            cx, cy, bw, bh, res = dets[d_i]
            self._tracks.append(_KalmanTrack(cx, cy, res, bw, bh))

        # 5. Xóa track quá cũ
        self._tracks = [t for t in self._tracks if t.time_since_update <= self.max_age]

        # 6. Trả về track ổn định
        out = []
        for t in self._tracks:
            if t.hits >= self.min_hits or t.time_since_update == 0:
                cx, cy = t.get_cx_cy()
                out.append((t.track_id, int(cx), int(cy), t.result_int))
        return out

    # ------------------------------------------------------------------
    def _match(self, dets):
        if not self._tracks or not dets:
            return [], list(range(len(dets)))

        pairs = []
        for d_i, (cx, cy, bw, bh, _) in enumerate(dets):
            det_box = (cx-bw/2, cy-bh/2, cx+bw/2, cy+bh/2)
            for t_i, trk in enumerate(self._tracks):
                iou = _iou_boxes(det_box, trk.get_bbox())
                if iou >= self.iou_thresh:
                    pairs.append((iou, d_i, t_i))
        pairs.sort(reverse=True)

        matched, used_d, used_t = [], set(), set()
        for iou_val, d_i, t_i in pairs:
            if d_i in used_d or t_i in used_t:
                continue
            matched.append((d_i, t_i))
            used_d.add(d_i); used_t.add(t_i)

        unmatched_dets = [d for d in range(len(dets)) if d not in used_d]
        return matched, unmatched_dets

    # ------------------------------------------------------------------
    def clear(self):
        self._tracks.clear()
        _KalmanTrack.reset_id()


# ==============================================================================
# TOGGLE SWITCH
# ==============================================================================

class ToggleSwitch(QWidget):
    def __init__(self, color=OK, parent=None):
        super().__init__(parent)
        self._checked  = False
        self._color    = QColor(color)
        self._handle_x = 4.0
        self.setFixedSize(72, 32)
        self._anim = QPropertyAnimation(self, b"handle_x", self)
        self._anim.setDuration(180)
        self._anim.setEasingCurve(QEasingCurve.InOutCubic)

    def get_handle_x(self): return self._handle_x
    def set_handle_x(self, v):
        self._handle_x = v
        self.update()
    handle_x = pyqtProperty(float, get_handle_x, set_handle_x)

    def isChecked(self): return self._checked

    def setChecked(self, val):
        self._checked = val
        self._anim.stop()
        self._anim.setStartValue(self._handle_x)
        self._anim.setEndValue(36.0 if val else 4.0)
        self._anim.start()

    def mousePressEvent(self, e):
        self.setChecked(not self._checked)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        if self._checked:
            g = QLinearGradient(0, 0, w, 0)
            g.setColorAt(0, self._color.darker(140))
            g.setColorAt(1, self._color)
            p.setBrush(QBrush(g))
            p.setPen(QPen(self._color.darker(110), 1))
        else:
            p.setBrush(QBrush(QColor("#1E2330")))
            p.setPen(QPen(QColor("#2E3550"), 1))
        p.drawRoundedRect(0, 0, w, h, h/2, h/2)
        hd = h - 8
        p.setBrush(QBrush(QColor("#FFFFFF") if self._checked else QColor("#4A5270")))
        p.setPen(Qt.NoPen)
        p.drawEllipse(int(self._handle_x), 4, hd, hd)


# ==============================================================================
# STAT CARD
# ==============================================================================

class StatCard(QWidget):
    def __init__(self, title, color, parent=None):
        super().__init__(parent)
        self.setFixedHeight(80)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setStyleSheet(f"""
            StatCard {{
                background: {CARD_BG};
                border: 1px solid {BORDER};
                border-left: 3px solid {color};
                border-radius: 8px;
            }}
        """)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 0, 14, 0)
        col = QVBoxLayout()
        col.setSpacing(1)
        t = QLabel(title)
        t.setFont(QFont("Consolas", 9))
        t.setStyleSheet(f"color:{TEXT_SEC};")
        self._val = QLabel("0")
        self._val.setFont(QFont("Consolas", 28, QFont.Bold))
        self._val.setStyleSheet(f"color:{color};")
        col.addWidget(t)
        col.addWidget(self._val)
        lay.addLayout(col)

    def setValue(self, v):
        self._val.setText(str(v))


# ==============================================================================
# ERROR ROW
# ==============================================================================

class ErrorRow(QWidget):
    def __init__(self, eid, name, color=ACCENT2, parent=None):
        super().__init__(parent)
        self._count = 0
        self.setFixedHeight(50)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setStyleSheet(f"ErrorRow{{background:{CARD_BG};border:1px solid {BORDER};border-radius:6px;}}")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 0, 14, 0)
        badge = QLabel(f"E{eid}")
        badge.setFixedSize(28, 20)
        badge.setAlignment(Qt.AlignCenter)
        badge.setFont(QFont("Consolas", 8, QFont.Bold))
        badge.setStyleSheet(f"background:{color}22;color:{color};border:1px solid {color}55;border-radius:4px;")
        sig = QLabel(f"→ {eid}")
        sig.setFixedSize(32, 20)
        sig.setAlignment(Qt.AlignCenter)
        sig.setFont(QFont("Consolas", 8, QFont.Bold))
        sig.setStyleSheet(f"background:{BORDER};color:{TEXT_SEC};border:1px solid {BORDER};border-radius:4px;")
        lbl = QLabel(name)
        lbl.setFont(QFont("Consolas", 10))
        lbl.setStyleSheet(f"color:{TEXT_PRI};")
        self._cnt = QLabel("0")
        self._cnt.setFont(QFont("Consolas", 20, QFont.Bold))
        self._cnt.setStyleSheet(f"color:{color};")
        self._cnt.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        lay.addWidget(badge)
        lay.addSpacing(6)
        lay.addWidget(sig)
        lay.addSpacing(8)
        lay.addWidget(lbl)
        lay.addStretch()
        lay.addWidget(self._cnt)

    def increment(self):
        self._count += 1
        self._cnt.setText(str(self._count))

    def reset(self):
        self._count = 0
        self._cnt.setText("0")


# ==============================================================================
# STATUS BANNER
# ==============================================================================

class StatusBanner(QWidget):
    CONFIGS = {
        "scanning": (TEXT_SEC,  "● Đang quét..."),
        "ok":       (OK,        "✔ Bình thường — ĐÃ QUA LINE"),
        "e1":       ("#FF6B9D", "✖ Lỗi 1 – Thiếu vòng bi — ĐÃ QUA LINE"),
        "e2":       (ACCENT2,   "✖ Lỗi 2 – Sứt mẻ — ĐÃ QUA LINE"),
        "e3":       (WARN,      "✖ Lỗi 3 – Bánh răng bẩn — ĐÃ QUA LINE"),
        "no_gear":  (TEXT_SEC,  "⊘ Không thấy bánh răng"),
        "detected": (ACCENT,    "◉ Phát hiện — chờ qua line..."),
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(44)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setStyleSheet(f"background:{CARD_BG};border:1px solid {BORDER};border-radius:8px;")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 0, 16, 0)
        self._lbl = QLabel("● Chờ camera...")
        self._lbl.setFont(QFont("Consolas", 11, QFont.Bold))
        self._lbl.setStyleSheet(f"color:{TEXT_SEC};")
        lay.addWidget(self._lbl)
        lay.addStretch()
        self._serial_lbl = QLabel("Serial: —")
        self._serial_lbl.setFont(QFont("Consolas", 9))
        self._serial_lbl.setStyleSheet(f"color:{TEXT_SEC};")
        lay.addWidget(self._serial_lbl)

    def setState(self, key, serial_val=None):
        color, text = self.CONFIGS.get(key, (TEXT_SEC, key))
        self._lbl.setText(text)
        self._lbl.setStyleSheet(f"color:{color};")
        if serial_val is not None:
            self._serial_lbl.setText(f"→ Arduino: {serial_val}")
            self._serial_lbl.setStyleSheet(f"color:{ACCENT};")
        else:
            self._serial_lbl.setText("Serial: —")
            self._serial_lbl.setStyleSheet(f"color:{TEXT_SEC};")


# ==============================================================================
# CỬA SỔ CHÍNH
# ==============================================================================

class MainWindow(QMainWindow):
    # Signal bridge: Firebase daemon thread → Qt main thread
    # QTimer.singleShot() KHÔNG hoạt động từ daemon thread vì không có event loop
    _firebase_cmd_signal = pyqtSignal(str, object)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Hệ thống kiểm tra bánh răng v4.0")
        self.setMinimumSize(1180, 720)

        self._total           = 0
        self._total_err       = 0
        self._api_lock        = threading.Lock()
        self._api_counts      = {
            "self_total": 0,
            "self_total_err": 0,
            "self_e0_count": 0,
            "self_e1_count": 0,
            "self_e2_count": 0,
            "self_e3_count": 0,
        }
        self._api_server      = None
        self._api_thread      = None
        self._firebase_ok     = False
        self._trigger_timer   = 0.0
        self._annotated_frame = None
        self._annotated_lock  = threading.Lock()
        self._display_fps_ts: deque = deque(maxlen=60)
        self._fb_executor     = ThreadPoolExecutor(max_workers=2, thread_name_prefix="firebase")
        self._tracker         = VirtualLineTracker(LINE_POSITION)
        self._kalman          = KalmanTracker(max_age=8, min_hits=2, iou_thresh=0.25)
        self._ser             = None

        # Trạng thái phần cứng
        self._conveyor_on     = False
        self._hw_speed        = 150
        self._last_fb_cmd_ts  = ""   # tránh xử lý lại lệnh cũ
        # Kết nối signal Firebase → execute (thread-safe)
        self._firebase_cmd_signal.connect(self._execute_hw_command)

        # Threads
        self._cam_thread    = CameraThread(CAMERA_INDEX)
        self._detect_thread = DetectThread(MODEL_PATH)
        self._detect_thread.result_ready.connect(self._on_detect_result)
        self._detect_thread.start()

        if USE_SERIAL:
            self._init_serial()

        # Timers
        self._ui_timer = QTimer()
        self._ui_timer.timeout.connect(self._ui_tick)
        self._fps_timer = QTimer()
        self._fps_timer.timeout.connect(self._update_fps_label)
        self._fps_timer.start(1000)

        # Timer đọc Serial liên tục mỗi 100ms
        self._serial_reader_timer = QTimer()
        self._serial_reader_timer.timeout.connect(self._read_continuous_serial)
        self._serial_reader_timer.start(100)

        self._build_ui()

        self.setStyleSheet(
            f"QMainWindow,QWidget{{background:{BG};color:{TEXT_PRI};}}"
            f"QLabel{{background:transparent;}}"
            f"QComboBox{{background:{CARD_BG};color:{TEXT_PRI};border:1px solid {BORDER};border-radius:6px;padding:4px 10px;}}"
            f"QSlider::groove:horizontal{{background:{BORDER};height:4px;border-radius:2px;}}"
            f"QSlider::handle:horizontal{{background:{ACCENT};width:14px;height:14px;margin:-5px 0;border-radius:7px;}}"
            f"QSlider::sub-page:horizontal{{background:{ACCENT}55;border-radius:2px;}}"
        )

        if USE_FIREBASE:
            ok = self._init_firebase()
            lbl = "Firebase: ✔ Đã kết nối — lắng nghe lệnh web" if ok else "Firebase: ✖ Lỗi key.json"
            clr = OK if ok else ACCENT2
            self._fb_lbl.setText(lbl)
            self._fb_lbl.setStyleSheet(f"color:{clr};font-family:Consolas;font-size:8pt;")
            if ok:
                self._start_firebase_listener()

        if USE_SERIAL and self._ser and self._ser.is_open:
            t = f"Serial: ✔ {self._ser.port}"
            self._serial_status_lbl.setText(t)
            self._serial_status_lbl.setStyleSheet(f"color:{OK};font-family:Consolas;font-size:8pt;")
        else:
            t = f"Serial: ✖ Không kết nối {SERIAL_PORT}"
            self._serial_status_lbl.setText(t)
            self._serial_status_lbl.setStyleSheet(f"color:{ACCENT2};font-family:Consolas;font-size:8pt;")
        self._start_api_server()

    # ------------------------------------------------------------------
    # KHỞI TẠO
    # ------------------------------------------------------------------

    def _start_api_server(self):
        try:
            server = TrackingHTTPServer((API_HOST, API_PORT), TrackingAPIHandler)
            server.window = self
            server.api_running = True
            self._api_server = server
            self._api_thread = threading.Thread(
                target=server.serve_forever, daemon=True, name="TrackingAPI")
            self._api_thread.start()
            print(f"[API] Listening on http://{API_HOST}:{API_PORT}")
        except OSError as error:
            print(f"[API] Không mở được cổng {API_PORT}: {error}")

    def _api_status(self):
        with self._api_lock:
            status = dict(self._api_counts)
        status.update({
            "camera_running": self._cam_thread.isRunning(),
            "yolo_running": self._detect_thread.isRunning(),
            "cam_fps": round(self._cam_thread.get_cam_fps(), 1),
            "detect_fps": round(self._detect_thread.get_detect_fps(), 1),
        })
        return status

    def _get_camera_frame(self):
        if not self._cam_thread.isRunning():
            return None
        frame = self._cam_thread.get_latest_frame()
        return frame.copy() if frame is not None else None

    def _get_yolo_frame(self):
        if not self._cam_thread.isRunning():
            return None
        with self._annotated_lock:
            return self._annotated_frame.copy() if self._annotated_frame is not None else None

    def _init_serial(self, port=None):
        target = port or SERIAL_PORT
        if self._ser and self._ser.is_open:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None
        try:
            self._ser = serial.Serial(port=target, baudrate=SERIAL_BAUD, timeout=1, write_timeout=2)
            time.sleep(2)
            self._ser.reset_input_buffer()
            self._ser.reset_output_buffer()
            print(f"[OK] Serial: {target} @ {SERIAL_BAUD}")
            return True
        except Exception as e:
            print(f"[LỖI] Serial ({target}): {e}")
            self._ser = None
            return False

    def _init_firebase(self):
        try:
            if not firebase_admin._apps:
                cred = credentials.Certificate(FIREBASE_KEY)
                firebase_admin.initialize_app(cred, {"databaseURL": FIREBASE_URL})
            self._firebase_ok = True
            print("[OK] Firebase connected")
            return True
        except Exception as e:
            self._firebase_ok = False
            print(f"[LỖI] Firebase: {e}")
            return False

    # ------------------------------------------------------------------
    # FIREBASE LISTENER — polling mỗi 500ms (ổn định hơn .listen())
    # ------------------------------------------------------------------

    def _start_firebase_listener(self):
        """
        Dùng vòng lặp polling thay vì .listen() vì firebase_admin SDK
        đôi khi bỏ qua event đầu tiên hoặc không reconnect sau mất mạng.

        Cách hoạt động:
          - Mỗi 500ms: đọc node 'lenh_dieu_khien' từ Firebase
          - So sánh 'timestamp' với lần đọc trước
          - Nếu khác → lệnh mới → thực thi
          - Ghi trạng thái phần cứng lên Firebase ngay khi khởi động
        """
        self._last_fb_cmd_ts = ""   # timestamp lệnh cuối đã xử lý

        def _poll_loop():
            print("[Firebase] Bắt đầu polling lệnh từ web (mỗi 500ms)...")
            while True:
                try:
                    data = fb_db.reference("lenh_dieu_khien").get()
                    if data and isinstance(data, dict):
                        ts     = str(data.get("timestamp", ""))
                        action = data.get("action", "")
                        value  = data.get("value", None)

                        # Chỉ xử lý khi có lệnh MỚI (timestamp thay đổi)
                        if ts and ts != self._last_fb_cmd_ts and action:
                            self._last_fb_cmd_ts = ts
                            print(f"[Firebase CMD] action={action}  value={value}  ts={ts}")
                            # Dispatch về main thread qua signal (WAJIB — QTimer.singleShot không hoạt động từ daemon thread)
                            self._firebase_cmd_signal.emit(action, value)
                except Exception as e:
                    print(f"[Firebase poll LỖI] {e}")

                time.sleep(0.5)   # polling interval

        t = threading.Thread(target=_poll_loop, daemon=True, name="FirebasePoller")
        t.start()

        # Đẩy trạng thái ban đầu lên Firebase để web hiển thị ngay
        self._fb_executor.submit(self._push_hw_status_to_firebase)

    def _execute_hw_command(self, action: str, value=None):
        if action == "conveyor_start":
            self._conveyor_on = True
            self._send_serial(CMD_CONVEYOR_START)

        elif action == "conveyor_stop":
            self._conveyor_on = False
            self._send_serial(CMD_CONVEYOR_STOP)

        elif action == "stop_all":
            self._conveyor_on = False
            self._send_serial(CMD_CONVEYOR_STOP)

        elif action == "speed_up":
            self._hw_speed = min(255, self._hw_speed + 20)
            self._send_serial(CMD_SPEED_UP)
            self._speed_slider.setValue(self._hw_speed)
            self._speed_val_lbl.setText(str(self._hw_speed))

        elif action == "speed_down":
            self._hw_speed = max(0, self._hw_speed - 20)
            self._send_serial(CMD_SPEED_DOWN)
            self._speed_slider.setValue(self._hw_speed)
            self._speed_val_lbl.setText(str(self._hw_speed))

        elif action == "set_speed":
            if value is not None:
                self._hw_speed = max(0, min(255, int(value)))
                self._send_speed_pwm(self._hw_speed)
                self._speed_slider.setValue(self._hw_speed)
                self._speed_val_lbl.setText(str(self._hw_speed))

        elif action == "reset_counter":
            self._on_reset()

        self._update_hw_ui()
        self._fb_executor.submit(self._push_hw_status_to_firebase)

    def _push_hw_status_to_firebase(self):
        if not self._firebase_ok:
            return
        serial_txt = self._serial_status_lbl.text() if hasattr(self, '_serial_status_lbl') else ""
        try:
            fb_db.reference("trang_thai_phan_cung").set({
                "conveyor_on":    self._conveyor_on,
                "speed":          self._hw_speed,
                "camera_running": self._cam_thread.isRunning(),
                "serial_status":  serial_txt,
                "updated_at":     datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
        except Exception as e:
            print(f"[Firebase push_hw LỖI] {e}")

    # ------------------------------------------------------------------
    # GIAO DIỆN
    # ------------------------------------------------------------------

    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        main = QVBoxLayout(root)
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)

        # Header
        header = QFrame()
        header.setFixedHeight(54)
        header.setStyleSheet(f"background:{PANEL};border-bottom:1px solid {BORDER};")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(20, 0, 20, 0)
        title = QLabel("⚙  HỆ THỐNG KIỂM TRA BÁNH RĂNG  v4.0")
        title.setFont(QFont("Consolas", 12, QFont.Bold))
        title.setStyleSheet(f"color:{ACCENT};")
        self._dot = QLabel("◉  OFFLINE")
        self._dot.setFont(QFont("Consolas", 9, QFont.Bold))
        self._dot.setStyleSheet(f"color:{TEXT_SEC};")
        self._fps_lbl = QLabel("CAM — fps  |  DET — fps")
        self._fps_lbl.setFont(QFont("Consolas", 8))
        self._fps_lbl.setStyleSheet(f"color:{TEXT_SEC};")
        self._port_combo = QComboBox()
        self._port_combo.setFixedWidth(110)
        self._port_combo.setFont(QFont("Consolas", 9))
        self._refresh_ports()
        refresh_btn = QPushButton("↻")
        refresh_btn.setFixedSize(28, 28)
        refresh_btn.setFont(QFont("Consolas", 10))
        refresh_btn.setStyleSheet(f"background:{CARD_BG};color:{TEXT_SEC};border:1px solid {BORDER};border-radius:5px;")
        refresh_btn.clicked.connect(self._refresh_ports)
        hl.addWidget(title)
        hl.addStretch()
        hl.addWidget(self._fps_lbl)
        hl.addSpacing(16)
        hl.addWidget(QLabel("COM:"))
        hl.addSpacing(4)
        hl.addWidget(self._port_combo)
        hl.addWidget(refresh_btn)
        hl.addSpacing(20)
        hl.addWidget(self._dot)
        main.addWidget(header)

        # Body
        body = QHBoxLayout()
        body.setContentsMargins(20, 16, 20, 20)
        body.setSpacing(16)

        # Cột trái: Camera
        left = QVBoxLayout()
        left.setSpacing(10)
        self._cam_label = QLabel("Chưa kết nối camera")
        self._cam_label.setAlignment(Qt.AlignCenter)
        self._cam_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._cam_label.setStyleSheet(
            f"background:#070A10;border:1px solid {BORDER};border-radius:8px;"
            f"color:{TEXT_SEC};font-family:Consolas;font-size:13px;"
        )
        left.addWidget(self._cam_label, 1)

        self._status_banner = StatusBanner()
        left.addWidget(self._status_banner)

        # Line slider
        line_ctrl = QHBoxLayout()
        line_ctrl.setSpacing(8)
        line_icon = QLabel("━")
        line_icon.setFixedWidth(16)
        line_icon.setFont(QFont("Consolas", 14, QFont.Bold))
        line_icon.setStyleSheet(f"color:{ACCENT};")
        line_lbl = QLabel("Vị trí line:")
        line_lbl.setFont(QFont("Consolas", 9))
        line_lbl.setStyleSheet(f"color:{TEXT_SEC};")
        self._line_slider = QSlider(Qt.Horizontal)
        self._line_slider.setRange(5, 95)
        self._line_slider.setValue(int(LINE_POSITION * 100))
        self._line_slider.setFixedHeight(24)
        self._line_slider.valueChanged.connect(self._on_line_slider)
        self._line_pct_lbl = QLabel(f"{int(LINE_POSITION * 100)}%")
        self._line_pct_lbl.setFixedWidth(36)
        self._line_pct_lbl.setFont(QFont("Consolas", 9, QFont.Bold))
        self._line_pct_lbl.setStyleSheet(f"color:{ACCENT};")
        line_ctrl.addWidget(line_icon)
        line_ctrl.addWidget(line_lbl)
        line_ctrl.addWidget(self._line_slider, 1)
        line_ctrl.addWidget(self._line_pct_lbl)
        left.addLayout(line_ctrl)

        # Bật/tắt
        ctrl = QHBoxLayout()
        ctrl.setSpacing(12)
        run_lbl = QLabel("Hệ thống:")
        run_lbl.setFont(QFont("Consolas", 10, QFont.Bold))
        run_lbl.setStyleSheet(f"color:{TEXT_SEC};")
        self._sw = ToggleSwitch()
        def _toggle(e):
            self._sw.setChecked(not self._sw.isChecked())
            self._set_running(self._sw.isChecked())
        self._sw.mousePressEvent = _toggle
        reset_btn = QPushButton("↺  Reset")
        reset_btn.setFixedHeight(40)
        reset_btn.setFont(QFont("Consolas", 10, QFont.Bold))
        reset_btn.setCursor(Qt.PointingHandCursor)
        reset_btn.setStyleSheet(f"""
            QPushButton{{background:{CARD_BG};color:{WARN};border:1px solid {WARN}66;border-radius:8px;padding:0 22px;}}
            QPushButton:hover{{background:{WARN}22;border-color:{WARN};}}
            QPushButton:pressed{{background:{WARN}44;}}
        """)
        reset_btn.clicked.connect(self._on_reset)
        ctrl.addWidget(run_lbl)
        ctrl.addWidget(self._sw)
        ctrl.addStretch()
        ctrl.addWidget(reset_btn)
        left.addLayout(ctrl)
        body.addLayout(left, 3)

        # Cột phải
        right = QVBoxLayout()
        right.setSpacing(10)

        lbl_tk = QLabel("THỐNG KÊ")
        lbl_tk.setFont(QFont("Consolas", 9, QFont.Bold))
        lbl_tk.setStyleSheet(f"color:{TEXT_SEC};letter-spacing:3px;")
        right.addWidget(lbl_tk)

        self._c_total = StatCard("TỔNG SẢN PHẨM (QUA LINE)", ACCENT)
        self._c_err   = StatCard("TỔNG LỖI",                  ACCENT2)
        right.addWidget(self._c_total)
        right.addWidget(self._c_err)

        def _sep():
            s = QFrame()
            s.setFixedHeight(1)
            s.setStyleSheet(f"background:{BORDER};")
            return s

        right.addWidget(_sep())

        lbl_pl = QLabel("PHÂN LOẠI LỖI")
        lbl_pl.setFont(QFont("Consolas", 9, QFont.Bold))
        lbl_pl.setStyleSheet(f"color:{TEXT_SEC};letter-spacing:2px;")
        right.addWidget(lbl_pl)

        self._e0 = ErrorRow(0, "Bình thường",   OK)
        self._e1 = ErrorRow(1, "Thiếu vòng bi", "#FF6B9D")
        self._e2 = ErrorRow(2, "Sứt mẻ",        ACCENT2)
        self._e3 = ErrorRow(3, "Bánh răng bẩn", WARN)
        for e in (self._e0, self._e1, self._e2, self._e3):
            right.addWidget(e)

        right.addWidget(_sep())

        # ── Điều khiển phần cứng ──────────────────────────────────────
        lbl_hw = QLabel("ĐIỀU KHIỂN PHẦN CỨNG  (đồng bộ web Firebase)")
        lbl_hw.setFont(QFont("Consolas", 9, QFont.Bold))
        lbl_hw.setStyleSheet(f"color:{TEXT_SEC};letter-spacing:1px;")
        right.addWidget(lbl_hw)

        # Băng tải
        conv_row = QHBoxLayout()
        conv_row.setSpacing(8)
        for label, action, color in [
            ("▶ Bật băng tải",  "conveyor_start", OK),
            ("■ Dừng băng tải", "conveyor_stop",  ACCENT2),
        ]:
            btn = QPushButton(label)
            btn.setFixedHeight(34)
            btn.setFont(QFont("Consolas", 9, QFont.Bold))
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet(self._hw_btn_style(color))
            btn.clicked.connect(lambda checked=False, a=action: self._execute_hw_command(a))
            conv_row.addWidget(btn)
        right.addLayout(conv_row)

        # PWM slider
        spd_hdr = QLabel("Tốc độ băng tải (PWM 0–255):")
        spd_hdr.setFont(QFont("Consolas", 8))
        spd_hdr.setStyleSheet(f"color:{TEXT_SEC};")
        right.addWidget(spd_hdr)

        spd_row = QHBoxLayout()
        self._speed_slider = QSlider(Qt.Horizontal)
        self._speed_slider.setRange(0, 255)
        self._speed_slider.setValue(self._hw_speed)
        self._speed_slider.setFixedHeight(24)
        self._speed_slider.valueChanged.connect(
            lambda v: self._speed_val_lbl.setText(str(v)))
        self._speed_slider.sliderReleased.connect(
            lambda: self._execute_hw_command("set_speed", self._speed_slider.value()))
        self._speed_val_lbl = QLabel(str(self._hw_speed))
        self._speed_val_lbl.setFixedWidth(36)
        self._speed_val_lbl.setFont(QFont("Consolas", 10, QFont.Bold))
        self._speed_val_lbl.setStyleSheet(f"color:{ACCENT};")
        spd_row.addWidget(self._speed_slider, 1)
        spd_row.addWidget(self._speed_val_lbl)
        right.addLayout(spd_row)

        spd_btn_row = QHBoxLayout()
        spd_btn_row.setSpacing(8)
        for label, action, color in [
            ("⬆ Tăng tốc", "speed_up",   ACCENT),
            ("⬇ Giảm tốc", "speed_down", TEXT_SEC),
        ]:
            btn = QPushButton(label)
            btn.setFixedHeight(28)
            btn.setFont(QFont("Consolas", 8))
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet(self._hw_btn_style(color, small=True))
            btn.clicked.connect(lambda checked=False, a=action: self._execute_hw_command(a))
            spd_btn_row.addWidget(btn)
        right.addLayout(spd_btn_row)

        # Dừng tất cả
        btn_stop = QPushButton("■  DỪNG TẤT CẢ (băng tải)")
        btn_stop.setFixedHeight(34)
        btn_stop.setFont(QFont("Consolas", 9, QFont.Bold))
        btn_stop.setCursor(Qt.PointingHandCursor)
        btn_stop.setStyleSheet(self._hw_btn_style(ACCENT2))
        btn_stop.clicked.connect(lambda: self._execute_hw_command("stop_all"))
        right.addWidget(btn_stop)

        # Trạng thái HW
        self._hw_status_lbl = QLabel("Băng tải: TẮT  |  PWM: 150")
        self._hw_status_lbl.setFont(QFont("Consolas", 8))
        self._hw_status_lbl.setStyleSheet(f"color:{TEXT_SEC};")
        right.addWidget(self._hw_status_lbl)

        right.addWidget(_sep())

        # Firebase / Serial
        self._fb_lbl = QLabel("Firebase: chưa kết nối")
        self._fb_lbl.setFont(QFont("Consolas", 8))
        self._fb_lbl.setStyleSheet(f"color:{TEXT_SEC};")
        right.addWidget(self._fb_lbl)

        self._serial_status_lbl = QLabel("Serial: chưa kết nối")
        self._serial_status_lbl.setFont(QFont("Consolas", 8))
        self._serial_status_lbl.setStyleSheet(f"color:{TEXT_SEC};")
        right.addWidget(self._serial_status_lbl)

        test_btn = QPushButton("🔌  Test Serial (gửi 0)")
        test_btn.setFixedHeight(32)
        test_btn.setFont(QFont("Consolas", 9))
        test_btn.setCursor(Qt.PointingHandCursor)
        test_btn.setStyleSheet(f"""
            QPushButton{{background:{CARD_BG};color:{ACCENT};border:1px solid {ACCENT}66;border-radius:6px;padding:0 12px;}}
            QPushButton:hover{{background:{ACCENT}22;border-color:{ACCENT};}}
            QPushButton:pressed{{background:{ACCENT}44;}}
        """)
        test_btn.clicked.connect(self._test_serial)
        right.addWidget(test_btn)

        # =========================================================
        # TERMINAL KHUNG ĐEN (Nằm dưới nút Test Serial)
        # =========================================================
        lbl_term = QLabel("💻 ARDUINO TERMINAL")
        lbl_term.setFont(QFont("Consolas", 8, QFont.Bold))
        lbl_term.setStyleSheet(f"color:{TEXT_SEC}; margin-top: 8px;")
        right.addWidget(lbl_term)

        self._terminal = QTextEdit()
        self._terminal.setReadOnly(True)
        self._terminal.setFixedHeight(120)
        self._terminal.setStyleSheet(f"""
            QTextEdit {{
                background-color: #070A10;
                color: {OK};
                font-family: Consolas;
                font-size: 11px;
                border: 1px solid {BORDER};
                border-radius: 6px;
                padding: 6px;
            }}
        """)
        right.addWidget(self._terminal)
        # =========================================================

        self._info_lbl = QLabel(
            f"Virtual Line: {int(LINE_POSITION*100)}%  |  Cooldown: {COUNT_COOLDOWN}s  |  "
            f"Web: hethongnhung-a17f3.web.app"
        )
        self._info_lbl.setFont(QFont("Consolas", 7))
        self._info_lbl.setStyleSheet(f"color:{TEXT_SEC};")
        right.addWidget(self._info_lbl)

        right.addStretch()
        body.addLayout(right, 2)
        main.addLayout(body, 1)

    def _hw_btn_style(self, color, small=False):
        return (
            f"QPushButton{{background:{color}22;color:{color};"
            f"border:1px solid {color}55;border-radius:6px;padding:0 10px;}}"
            f"QPushButton:hover{{background:{color}44;border-color:{color};}}"
            f"QPushButton:pressed{{background:{color}66;}}"
        )

    def _update_hw_ui(self):
        conv_clr = OK if self._conveyor_on else TEXT_SEC
        self._hw_status_lbl.setText(
            f"Băng tải: <span style='color:{conv_clr}'>{'BẬT' if self._conveyor_on else 'TẮT'}</span>  |  "
            f"PWM: {self._hw_speed}"
        )
        self._hw_status_lbl.setTextFormat(Qt.RichText)

    # ------------------------------------------------------------------
    # CAMERA & UI TICK
    # ------------------------------------------------------------------

    def _set_running(self, on: bool):
        if on:
            self._cam_thread.start()
            t0 = time.time()
            while not self._cam_thread.is_opened() and time.time() - t0 < 3:
                time.sleep(0.05)
            if not self._cam_thread.is_opened():
                QMessageBox.warning(self, "Camera", f"Không thể mở camera index={CAMERA_INDEX}!")
                self._cam_thread.stop()
                self._sw.setChecked(False)
                return
            if USE_SERIAL and (self._ser is None or not self._ser.is_open):
                port = self._port_combo.currentText()
                if port:
                    ok = self._init_serial(port)
                    c = OK if ok else ACCENT2
                    t = f"Serial: {'✔' if ok else '✖'} {port}"
                    self._serial_status_lbl.setText(t)
                    self._serial_status_lbl.setStyleSheet(f"color:{c};font-family:Consolas;font-size:8pt;")
            self._ui_timer.start(33)
            self._dot.setText("◉  ĐANG CHẠY")
            self._dot.setStyleSheet(f"color:{OK};font-weight:bold;")
            self._fb_executor.submit(self._push_hw_status_to_firebase)
        else:
            self._ui_timer.stop()
            self._cam_thread.stop()
            if self._ser and self._ser.is_open:
                self._send_serial(CMD_CONVEYOR_STOP)
            self._conveyor_on = False
            with self._annotated_lock:
                self._annotated_frame = None
            self._cam_label.clear()
            self._cam_label.setText("Camera đã tắt")
            self._dot.setText("◉  ĐÃ DỪNG")
            self._dot.setStyleSheet(f"color:{WARN};font-weight:bold;")
            self._status_banner.setState("no_gear")
            self._update_hw_ui()
            self._fb_executor.submit(self._push_hw_status_to_firebase)

    def _ui_tick(self):
        frame = self._cam_thread.get_latest_frame()
        if frame is None:
            return
        self._detect_thread.submit_frame(frame)
        triggered = (time.time() - self._trigger_timer) < 0.4
        with self._annotated_lock:
            display_src = self._annotated_frame if self._annotated_frame is not None else frame
        display = display_src.copy()
        display = self._tracker.draw_line(display, triggered=triggered)
        rgb = cv2.cvtColor(display, cv2.COLOR_BGR2RGB)
        h, w, ch = rgb.shape
        if not rgb.flags['C_CONTIGUOUS']:
            rgb = np.ascontiguousarray(rgb)
        img = QImage(rgb.data, w, h, ch * w, QImage.Format_RGB888)
        pix = QPixmap.fromImage(img).scaled(
            self._cam_label.width(), self._cam_label.height(),
            Qt.KeepAspectRatio, Qt.SmoothTransformation
        )
        self._cam_label.setPixmap(pix)
        self._display_fps_ts.append(time.perf_counter())

    # ------------------------------------------------------------------
    # KẾT QUẢ DETECT
    # ------------------------------------------------------------------

    def _on_detect_result(self, det: DetectResult):
        with self._annotated_lock:
            self._annotated_frame = det.annotated
        w_frame = det.orig_frame.shape[1]
        # Đưa kết quả YOLO qua Kalman tracker → bám vật kể cả khi YOLO miss frame
        kalman_boxes = self._kalman.update(det.tracked_boxes)
        if not kalman_boxes:
            self._status_banner.setState("no_gear")
            return
        self._status_banner.setState("detected")
        crossed = self._tracker.update(w_frame, kalman_boxes)
        label_names = {0: "Bình thường", 1: "Thiếu vòng bi", 2: "Sứt mẻ", 3: "Bánh răng bẩn"}
        for (tid, result) in crossed:
            self._trigger_timer = time.time()
            with self._api_lock:
                self._api_counts["self_total"] += 1
                if result in (1, 2, 3):
                    self._api_counts["self_total_err"] += 1
                self._api_counts[f"self_e{result}_count"] += 1
            self._total += 1
            self._c_total.setValue(self._total)
            if result == 0:
                self._e0.increment()
                self._status_banner.setState("ok", serial_val=0)
            elif result == 1:
                self._total_err += 1
                self._c_err.setValue(self._total_err)
                self._e1.increment()
                self._status_banner.setState("e1", serial_val=1)
            elif result == 2:
                self._total_err += 1
                self._c_err.setValue(self._total_err)
                self._e2.increment()
                self._status_banner.setState("e2", serial_val=2)
            elif result == 3:
                self._total_err += 1
                self._c_err.setValue(self._total_err)
                self._e3.increment()
                self._status_banner.setState("e3", serial_val=3)
            self._send_serial(result)
            frame_snap = det.orig_frame.copy()
            self._fb_executor.submit(
                self._send_firebase_bg, frame_snap, result, label_names.get(result, "Không rõ"))

    # ------------------------------------------------------------------
    # FPS LABEL
    # ------------------------------------------------------------------

    def _update_fps_label(self):
        ts = list(self._display_fps_ts)
        disp_fps = (len(ts)-1)/(ts[-1]-ts[0]) if len(ts) >= 2 and ts[-1]-ts[0] > 0 else 0
        cam_fps  = self._cam_thread.get_cam_fps() if self._cam_thread.isRunning() else 0
        det_fps  = self._detect_thread.get_detect_fps()
        self._fps_lbl.setText(f"CAM {cam_fps:.0f}fps  |  DET {det_fps:.1f}fps  |  UI {disp_fps:.0f}fps")

    # ------------------------------------------------------------------
    # LINE SLIDER
    # ------------------------------------------------------------------

    def _on_line_slider(self, val):
        self._tracker.set_line_ratio(val / 100.0)
        self._line_pct_lbl.setText(f"{val}%")

    # ------------------------------------------------------------------
    # SERIAL / TERMINAL
    # ------------------------------------------------------------------

    def _read_continuous_serial(self):
        if self._ser and self._ser.is_open:
            try:
                while self._ser.in_waiting > 0:
                    line = self._ser.readline().decode(errors="replace").strip()
                    if line:
                        time_str = datetime.datetime.now().strftime("%H:%M:%S")
                        log_text = f"[{time_str}] {line}"
                        
                        self._terminal.append(log_text)
                        
                        scrollbar = self._terminal.verticalScrollBar()
                        scrollbar.setValue(scrollbar.maximum())
            except Exception:
                pass

    def _send_serial(self, cmd: int):
        signal = SIGNAL_MAP.get(cmd, cmd) if cmd <= 3 else cmd
        if not (self._ser and self._ser.is_open):
            self._serial_status_lbl.setText("Serial: ✖ Chưa kết nối")
            self._serial_status_lbl.setStyleSheet(f"color:{ACCENT2};font-family:Consolas;font-size:8pt;")
            return
        ser_ref = self._ser
        def _do():
            try:
                ser_ref.write(f"{signal}\n".encode())
                ser_ref.flush()
                QTimer.singleShot(0, lambda: self._on_serial_done(f"Serial: ✔ Đã gửi → {signal}", True))
            except Exception as e:
                QTimer.singleShot(0, lambda: self._on_serial_done(f"Serial: ✖ Lỗi gửi", False))
        threading.Thread(target=_do, daemon=True).start()

    def _send_speed_pwm(self, spd: int):
        if not (self._ser and self._ser.is_open):
            return
        ser_ref = self._ser
        def _do():
            try:
                ser_ref.write(f"S{spd}\n".encode())
                ser_ref.flush()
                QTimer.singleShot(0, lambda: self._on_serial_done(f"Serial: ✔ Gửi PWM={spd}", True))
            except Exception as e:
                QTimer.singleShot(0, lambda: self._on_serial_done(f"Serial: ✖ Lỗi gửi", False))
        threading.Thread(target=_do, daemon=True).start()

    def _on_serial_done(self, text: str, is_ok: bool):
        color = OK if is_ok else ACCENT2
        self._serial_status_lbl.setText(text)
        self._serial_status_lbl.setStyleSheet(f"color:{color};font-family:Consolas;font-size:8pt;")
        self._fb_executor.submit(self._push_hw_status_to_firebase)

    def _test_serial(self):
        port = self._port_combo.currentText()
        if not port:
            QMessageBox.warning(self, "Serial", "Không tìm thấy cổng COM nào!")
            return
        if self._ser is None or not self._ser.is_open:
            ok = self._init_serial(port)
            if not ok:
                QMessageBox.warning(self, "Serial",
                    f"Không thể mở {port}!\n• Arduino đã cắm chưa?\n• Driver CH340/CP210x?\n• Đúng cổng COM chưa?")
                return
        self._send_serial(0)

    def _refresh_ports(self):
        self._port_combo.clear()
        ports = [p.device for p in serial.tools.list_ports.comports()]
        for p in ports:
            self._port_combo.addItem(p)
        if SERIAL_PORT in ports:
            self._port_combo.setCurrentText(SERIAL_PORT)

    # ------------------------------------------------------------------
    # FIREBASE — ghi kết quả kiểm tra
    # ------------------------------------------------------------------

    def _send_firebase_bg(self, frame_bgr: np.ndarray, result: int, loai_loi: str):
        if not self._firebase_ok:
            return
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            small = cv2.resize(frame_bgr, (320, 240))
            _, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 60])
            img_data = "data:image/jpeg;base64," + base64.b64encode(buf).decode()
        except Exception:
            img_data = ""
        data = {
            "thoi_gian":    now,
            "loai_loi":     loai_loi,
            "tin_hieu":     result,
            "do_tin_cay":   85,
            "hinh_anh_vga": img_data,
        }
        try:
            fb_db.reference("lich_su_kiem_tra").push(data)
            txt = f"Firebase: ✔ {now[-8:]}"
            QTimer.singleShot(0, lambda: self._fb_lbl.setText(txt))
            QTimer.singleShot(0, lambda: self._fb_lbl.setStyleSheet(
                f"color:{OK};font-family:Consolas;font-size:8pt;"))
            print(f"[Firebase] {loai_loi} @ {now}")
        except Exception as e:
            self._firebase_ok = False
            err = f"Firebase: ✖ {str(e)[:30]}"
            QTimer.singleShot(0, lambda: self._fb_lbl.setText(err))
            QTimer.singleShot(0, lambda: self._fb_lbl.setStyleSheet(
                f"color:{ACCENT2};font-family:Consolas;font-size:8pt;"))
            print(f"[LỖI Firebase] {e}")

    # ------------------------------------------------------------------
    # RESET
    # ------------------------------------------------------------------

    def _on_reset(self):
        self._total     = 0
        self._total_err = 0
        with self._api_lock:
            for key in self._api_counts:
                self._api_counts[key] = 0
        self._c_total.setValue(0)
        self._c_err.setValue(0)
        for e in (self._e0, self._e1, self._e2, self._e3):
            e.reset()
        self._tracker.clear()
        self._kalman.clear()
        with self._annotated_lock:
            self._annotated_frame = None
        print("[RESET] Đã reset toàn bộ")

    # ------------------------------------------------------------------
    # ĐÓNG
    # ------------------------------------------------------------------

    def closeEvent(self, e):
        self._ui_timer.stop()
        self._fps_timer.stop()
        self._serial_reader_timer.stop()
        if self._api_server:
            self._api_server.api_running = False
            self._api_server.shutdown()
            self._api_server.server_close()
        self._cam_thread.stop()
        self._detect_thread.stop()
        self._fb_executor.shutdown(wait=False)
        if self._ser and self._ser.is_open:
            try:
                self._ser.write(f"{CMD_CONVEYOR_STOP}\n".encode())
                self._ser.flush()
            except Exception:
                pass
            self._ser.close()
        super().closeEvent(e)


# ==============================================================================
# MAIN
# ==============================================================================

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())
