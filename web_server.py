"""
Web Server for iPad Pattern Display & Remote Monitoring
=======================================================
Serves the ChArUco calibration pattern to an iPad (or any device) via
a web page with real-time visual feedback — coloured border overlays
indicate detection state so the user holding the iPad knows whether
the camera can see the board without looking at the laptop.

Endpoints
---------
/            — iPad pattern page (fullscreen ChArUco + border feedback)
/monitor     — MJPEG feed viewer (for a second monitoring device)
/pattern.png — Raw pattern image
/events      — SSE stream of detection state
/feed        — MJPEG stream of annotated camera feed
"""

import json
import logging
import queue
import socket
import threading
import time

import cv2
from flask import Flask, Response

# ── Flask app ────────────────────────────────────────────────────────────────
app = Flask(__name__)

# Silence Flask request logs (noisy during calibration)
logging.getLogger("werkzeug").setLevel(logging.ERROR)

# ── Shared state (written by main loop, read by SSE/MJPEG) ──────────────────
_detection_state = "no_detection"
_instruction = "Waiting for calibration to start..."
_samples = 0
_state_lock = threading.Lock()

_frame_jpg: bytes | None = None
_frame_lock = threading.Lock()

_pattern_png_bytes: bytes | None = None

_sse_clients: list[queue.Queue] = []


# ── Public API (called from main.py) ────────────────────────────────────────

def update_detection_state(state: str, instruction: str = "", samples: int = 0):
    """
    Push a detection-state change to every connected iPad.

    state: one of  no_detection | detected | stable | captured | complete
    """
    global _detection_state, _instruction, _samples
    with _state_lock:
        _detection_state = state
        _instruction = instruction
        _samples = samples

    payload = json.dumps(
        {"detection": state, "instruction": instruction, "samples": samples}
    )

    dead: list[queue.Queue] = []
    for q in _sse_clients:
        try:
            if q.full():
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
            q.put_nowait(payload)
        except Exception:
            dead.append(q)
    for q in dead:
        try:
            _sse_clients.remove(q)
        except ValueError:
            pass


def update_feed_frame(frame):
    """Encode an annotated frame and store it for the MJPEG endpoint."""
    global _frame_jpg
    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
    with _frame_lock:
        _frame_jpg = buf.tobytes()


def set_pattern_png(png_bytes: bytes):
    """Store the pre-rendered pattern PNG for the /pattern.png route."""
    global _pattern_png_bytes
    _pattern_png_bytes = png_bytes


# ── iPad pattern page ────────────────────────────────────────────────────────

IPAD_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport"
      content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black">
<title>MDCT Calibration</title>
<style>
/* ── reset ─────────────────────────────────────────────────────── */
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:100%;height:100%;background:#fff;
          overflow:hidden;-webkit-user-select:none;user-select:none}

/* ── layout ────────────────────────────────────────────────────── */
#container{
  width:100vw;height:100vh;
  display:flex;align-items:center;justify-content:center;
  position:relative;background:#fff;
}
#pattern{
  /* leave room for the border overlay */
  max-width:calc(100vw - 48px);
  max-height:calc(100vh - 48px);
  object-fit:contain;
  position:relative;z-index:1;
}

/* ── border overlay (the primary feedback channel) ─────────────── */
#border-overlay{
  position:absolute;top:0;left:0;right:0;bottom:0;
  border:24px solid transparent;
  pointer-events:none;
  transition:border-color 0.15s ease,border-width 0.15s ease;
  z-index:10;
  border-radius:4px;
}

/* ── detection states ──────────────────────────────────────────── */
.state-no_detection{
  border-color:#ee1111 !important;
  animation:pulse 1s ease-in-out infinite;
}
.state-detected{
  border-color:#ff9900 !important;
  animation:none;
}
.state-stable{
  border-color:#00bb44 !important;
  animation:glow 0.8s ease-in-out infinite alternate;
}
.state-captured{
  border-color:#00ff55 !important;
  animation:captureFlash 0.55s ease-out forwards;
}
.state-complete{
  border-color:#00ff55 !important;
  animation:none;
}

