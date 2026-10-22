# WhisperQuill — Whisper for Linux that types where you look

Offline voice typing for Linux. Press two keys, talk, press them again — the
words appear in whatever field your cursor is in. No account. No upload. No
word limit. Works on Wayland and X11 desktops, English, Urdu, and any other
language Whisper knows.

> Whisper made speaking into text easy. It just never typed it for you on
> Linux. This is the missing half.

## The problem it solves

You can already transcribe audio with Whisper. What you cannot do, out of the
box, is get that text into the app you are typing in — a browser, an editor, a
chat box, a terminal. WhisperQuill is that bridge: it records, transcribes
locally, and injects the text as if you had typed it, while you are still
speaking.

## What it feels like

1. Put the cursor where the text should go.
2. Press `Ctrl+Alt+Space`. A beep. "Listening…".
3. Talk. Words start appearing after about four seconds, and keep coming.
4. Press `Ctrl+Alt+Space` again. Second beep. The rest lands.

It streams: each ~4 second slice is transcribed and typed while the next slice
is still being recorded. You are not waiting for the whole take.

## Install

Requires Arch/CachyOS-style packages, but the parts are standard.

```bash
git clone https://github.com/sahil2837j-eng/whisperquill.git
cd whisperquill
./install.sh
```

`install.sh` creates a Python venv, installs `faster-whisper`, copies the
scripts to `~/.local/share/whisperquill`, and registers the global shortcut.

### The one part that needs root

Linux does not let a normal program type into other windows. That job belongs
to a kernel device, `/dev/uinput`, served by `ydotoold`. Three things must be
true:

```bash
# 1. the kernel module must be loaded (and stay loaded)
sudo modprobe uinput
echo uinput | sudo tee /etc/modules-load.d/uinput.conf

# 2. a daemon must hold /dev/uinput open
sudo install -m 644 packaging/ydotoold@.service /etc/systemd/system/
sudo systemctl enable --now ydotoold@$(id -u)
```

`/dev/uinput` is `root:input 0660`. Either add yourself to the `input` group
and re-login, or let `packaging/ydotoold@.service` run the daemon as root and
hand you a private socket — it uses `--socket-own=1000:1000`, so `ydotool`
works without any group change or logout.

Check it:

```bash
systemctl is-active ydotoold@$(id -u)   # active
ls -l /run/user/1000/.ydotool_socket
ydotool type --file /dev/null && echo ok
```

## Languages

Nothing is pinned by default: Whisper detects the language per take, so you can
switch between English and Urdu mid-sentence and it keeps up.

To pin one language:

```bash
export WHISPERQUILL_LANGUAGE=ur    # or en, hi, ar, es, ... 90+ languages
```

Latin text is typed key by key. Anything non-ASCII (Urdu, Arabic, Hindi) goes
through the clipboard and is pasted with `Ctrl+V`, because `ydotool` can only
send physical keycodes. That replaces your clipboard for that utterance.

## Choose the model

```bash
export WHISPERQUILL_MODEL=base     # faster, less accurate
export WHISPERQUILL_MODEL=medium   # slower, better Urdu (~1.5 GB download)
```

Default is `small`: the best balance on a CPU-only machine.

## Speed, honestly

On a 4-core i5-8350U: a 10 second sentence takes about 15 seconds to appear,
a 30 second one about 45. The streaming design hides most of that — you see
text while you keep talking. First use after five idle minutes pays a ~7 second
model load.

## Files

| Path | Purpose |
|---|---|
| `dictate` | the toggle the hotkey runs: start → stop → transcribe → type |
| `streamer.py` | live slicing: transcribes growing audio every ~4 s and types it |
| `model_server.py` | keeps the model resident, exits after 5 min idle to free RAM |
| `transcribe.py` | one-shot path, also the fallback when streaming fails |
| `vdlib.py` | shared helpers: server socket, text insertion, history |
| `packaging/` | systemd unit and desktop entry templates |
| `sounds/` | start/stop beeps |

Every take gets its own wav under `state/takes/`, so starting a new one can
never overwrite the previous recording.

## Troubleshooting

**It listens and notifies, but nothing is typed.** This is almost always
`uinput`. The log says `failed to open uinput device` or `ydotool type:`
with an empty message.

```bash
ls /dev/uinput                       # must exist
lsmod | grep uinput                  # must be listed
systemctl is-active ydotoold@$(id -u)  # must be active
journalctl -u ydotoold -n 20         # explains why it is not
```

Fix the three root steps above. Nothing else in the app can cause "no text".

**It types into the wrong place.** The text goes wherever focus is at the
moment of typing. Wait for the beep before clicking away.

**Urdu comes out as broken English.** Pin the language with
`WHISPERQUILL_LANGUAGE=ur`, or move from `base` to `small`/`medium`.

## Not affiliated

Uses [faster-whisper](https://github.com/SYSTRAN/faster-whisper) and OpenAI's
Whisper models, [ydotool](https://github.com/ReimuNotMoe/ydotool), and
[wl-clipboard](https://github.com/bugaevc/wl-clipboard). WhisperQuill is not
affiliated with OpenAI or Microsoft. "Whisper" here names the model family the
tool runs on.

## License

MIT. See [LICENSE](LICENSE).
