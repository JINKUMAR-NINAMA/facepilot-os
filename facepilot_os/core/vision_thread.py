import os
import sys
import time
import traceback
from dataclasses import dataclass

import cv2
import mediapipe as mp
from PyQt6.QtCore import QThread, pyqtSignal

from config import FacePilotConfig
from core import win32_input
from core.one_euro_filter import OneEuroFilter2D
from core.pose_mapper import PoseMapper, compute_ear

def get_resource_path(relative_path: str) -> str:
    """Get absolute path to resource, works for dev and for PyInstaller."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, relative_path)       # compiled build: unpacked bundle folder
    # dev: this file is in core/, models/ sits next to it in the package root
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), relative_path)


# facepilot_os/models/face_landmarker.task (cwd-independent, dev and frozen)
MODEL_PATH = get_resource_path("models/face_landmarker.task")


@dataclass
class FrameTelemetry:
    timestamp: float
    face_detected: bool
    screen_x: float
    screen_y: float
    velocity: float          # px/sec
    raw_ear: float
    comp_ear: float
    pitch_compensation_factor: float
    yaw: float
    pitch: float
    fps: float
    left_ear: float = 0.0       # pitch-compensated, subject's left eye
    right_ear: float = 0.0
    bar: float = 1.0            # brow raise relative to neutral


class VisionThread(QThread):
    telemetry_signal = pyqtSignal(object)   # FrameTelemetry
    error_signal = pyqtSignal(str)

    def __init__(self, cfg: FacePilotConfig, parent=None) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self._running = True
        self._recenter = False

    def request_recenter(self) -> None:
        self._recenter = True

    def stop(self) -> None:
        self._running = False
        self.wait(3000)

    def run(self) -> None:
        cap = None
        landmarker = None
        try:
            cfg = self.cfg
            if not os.path.exists(MODEL_PATH):
                raise FileNotFoundError(f"Face model not found: {MODEL_PATH}")

            cap = cv2.VideoCapture(cfg.camera_index, cv2.CAP_DSHOW)
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg.frame_width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg.frame_height)
            cap.set(cv2.CAP_PROP_FPS, cfg.fps)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, cfg.buffer_size)
            if not cap.isOpened():
                raise RuntimeError(f"Cannot open camera {cfg.camera_index}")

            vision = mp.tasks.vision
            landmarker = vision.FaceLandmarker.create_from_options(
                vision.FaceLandmarkerOptions(
                    base_options=mp.tasks.BaseOptions(model_asset_path=MODEL_PATH),
                    running_mode=vision.RunningMode.VIDEO,
                    num_faces=1,
                    min_face_detection_confidence=0.5,
                    min_face_presence_confidence=0.5,
                    min_tracking_confidence=0.5,
                ))

            sw, sh = win32_input.screen_size()
            mapper = PoseMapper(cfg, sw, sh)
            flt = OneEuroFilter2D(cfg.min_cutoff, cfg.beta, cfg.d_cutoff,
                                  scale_x=sw - 1, scale_y=sh - 1, window=cfg.velocity_window)

            t0 = time.perf_counter()
            last_ts = -1
            fps, last, last_xy = 0.0, t0, (sw / 2.0, sh / 2.0)

            while self._running:
                try:
                    ok, frame = cap.read()
                    if not ok:
                        self.msleep(2)
                        continue
                    now = time.perf_counter()
                    dt = now - last
                    last = now
                    if dt > 0:
                        fps = 0.9 * fps + 0.1 * (1.0 / dt) if fps else 1.0 / dt

                    h, w = frame.shape[:2]
                    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    img = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)

                    # VIDEO mode requires strictly increasing millisecond timestamps
                    ts = int((now - t0) * 1000)
                    if ts <= last_ts:
                        ts = last_ts + 1
                    last_ts = ts
                    res = landmarker.detect_for_video(img, ts)

                    if not res.face_landmarks:
                        self.telemetry_signal.emit(FrameTelemetry(
                            now, False, last_xy[0], last_xy[1], 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, fps))
                        continue

                    lm = res.face_landmarks[0]
                    if self._recenter:
                        mapper.recenter(lm)
                        flt.reset()
                        self._recenter = False

                    pose = mapper.map(lm)
                    x, y, vel = flt(pose.norm_x, pose.norm_y, now)
                    last_xy = (x, y)
                    raw = (compute_ear(lm, cfg.left_eye, w, h)
                           + compute_ear(lm, cfg.right_eye, w, h)) / 2.0
                    self.telemetry_signal.emit(FrameTelemetry(
                        now, True, x, y, vel, raw, raw * pose.pitch_compensation_factor,
                        pose.pitch_compensation_factor, pose.yaw, pose.pitch, fps,
                        pose.left_ear, pose.right_ear, pose.bar))
                except Exception:
                    # Never let an exception escape the thread (PyQt6 would abort the process)
                    self.error_signal.emit("Frame loop error:\n" + traceback.format_exc())
                    break
        except Exception:
            self.error_signal.emit("VisionThread failed:\n" + traceback.format_exc())
        finally:
            if landmarker is not None:
                landmarker.close()
            if cap is not None:
                cap.release()