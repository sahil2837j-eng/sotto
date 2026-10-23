#!/usr/bin/env python3
"""Live streaming transcription for sotto.

Started by `dictate` when recording begins. It watches the growing recording,
transcribes it in ~3 s slices and types each slice into the focused window as
soon as it is decoded - so words appear while you are still speaking.

When you press the hotkey again, `dictate` sends SIGTERM: we transcribe the
remaining tail, stitch it to what was already typed, write the state file and
exit. `dictate` then just cleans up.

Stitching: each slice re-covers a short overlap of the previous one. Only the
words that end before the next slice's start are typed now; the next slice
re-reads the boundary with the right context and types the rest, so nothing is
typed twice and a bad guess at an edge cannot leak into the take. The text you
saw live is what stays in the document - there is deliberately no full
re-transcription at the end, which would type everything a second time.

If this process cannot start the model server it exits with code 2 and
`dictate` falls back to transcribing the whole take after you stop.
"""
import json
import os
import signal
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from vdlib import (  # noqa: E402
    STATE,
    append_history,
    ask_server,
    ensure_server,
    insert_text,
    run,
)

WAV = sys.argv[1] if len(sys.argv) > 1 else os.path.join(STATE, "recording.wav")
PIDF = os.path.join(STATE, "recording.pid")
STREAM_PID = os.path.join(STATE, "stream.pid")
STREAM_STATE = os.path.join(STATE, "stream.json")
LAST_TAKE = os.path.join(STATE, "last_take.txt")

CHUNK = float(os.environ.get("VD_CHUNK", "3.0"))  # seconds of new audio per emission
FIRST_CHUNK = float(os.environ.get("VD_FIRST_CHUNK", "2.0"))  # first words out sooner
OVERLAP = 1.2  # seconds re-covered before the boundary, used to dedupe words
WORD_MARGIN = 0.4  # type words ending this far before the next slice starts
WORD_EPS = 0.10  # timestamp slop: a word starting this close to typed_until is a repeat
MAX_MERGE = 10.0  # seconds; one larger slice when the stream has fallen behind

stop_requested = False
typed = ""
typed_until = 0.0
lang = None
pending = None  # first word deferred at the last slice boundary: {"word","start","end"}
offset = 0.0
state = {"status": "recording", "lang": None, "chunks": 0, "words": 0}


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def save_state(status: str | None = None) -> None:
    if status:
        state["status"] = status
    state["lang"] = lang
    state["words"] = len(typed.split())
    tmp = STREAM_STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False)
    os.replace(tmp, STREAM_STATE)
    with open(LAST_TAKE, "w", encoding="utf-8") as fh:
        fh.write(typed + "\n")


def on_signal(signum, frame):  # noqa: ARG001
    global stop_requested
    stop_requested = True


def probe_duration(path: str) -> float:
    r = run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", path]
    )
    if r.returncode != 0:
        return 0.0
    try:
        return float(r.stdout.decode().strip())
    except ValueError:
        return 0.0


def extract(start: float, dur: float):
    """Slice [start, start+dur) out of the still-growing recording."""
    fd, out = tempfile.mkstemp(suffix=".wav", prefix="vd-chunk-")
    os.close(fd)
    r = run(
        ["ffmpeg", "-v", "error", "-y",
         "-ss", f"{max(0.0, start):.3f}", "-t", f"{dur:.3f}", "-i", WAV, out]
    )
    if r.returncode != 0:
        try:
            os.unlink(out)
        except OSError:
            pass
        return None
    return out


def recorder_alive() -> bool:
    """The pidfile is "<recorder pid><TAB><wav path>" (dictate writes it that
    way so a racing take can never grab another take's wav). Older single-field
    files are still accepted: split() handles both."""
    try:
        pid = int(open(PIDF).read().split()[0])
    except (OSError, ValueError, IndexError):
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _toks(s: str):
    out = []
    for t in s.split():
        k = t.strip(" ,.!?;:\"'()…-").lower()
        if k:
            out.append((t, k))
    return out


def _norm_word(s: str) -> str:
    return s.strip(" ,.!?;:\"'()…-").lower()


def _same_word(a: str, b: str) -> bool:
    """Loose comparison for a word re-heard across a slice boundary."""
    a, b = _norm_word(a), _norm_word(b)
    if not a or not b:
        return False
    if a == b:
        return True
    if min(len(a), len(b)) >= 4 and (a.startswith(b) or b.startswith(a)):
        return True
    return False


