#!/usr/bin/env python3
"""Keeps the whisper models loaded so dictation results arrive in seconds
instead of paying a few seconds of model load on every utterance.

Started on demand by transcribe.py, talks over a unix socket, one request per
connection: send a wav path + newline, get one JSON line back. Shuts itself
down after IDLE_TIMEOUT seconds with no requests so RAM is given back.

On a cold start the tiny language detector and a small fast model come up
first; the first slice is transcribed while the main model is still loading.
That keeps the first words inside the five-second target without keeping the
large model resident all day.
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
IDLE_TIMEOUT = 1800  # seconds; warm through a work session, then give RAM back

model = None
fast_model = None
detector = None
last_used = time.time()
busy = threading.Event()


def load_fast_model():
    """Load the models that can answer before the main model is warm.

    Tiny detects the language cheaply; a small "fast" model transcribes the
    first slice so the first words still land within five seconds on a cold
    start. The main model takes over as soon as it finishes loading."""
    global detector, fast_model
    t0 = time.time()
    # Detecting the language with the small model is a full extra encoder pass
    # (~1.6 s here), which doubles the wait for the first words. Tiny does the
    # same job in ~0.3 s, so every slice can detect its own language instead of
    # pinning the whole take to whatever the first slice guessed.
    det_name = os.environ.get("SOTTO_DETECT_MODEL", "tiny").strip()
    if det_name and det_name.lower() != "none":
        try:
            detector = WhisperModel(det_name, device="cpu", compute_type="int8", cpu_threads=2)
        except Exception as e:  # noqa: BLE001
            detector = None
            print(f"language detector unavailable ({e}); using full detection", flush=True)
    else:
        detector = None

    # A 2 s first slice with the main model is what used to take nine seconds
    # on a cold start. Base is quick to load, materially better than tiny on a
    # short slice, and small enough to keep the first words inside the budget.
    fast_name = os.environ.get("SOTTO_FAST_MODEL", "base").strip()
    if fast_name and fast_name.lower() not in ("none", "off"):
        if detector is not None and fast_name == det_name:
            fast_model = detector
        else:
            try:
                fast_model = WhisperModel(fast_name, device="cpu", compute_type="int8", cpu_threads=2)
            except Exception as e:  # noqa: BLE001
                fast_model = None
                print(f"fast transcriber unavailable ({e}); using the detector", flush=True)
    print(f"fast models ready in {time.time() - t0:.1f}s", flush=True)


def load_main_model():
    global model
    t0 = time.time()
    # 4 threads match the physical cores; 8 (SMT) measured no faster and
    # contends with whatever else is running.
    try:
        model = WhisperModel(os.environ.get("SOTTO_MODEL", "small"), device="cpu", compute_type="int8", cpu_threads=4)
    except Exception as e:  # noqa: BLE001
        print(f"main model unavailable ({e}); staying on the fast model", flush=True)
        return
    print(f"main model ready in {time.time() - t0:.1f}s", flush=True)


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


def transcribe(path: str, language=None, prompt=None, beam_size: int = 5, vad: bool = True, words: bool = False) -> dict:
    audio = decode(path)
    if audio.size < 1600:
        return {"status": "no_speech", "text": ""}
    active = model or fast_model or detector
    if active is None:
        return {"status": "error", "text": "", "error": "model still loading"}
    t0 = time.time()
    detected_prob = None
    if language is None and detector is not None:
        try:
            language, detected_prob, _ = detector.detect_language(audio=audio)
        except Exception:  # noqa: BLE001
            language = None
    # Hard bounds on the decoder. Whisper's default temperature list retries a
    # bad slice at 0.2 ... 1.0 with up to 448 tokens each; one noisy or silent
    # slice could therefore block the single server for 10-35 s while every
    # later slice queued behind it. Greedy, one temperature, token cap.
    duration = audio.size / 16000.0
    max_tokens = max(24, min(192, int(duration * 8) + 16))
    segments, info = active.transcribe(
        audio,
        # The fast first-pass model is a bridge until the main model is ready;
        # keep it greedy so it answers immediately.
        beam_size=1 if active is not model else beam_size,
        best_of=1,
        temperature=0.0,
        max_new_tokens=max_tokens,
        no_repeat_ngram_size=3,
        vad_filter=vad,
        vad_parameters={"min_silence_duration_ms": 400},
        language=language,  # None = let the tiny detector (or whisper) decide
        condition_on_previous_text=False,
        without_timestamps=not words,
        word_timestamps=words,
        initial_prompt=prompt,
    )
    segments = list(segments)
    text = " ".join(s.text.strip() for s in segments if s.text.strip()).strip()
    prob = detected_prob if detected_prob is not None else info.language_probability
    result = {
        "status": "ok" if text else "no_speech",
        "text": text,
        "lang": language or info.language,
        "prob": round(prob or 0.0, 2),
        "seconds": round(time.time() - t0, 1),
    }
    if words:
        result["words"] = [
            {"start": round(w.start, 2), "end": round(w.end, 2), "word": w.word}
            for seg in segments
            for w in (seg.words or [])
        ]
    return result


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
            opts = {k: req[k] for k in ("language", "prompt", "beam_size", "vad", "words") if k in req}
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
    load_fast_model()
    if fast_model is None and detector is None:
        # No fast models configured: the main model is the only option, so
        # load it before accepting requests.
        load_main_model()
    else:
        # Accept the first slice immediately; the main model takes over when
        # it finishes loading in the background.
        threading.Thread(target=load_main_model, daemon=True).start()
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
