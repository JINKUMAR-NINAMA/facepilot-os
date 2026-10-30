import sys

print("python:", sys.version, flush=True)

import cv2
print("cv2:", cv2.__version__, flush=True)

import numpy as np
print("numpy:", np.__version__, flush=True)

import mediapipe as mp
print("mediapipe:", getattr(mp, "__version__", "unknown"), flush=True)
print("public attrs:", [a for a in dir(mp) if not a.startswith("_")], flush=True)
print("has mp.solutions:", hasattr(mp, "solutions"), flush=True)

print("STAGE 1: open camera (CAP_DSHOW)", flush=True)
cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
print("  isOpened:", cap.isOpened(), flush=True)
cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
cap.set(cv2.CAP_PROP_FPS, 60)
cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
print("STAGE 1b: read one frame", flush=True)
ok, frame = cap.read()
print("  read ok:", ok, "shape:", None if frame is None else frame.shape, flush=True)

print("STAGE 2: create FaceMesh", flush=True)
try:
    mesh = mp.solutions.face_mesh.FaceMesh(max_num_faces=1, refine_landmarks=True)
except AttributeError as e:
    print("  FAILED (legacy API missing):", e, flush=True)
    cap.release()
    sys.exit(2)
print("  created", flush=True)

print("STAGE 3: process one frame", flush=True)
rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
res = mesh.process(rgb)
print("  face found:", bool(res.multi_face_landmarks), flush=True)

mesh.close()
cap.release()
print("DIAG OK", flush=True)