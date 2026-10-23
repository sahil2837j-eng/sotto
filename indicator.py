#!/usr/bin/env python3
"""The little strip that shows Sotto is listening.

It is a Wayland layer-shell surface: it sits above normal windows, never takes
focus, lets clicks pass through, and leaves with the recorder. The bars are the
real microphone level, read from the wav that pw-record is writing, so a muted
or dead microphone looks flat instead of pretending to hear something.

If the session cannot do layer-shell (X11, or the library is missing), it exits
quietly: dictation is unaffected and the desktop notification is still there.

    indicator.py --pid <recorder pid> --wav <file being recorded>

Nothing here is on the critical path. If it dies, dictation keeps working.
"""
from __future__ import annotations

import argparse
import array
import math
import os
import signal
import subprocess
import sys

BARS = 7
TICK_MS = 100         # 10 fps: a level meter does not need 30
HEIGHT = 26           # height of the bar canvas
BAR_W = 4
GAP = 3


def quiet_exit(why: str = "") -> None:
    if why:
        print(f"indicator: {why}", file=sys.stderr)
    raise SystemExit(0)


try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Gtk4LayerShell", "1.0")
    gi.require_version("GdkPixbuf", "2.0")
    gi.require_version("GLibUnix", "2.0")
    from gi.repository import GdkPixbuf, GLib, GLibUnix, Gtk, Gtk4LayerShell as Layer
except Exception as exc:  # no GTK4, no layer shell, no display
    quiet_exit(f"no layer-shell here ({exc})")

try:
    import cairo
except Exception:
    cairo = None


CSS = b"""
.overlay { background: transparent; }
.pill {
    background: rgba(20, 11, 44, 0.88);
    border: 1px solid rgba(255, 255, 255, 0.13);
    border-radius: 999px;
    padding: 7px 16px 7px 12px;
}
.dot { background: #EF4444; border-radius: 999px; }
.dot.muted { background: #94A3B8; }
.word {
    color: #E5DEFF;
    font-family: Cantarell, "DejaVu Sans", sans-serif;
    font-size: 13px;
    letter-spacing: .2px;
}
"""


def read_level(path: str) -> float:
    """RMS of the last tenth of a second, 0..1."""
    try:
        size = os.path.getsize(path)
        if size <= 44:
            return 0.0
        with open(path, "rb") as fh:
            fh.seek(max(44, size - 3200))
            raw = fh.read()
        raw = raw[: len(raw) // 2 * 2]
        samples = array.array("h")
        samples.frombytes(raw)
        if not samples:
            return 0.0
        rms = math.sqrt(sum(s * s for s in samples) / len(samples)) / 32768.0
        return min(1.0, rms * 7.0)
    except Exception:
        return 0.0


def source_muted() -> bool:
    """True when the default microphone is muted. Best effort, never fatal."""
    try:
        out = subprocess.run(
            ["wpctl", "get-volume", "@DEFAULT_AUDIO_SOURCE@"],
            capture_output=True, text=True, timeout=0.5,
        ).stdout
        return "[MUTED]" in out.upper()
    except Exception:
        return False


def scaled_icon(path: str, size: int):
    """A picture at exactly `size` px - Gtk.Picture alone keeps the file's size."""
    try:
        pix = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, size, size, True)
        return Gtk.Picture.new_for_pixbuf(pix)
    except Exception:
        return None


def find_icon(explicit: str) -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (
        explicit,
        os.path.expanduser("~/.local/share/icons/hicolor/256x256/apps/sotto.png"),
        os.path.join(here, "assets", "icon-256.png"),
    ):
        if candidate and os.path.exists(candidate):
            return candidate
    return ""