def _align_start(words, s: float, pending) -> int:
    """Index of the word the previous slice deferred, or -1 if not found.

    Time proximity matters: a legitimate repeat ("no, no") has two identical
    words, and the first one is usually already in the document. Pick the
    occurrence closest to the deferred word's timestamp.
    """
    if not pending:
        return -1
    want = str(pending.get("word") or "")
    try:
        want_at = float(pending.get("start") or 0.0)
    except (TypeError, ValueError):
        want_at = 0.0
    best_i = -1
    best_dt = 1.01
    for i, w in enumerate(words):
        try:
            a = s + float(w.get("start") or 0.0)
        except (TypeError, ValueError):
            continue
        if not _same_word(str(w.get("word") or ""), want):
            continue
        dt = abs(a - want_at)
        if dt <= 1.0 and dt < best_dt:
            best_i, best_dt = i, dt
    return best_i


def overlap_trim(prev: str, new: str) -> str:
    """Drop words at the start of `new` that repeat the end of `prev`."""
    if not prev:
        return new
    pt, nt = _toks(prev), _toks(new)
    if not pt or not nt:
        return new
    for k in range(min(8, len(pt), len(nt)), 0, -1):
        if [x[1] for x in pt[-k:]] == [x[1] for x in nt[:k]]:
            return " ".join(x[0] for x in nt[k:]).strip(" ,.!?:;").strip()
    return new


def emit(start: float, end: float, final: bool = False) -> bool:
    """Transcribe audio [start-OVERLAP, end] and type the new words.

    Before the final tail, only words that end before the next slice's start
    are typed. The next slice re-covers the last OVERLAP seconds, so deferring
    this boundary means the same audio is never typed twice with two different
    guesses - which is what produced the drifted text in longer takes.

    Returns False on a server problem (caller retries), True when the slice
    was consumed - including the 'silence, nothing to say' case.
    """
    global typed, typed_until, lang, pending
    s = max(0.0, start - OVERLAP)
    dur = end - s
    if dur < 0.35:
        return True

    chunk = extract(s, dur)
    if chunk is None:
        log(f"ffmpeg slice failed for {s:.1f}..{end:.1f}")
        return False
    try:
        # Pin the language after the first confident slice: re-detecting with
        # the tiny model on every short slice sometimes flipped a slice to the
        # wrong language (German in one real take) and produced garbage.
        # SOTTO_LANGUAGE still pins one language from the start.
        res = ask_server(chunk, language=lang, beam_size=1, words=True)
    finally:
        try:
            os.unlink(chunk)
        except OSError:
            pass

    if res is None:
        # A dead/stuck server must not poison every later slice: make sure a
        # fresh one is up before the caller retries this slice.
        ensure_server(timeout=30)
        log(f"slice {s:.1f}..{end:.1f} failed: server unreachable")
        return False
    if res.get("status") == "error":
        log(f"slice {s:.1f}..{end:.1f} failed: {res}")
        return False
    if res.get("status") == "no_speech":
        pending = None  # the deferred boundary was not speech after all
        return True  # silence - advance, nothing to type

    text = (res.get("text") or "").strip()
    words = res.get("words") or []
    new_until = typed_until
    next_pending = None
    if words:
        cutoff = end - OVERLAP + WORD_MARGIN

        # Words in the next slice's overlap are left for that slice, which has
        # the right context. The previous slice recorded the first deferred
        # word, so this slice can start there instead of guessing by time.

        # The previous slice deliberately deferred the first word at its
        # boundary. Find that word again and start there: anything the model
        # put before it is a re-hearing of audio at or before the boundary,
        # not new text. This is what produced hallucinated lead-ins
        # ("I just don't") and duplicates ("artist" / "artists"). If the word
        # is not found, fall back to the time rule below.
        aligned = False
        best_i = _align_start(words, s, pending)
        if best_i >= 0:
            if best_i:
                lead = "".join(str(x.get("word") or "") for x in words[:best_i]).strip()
                if lead:
                    log(f"skip lead-in: {lead!r}")
            words = words[best_i:]
            aligned = True

        kept = []
        for w in words:
            try:
                a = s + float(w.get("start") or 0.0)
                b = min(end, s + float(w.get("end") or 0.0))
            except (TypeError, ValueError):
                kept.append(w)
                continue
            if not aligned and a < typed_until - WORD_EPS:
                continue
            if start > 0.0 and not final and b > cutoff:
                continue
            kept.append(w)
            if b > new_until:
                new_until = b
        text = "".join(str(w.get("word") or "") for w in kept).strip()

        # Remember the first word this slice had to leave for the next one.
        if start > 0.0 and not final:
            for w in words:
                try:
                    a = s + float(w.get("start") or 0.0)
                    b = min(end, s + float(w.get("end") or 0.0))
                except (TypeError, ValueError):
                    continue
                if b > cutoff and a >= typed_until - WORD_EPS:
                    next_pending = {"word": str(w.get("word") or ""), "start": a, "end": b}
                    break
    elif text:
        # Older server with no timestamps: trust the text and advance to the end.
        new_until = end
    pending = next_pending
    if not text:
        return True
    if lang is None and res.get("lang") and float(res.get("prob") or 0) >= 0.5:
        lang = res.get("lang")

    # With word times, audio-time commit already guarantees no overlap; a
    # text match would wrongly drop a legitimate repeated word ("no, no").
    if not words:
        text = overlap_trim(typed, text)
    if text:
        # Slices are typed as separate keystroke runs, so a slice that follows
        # another one needs its own leading space or the two run together
        # ("regions.Revenue"). overlap_trim strips leading punctuation, so we
        # know the text never starts with whitespace on its own.
        to_type = (" " + text) if typed else text
        try:
            insert_text(to_type)
        except Exception as e:  # noqa: BLE001
            # Focus went somewhere we can't type into (or ydotool hiccuped):
            # keep going, `dictate` will report it.
            log(f"insert failed: {e}")
        typed = (typed + " " + text).strip()
        if new_until > typed_until:
            typed_until = new_until
        append_history(lang or "?", text)
        state["chunks"] += 1
        save_state()
        log(f"typed [{lang}] {text[:70]}")
    return True


