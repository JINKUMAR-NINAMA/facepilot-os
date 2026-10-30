import os
import sys
import time
import traceback
import urllib.request

import cv2
import mediapipe as mp

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR = os.path.join(HERE, "models")
MODEL_PATH = os.path.join(MODEL_DIR, "face_landmarker.task")
MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/face_landmarker/"
             "face_landmarker/float16/1/face_landmarker.task")

print("mediapipe:", mp.__version__, flush=True)
print("mp.tasks attrs:", [a for a in dir(mp.tasks) if not a.startswith("_")], flush=True)
vision = mp.tasks.vision
print("vision face attrs:", [a for a in dir(vision) if "Face" in a or "Running" in a], flush=True)
opts_cls = vision.FaceLandmarkerOptions
print("options fields:", list(getattr(opts_cls, "__dataclass_fields__", {}).keys()), flush=True)

print("STAGE A: model file", flush=True)
try:
    if not os.path.exists(MODEL_PATH):
        os.makedirs(MODEL_DIR, exist_ok=True)
        print("  downloading:", MODEL_URL, flush=True)
        urllib.request.urlretrieve(MODEL_URL, MODEL_PATH)
    print("  model bytes:", os.path.getsize(MODEL_PATH), flush=True)
except Exception:
    traceback.print_exc()
    sys.exit(3)

print("STAGE B: create FaceLandmarker (VIDEO mode)", flush=True)
try:
    options = opts_cls(
        base_options=mp.tasks.BaseOptions(model_asset_path=MODEL_PATH),
        running_mode=vision.RunningMode.VIDEO,
        num_faces=1,
    )
    landmarker = vision.FaceLandmarker.create_from_options(options)
    print("  created", flush=True)
except Exception:
    traceback.print_exc()
    sys.exit(4)

print("STAGE C: detect on live frames", flush=True)
cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
try:
    t0 = time.perf_counter()
    for i in range(30):
        ok, frame = cap.read()
        if not ok:
            print("  frame", i, "read failed", flush=True)
            continue
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        res = landmarker.detect_for_video(img, int((time.perf_counter() - t0) * 1000))
        if i % 10 == 0 or i == 29:
            n = len(res.face_landmarks)
            first = res.face_landmarks[0] if n else None
            print("  frame", i, "faces:", n,
                  "landmarks:", None if first is None else len(first),
                  "nose z:", None if first is None else round(first[1].z, 4), flush=True)
    print("DIAG OK", flush=True)
except Exception:
    traceback.print_exc()
    sys.exit(5)
finally:
    landmarker.close()
    cap.release()