class Indicator:
    def __init__(self, pid: int, wav: str, icon: str, margin: int) -> None:
        self.pid = pid
        self.wav = wav
        self.margin = margin
        self.muted = False
        self.current = 0.0                 # smoothed level, fast attack / slow decay
        self.levels = [0.0] * BARS         # what the bars show, newest last
        self.started = GLib.get_monotonic_time() / 1e6
        self.closing = False
        self.loop = GLib.MainLoop()
        self.label_text = ""

        self.win = Gtk.Window()
        self.win.set_decorated(False)
        self.win.set_resizable(False)
        self.win.set_css_classes(["overlay"])

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=9)
        box.add_css_class("pill")
        box.set_valign(Gtk.Align.CENTER)
        self.win.set_child(box)

        if icon:
            pic = scaled_icon(icon, 18)
            if pic is not None:
                pic.set_opacity(0.6)       # brand, not state
                pic.set_valign(Gtk.Align.CENTER)
                box.append(pic)

        self.dot = Gtk.Box()
        self.dot.add_css_class("dot")
        self.dot.set_size_request(8, 8)
        self.dot.set_opacity(0.85)
        self.dot.set_valign(Gtk.Align.CENTER)
        box.append(self.dot)

        self.area = Gtk.DrawingArea()
        self.area.set_content_width(BARS * (BAR_W + GAP) - GAP)
        self.area.set_content_height(HEIGHT)
        self.area.set_valign(Gtk.Align.CENTER)
        self.area.set_draw_func(self.draw)
        box.append(self.area)

        self.label = Gtk.Label(label="listening…")
        self.label.add_css_class("word")
        self.label.set_xalign(0.0)
        box.append(self.label)

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(
            self.win.get_display(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    # -- drawing ---------------------------------------------------------
    def draw(self, area, cr, width, height, *_):
        total = BARS * BAR_W + (BARS - 1) * GAP
        x0 = (width - total) / 2
        for i, level in enumerate(self.levels):
            bar_h = max(2.0, level * (height - 2))   # a floor, never a vanished bar
            y = (height - bar_h) / 2
            cr.set_source_rgba(0.13, 0.83, 0.93, 0.95)
            cr.rectangle(x0 + i * (BAR_W + GAP), y, BAR_W, bar_h)
            cr.fill()
        if self.muted:
            cr.set_source_rgba(0.95, 0.45, 0.52, 0.9)
            cr.set_line_width(1.6)
            cr.move_to(x0, height - 2)
            cr.line_to(x0 + total, 2)
            cr.stroke()

    # -- state -----------------------------------------------------------
    def tick(self) -> bool:
        now = GLib.get_monotonic_time() / 1e6
        t = now - self.started

        # Fast attack, slow decay: speech should snap, quiet should settle.
        before = self.current
        target = read_level(self.wav) if self.wav else 0.0
        tau = 0.04 if target > self.current else 0.25
        self.current += (target - self.current) * (1 - math.exp(-(TICK_MS / 1000) / tau))
        self.levels = self.levels[1:] + [self.current]
        changed = abs(self.current - before) > 0.002

        # Dot: fixed brightness. Per-frame opacity forced a full redraw; the
        # bars carry the live state and the muted class changes the color.

        # Text teaches once, then the strip stays compact for the rest of the take:
        # the dot and the live bars are the state, the word was only the intro.
        if t < 2.3:
            text = "mic is muted" if self.muted else "listening…"
            opacity = 0.85 if t < 1.8 else 0.85 * (1 - (t - 1.8) / 0.5)
            if text != self.label_text:
                self.label.set_text(text)
                self.label_text = text
            self.label.set_visible(True)
            self.label.set_opacity(max(0.0, opacity))
        elif self.label.get_visible():
            self.label.set_visible(False)

        if changed:
            self.area.queue_draw()
        return True

    def poll_mute(self) -> bool:
        muted = source_muted()
        if muted != self.muted:
            self.muted = muted
            if muted:
                self.dot.add_css_class("muted")
            else:
                self.dot.remove_css_class("muted")
            self.area.queue_draw()
        return True

    # -- lifecycle -------------------------------------------------------
    def fade(self, start: float, end: float, ms: int, done=None) -> None:
        steps = max(1, int(ms / 16))

        def step(i: list[int] = [0]) -> bool:
            i[0] += 1
            self.win.set_opacity(max(0.0, min(1.0, start + (end - start) * i[0] / steps)))
            if i[0] >= steps:
                if done is not None:
                    done()
                return False
            return True

        GLib.timeout_add(16, step)

    def close(self) -> None:
        if self.closing:
            return
        self.closing = True
        self.fade(self.win.get_opacity(), 0.0, 120, self.loop.quit)  # exit is quicker

    def watch_pid(self) -> bool:
        if self.pid and not os.path.exists(f"/proc/{self.pid}"):
            self.close()
            return False
        return True

    def on_signal(self, *_args) -> bool:
        self.close()
        return False

    def place(self) -> None:
        """Layer-shell setup must happen before the window is realized."""
        Layer.init_for_window(self.win)
        Layer.set_namespace(self.win, "sotto-indicator")
        Layer.set_layer(self.win, Layer.Layer.OVERLAY)
        Layer.set_anchor(self.win, Layer.Edge.BOTTOM, True)
        Layer.set_margin(self.win, Layer.Edge.BOTTOM, self.margin)
        Layer.set_keyboard_mode(self.win, Layer.KeyboardMode.NONE)
        Layer.set_exclusive_zone(self.win, -1)

    def make_click_through(self) -> bool:
        if cairo is None:
            return False
        surface = self.win.get_surface()
        if surface is None:
            return False
        try:
            surface.set_input_region(cairo.Region())
        except Exception:
            return False
        # GLib.idle_add repeats while the callback returns True. This only
        # needs to run once; returning True here spun the main loop forever
        # and burned a full CPU core for the entire dictation.
        return False

    def run(self) -> None:
        self.place()
        self.win.set_opacity(0.0)
        self.win.present()
        GLib.idle_add(self.make_click_through)
        self.fade(0.0, 1.0, 150)            # enter: 150 ms fade
        GLib.timeout_add(TICK_MS, self.tick)
        GLib.timeout_add(5000, self.poll_mute)
        if self.pid:
            GLib.timeout_add(400, self.watch_pid)
        GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, self.on_signal)
        GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, self.on_signal)
        self.loop.run()


def main() -> None:
    ap = argparse.ArgumentParser(description="Sotto listening indicator")
    ap.add_argument("--pid", type=int, default=0, help="recorder pid to follow")
    ap.add_argument("--wav", default="", help="recording being written")
    ap.add_argument("--icon", default="", help="icon file")
    ap.add_argument("--margin", type=int, default=90)
    args = ap.parse_args()

    Gtk.init()
    Indicator(args.pid, args.wav, find_icon(args.icon), args.margin).run()


if __name__ == "__main__":
    main()
