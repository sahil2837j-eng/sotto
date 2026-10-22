#!/usr/bin/env python3
"""Keeps the whisper model loaded so dictation results arrive in seconds
instead of paying ~7 s of model load on every utterance.

Started on demand by transcribe.py, talks over a unix socket, one request per
connection: send a wav path + newline, get one JSON line back. Shuts itself
down after IDLE_TIMEOUT seconds with no requests so RAM is given back.
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time

# Must be set before numpy/ctranslate2 pull in OpenMP: without them the
# worker threads spin at ~100% CPU for minutes after every transcription and
# drag down the rest of the machine.
os.environ.setdefault("OMP_WAIT_POLICY", "passive")
os.environ.setdefault("KMP_BLOCKTIME", "0")
os.environ.setdefault("MKL_DYNAMIC", "FALSE")

import numpy as np  # noqa: E402
from faster_whisper import WhisperModel  # noqa: E402

BASE = os.path.dirname(os.path.abspath(__file__))
SOCK = os.path.join(BASE, "state", "model.sock")
PIDFILE = os.path.join(BASE, "state", "model.pid")
IDLE_TIMEOUT = 300  # seconds

model = None
last_used = time.time()
busy = threading.Event()


def load_model():
    global model
    t0 = time.time()
    # 4 threads match the physical cores; 8 (SMT) measured no faster and
    # contends with whatever else is running.
    model = WhisperModel(os.environ.get("WHISPERQUILL_MODEL", "small"), device="cpu", compute_type="int8", cpu_threads=4)
    print(f"model ready in {time.time() - t0:.1f}s", flush=True)


def decode(path: str) -> np.ndarray:
    fd, tmp = tempfile.mkstemp(suffix=".f32", prefix="vd-")
    os.close(fd)
    try:
        r = subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", path,
             "-f", "f32le", "-acodec", "pcm_f32le", "-ac", "1", "-ar", "16000", tmp],
            capture_output=True,
        )
        if r.returncode != 0:
            raise RuntimeError("ffmpeg: " + r.stderr.decode(errors="replace")[:200])
        return np.fromfile(tmp, dtype=np.float32)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def transcribe(path: str, language=None, prompt=None, beam_size: int = 5, vad: bool = True) -> dict:
    audio = decode(path)
    if audio.size < 1600:
        return {"status": "no_speech", "text": ""}
    t0 = time.time()
    segments, info = model.transcribe(
        audio,
        beam_size=beam_size,
        vad_filter=vad,
        vad_parameters={"min_silence_duration_ms": 400},
        language=language,  # None = auto-detect English / Urdu / mixed
        condition_on_previous_text=False,
        without_timestamps=True,
        initial_prompt=prompt,  # keeps casing/wording continuous across live slices
    )
    text = " ".join(s.text.strip() for s in segments if s.text.strip()).strip()
    return {
        "status": "ok" if text else "no_speech",
        "text": text,
        "lang": info.language,
        "prob": round(info.language_probability, 2),
        "seconds": round(time.time() - t0, 1),
    }


def idle_watchdog():
    while True:
        time.sleep(15)
        if not busy.is_set() and time.time() - last_used > IDLE_TIMEOUT:
            print("idle timeout, exiting", flush=True)
            try:
                os.unlink(SOCK)
            except OSError:
                pass
            os._exit(0)


def handle(conn):
    global last_used
    busy.set()
    try:
        conn.settimeout(900)
        f = conn.makefile("rwb")
        line = f.readline().decode(errors="replace").strip()
        if not line:
            return
        # Two request shapes: a bare wav path (one-shot), or a JSON object
        # {"path", "language", "prompt", "beam_size", "vad"} for live slices.
        path, opts = line, {}
        if line.startswith("{"):
            try:
                req = json.loads(line)
            except Exception as e:  # noqa: BLE001
                f.write((json.dumps({"status": "error", "text": "", "error": f"bad request: {e}"}) + "\n").encode())
                f.flush()
                return
            path = req.get("path", "")
            opts = {k: req[k] for k in ("language", "prompt", "beam_size", "vad") if k in req}
        try:
            result = transcribe(path, **opts)
        except Exception as e:  # noqa: BLE001
            result = {"status": "error", "text": "", "error": str(e)[:400]}
        f.write((json.dumps(result, ensure_ascii=False) + "\n").encode())
        f.flush()
    except Exception as e:  # noqa: BLE001
        print(f"request failed: {e}", flush=True)
    finally:
        try:
            conn.close()
        finally:
            last_used = time.time()
            busy.clear()


def main():
    os.makedirs(os.path.dirname(SOCK), exist_ok=True)
    if os.path.exists(SOCK):
        try:
            os.unlink(SOCK)
        except OSError:
            pass

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCK)
    server.listen(4)

    with open(PIDFILE, "w") as fh:
        fh.write(str(os.getpid()))

    threading.Thread(target=idle_watchdog, daemon=True).start()
    load_model()
    print(f"listening on {SOCK}", flush=True)

    while True:
        try:
            conn, _ = server.accept()
        except KeyboardInterrupt:
            break
        handle(conn)


if __name__ == "__main__":
    try:
        main()
    finally:
        for p in (SOCK, PIDFILE):
            try:
                os.unlink(p)
            except OSError:
                pass
        sys.exit(0)