@keyframes pulse{
  0%,100%{opacity:0.25;border-width:24px}
  50%    {opacity:1;   border-width:28px}
}
@keyframes glow{
  from{border-color:#00bb44}
  to  {border-color:#00ff66}
}
@keyframes captureFlash{
  0%  {border-width:44px;border-color:#00ff88;opacity:1}
  100%{border-width:24px;border-color:#00cc44;opacity:1}
}

/* ── status bar (bottom of iPad screen) ────────────────────────── */
#status-bar{
  position:absolute;bottom:0;left:0;right:0;
  background:rgba(0,0,0,0.72);backdrop-filter:blur(6px);
  -webkit-backdrop-filter:blur(6px);
  color:#fff;font:500 15px/1.4 -apple-system,system-ui,sans-serif;
  text-align:center;padding:10px 20px;
  z-index:20;transition:opacity 0.3s;
}

/* ── tap-to-start overlay ──────────────────────────────────────── */
#tap-overlay{
  position:absolute;top:0;left:0;right:0;bottom:0;
  background:rgba(0,0,0,0.88);
  display:flex;flex-direction:column;align-items:center;justify-content:center;
  z-index:100;color:#fff;
  font-family:-apple-system,system-ui,sans-serif;
}
#tap-overlay h2{font-size:24px;margin-bottom:14px;font-weight:600}
#tap-overlay p{font-size:16px;color:#bbb;max-width:320px;
               text-align:center;line-height:1.55}
#tap-overlay .legend{margin-top:28px;text-align:left;font-size:14px;
                     color:#ccc;line-height:2}
#tap-overlay .dot{display:inline-block;width:14px;height:14px;
                  border-radius:50%;margin-right:8px;vertical-align:middle}
