<p align="center">
  <img src="assets/icon-256.png" width="120" alt="Sotto">
</p>

<h1 align="center">Sotto</h1>

<p align="center"><b>Offline voice typing for Linux.</b><br>
Press two keys, talk, press them again — the words land in whatever field your cursor is in.</p>

---

*Sotto voce* is Italian for "in a low voice". You mutter under your breath;
Sotto writes it down. No account, no upload, no word limit, nothing to
subscribe to. Wayland and X11. English, Urdu, and 90 more languages — non-Latin
scripts (Urdu, Arabic, Hindi) need Wayland, because they go through the
clipboard. Runs on the CPU.

## Why it exists

Typing is slower than talking. Whisper already turns speech into text — it
just never got that text into the app you were typing in, on Linux, without a
subscription.

Sotto is that last step. It records, transcribes locally, and types the result
wherever your cursor is, while you are still speaking.

If you have been searching for *Whisper for Linux* — this is the half that
types.

## Install

Two commands:

```bash
git clone https://github.com/sahil2837j-eng/sotto.git
cd sotto && ./setup
```

Or, if you would rather not clone first — the script fetches its own files:

```bash
curl -fsSL https://raw.githubusercontent.com/sahil2837j-eng/sotto/main/setup | bash
```

The installer walks six steps and asks before each change:

1. checks the tools it needs, offers to install the missing ones
2. copies itself to `~/.local/share/sotto`
3. builds a small Python environment with `faster-whisper`
4. loads the `uinput` kernel module and starts the typing daemon
5. writes the application entry that carries the shortcut
6. checks every piece and tells you what (if anything) is still wrong

The two root-level things are unavoidable, because Linux does not let a normal
program type into other windows:

```bash
# loaded now, and at every boot
sudo modprobe uinput
echo uinput | sudo tee /etc/modules-load.d/uinput.conf

# a daemon holding /dev/uinput open, giving you a private socket
sudo install -m 644 packaging/ydotoold@.service /etc/systemd/system/ydotoold@.service
sudo systemctl enable --now ydotoold@$(id -u)
```

`/dev/uinput` is `root:input 0660`. Either add yourself to the `input` group
and log out, or let the unit above run the daemon as root and hand you a socket
you own — no group change, no logout.

Uninstall with `./setup --uninstall`. Your recordings are left alone.

## Use

1. Put the cursor where the text should go.
2. Press `Ctrl+Alt+Space`. A beep. "Listening…".
3. Talk. Words start appearing after about four seconds.
4. Press `Ctrl+Alt+Space` again. Second beep. The rest lands.

It streams: each ~4 second slice is transcribed and typed while the next one
is still being recorded. You are not waiting for the whole take.

## Languages

Nothing is pinned by default — Whisper detects the language per take, so you
can switch between English and Urdu mid-sentence and it keeps up.

To pin one:

```bash
export SOTTO_LANGUAGE=ur    # en, hi, ar, es, tr, ... 90+
```

Latin text is typed key by key. Anything non-ASCII (Urdu, Arabic, Hindi) goes
through the clipboard and is pasted with `Ctrl+V`, because `ydotool` can only
send physical keycodes. That replaces your clipboard for that utterance.

## Choose the model

```bash
export SOTTO_MODEL=base     # faster, less accurate
export SOTTO_MODEL=medium   # slower, better Urdu (~1.5 GB download)
```

Default is `small`: the best balance on a CPU-only machine.

## Speed, honestly

On a 4-core i5-8350U: a 10 second sentence takes about 15 seconds to appear, a
30 second one about 45. The streaming design hides most of that — you see text
while you keep talking. The first use after five idle minutes pays a ~7 second
model load.

## Troubleshooting

**It listens and notifies, but nothing is typed.** This is almost always
`uinput`. Look for `failed to open uinput device` in the log.

```bash
ls /dev/uinput                       # must exist
lsmod | grep uinput                  # must be listed
systemctl is-active ydotoold@$(id -u)   # must say active
journalctl -u ydotoold@$(id -u) -n 20   # explains why it is not
```

Fix the three root steps above. Nothing else in the app can cause "no text".

**It types into the wrong place.** Text goes wherever focus is at the moment
of typing. Wait for the beep before clicking away.

**Urdu comes out as broken English.** Pin the language with
`SOTTO_LANGUAGE=ur`, or move from `base` to `small` or `medium`.

**The shortcut does nothing.** Log out and back in once so KDE picks up the new
entry, or bind it by hand in System Settings → Keyboard → Shortcuts.

## Files

| Path | Purpose |
|---|---|
| `dictate` | the toggle the hotkey runs: start → stop → transcribe → type |
| `streamer.py` | live slicing: transcribes growing audio every ~4 s and types it |
| `model_server.py` | keeps the model resident, exits after 5 min idle to free RAM |
| `transcribe.py` | one-shot path, also the fallback when streaming fails |
| `vdlib.py` | shared helpers: server socket, text insertion, history |
| `setup` | the installer above |
| `packaging/` | systemd unit and desktop entry templates |
| `assets/` | icon |
| `sounds/` | start and stop beeps |

Every take gets its own wav under `state/takes/`, so starting a new one can
never overwrite the previous recording. `history.log` keeps what you dictated;
delete it whenever you like.

## Not affiliated

Uses [faster-whisper](https://github.com/SYSTRAN/faster-whisper) and OpenAI's
Whisper models, [ydotool](https://github.com/ReimuNotMoe/ydotool), and
[wl-clipboard](https://github.com/bugaevc/wl-clipboard). Sotto is not
affiliated with OpenAI, Microsoft, or KDE.

## License

MIT. See [LICENSE](LICENSE).
