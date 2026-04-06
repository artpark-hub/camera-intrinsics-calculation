"""
Remote Feed Ingestion
=====================
Threaded frame grabber for RTSP streams, local video files, and webcams.
Resizes frames to a workable resolution for lower-latency detection.
"""

import cv2
import sys
import threading
import time
from pathlib import Path


TARGET_HEIGHT = 720   # Resize incoming frames to this height


class ThreadedCapture:
    """
    Continuously grabs frames in a background thread so that the main
    processing loop always gets the latest frame without blocking on
    network I/O.
    """

    def __init__(self, cap):
        self.cap = cap
        self.frame = None
        self.ret = False
        self._lock = threading.Lock()
        self._running = True
        self._thread = threading.Thread(target=self._grab_loop, daemon=True)
        self._thread.start()

    def _grab_loop(self):
        while self._running:
            ret, frame = self.cap.read()
            with self._lock:
                self.ret = ret
                self.frame = frame
            if not ret:
                time.sleep(0.01)

    def read(self):
        with self._lock:
            return self.ret, self.frame.copy() if self.frame is not None else None

    def release(self):
        self._running = False
        self._thread.join(timeout=2)
        self.cap.release()

    def isOpened(self):
        return self.cap.isOpened()

    def get(self, prop):
        return self.cap.get(prop)


def open_source(rtsp=None, video=None, camera=None):
    """
    Open and return (source_type, capture_object, frame_size).
    source_type is one of: 'rtsp', 'video', 'camera'
    """
    if rtsp:
        print(f"[INFO] Source : RTSP stream  → {rtsp}")
        cap = cv2.VideoCapture(rtsp, cv2.CAP_FFMPEG)
        if not cap.isOpened():
            print("[ERROR] Could not open RTSP stream.")
            sys.exit(1)
        tcap = ThreadedCapture(cap)
        # Wait for first frame
        for _ in range(50):
            ret, frm = tcap.read()
            if ret and frm is not None:
                break
            time.sleep(0.1)
        if frm is None:
            print("[ERROR] No frames received from RTSP stream.")
            sys.exit(1)
        h, w = frm.shape[:2]
        return "rtsp", tcap, (w, h)

    if video:
        p = Path(video)
        if not p.exists():
            print(f"[ERROR] Video file not found: {video}")
            sys.exit(1)
        print(f"[INFO] Source : local video  → {video}")
        cap = cv2.VideoCapture(str(video))
        if not cap.isOpened():
            print("[ERROR] Could not open video file.")
            sys.exit(1)
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        return "video", cap, (w, h)

    if camera is not None:
        print(f"[INFO] Source : webcam       → index {camera}")
        cap = cv2.VideoCapture(camera)
        if not cap.isOpened():
            print(f"[ERROR] Could not open camera {camera}.")
            sys.exit(1)
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        return "camera", cap, (w, h)

    print("[ERROR] No input source specified.")
    sys.exit(1)


def resize_frame(frame, target_h=TARGET_HEIGHT):
    """Resize frame to target height, preserving aspect ratio."""
    h, w = frame.shape[:2]
    if h <= target_h:
        return frame
    scale = target_h / h
    new_w = int(w * scale)
    return cv2.resize(frame, (new_w, target_h), interpolation=cv2.INTER_AREA)


def read_frame(source_type, source):
    """Read one frame from the source. Returns (success, frame)."""
    if source_type == "rtsp":
        return source.read()
    else:
        return source.read()