.dot-red{background:#ee1111}
.dot-amber{background:#ff9900}
.dot-green{background:#00cc44}
.hidden{display:none !important}
</style>
</head>
<body>
<div id="container">
  <img id="pattern" src="/pattern.png" alt="ChArUco">
  <div id="border-overlay"></div>
  <div id="status-bar">Connecting...</div>

  <!-- tap-to-start splash -------------------------------------------------->
  <div id="tap-overlay" onclick="startCalibration()">
    <h2>MDCT Calibration Pattern</h2>
    <p>Hold this iPad facing the camera.<br>
       The border colour tells you what the camera sees.</p>
    <div class="legend">
      <span class="dot dot-red"></span>  Not visible &mdash; keep moving<br>
      <span class="dot dot-amber"></span> Detected &mdash; hold position<br>
      <span class="dot dot-green"></span> Captured! &mdash; move to next pose
    </div>
    <p style="margin-top:28px;font-size:17px;color:#fff">Tap to begin</p>
  </div>
</div>

<script>
/* ── globals ──────────────────────────────────────────────────────────────── */
const overlay  = document.getElementById('border-overlay');
const statusBar= document.getElementById('status-bar');
const tapOvly  = document.getElementById('tap-overlay');
let curState   = '';
let flashTimer = null;

/* ── fullscreen ───────────────────────────────────────────────────────────── */
function startCalibration(){
  tapOvly.classList.add('hidden');
  const el = document.documentElement;
  if(el.requestFullscreen)        el.requestFullscreen();
  else if(el.webkitRequestFullscreen) el.webkitRequestFullscreen();
}

/* ── state machine ────────────────────────────────────────────────────────── */
function setState(s){
  if(s===curState) return;

  /* 'captured' is a momentary flash — revert to detected afterwards */
  if(s==='captured'){
    overlay.className='state-captured';
    curState='captured';
    if(flashTimer) clearTimeout(flashTimer);
    flashTimer=setTimeout(()=>{ overlay.className='state-detected'; curState='detected'; },750);
    return;
  }

  overlay.className='';
  void overlay.offsetWidth;          /* force reflow so animation restarts */
  if(s) overlay.className='state-'+s;
  curState=s;
}

/* ── SSE with auto-reconnect ──────────────────────────────────────────────── */
function connectSSE(){
  const es = new EventSource('/events');

  es.onmessage = function(e){
    try{
      const d = JSON.parse(e.data);
      setState(d.detection || 'no_detection');

      let txt = d.instruction || '';
      if(d.samples > 0) txt = '\u2714 '+d.samples+' captured  \u2014  '+txt;
      statusBar.textContent = txt;
    }catch(err){ console.error('SSE parse',err); }
  };

  es.onerror = function(){
    statusBar.textContent = 'Connection lost \u2014 reconnecting\u2026';
    es.close();
    setTimeout(connectSSE, 2000);
  };
}
connectSSE();

/* ── keep screen awake ────────────────────────────────────────────────────── */
if('wakeLock' in navigator){
  navigator.wakeLock.request('screen').catch(()=>{});
}
document.addEventListener('visibilitychange',()=>{
  if(document.visibilityState==='visible' && 'wakeLock' in navigator)
    navigator.wakeLock.request('screen').catch(()=>{});
});
</script>
</body>
</html>"""


# ── Feed monitor page ────────────────────────────────────────────────────────

MONITOR_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MDCT Feed Monitor</title>
<style>
*{margin:0;padding:0}
body{background:#111;display:flex;align-items:center;
     justify-content:center;height:100vh;overflow:hidden;
     font-family:-apple-system,system-ui,sans-serif}
img{max-width:100vw;max-height:100vh;object-fit:contain}
#msg{color:#666;font-size:18px;position:absolute}
</style>
</head>
<body>
<span id="msg">Waiting for feed&hellip;</span>
<img id="f" src="/feed"
     onerror="this.style.display='none'"
     onload="document.getElementById('msg').style.display='none';
             this.style.display='block'">
</body>
</html>"""


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return Response(IPAD_PAGE, mimetype="text/html")


@app.route("/monitor")
def monitor():
    return Response(MONITOR_PAGE, mimetype="text/html")


@app.route("/pattern.png")
def pattern_image():
    if _pattern_png_bytes is None:
        return Response("Pattern not ready", status=503)
    return Response(_pattern_png_bytes, mimetype="image/png")


@app.route("/events")
def sse_events():
    """Server-Sent Events — pushes detection state to the iPad in real time."""

    def stream():
        q: queue.Queue = queue.Queue(maxsize=60)
        _sse_clients.append(q)
        try:
            # Immediately send current state so the page doesn't wait
            with _state_lock:
                init = json.dumps(
                    {
                        "detection": _detection_state,
                        "instruction": _instruction,
                        "samples": _samples,
                    }
                )
            yield f"data: {init}\n\n"

            while True:
                try:
                    data = q.get(timeout=15)
                    yield f"data: {data}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        except GeneratorExit:
            pass
        finally:
            try:
                _sse_clients.remove(q)
            except ValueError:
                pass

    resp = Response(stream(), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"  # nginx compatibility
    return resp


@app.route("/feed")
def mjpeg_feed():
    """MJPEG stream of the annotated camera feed — open /monitor to view."""

    def generate():
        while True:
            with _frame_lock:
                jpg = _frame_jpg
            if jpg:
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n\r\n" + jpg + b"\r\n"
                )
            time.sleep(0.05)  # cap at ~20 fps

    return Response(
        generate(), mimetype="multipart/x-mixed-replace; boundary=frame"
    )


# ── Server lifecycle ─────────────────────────────────────────────────────────

def get_local_ip() -> str:
    """Best-effort detection of the LAN IP reachable from other devices."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def start_server(port: int = 8080) -> tuple[str, int]:
    """
    Start the Flask web server on a background daemon thread.

    Returns (local_ip, port).
    """
    thread = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=port, threaded=True),
        daemon=True,
    )
    thread.start()

    ip = get_local_ip()
    print(f"\n{'─'*60}")
    print(f"  Web server listening on port {port}")
    print(f"  iPad pattern  →  http://{ip}:{port}/")
    print(f"  Feed monitor  →  http://{ip}:{port}/monitor")
    print(f"{'─'*60}\n")
    return ip, port