def main() -> int:
    global offset
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    save_state("recording")

    if not ensure_server(timeout=60):
        log("model server unavailable - exiting so dictate can do it in one pass")
        save_state("error")
        return 2

    log(f"streaming on {WAV} (chunk={CHUNK}s overlap={OVERLAP}s) "
         f"probe_at_start={probe_duration(WAV):.1f}s recorder_alive={recorder_alive()}")
    failures = 0

    while not stop_requested and recorder_alive():
        dur = probe_duration(WAV)
        step = FIRST_CHUNK if offset == 0.0 else CHUNK
        backlog = dur - offset
        if backlog >= step:
            end = offset + step
            if backlog > CHUNK * 2:
                # It fell behind (slow decode, busy machine): transcribe one
                # larger slice instead of a queue of 3 s slices, so the lag
                # does not keep growing for the rest of the take.
                end = min(dur, offset + MAX_MERGE)
                log(f"catch up: {backlog:.1f}s behind, one {end-offset:.1f}s slice")
            log(f"emit slice offset={offset:.1f} end={end:.1f} dur={dur:.1f}")
            if emit(offset, end):
                failures = 0
                offset = end
            else:
                failures += 1
                if failures >= 6:
                    save_state("error")
                    log("too many failures, giving up on live mode")
                    return 3
                time.sleep(1.0)
        else:
            time.sleep(0.35)

    # Recording ended: finish whatever speech came after the last slice. Cap
    # each tail request so a long backlog cannot become one huge decode.
    if failures < 6:
        dur = probe_duration(WAV)
        while dur - offset > 0.4 or (offset == 0.0 and dur >= 0.5):
            end = min(dur, offset + MAX_MERGE)
            final = end >= dur - 0.05
            if not emit(offset, end, final=final):
                save_state("error")
                return 3
            offset = end

    save_state("done")
    log(f"done: {state['chunks']} slices, {len(typed.split())} words")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception as e:  # noqa: BLE001
        log(f"streamer crashed: {e}")
        state["status"] = "error"
        try:
            save_state("error")
        except Exception:  # noqa: BLE001
            pass
        code = 1
    try:
        os.unlink(STREAM_PID)
    except OSError:
        pass
    sys.exit(code)
