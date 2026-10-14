# /// script
# requires-python = "==3.12.*"
# dependencies = [
#   "flask>=3", "fastembed>=0.9", "imageio-ffmpeg>=0.6", "numpy",
#   "pywebview>=6; sys_platform == 'darwin'",
#   "sounddevice>=0.5; sys_platform == 'darwin'",
# ]
# ///
"""Transclip:  uv run app.py

Finds the recordings in a folder you choose, has transcribe.py transcribe them, and
shows the result in a window of its own on a Mac and in your browser elsewhere: read,
search, select clips, export them.

Everything it keeps is in one folder, Transclip in your home folder (TRANSCLIP_HOME
moves it): tapes/ holds each recording's transcript, listening copy and waveform
outline, selects.json the clips, exports/ what you export. A session is another such
folder inside it, in sessions/: one project's recordings, clips and search, apart
from every other's. On a Mac a tape can be recorded straight into a session's recordings
folder: `app.py --record` is the program that listens to the sound input, started and
stopped by the app. Recordings can be collected into one file for someone else and
imported from one; originals that arrive that way are kept in originals/. Exports are
cut from the original recordings, which are only ever read. Corrections to a transcript are kept
beside it in <tape>.fixes and laid over the machine's words when it is read; the
machine's own transcript is never rewritten. Search by meaning uses a small language
model kept in models/. Nothing is sent anywhere, and only this computer can reach the app.
"""
import csv
import json
import math
import os
import posixpath
import queue
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import imageio_ffmpeg
from flask import Flask, abort, jsonify, request, send_from_directory
from werkzeug.security import safe_join

ROOT = Path(__file__).parent
# The first library, and the home of what every session shares: the other sessions, which one is open, the model for meaning.
BASE = Path(os.environ.get("TRANSCLIP_HOME") or Path.home() / "Transclip")
# The library being worked on, which open_session() changes: the first one, or a session's folder laid out the same way.
HOME = BASE
TAPES, SELECTS, SETTINGS, EXPORTS = HOME / "tapes", HOME / "selects.json", HOME / "settings.json", HOME / "exports"
ORIGINALS = HOME / "originals"  # recordings that arrived with their transcripts, in a file someone collected
PORT = int(os.environ.get("TRANSCLIP_PORT") or 8790)
QUIET = "--no-open" in sys.argv  # no window, no browser tab and no folder windows: for tests
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()  # comes with the package, so nothing has to be installed by hand
# ponytail: two recordings that differ only in extension (a.wav, a.mp3) count as one; keep the extension in the id if that ever bites.
AUDIO = {".wav", ".aif", ".aiff", ".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wma", ".aac"}
PASSAGE, STEP, BREAK = 80, 40, 5  # meaning is searched in overlapping 80-word passages that never span a 5 s silence
# Beyond the five closest, passages scoring below this are not offered as "close in meaning".
# Found by trial on this archive: nonsense queries top out just under 0.6, most real ones start around 0.65.
MIN_SCORE = 0.6
# ponytail: one archive's convention, built in. A folder with this name holds raw transfers; a titled file
# elsewhere with the same wording lends the transfer its name and is labelled as a copy. Make it a setting if another archive differs.
RAW = "recordings"
# What a recording is in a collected file besides its transcript: corrections, the listening copy, the waveform.
# Where the music is gets measured again from the listening copy, in a second or two.
EXTRAS = (".fixes", ".m4a", ".peaks")
# Music is looked for in the sound itself, about a quarter of a second at a time: where there are no words,
# and the sound is made of tones rather than hiss (FLAT) and keeps changing rather than humming on (CHANGE).
# ponytail: two measurements with limits found by trial on one archive of cassettes, checked against spectrograms.
# They cannot tell music from other tonal, changing sound without words (faint voices, a busy room with a hum
# in it), and music under words is never marked. A trained sound classifier is the step up if that is not enough.
RATE, FRAME, HOP, PER_BLOCK = 16000, 1024, 512, 8
BLOCKS = RATE / HOP / PER_BLOCK  # measurements a second
FLAT, CHANGE = 0.1, 0.15
SMOOTH, BRIDGE, SHORTEST = 5, 3, 10  # seconds: judged over, a lapse a piece may contain, and the least that counts
# Recording a tape. The master is what came in, as 24-bit stereo at 48 kHz, judged a tenth of a second at a time.
REC_RATE, REC_BYTES = 48000, 6  # frames a second, and bytes to a frame: two channels of three
REC_BLOCK = REC_RATE // 10 * REC_BYTES  # bytes in a tenth of a second
# A deck that has stopped is far quieter than blank tape still running: about 45 dB on the chain this was built
# against (-117 dBFS stopped, -72 running). The level under which the deck counts as stopped is either set by
# the person, or learned between Record and the tape starting:
STOPPED_ABOVE, STARTED_ABOVE = 10, 15  # dB over the learned level: still stopped, and certainly playing
# ponytail: one fixed level stands guard over the learning. A lead-in louder than this is taken for a tape already
# running, and the side is then never ended automatically. With the gain set very low, running tape could slip
# under it; setting the stop level by hand is the way round that.
QUIET_ENOUGH = -80  # dBFS
SIDE_ENDS, LEAD_IN, TAIL = 30, 1, 3  # seconds: stopped this long is the end; kept before the tape's first sound, and after its last
STEADY = 5  # tenths of a second that sound must last to be the tape starting, and not a click
TEST_INPUT = os.environ.get("TRANSCLIP_TEST_INPUT")  # a sound file to stand in for the sound inputs: for tests

app = Flask(__name__, static_folder="static", static_url_path="")


@app.before_request
def local_only():
    """Only pages this app served may talk to it: not another site open in the browser,
    nor a web address made to point at this computer."""
    origin = request.headers.get("Origin")
    if request.host.split(":")[0] not in ("127.0.0.1", "localhost") or (origin and urlsplit(origin).netloc != request.host):
        abort(403)


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def dump(path, value):
    """Written to one side, then swapped in: never a half-written file."""
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def load_tape(tape_id):
    path = safe_join(str(TAPES), tape_id + ".json")  # refuses ids that climb out of the tapes folder
    if not path or not os.path.exists(path):
        abort(404, f"no transcript for {tape_id}")
    return load(path)


def mid(w):
    return (w["s"] + w["e"]) / 2


def spread(text, s, e, was):
    """The words of a correction, sharing out the stretch of audio it replaces by their length.
    A correction to nothing leaves a marker, so there is still something to click to undo it."""
    parts = text.split() or ["[…]"]
    total, out, t = sum(len(p) + 1 for p in parts), [], s
    for p in parts:
        d = (e - s) * (len(p) + 1) / total
        out.append({"s": round(t, 2), "e": round(t + d, 2), "w": p, "was": was})
        t += d
    return out


def apply_fixes(chunks, fixes):
    """Lay the corrections over the machine's words. Corrections never overlap one another."""
    for f in fixes:
        new = spread(f["text"], f["s"], f["e"], f["was"])
        for c in chunks:
            kept = []
            for w in c["words"]:
                if f["s"] <= mid(w) <= f["e"]:
                    kept, new = kept + new, []  # the correction goes where its first replaced word was
                else:
                    kept.append(w)
            c["words"] = kept
    return chunks


def revise(machine, fixes, s, e, text):
    """The corrections after replacing what is now shown between s and e with text, or, with
    text None, after putting the machine's words back there. `machine` is the untouched transcript."""
    words = lambda chunks: [w for c in chunks for w in c["words"]]
    original = words(machine)
    current = words(apply_fixes([{"words": list(c["words"])} for c in machine], fixes))
    chosen = [w for w in current if s <= mid(w) <= e]
    touched = [f for f in fixes if any(f["s"] <= mid(w) <= f["e"] for w in chosen)]
    rest = [f for f in fixes if f not in touched]
    if text is None:
        return rest
    # An earlier correction that is partly re-edited is replaced whole, keeping its other words.
    S, E = min([s] + [f["s"] for f in touched]), max([e] + [f["e"] for f in touched])
    span = [w for w in current if S <= mid(w) <= E]
    full = " ".join([w["w"] for w in span if mid(w) < s] + text.split() + [w["w"] for w in span if mid(w) > e])
    was = " ".join(w["w"] for w in original if S <= mid(w) <= E)
    if full != was:
        rest.append({"s": S, "e": E, "text": full, "was": was})
    return sorted(rest, key=lambda f: f["s"])


def fixes_file(tape_id):
    return TAPES / (tape_id + ".fixes")


def read_fixes(tape_id):
    return load(fixes_file(tape_id)) if fixes_file(tape_id).exists() else []


def transcript(tape_id):
    """A recording's transcript as it now stands: the machine's words with any corrections laid over them."""
    d = load_tape(tape_id)  # also refuses a bad id before anything else touches the disk
    apply_fixes(d["chunks"], read_fixes(tape_id))
    return d


def sound(audio):
    """Two measures of a recording, each BLOCKS times a second: how like hiss it is (near 1 for hiss, near 0 for
    clear tones), and how much its make-up changed since the moment before (near 0 for a steady hum)."""
    import numpy as np
    n = (len(audio) - FRAME) // HOP // PER_BLOCK * PER_BLOCK
    lo, hi = 150 * FRAME // RATE, 4000 * FRAME // RATE  # where voices and instruments are; below is rumble and mains hum
    flat, blocks = [], [np.zeros((0, hi - lo))]
    for s in range(0, n, 4096):  # a slice at a time, or an hour of sound would need gigabytes
        at = np.arange(FRAME) + HOP * np.arange(s, min(s + 4096, n))[:, None]
        m = np.abs(np.fft.rfft(audio[at] * np.hanning(FRAME), axis=1))[:, lo:hi] + 1e-9
        flat.append((np.exp(np.log(m ** 2).mean(1)) / (m ** 2).mean(1)).reshape(-1, PER_BLOCK).mean(1))
        blocks.append(m.reshape(-1, PER_BLOCK, hi - lo).mean(1))
    b = np.concatenate(blocks)
    before = np.concatenate([b[:1], b[:-1]])
    return np.stack([np.concatenate(flat or [[]]), np.abs(b - before).sum(1) / (b + before).sum(1)]).astype(np.float32)


def music(measured, words):
    """The stretches without words that sound like music, as [start, end] in seconds."""
    import numpy as np
    flat, change = measured
    if not len(flat):
        return []
    k = int(SMOOTH * BLOCKS) // 2
    smooth = lambda v: np.median(np.lib.stride_tricks.sliding_window_view(np.pad(v, k, mode="edge"), 2 * k + 1), axis=1)
    edges = lambda mask: np.flatnonzero(np.diff(np.concatenate([[0], mask.astype(np.int8), [0]]))).reshape(-1, 2)
    wordless = np.ones(len(flat), bool)
    for w in words:  # half a second clear of every word
        wordless[max(int((w["s"] - 0.5) * BLOCKS), 0):int((w["e"] + 0.5) * BLOCKS) + 1] = False
    found = wordless & (smooth(flat) < FLAT) & (smooth(change) > CHANGE)
    for a, b in edges(~found):  # a short lapse inside a piece does not end it
        if a and b < len(found) and b - a <= BRIDGE * BLOCKS and wordless[a:b].all():
            found[a:b] = True
    return [[round(a / BLOCKS, 1), round(b / BLOCKS, 1)] for a, b in edges(found) if b - a >= SHORTEST * BLOCKS]


def shown(tape_id):
    """A recording's transcript as the page gets it: as it now stands, with where the music is."""
    import numpy as np
    d = transcript(tape_id)
    kept = TAPES / (tape_id + ".sound.npy")  # measured once, from the listening copy, the first time the recording is opened
    if not kept.exists():
        heard = subprocess.run([FFMPEG, "-v", "error", "-i", str(TAPES / (tape_id + ".m4a")), "-f", "f32le", "-ac", "1",
                                "-ar", str(RATE), "-"], capture_output=True)
        if heard.returncode:  # no listening copy to measure: the transcript is still worth showing
            return {**d, "music": []}
        # ponytail: the whole recording in memory at once, as the transcriber has it: about 4 MB a minute.
        part = kept.with_name(kept.name + ".part.npy")
        np.save(part, sound(np.frombuffer(heard.stdout, np.float32)))
        os.replace(part, kept)
    return {**d, "music": music(np.load(kept), [w for c in d["chunks"] for w in c["words"]])}


def measure(block):
    """A stretch of 24-bit stereo sound: its level over both channels, each channel's peak (all in dBFS),
    and whether it clipped."""
    import numpy as np
    b = np.frombuffer(block, np.uint8).reshape(-1, 2, 3).astype(np.int32)
    v = b[..., 0] | b[..., 1] << 8 | (b[..., 2] ^ 0x80) - 0x80 << 16  # three bytes to a number, the top one carrying the sign
    peak = np.abs(v).max(0)
    db = lambda x: round(20 * math.log10(max(float(x), 0.5) / 2 ** 23), 1)
    return db(math.sqrt(float((v.astype(np.float64) ** 2).mean()))), db(peak[0]), db(peak[1]), bool(peak.max() >= 2 ** 23 - 1)


class Side:
    """One side of a tape as it comes in, a tenth of a second at a time: what of it to keep, and when it is over.
    The master starts LEAD_IN before the tape's first sound and ends TAIL after its last. A deck stopped for
    less than SIDE_ENDS is kept whole; stopped for that long, the side has ended."""

    def __init__(self, below=None):
        self.state = "waiting"  # for the tape to start; then "playing", then "over"
        self.below = below      # the level under which the deck counts as stopped, if the person has set one
        self.floor = None       # otherwise the level of the stopped deck, once learned; False if the lead-in was no stopped deck
        self.before = []        # the lead-in as (level, sound, clipped): all of it until the floor is known, then the last LEAD_IN
        self.held = []          # sound at the stopped level, kept back until it is known whether the side has ended
        self.kept = 0           # tenths of a second in the master so far
        self.clipped = []       # where in the master the sound clipped, in seconds, at most once a second

    def hear(self, block, measured=None):
        """Take in the next tenth of a second. Returns what belongs in the master now, which may be nothing."""
        level, _, _, clipped = measured or measure(block)
        if self.state == "over":
            return b""
        if self.state == "waiting":
            self.before.append((level, block, clipped))
            if self.below is None and self.floor is None:
                if len(self.before) < LEAD_IN * 10:
                    return b""
                low = sorted(l for l, *_ in self.before)[len(self.before) // 5]  # near the quietest, a stray click aside
                self.floor = low if low <= QUIET_ENOUGH else False
            if self.stops_below is None:  # the tape was already running: keep it all, and leave the stopping to the person
                return self.start(0)
            playing = self.below if self.below is not None else self.floor + STARTED_ABOVE
            quiet = [l <= playing for l, *_ in reversed(self.before)]
            lasted = quiet.index(True) if True in quiet else len(quiet)  # how long there has now been sound
            if lasted < STEADY:
                del self.before[:-(LEAD_IN * 10 + STEADY)]
                return b""
            return self.start(max(len(self.before) - lasted - LEAD_IN * 10, 0))
        if self.stops_below is not None and level <= self.stops_below:
            self.held.append(block)
            return self.stop() if len(self.held) >= SIDE_ENDS * 10 else b""
        back, self.held = self.held, []  # sound again: the deck had only paused
        return self.keep([(b, False) for b in back] + [(block, clipped)])

    @property
    def stops_below(self):
        """The level under which the deck counts as stopped: set, or learned. None while that is not known, or never will be."""
        return self.below if self.below is not None else self.floor + STOPPED_ABOVE if self.floor else None

    def start(self, at):
        self.state, lead, self.before = "playing", self.before[at:], []
        return self.keep([(b, c) for _, b, c in lead])

    def keep(self, blocks):
        for _, clipped in blocks:
            if clipped and (not self.clipped or self.kept / 10 - self.clipped[-1] >= 1):
                self.clipped.append(self.kept / 10)
            self.kept += 1
        return b"".join(b for b, _ in blocks)

    def stop(self):
        """End the side. Returns the last of what belongs in the master: at most TAIL of the stopped deck."""
        self.state, tail, self.held = "over", self.held[:TAIL * 10], []
        return self.keep([(b, False) for b in tail])


class Master:
    """A WAV file being written. Its header is brought up to date as it grows, so that whatever happens
    the file on disk is one that plays as far as it had got."""

    def __init__(self, path):
        self.f, self.size = open(path, "xb"), 0  # never over a file that is already there
        self.f.write(self.head())

    def head(self):
        return struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + self.size, b"WAVE", b"fmt ", 16, 1, 2, REC_RATE,
                           REC_RATE * REC_BYTES, REC_BYTES, 24, b"data", self.size)

    def write(self, sound):
        self.f.write(sound)
        self.size += len(sound)

    def settle(self):
        self.f.seek(0)
        self.f.write(self.head())
        self.f.seek(0, 2)
        self.f.flush()
        os.fsync(self.f.fileno())


def recorder(spec):
    """`app.py --record <json>`: the program that listens to a sound input. The app starts it, gives it orders one
    to a line on its standard input ({"record": path}, {"stop": true}), and ends it by closing that. It answers one
    JSON object to a line: the level ten times a second, and {"done": ...} when a side is finished.
    It is a program of its own so that nothing the app is busy with can hold up the sound."""
    def say(**message):
        try:
            print(json.dumps(message), flush=True)
        except OSError:
            pass  # the app has gone: the side is still finished properly

    orders = queue.Queue()

    def listen_for_orders():
        for line in sys.stdin:
            orders.put(json.loads(line))
        orders.put(None)  # the app closed the panel, or is no longer there
    threading.Thread(target=listen_for_orders, daemon=True).start()
    if spec.get("file"):
        # Tests: a sound file stands in for an input. It would be over before Record was pressed, so it waits for that.
        print(json.dumps({"test": "waiting for Record"}), flush=True)
        orders.put(orders.get())
        print(json.dumps({"test": "playing the file"}), flush=True)
        played = subprocess.Popen([FFMPEG, "-v", "error", "-i", spec["file"], "-af", f"pan=stereo|c0=c{int(spec['left'])}|c1=c{int(spec['right'])}",
                                   "-ar", str(REC_RATE), "-f", "s24le", "-"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE)
        incoming = lambda: (played.stdout.read(REC_BLOCK), False)
    else:
        # The sound system hands over every buffer by calling arrived(), and they wait in a queue until taken:
        # nothing is skipped because this program was busy. (ffmpeg's own Mac input keeps only the newest buffer,
        # and on the interface this was built with it lost one in nine.) If the sound system itself ever has to
        # throw sound away it says so, and so does a jump in the buffers' own clock: either way the side is told.
        import sounddevice as sd
        waiting, clock = queue.Queue(), {"next": None}

        def arrived(data, frames, when, status):
            late = clock["next"] is not None and when.inputBufferAdcTime - clock["next"] > 0.02
            clock["next"] = when.inputBufferAdcTime + frames / REC_RATE
            waiting.put((bytes(data), bool(status.input_overflow) or late))
        device = next(i for i, d in enumerate(sd.query_devices()) if d["name"] == spec["device"] and d["max_input_channels"])
        stream = sd.RawInputStream(device=device, samplerate=REC_RATE, channels=2, dtype="int24", blocksize=REC_RATE // 10, dither_off=True,
                                   extra_settings=sd.CoreAudioSettings(channel_map=[int(spec["left"]), int(spec["right"])]), callback=arrived)
        stream.start()

        def incoming():
            try:
                return waiting.get(timeout=5)
            except queue.Empty:  # unplugged, or switched off
                return b"", False
    side = master = path = None

    def finish(why):
        """Close the side being kept, give it its name, and say what became of it."""
        nonlocal side, master
        done = {"why": why, "file": None, "seconds": side.kept / 10, "auto": side.stops_below is not None, "lost": lost}
        try:
            last = side.stop() if side.state != "over" else b""
            if master:
                master.write(last)
                done.update(seconds=side.kept / 10, clipped=side.clipped)
                master.settle()
                master.f.close()
                final, n = Path(path), 1
                while final.exists():  # the app looked before starting; this is for a file that turned up since
                    n += 1
                    final = Path(path).with_name(f"{Path(path).stem} ({n}).wav")
                os.replace(path + ".part", final)
                done["file"] = str(final)
        except OSError as e:
            done["why"] = f"{why}; and the file could not be finished: {e}. What was recorded is in {path}.part"
        side = master = None
        say(done=done)

    kept_at_last_settle = 0
    top, quietest = [-200.0, -200.0], 0.0  # the loudest each channel has been and the quietest the input has been, since asked to forget
    lost = []  # where in the side being kept the sound system lost sound, in seconds
    while True:
        block, gap = incoming()  # waits until a tenth of a second has come in
        closing = False
        while not orders.empty():
            order = orders.get()
            if order is None:
                closing = True
            elif order.get("record") and not side:
                path, side, kept_at_last_settle, lost = order["record"], Side(order.get("below")), 0, []
            elif order.get("stop") and side:
                finish("stopped")
            elif order.get("forget"):
                top, quietest = [-200.0, -200.0], 0.0
            elif "below" in order and side and order["below"] is not None:  # the stop level, moved while the side runs
                side.below = order["below"]
        if len(block) < REC_BLOCK:  # the input has gone: unplugged, refused, or a test's file at its end
            if side:
                finish("the input stopped")
            say(error="The sound input stopped. Is it still plugged in and switched on?")
            break
        if gap and side and side.state == "playing":
            lost.append(side.kept / 10)
        level, left, right, clipped = measure(block)
        top, quietest = [max(top[0], left), max(top[1], right)], min(quietest, level)
        if side:
            try:
                sound = side.hear(block, (level, left, right, clipped))
                if sound:
                    master = master or Master(path + ".part")
                    master.write(sound)
                    if side.kept - kept_at_last_settle >= 20:
                        master.settle()
                        kept_at_last_settle = side.kept
            except OSError as e:  # the disk is full, or has gone
                finish(f"it could not be written: {e}")
        if side and side.state == "over":
            finish("the side ended")
        learning = bool(side) and side.below is None and side.floor is None
        say(l=left, r=right, clip=clipped, level=level, top=top, quietest=quietest, state=side.state if side else "listening",
            seconds=side.kept / 10 if side else 0, stopped=len(side.held) / 10 if side else 0,
            below=side.stops_below if side else None, auto=None if not side or learning else side.stops_below is not None, lost=len(lost))
        if closing:
            if side:
                finish("Transclip was closed")
            break


def ears(hear):
    """Which of the two channels coming in goes to the left ear and which to the right: both as they are,
    or one of them in both ears, for a tape recorded on one track."""
    return {"left": [0, 0], "right": [1, 1]}.get(hear, [0, 1])


def to_outputs(count, left, right):
    """For a device with `count` outputs: what each one is sent. 0 is the left of what is heard, 1 the right, -1 nothing."""
    sent = [-1] * count
    sent[left], sent[right] = 0, 1
    return sent


def monitor(spec):
    """`app.py --monitor <json>`: plays a sound input straight through to an output, so that a tape can be heard.
    It runs until its standard input is closed. It is a program of its own and takes no part in recording:
    whatever becomes of it, the master is not touched."""
    import sounddevice as sd
    try:
        all_of_them = sd.query_devices()
        find = lambda name, kind: next(i for i, d in enumerate(all_of_them) if d["name"] == name and d[kind])
        coming, going = find(spec["input"], "max_input_channels"), find(spec["output"], "max_output_channels")
        route = ears(spec.get("hear"))

        def through(indata, outdata, frames, when, status):
            outdata[:] = indata[:, route]
        wiring = (sd.CoreAudioSettings(channel_map=[int(spec["left"]), int(spec["right"])]),
                  sd.CoreAudioSettings(channel_map=to_outputs(all_of_them[going]["max_output_channels"], int(spec["out_left"]), int(spec["out_right"]))))
        with sd.Stream(device=(coming, going), samplerate=REC_RATE, channels=2, dtype="float32", latency="low", extra_settings=wiring, callback=through):
            print(json.dumps({"on": True}), flush=True)
            sys.stdin.read()
    except Exception as e:  # no such device any more, or one that will not open: said, not thrown
        print(json.dumps({"error": str(e) or type(e).__name__}), flush=True)


def norm(word):
    return "".join(ch for ch in word.lower() if ch.isalnum())


def natural(s):
    return [int(x) if x.isdigit() else x.lower() for x in re.split(r"(\d+)", s)]


_index = {"sig": None}


def library():
    """Every transcript, held in memory and reloaded when any of the files changes."""
    global _index
    files = sorted(TAPES.rglob("*.json"))
    changed = lambda p: p.stat().st_mtime_ns if p.exists() else 0
    sig = tuple((str(p), changed(p), changed(p.with_suffix(".fixes"))) for p in files)
    if _index["sig"] == sig:
        return _index
    tapes = {}
    for p in files:
        tid = p.relative_to(TAPES).as_posix()[:-len(".json")]
        d = load(p)
        apply_fixes(d["chunks"], read_fixes(tid))
        pairs = [(w, norm(w["w"])) for c in d["chunks"] for w in c["words"]]
        words, tokens = [w for w, t in pairs if t], [t for w, t in pairs if t]
        where = {}
        for i, t in enumerate(tokens):
            where.setdefault(t, []).append(i)
        tapes[tid] = {"id": tid, "title": tid, "same_as": None, "duration": d["duration"], "source": d["source"],
                      "words": words, "tokens": tokens, "where": where,
                      "grams": set(zip(*(tokens[i:] for i in range(5))))}
    # A titled file whose wording is mostly found in one raw recording is the same audio:
    # it is labelled as a copy, and the raw recording takes its name. Both stay searchable.
    raws = [t for t in tapes if t.split("/")[0] == RAW]
    lent = {}  # raw recording -> (wording shared, titled file): the copy that covers most of it gives the name
    for tid in sorted(tapes, key=natural):
        grams = tapes[tid]["grams"]
        if tid in raws or len(grams) < 50:
            continue
        best = max(raws, key=lambda r: len(grams & tapes[r]["grams"]), default=None)
        shared = len(grams & tapes[best]["grams"]) if best else 0
        if shared >= 0.5 * len(grams):
            tapes[tid]["same_as"] = best
            tapes[tid]["title"] = f"{tid} (copy of {best.split('/')[-1]})"
            if shared > lent.get(best, (0, None))[0]:
                lent[best] = (shared, tid)
    for raw, (_, tid) in lent.items():
        tapes[raw]["title"] = f"{tid} ({raw.split('/')[-1]})"
    vocab = sorted({t for tape in tapes.values() for t in tape["tokens"]})
    _index = {"sig": sig, "tapes": tapes, "vocab": vocab, "expanded": {}}
    return _index


def edits(a, b, limit):
    """Edit distance between two words; limit + 1 as soon as it is certain to be more than the limit."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    row = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        prev, row[0] = row[0], i
        for j, cb in enumerate(b, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (ca != cb))
        if min(row) > limit:
            return limit + 1
    return row[-1]


def expand(term, vocab):
    """The transcript words a typed term may stand for: itself, words it begins, and near-misspellings
    (one letter out for words of four letters or more, two for eight or more)."""
    limit = 0 if len(term) < 4 else 1 if len(term) < 8 else 2
    return {term} | {v for v in vocab if (len(term) >= 3 and v.startswith(term)) or (limit and edits(term, v, limit) <= limit)}


def phrase_starts(tokens, where, sets):
    """Positions where consecutive words match the term sets, in order."""
    return [i for i in sorted(i for v in sets[0] for i in where.get(v, ()))
            if i + len(sets) <= len(tokens) and all(tokens[i + k] in s for k, s in enumerate(sets))]


def passages(words):
    """(first, end) word positions of the overlapping passages, starting afresh after any long silence."""
    out, start = [], 0
    for i in range(1, len(words) + 1):
        if i == len(words) or words[i]["s"] - words[i - 1]["e"] >= BREAK:
            for a in range(start, i, STEP):
                if min(a + PASSAGE, i) - a >= 8:  # a few stray words are not worth a passage
                    out.append((a, min(a + PASSAGE, i)))
                if a + PASSAGE >= i:
                    break
            start = i
    return out


_embedder, _meaning, _meaning_lock = None, {"sig": None}, threading.Lock()


def embedder():
    global _embedder
    if _embedder is None:
        from fastembed import TextEmbedding  # imported late: it takes seconds and only meaning search needs it
        _embedder = TextEmbedding("BAAI/bge-small-en-v1.5", cache_dir=str(BASE / "models"))
    return _embedder


def meaning_index():
    """A vector for every passage of every searched recording. The vectors are kept next to
    each transcript and worked out again only when that transcript changes."""
    global _meaning
    import numpy as np
    with _meaning_lock:
        ix = library()
        if _meaning["sig"] == ix["sig"]:
            return _meaning
        spans, vecs = [], [np.zeros((0, 384), np.float32)]
        for tape in ix["tapes"].values():
            spots = passages(tape["words"])
            if not spots:
                continue
            kept = TAPES / (tape["id"] + ".emb.npy")
            newest = max(f.stat().st_mtime for f in (TAPES / (tape["id"] + ".json"), fixes_file(tape["id"])) if f.exists())
            fresh = kept.exists() and kept.stat().st_mtime >= newest
            v = np.load(kept) if fresh else None
            if v is None or len(v) != len(spots):
                v = np.array(list(embedder().embed(" ".join(w["w"] for w in tape["words"][a:b]) for a, b in spots)), np.float32)
                part = kept.with_name(kept.name + ".part.npy")
                np.save(part, v)
                os.replace(part, kept)
            spans += [(tape["id"], a, b) for a, b in spots]
            vecs.append(v)
        _meaning = {"sig": ix["sig"], "tapes": ix["tapes"], "spans": spans, "vecs": np.concatenate(vecs)}
        return _meaning


def read_selects():
    if SELECTS.exists():
        return load(SELECTS)
    return {"handle": 1, "target": 0, "bins": [{"name": "Selects", "clips": []}]}


def write_selects(sel):
    if not isinstance(sel, dict) or not isinstance(sel.get("bins"), list):
        abort(400, "selects must have a list of bins")
    dump(SELECTS, sel)


def open_session(name):
    """Work on a session from here on: its recordings, clips, settings and exports, and no other's.
    One that does not exist yet is made. No name means the first library."""
    global HOME, TAPES, SELECTS, SETTINGS, EXPORTS, ORIGINALS
    home = BASE / "sessions" / name if name else BASE
    (home / "tapes").mkdir(parents=True, exist_ok=True)
    HOME, TAPES, SELECTS, SETTINGS, EXPORTS, ORIGINALS = (
        home, home / "tapes", home / "selects.json", home / "settings.json", home / "exports", home / "originals")
    backup = SELECTS.with_name(f"selects.{time.strftime('%Y-%m-%d')}.bak.json")
    if SELECTS.exists() and not backup.exists():
        shutil.copy2(SELECTS, backup)  # one copy per day, as things stood when first opened
    (BASE / "session.txt").write_text(name, encoding="utf-8")  # the one to come back to at the next launch


def settings():
    s = {"folder": "", "depth": 3}
    return {**s, **load(SETTINGS)} if SETTINGS.exists() else s


def recordings(folder, depth):
    """The audio files in a folder and in up to `depth` levels of folders inside it, as paths relative to it."""
    found = []
    for here, dirs, files in os.walk(folder):
        level = len(Path(here).relative_to(folder).parts)
        dirs[:] = sorted(d for d in dirs if not d.startswith(".")) if level < depth else []
        found += [(Path(here) / f).relative_to(folder).as_posix() for f in files
                  if not f.startswith(".") and Path(f).suffix.lower() in AUDIO]
    return sorted(found, key=natural)


def id_of(recording):
    return PurePosixPath(recording).with_suffix("").as_posix()


def pending():
    """The chosen folder (None while it cannot be reached), the recordings in it, and those not yet transcribed."""
    s = settings()
    try:  # listing it is the test: a folder the system will not let us read is no more use than a missing one
        folder = Path(s["folder"]) if s["folder"] and os.listdir(s["folder"]) is not None else None
    except OSError:
        folder = None
    found = recordings(folder, s["depth"]) if folder else []
    return folder, found, [r for r in found if not (TAPES / (id_of(r) + ".json")).exists()]


def original(tid, tape):
    """Where a recording's original is now: in the chosen folder, or failing that where it was when transcribed."""
    here = Path(settings()["folder"] or ".") / (tid + Path(tape["source"]).suffix)
    return here if here.exists() else Path(tape["source"])


def collect(ids, raw):
    """Gather recordings into one file to give to someone else: what this app keeps of each and,
    with `raw`, the original recordings too. Returns the file."""
    what = posixpath.commonpath(ids)  # the one recording, the folder they share, or nothing in particular
    out = EXPORTS / f"{safe(what) if what else 'Recordings'} (Transclip {time.strftime('%Y-%m-%d %H.%M.%S')}).zip"
    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_name(out.name + ".part")
    # Stored, not squeezed: the sound is compressed already or too big to be worth the minutes.
    # ponytail: one request does the whole job and the page just waits; give it a progress bar if tens of GB become usual.
    with zipfile.ZipFile(part, "w") as z:
        for tid in ids:
            d = load_tape(tid)
            src = original(tid, d)
            # The transcript says where the original was on this computer. Its name is all anyone else needs.
            z.writestr(f"tapes/{tid}.json", json.dumps({**d, "source": src.name}, ensure_ascii=False))
            for ext in EXTRAS:
                if (TAPES / (tid + ext)).exists():
                    z.write(TAPES / (tid + ext), f"tapes/{tid}{ext}")
            if raw:
                z.write(src, f"originals/{tid}{src.suffix}")
    os.replace(part, out)
    return out


def member(name):
    """What a file in a collected file is, as (folder, recording, extension), or None for anything
    that has no business there or would land outside the library."""
    parts = name.split("/")
    ext = PurePosixPath(name).suffix
    if (len(parts) < 2 or "\\" in name or ":" in name or any(part in ("", "..") or part.startswith(".") for part in parts)
            or not (parts[0] == "tapes" and ext in (".json", *EXTRAS) or parts[0] == "originals" and ext.lower() in AUDIO)):
        return None
    return parts[0], "/".join(parts[1:])[:-len(ext)], ext


def well_formed(d, fixes):
    """Whether a transcript and its corrections from elsewhere have the shape this app's own do.
    One that had not would trip up the whole library, not just itself."""
    num = lambda v: type(v) in (int, float) and v - v == 0  # a number, and not one of the infinite or undefined ones
    try:
        return (num(d["duration"]) and isinstance(d["source"], str)
                and all(num(c["start"]) and num(c["end"]) and bool(re.fullmatch("[a-z]{2,3}", c["lang"]))
                        and all(num(w["s"]) and num(w["e"]) and isinstance(w["w"], str) for w in c["words"])
                        for c in d["chunks"])
                and all(num(f["s"]) and num(f["e"]) and isinstance(f["text"], str) and isinstance(f["was"], str) for f in fixes))
    except (KeyError, TypeError):
        return False


def take_in(file):
    """Add the recordings in a collected file to the library. Ones already here are left as they are.
    The file is someone else's, so nothing in it is taken on trust: see member() and well_formed()."""
    added = kept = bad = 0
    with zipfile.ZipFile(file) as z:
        inside = {m: z.getinfo(n) for n in z.namelist() if (m := member(n))}
        if sum(i.file_size for i in inside.values()) > shutil.disk_usage(HOME).free:
            return None

        def put(info, root, name):
            dest = Path(safe_join(str(root), name))  # refuses a name that climbs out of the folder, should member() ever let one by
            dest.parent.mkdir(parents=True, exist_ok=True)
            part = dest.with_name(dest.name + ".part")
            with z.open(info) as a, open(part, "wb") as b:
                shutil.copyfileobj(a, b)
            os.replace(part, dest)

        for (kind, tid, ext), info in sorted(inside.items()):
            if (kind, ext) != ("tapes", ".json"):
                continue
            if (TAPES / (tid + ".json")).exists():
                kept += 1
                continue
            try:
                # ponytail: read whole, like every transcript here; 50 MB is far beyond a day-long recording's.
                assert info.file_size < 50e6
                d = json.loads(z.read(info))
                fixes = json.loads(z.read(inside["tapes", tid, ".fixes"])) if ("tapes", tid, ".fixes") in inside else []
                assert well_formed(d, fixes)
            except (ValueError, AssertionError):
                bad += 1
                continue
            # Never somebody else's idea of where a file is on this computer: the original that came
            # with it, or just its name, to be looked for in the recordings folder.
            d["source"] = Path(d["source"].replace("\\", "/")).name
            for (k, t, e), i in inside.items():
                if t != tid:
                    continue
                if k == "originals":
                    put(i, ORIGINALS, tid + e)
                    d["source"] = str(ORIGINALS / (tid + e))
                elif e != ".json":
                    put(i, TAPES, tid + e)
            dump(Path(safe_join(str(TAPES), tid + ".json")), d)  # last: its presence is what makes a recording part of the library
            added += 1
    return {"added": added, "kept": kept, "bad": bad}


def unfound(ids):
    """The recordings among these whose original is not where it is looked for, each with the file name wanted."""
    tapes = library()["tapes"]
    return [{"tape": t, "name": src.name} for t in sorted(set(ids), key=natural) if not (src := original(t, tapes[t])).exists()]


def length(path):
    """How long a sound file runs, in seconds; None if ffmpeg cannot read it as one."""
    said = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path)], capture_output=True, text=True, errors="replace").stderr
    found = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", said)
    return found and int(found[1]) * 3600 + int(found[2]) * 60 + float(found[3])


_job = {"proc": None, "state": {"running": False}}


def start_job(folder, todo):
    uv = os.environ.get("UV") or shutil.which("uv")  # uv names itself to what it runs
    if not uv:
        abort(500, "uv was not found, so the transcriber cannot be started. Start the app with: uv run app.py")
    dump(HOME / "jobs.json", [[str(folder / r), str(TAPES / id_of(r))] for r in todo])
    # The transcriber is a program of its own, with its own heavy libraries. It runs for as long
    # as we hold its standard input open, so it can never outlive the app.
    proc = subprocess.Popen([uv, "run", "--script", str(ROOT / "transcribe.py"), str(HOME / "jobs.json"), "--stop-with-stdin"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace")
    state = {"running": True, "i": 0, "n": len(todo), "file": "", "fraction": 0, "failed": [],
             "note": "Starting the transcriber (the first time, it downloads what it needs)"}
    _job.update(proc=proc, state=state)

    def watch():
        for line in proc.stdout:
            try:
                said = json.loads(line)
            except ValueError:
                said = None
            if not isinstance(said, dict):  # uv, a download or a crash talking
                state["note"] = line.strip()[-200:] or state["note"]
                continue
            if "file" in said:
                said["file"] = Path(said["file"]).relative_to(folder).as_posix()
                said.setdefault("note", "")
            if "error" in said:
                state["failed"].append({"file": said["file"], "error": said["error"]})
            state.update({k: said[k] for k in ("i", "n", "file", "fraction", "note") if k in said})
        if proc.wait() == 0 and state["running"]:
            state["note"] = "Finished." if not state["failed"] else f"Finished, but {len(state['failed'])} could not be read."
            waiting = [r for f, r in _queue if f == folder]  # sides recorded while this was going on
            del _queue[:]
            if waiting:
                return start_job(folder, waiting)
        elif state["running"]:
            state["note"] = "The transcriber stopped: " + state["note"]
        del _queue[:]  # stopped or failed: they are not lost, only left "to do" like any other recording
        state["running"] = False

    threading.Thread(target=watch, daemon=True).start()


def stop_job():
    proc, state = _job["proc"], _job["state"]
    if proc and state["running"]:
        state.update(running=False, note="Stopped. Recordings already finished are kept.")
        proc.stdin.close()
        proc.terminate()  # in case it had not got as far as listening


_rec = {"proc": None, "state": {"on": False}}  # the program listening to a sound input, and the last it said
_queue = []  # sides recorded while the transcriber was busy, as (folder, recording)


def recording():
    return _rec["state"].get("state") in ("waiting", "playing")


def inputs(way="input"):
    """The sound inputs this computer has (or with "output", its outputs): each one's name, how many channels it
    offers, the rate it is running at, and whether it is the one the system uses unless told otherwise."""
    if TEST_INPUT:
        return [{"name": "Test input", "channels": 2, "rate": REC_RATE, "usual": True}] if way == "input" else []
    if sys.platform != "darwin":
        return []  # ponytail: recording is built on the Mac's sound system. PortAudio speaks to Windows' and Linux's too, when wanted.
    told = json.loads(subprocess.run(["system_profiler", "SPAudioDataType", "-json"], capture_output=True, text=True).stdout or "{}")
    return [{"name": d["_name"], "channels": int(d[f"coreaudio_device_{way}"]), "rate": int(d.get("coreaudio_device_srate") or 0),
             "usual": f"coreaudio_default_audio_{way}_device" in d}
            for d in told.get("SPAudioDataType", [{}])[0].get("_items", []) if d.get(f"coreaudio_device_{way}")]


_mon = {"proc": None, "state": {"on": False}}  # the program playing the input through to an output, and what it last said


def unmonitor():
    proc, _mon["proc"] = _mon["proc"], None
    if proc and proc.poll() is None:
        proc.stdin.close()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.terminate()


def hush():
    """Stop listening, and stop playing the input through. A side being recorded is finished properly first."""
    unmonitor()
    proc, _rec["proc"] = _rec["proc"], None
    if proc and proc.poll() is None:
        proc.stdin.close()
        try:
            proc.wait(15)
        except subprocess.TimeoutExpired:
            proc.terminate()


def listen(device, left, right):
    """Start listening to an input, for the meter and ready to record from."""
    hush()
    spec = {"file": TEST_INPUT} if TEST_INPUT else {"device": device}
    proc = subprocess.Popen([sys.executable, str(ROOT / "app.py"), "--record", json.dumps({**spec, "left": left, "right": right})],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    state = {"on": True, "device": device, "left": left, "right": right, "state": "listening", "last": None}
    _rec.update(proc=proc, state=state)

    def watch():
        for line in proc.stdout:
            try:
                said = json.loads(line)
            except ValueError:
                said = None
            if not isinstance(said, dict):  # not its own voice: the sound system's, or a crash
                state["complaint"] = line.strip()[-300:] or state.get("complaint")
                continue
            if "done" not in said:
                state.update(said)
                continue
            state.update(last={**said["done"], "tape": state.get("tape"), "side": state.get("side")}, state="listening", seconds=0, stopped=0)
            if said["done"]["file"]:  # transcribed at once, or as soon as the transcriber is free
                folder, rel = Path(state["folder"]), Path(said["done"]["file"]).relative_to(state["folder"]).as_posix()
                try:
                    _queue.append((folder, rel)) if _job["state"]["running"] else start_job(folder, [rel])
                except Exception:
                    pass  # the sound is safe, and the side is "to do" like any other recording
        if proc is _rec["proc"]:  # it stopped without being told to: say what it said last
            state.setdefault("error", state.get("complaint") or "The program that listens to the input stopped.")
        state["on"] = False
    threading.Thread(target=watch, daemon=True).start()


def tell(order):
    """Give the listening program an order."""
    _rec["proc"].stdin.write(json.dumps(order) + "\n")
    _rec["proc"].stdin.flush()


def level(given):
    """A stop level as the page sends it: a number of dBFS, kept within reason, or None for "learn it"."""
    return None if given is None else max(-120.0, min(float(given), -20.0))


def bin_it(path):
    """Put a file in the system's bin, where it can be had back; or where there is no bin to be had, set it aside
    under a name the app does not take for a recording."""
    try:
        from Foundation import NSURL, NSFileManager
        if not TEST_INPUT and NSFileManager.defaultManager().trashItemAtURL_resultingItemURL_error_(NSURL.fileURLWithPath_(str(path)), None, None)[0]:
            return  # (a test's leavings are kept out of the person's bin)
    except ImportError:
        pass
    os.replace(path, path.with_name(path.name + time.strftime(".replaced %Y-%m-%d %H.%M.%S")))


def rescue():
    """Give a recording that was cut short (a crash, the power) its name. It is whole as far as it got."""
    folder, saved = pending()[0], []
    for part in sorted(folder.glob("*/*.wav.part")) if folder else []:
        final, size = part.with_suffix(""), (part.stat().st_size - 44) // REC_BYTES * REC_BYTES
        if size <= 0 or final.exists():
            continue
        with open(part, "r+b") as f:  # its header may be a couple of seconds behind
            f.seek(4), f.write(struct.pack("<I", 36 + size)), f.seek(40), f.write(struct.pack("<I", size))
        os.replace(part, final)
        saved.append(final.relative_to(folder).as_posix())
    return saved


def status():
    folder, found, todo = pending()
    return {**settings(), "reachable": folder is not None, "found": len(found), "todo": len(todo),
            "transcribed": len(library()["tapes"]), "job": _job["state"], "home": str(HOME),
            "session": "" if HOME == BASE else HOME.name,
            "sessions": sorted((p.name for p in (BASE / "sessions").glob("[!.]*") if p.is_dir()), key=natural)}


def window(webview, url):
    """Show the page in a window of the app's own. Returns when the window has been closed."""
    from Foundation import NSBundle
    NSBundle.mainBundle().infoDictionary()["CFBundleName"] = "Transclip"  # or the menus say "Quit" where they should say "Quit Transclip"
    # Words are selected by dragging across them, which a window does not allow unless told to.
    w = webview.create_window("Transclip", url, width=1440, height=900, min_size=(900, 560), text_select=True)
    # Closing the window quits, and quitting stops the transcriber: ask first while it is at work.
    w.events.closing += lambda: setattr(w, "confirm_close", _job["state"]["running"] or recording())
    webview.start(private_mode=False,  # a private window would forget which recording was open
                  localization={"global.quitConfirmation": "A recording or a transcription is still running. Quitting stops it; "
                                "what is already recorded or transcribed is kept."})


def pick(what, prompt):
    """Ask for a "folder" or a "file" with the system's own dialog. Empty if none was chosen."""
    if sys.platform == "darwin":
        # The prompt goes in as an argument, not as part of the script: it may hold a file name that came from someone else.
        cmd = ["osascript", "-e", "on run argv", "-e", "activate", "-e", f"POSIX path of (choose {what} with prompt (item 1 of argv))",
               "-e", "end run", prompt]
    else:
        # ponytail: not tried on Windows or Linux yet; the folder can always be typed or pasted instead.
        cmd = [sys.executable, "-c", "import tkinter as t, tkinter.filedialog as f; r = t.Tk(); r.withdraw(); "
               f"r.attributes('-topmost', True); print(f.{'askdirectory' if what == 'folder' else 'askopenfilename'}() or '')"]
    return subprocess.run(cmd, capture_output=True, text=True).stdout.strip()


def reveal(folder):
    """Show a folder in the system's file manager."""
    if QUIET:
        return
    try:
        if sys.platform == "win32":
            os.startfile(folder)
        else:
            subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", str(folder)])
    except OSError:
        pass  # nothing to show it with: the page still says where the folder is


def safe(name):
    """A file-name-safe version of a tape, bin or clip name."""
    name = re.sub(r"\s*/\s*", " - ", name)
    name = re.sub(r'[\\:*?"<>|\x00-\x1f]', "_", name)
    return re.sub(r"\s+", " ", name).strip(" .")[:80] or "untitled"


def stamp(t):
    return f"{int(t // 60):02}m{int(t % 60):02}s"


def clock(t):
    t = round(t, 1)
    return f"{int(t // 3600):02}:{int(t % 3600 // 60):02}:{t % 60:04.1f}"


@app.get("/")
def index():
    return app.send_static_file("index.html")


@app.get("/tapes/<path:name>")
def tape_file(name):
    return send_from_directory(TAPES, name, mimetype="audio/mp4" if name.endswith(".m4a") else None)


@app.get("/api/ping")
def ping():
    return jsonify({"app": "transclip"})


@app.post("/api/quit")
def quit_app():
    threading.Timer(0.3, os._exit, [0]).start()  # after the answer has gone out; the transcriber follows us down
    return "", 204


@app.get("/api/library")
def get_library():
    return jsonify(status())


@app.put("/api/library")
def put_library():
    body = request.get_json()
    if recording():
        return jsonify({"error": "A side is being recorded into this folder."}), 409
    try:
        depth = max(0, min(int(body.get("depth", 3)), 20))
    except (TypeError, ValueError):
        abort(400, "depth must be a whole number")
    dump(SETTINGS, {"folder": str(body.get("folder") or "").strip(), "depth": depth})
    return jsonify(status())


@app.put("/api/session")
def put_session():
    """Open the session of that name, making it if there is none; with no name, the first library."""
    if _job["state"]["running"] or recording():
        return jsonify({"error": "Stop the transcription or the recording before changing session: it belongs to this one."}), 409
    name = str(request.get_json().get("name") or "").strip()
    open_session(safe(name) if name else "")
    return jsonify(status())


@app.post("/api/choose-folder")
def choose():
    return jsonify({"folder": pick("folder", "The folder containing your recordings")})


@app.get("/api/inputs")
def get_inputs():
    return jsonify(inputs())


@app.get("/api/outputs")
def get_outputs():
    return jsonify(inputs("output"))


@app.put("/api/monitor")
def monitor_start():
    """Play an input straight through to an output: {"input", "left", "right", "output", "out_left", "out_right", "hear"}.
    Apart from the recorder, so it can be started and stopped while a side is being recorded."""
    body = request.get_json()
    coming = next((d for d in inputs() if d["name"] == body.get("input")), None)
    going = next((d for d in inputs("output") if d["name"] == body.get("output")), None)
    if not coming or not going:
        return jsonify({"error": "That input or output is not there. Is it plugged in?"}), 409
    try:
        spec = {"input": coming["name"], "output": going["name"], "hear": str(body.get("hear") or "both"),
                **{k: min(max(int(body.get(k, i % 2)), 0), (coming if i < 2 else going)["channels"] - 1)
                   for i, k in enumerate(("left", "right", "out_left", "out_right"))}}
    except (TypeError, ValueError):
        abort(400, "the channels must be numbers")
    unmonitor()
    proc = subprocess.Popen([sys.executable, str(ROOT / "app.py"), "--monitor", json.dumps(spec)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    state = {**spec, "on": True}
    _mon.update(proc=proc, state=state)

    def watch():
        for line in proc.stdout:
            try:
                state.update(json.loads(line))
            except ValueError:
                state["complaint"] = line.strip()[-300:]
        if proc is _mon["proc"]:  # it stopped without being told to
            state.setdefault("error", state.get("complaint") or "The program that plays the input through stopped.")
        state["on"] = False
    threading.Thread(target=watch, daemon=True).start()
    return jsonify(state)


@app.delete("/api/monitor")
def monitor_stop():
    unmonitor()
    return jsonify({"on": False})


@app.get("/api/monitor")
def monitor_state():
    return jsonify(_mon["state"])


@app.get("/api/record")
def record_state():
    return jsonify(_rec["state"])


@app.put("/api/record")
def record_listen():
    """Listen to a sound input: for the meter, and ready to record from. Also says which recordings,
    cut short earlier, have just been given their names."""
    body = request.get_json()
    device = next((d for d in inputs() if d["name"] == body.get("device")), None)
    if recording() or not device:
        return jsonify({"error": "A side is being recorded." if recording() else "That input is not there. Is it plugged in?"}), 409
    try:
        left, right = (min(max(int(body.get(k, i)), 0), device["channels"] - 1) for i, k in enumerate(("left", "right")))
    except (TypeError, ValueError):
        abort(400, "left and right must be channel numbers")
    saved = rescue()
    listen(device["name"], left, right)
    return jsonify({**_rec["state"], "rate": device["rate"], "rescued": saved})


@app.post("/api/record")
def record_start():
    """Record a side from the input being listened to, as <recordings folder>/<tape>/<side>.wav."""
    body = request.get_json()
    tape, side = (safe(name) if (name := str(body.get(k) or "").strip()) else "" for k in ("tape", "side"))
    folder = pending()[0]
    path = folder / tape / (side + ".wav") if folder and tape and side else None
    there = [f for f in path.parent.iterdir() if f.name == path.name + ".part" or (f.stem == side and f.suffix.lower() in AUDIO)
             ] if path and path.parent.is_dir() else []
    kept = [f for ext in (".json", ".fixes", ".m4a", ".peaks", ".emb.npy", ".sound.npy") if path and (f := TAPES / tape / (side + ext)).exists()]
    taken = bool(there or kept) and not body.get("replace")
    problem = ("Open the Record panel, so the input is being listened to." if not _rec["state"]["on"] or not _rec["proc"]
               else "A side is already being recorded." if recording()
               else "Give the tape and the side each a name." if not (tape and side)
               else "Choose this session's recordings folder first, in Recordings…: the master is written there." if not folder
               else "The recordings folder cannot be written to." if not os.access(folder, os.W_OK)
               else f"There is already a recording called {tape} / {side}." if taken
               else "There is not room on that disk for two hours of recording (2 GB)." if shutil.disk_usage(folder).free < 2e9
               else None)
    if problem:
        return jsonify({"error": problem, "exists": problem.startswith("There is already")}), 409
    for f in there:  # recorded again: the old master goes to the bin, not out of existence
        bin_it(f)
    for f in kept:   # what the app made from it is of no more use
        f.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    _rec["state"].update(folder=str(folder), tape=tape, side=side, last=None, state="waiting", seconds=0, stopped=0, auto=None)
    tell({"record": str(path), "below": level(body.get("below"))})
    return jsonify(_rec["state"])


@app.patch("/api/record")
def record_adjust():
    """While listening: move the stop level of the side being recorded ({"below": dBFS}), or forget the loudest and
    quietest heard so far ({"forget": true})."""
    body = request.get_json()
    if _rec["state"]["on"] and _rec["proc"]:
        tell({"forget": True} if body.get("forget") else {"below": level(body.get("below"))})
    return jsonify(_rec["state"])


@app.delete("/api/record")
def record_stop():
    """Stop the side being recorded, and go on listening; with ?all, stop listening too (the panel was closed)."""
    if request.args.get("all") is not None:
        hush()
    elif recording():
        tell({"stop": True})
    return jsonify(_rec["state"])


@app.post("/api/share")
def share():
    """Collect the named recordings into one file in the exports folder."""
    body = request.get_json()
    tapes, raw = library()["tapes"], bool(body.get("raw"))
    ids = [t for t in body.get("tapes") or [] if t in tapes]
    missing = unfound(ids) if raw else []
    if missing or not ids:
        return jsonify({"error": f"{len(missing)} original recording(s) not found. Is the drive connected, and is the "
                        "recordings folder the right one?" if missing else "No recordings to collect.", "missing": missing}), 409
    out = collect(ids, raw)
    reveal(out.parent)
    return jsonify({"file": str(out), "recordings": len(ids), "bytes": out.stat().st_size})


@app.post("/api/locate")
def locate():
    """Say where a recording's original is: the file named, or one chosen in the system's dialog.
    Clips are cut from it from then on, unless the recordings folder has the recording under its own name."""
    body = request.get_json()
    tid = body.get("tape", "")
    d = load_tape(tid)
    file = body.get("file") or pick("file", f"The original recording of {tid}: {Path(d['source']).name}")
    if not file:
        return jsonify({"cancelled": True})
    runs = length(file) if Path(file).suffix.lower() in AUDIO and Path(file).is_file() else None
    if runs is None:
        return jsonify({"error": "That is not a sound file clips can be cut from."}), 409
    # A file of another length is very likely another recording, and clips would be cut from it at this one's times.
    if abs(runs - d["duration"]) > 1 and not body.get("anyway"):
        return jsonify({"mismatch": True, "file": file, "runs": runs, "was": d["duration"]}), 409
    dump(Path(safe_join(str(TAPES), tid + ".json")), {**d, "source": file})
    return jsonify({"source": file})


@app.post("/api/import")
def import_recordings():
    """Take in a file made by /api/share: the one named, or one chosen in the system's dialog."""
    file = (request.get_json(silent=True) or {}).get("file") or pick("file", "A file of recordings collected in Transclip")
    if not file:
        return jsonify({"cancelled": True})
    try:
        got = take_in(file)
    except (OSError, RuntimeError, zipfile.BadZipFile) as e:  # not there, not a zip, or one locked with a password
        return jsonify({"error": f"That file could not be read as collected recordings: {e}"}), 409
    return jsonify(got) if got else (jsonify({"error": "There is not enough room on the disk for the recordings in that file."}), 409)


@app.post("/api/transcribe")
def transcribe_start():
    folder, _, todo = pending()
    if _job["state"]["running"] or not todo:
        abort(409, "already transcribing, or nothing to transcribe")
    start_job(folder, todo)
    return jsonify(status())


@app.delete("/api/transcribe")
def transcribe_stop():
    stop_job()
    return jsonify(status())


@app.get("/api/transcript/<path:tape_id>")
def get_transcript(tape_id):
    return jsonify(shown(tape_id))


@app.post("/api/fix")
def fix():
    """Correct the words between two times of a recording, or with text null put the machine's back."""
    body = request.get_json()
    tape_id, text = body.get("tape", ""), body.get("text")
    machine = load_tape(tape_id)["chunks"]
    try:
        s, e = float(body["s"]), float(body["e"])
    except (KeyError, TypeError, ValueError):
        abort(400, "s and e must be times in seconds")
    if s > e or not (text is None or (isinstance(text, str) and len(text) <= 2000)):
        abort(400, "text must be a string of at most 2000 characters, or null")
    fixes = revise(machine, read_fixes(tape_id), s, e, text)
    dump(fixes_file(tape_id), fixes)
    return jsonify(shown(tape_id))


@app.get("/api/tapes")
def tapes():
    return jsonify([{"id": t["id"], "title": t["title"], "sameAs": t["same_as"], "duration": t["duration"],
                     "words": len(t["words"])} for t in library()["tapes"].values()])


@app.get("/api/search")
def search():
    ix = library()
    terms = [t for t in map(norm, request.args.get("q", "").split()) if t]
    if not terms:
        return jsonify({"hits": [], "total": 0, "recordings": 0})
    sets = [ix["expanded"].get(t) or ix["expanded"].setdefault(t, expand(t, ix["vocab"])) for t in terms]
    hits = []
    for tape in ix["tapes"].values():
        words = tape["words"]
        text = lambda a, b: " ".join(w["w"] for w in words[max(a, 0):b])
        for i in phrase_starts(tape["tokens"], tape["where"], sets):
            j = i + len(terms)
            hits.append({"tape": tape["id"], "title": tape["title"], "t": words[i]["s"], "end": words[j - 1]["e"],
                         "before": text(i - 8, i), "match": text(i, j), "after": text(j, j + 12),
                         "exact": tape["tokens"][i:j] == terms})
    hits.sort(key=lambda h: (not h["exact"], natural(h["title"]), h["t"]))  # exact wording first
    return jsonify({"hits": hits[:300], "total": len(hits), "recordings": len({h["tape"] for h in hits})})


@app.get("/api/meaning")
def meaning():
    q = request.args.get("q", "").strip()
    if len(q) < 3:
        return jsonify({"hits": []})
    import numpy as np
    m = meaning_index()
    scores = m["vecs"] @ next(iter(embedder().query_embed(q)))
    hits = []
    for k in np.argsort(-scores)[:100]:
        tid, a, b = m["spans"][k]
        if len(hits) == 20 or (len(hits) >= 5 and scores[k] < MIN_SCORE):
            break  # always the five closest; beyond those, only passages that score well
        if any(h["tape"] == tid and h["a"] < b and a < h["b"] for h in hits):
            continue  # overlaps a passage already listed higher up
        tape = m["tapes"][tid]
        hits.append({"tape": tid, "a": a, "b": b, "title": tape["title"], "t": tape["words"][a]["s"],
                     "end": tape["words"][b - 1]["e"], "text": " ".join(w["w"] for w in tape["words"][a:b]),
                     "score": round(float(scores[k]), 3)})
    return jsonify({"hits": hits})


@app.get("/api/selects")
def get_selects():
    return jsonify(read_selects())


@app.put("/api/selects")
def put_selects():
    write_selects(request.get_json())
    return "", 204


@app.post("/api/export")
def export():
    sel = request.get_json()
    write_selects(sel)
    handle = max(0.0, float(sel.get("handle") or 0))
    # A clip still marked for review stays out. It keeps its place in the numbering, so the others' file names
    # do not change when it is let through later.
    jobs = [(b, n, c, transcript(c["tape"])) for b in sel["bins"] for n, c in enumerate(b["clips"], 1) if not c.get("review")]
    jobs = [(b, n, c, tape, original(c["tape"], tape)) for b, n, c, tape in jobs]
    missing = unfound(c["tape"] for _, _, c, *_ in jobs)
    if missing:
        return jsonify({"error": f"{len(missing)} original recording(s) not found. Is the drive connected, "
                                 "and is the recordings folder the right one?", "missing": missing}), 409

    out = EXPORTS / time.strftime("%Y-%m-%d %H.%M.%S")  # a fresh folder per export: nothing is overwritten
    titles = {t["id"]: t["title"] for t in library()["tapes"].values()}
    rows, failed = [], 0
    for b, n, c, tape, src in jobs:
        title = titles.get(c["tape"], c["tape"])
        start = max(0.0, c["start"] - handle)
        end = min(tape["duration"], c["end"] + handle)
        name = f"{n:02}_{safe(title)}_{stamp(c['start'])}-{stamp(c['end'])}"
        if c.get("name"):
            name += "_" + safe(c["name"])
        dest = out / safe(b["name"]) / (name + src.suffix)
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Stream copy: the samples are the original's, untouched, in the original's format.
        # ponytail: two bins whose names clean up to the same folder name could collide;
        # -n then reports the second clip as failed instead of overwriting the first.
        r = subprocess.run([FFMPEG, "-v", "error", "-n", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}",
                            "-i", str(src), "-c", "copy", str(dest)], capture_output=True, text=True)
        failed += r.returncode != 0
        words = " ".join(w["w"] for ch in tape["chunks"] for w in ch["words"]
                         if c["start"] <= (w["s"] + w["e"]) / 2 <= c["end"])
        rows.append([b["name"], n, c.get("name", ""), dest.name, title, str(src), clock(c["start"]), clock(c["end"]),
                     f"{c['end'] - c['start']:.1f}", f"{handle:g}", words,
                     "ok" if r.returncode == 0 else "FAILED: " + r.stderr.strip()[-200:]])

    out.mkdir(parents=True, exist_ok=True)
    with open(out / "log.csv", "w", newline="", encoding="utf-8-sig") as f:  # BOM so Excel reads accents
        w = csv.writer(f)
        w.writerow(["Bin", "No.", "Clip", "File", "Tape", "Cut from", "In", "Out", "Length (s)", "Handles (s)",
                    "Words (machine transcript, with your corrections)", "Status"])
        w.writerows(rows)
    reveal(out)
    return jsonify({"dir": str(out), "clips": len(rows), "failed": failed})


def check():
    # Recording. Sound made to order: a tenth of a second of noise at a level, different every time.
    import tempfile
    import wave
    import numpy as np
    rng = np.random.default_rng(1)
    as_bytes = lambda v: np.asarray(v, "<i4").view(np.uint8).reshape(-1, 4)[:, :3].tobytes()
    noise = lambda db: as_bytes(rng.normal(0, 2 ** 23 * 10 ** (db / 20), REC_BLOCK // 3).clip(-2 ** 23, 2 ** 23 - 1).astype(np.int64))
    level, left, right, clipped = measure(as_bytes([2 ** 22, -2 ** 23] * 4800))  # left at half scale, right hard against the bottom
    assert (level, left, right, clipped) == (-2.0, -6.0, 0.0, True), (level, left, right, clipped)
    assert measure(noise(-60))[0] == -60.0 and measure(bytes(REC_BLOCK))[:3] == (-144.5,) * 3

    # Hearing a tape: as recorded, or one track in both ears; and which of an interface's outputs it is sent to.
    assert (ears("both"), ears("left"), ears("right"), ears(None)) == ([0, 1], [0, 0], [1, 1], [0, 1])
    assert to_outputs(6, 2, 3) == [-1, -1, 0, 1, -1, -1] and to_outputs(2, 0, 1) == [0, 1] and to_outputs(2, 1, 1) == [-1, 1]

    # A side as it should go: Record, the deck still stopped; Play; a ten-second pause; on to the end, where the deck stops.
    tape = lambda seconds: [noise(-72 if i % 3 else -25) for i in range(seconds * 10)]  # speech over the hiss of a running tape
    stopped = lambda seconds: [noise(-117) for _ in range(seconds * 10)]
    parts = [stopped(2) + [noise(-100)] + stopped(2), tape(20), stopped(10), tape(5), stopped(40)]  # a click in the lead-in is not the tape
    came, side, got = sum(parts, []), Side(), []
    for block in came:
        got.append(side.hear(block))
        if side.state == "over":
            break
    start, end = 41, 41 + 350
    assert side.floor < -110 and -108 < side.stops_below < -106 and side.state == "over" and len(got) == end + SIDE_ENDS * 10, (side.floor, len(got))
    assert b"".join(got) == b"".join(came[start - LEAD_IN * 10:end + TAIL * 10]), "the master is not the tape, a second before to three after"
    assert side.kept == 10 + 350 + 30 and not side.clipped and not any(got[:start]), side.kept
    # The tape already running when Record was pressed: all of it is kept, and it never ends by itself.
    side = Side()
    came = tape(3) + stopped(60)
    assert b"".join(side.hear(b) for b in came) == b"".join(came) and (side.floor, side.stops_below, side.state) == (False, None, "playing")
    assert side.stop() == b"" and side.state == "over"
    # With the stop level set by hand nothing is learned, so the same side does end by itself, three seconds after the tape.
    side = Side(-90)
    assert b"".join(side.hear(b) for b in came) == b"".join(came[:30 + TAIL * 10]) and (side.floor, side.state) == (None, "over")
    # Set above the hiss of the tape it would take a quiet stretch for the end; moved down while the side runs, it no longer does.
    side, came = Side(-60), stopped(2) + [noise(-25) for _ in range(40)] + [noise(-72) for _ in range(400)] + tape(1)
    got = [side.hear(b) for b in came[:359]]
    assert len(side.held) == 299 and side.state == "playing"  # a breath from ending the side on hiss
    side.below = -90
    assert b"".join(got + [side.hear(b) for b in came[359:]]) == b"".join(came[10:]) and side.state == "playing"
    # Stopped by hand: within the lead-in there is nothing to keep; after the deck has stopped, three seconds of it.
    side = Side()
    assert not any(side.hear(b) for b in stopped(5)) and side.stop() == b"" and side.kept == 0
    side, came = Side(), stopped(2) + tape(2) + stopped(8)
    assert b"".join([side.hear(b) for b in came] + [side.stop()]) == b"".join(came[10:40 + TAIL * 10])
    # Clipping is noted where it falls in the master, once a second at most.
    side, loud = Side(), as_bytes([2 ** 23 - 1, 0] * 4800)
    for block in stopped(2) + tape(1) + [loud, loud] + tape(2) + [loud]:
        side.hear(block)
    assert side.clipped == [2.0, 4.2], side.clipped
    # The file plays as far as it has got, whenever it is looked at.
    with tempfile.TemporaryDirectory() as tmp:
        master = Master(tmp + "/a.wav")
        master.write(came[25] * 7), master.settle(), master.write(came[26])
        with wave.open(tmp + "/a.wav") as w:
            assert (w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()) == (48000, 2, 3, 7 * 4800)
            assert w.readframes(4800) == came[25]
        master.settle(), master.f.close()
        with wave.open(tmp + "/a.wav") as w:
            assert w.getnframes() == 8 * 4800
        try:
            Master(tmp + "/a.wav")
            raise AssertionError("a master was written over another")
        except FileExistsError:
            pass

    # A collected file from elsewhere: what is taken from it, and what is refused.
    assert member("tapes/Proj/Tape 5.1.2/Side A.json") == ("tapes", "Proj/Tape 5.1.2/Side A", ".json")
    assert member("originals/Side A.WAV") == ("originals", "Side A", ".WAV") and member("tapes/a.m4a")
    for name in ("tapes/../../evil.json", "/tapes/a.json", "tapes//a.json", "a.json", "tapes/a.exe", "tapes/.hidden.json",
                 "tapes\\a.json", "tapes/C:/a.json", "originals/a.json", "elsewhere/a.json", "tapes/a.sound.npy", "tapes/x/"):
        assert member(name) is None, name
    word, good = {"s": 1, "e": 1.5, "w": "hello"}, {"s": 1, "e": 2, "text": "hallo", "was": "hello"}
    ok = lambda **over: {"duration": 9.5, "source": "a.wav", "chunks": [{"start": 0, "end": 9, "lang": "en", "words": [word]}], **over}
    assert well_formed(ok(), []) and well_formed(ok(), [good]) and well_formed(ok(chunks=[]), [])
    for d, fixes in ((ok(duration="9"), []), (ok(duration=float("nan")), []), (ok(source=None), []), ({"chunks": []}, []), ([], []),
                     (ok(chunks=[{"start": 0, "end": 9, "lang": "<b>", "words": []}]), []), (ok(chunks=[{"words": [word]}]), []),
                     (ok(chunks=[{"start": 0, "end": 9, "lang": "en", "words": [{"s": 1, "e": 2, "w": 3}]}]), []),
                     (ok(), [{**good, "was": None}]), (ok(), [["s"]]), (ok(), "nonsense")):
        assert not well_formed(d, fixes), (d, fixes)

    assert safe("8. /first/Side A") == "8. - first - Side A", safe("8. /first/Side A")
    assert safe('a:b?"c"') == "a_b__c_" and safe(" . ") == "untitled"
    assert stamp(754.3) == "12m34s" and clock(754.34) == "00:12:34.3" and clock(3599.96) == "01:00:00.0"
    assert sorted(["10. b", "2. a", "1. c"], key=natural) == ["1. c", "2. a", "10. b"]
    tokens = [norm(w) for w in "It was Nora Bennett, on the telephone to Nara.".split()]
    where = {}
    for i, t in enumerate(tokens):
        where.setdefault(t, []).append(i)
    vocab = sorted(set(tokens))
    assert expand("nora", vocab) == {"nora", "nara"} and expand("tele", vocab) == {"tele", "telephone"}
    assert edits("nora", "nara", 1) == 1 and edits("benett", "bennett", 1) == 1 and edits("nora", "mara", 1) == 2
    assert phrase_starts(tokens, where, [expand("nora", vocab)]) == [2, 8]
    assert phrase_starts(tokens, where, [expand("nora", vocab), expand("benett", vocab)]) == [2]  # misspelt, still found
    assert phrase_starts(tokens, where, [expand("nara", vocab), expand("bennett", vocab)]) == [2]
    assert phrase_starts(tokens, where, [expand("telephone", vocab), expand("nora", vocab)]) == []  # not adjacent
    heard = lambda: [{"words": [{"s": i, "e": i + 1, "w": w} for i, w in enumerate("it was nona bennett on the phone".split())]}]
    said = lambda fixes: " ".join(w["w"] for c in apply_fixes(heard(), fixes) for w in c["words"])
    f1 = revise(heard(), [], 2, 3, "Nora")
    assert said(f1) == "it was Nora bennett on the phone" and f1[0]["was"] == "nona"
    f2 = revise(heard(), f1, 3, 4, "Bennett, ringing")  # a neighbouring word: its own correction, two words in one slot
    assert said(f2) == "it was Nora Bennett, ringing on the phone" and len(f2) == 2
    f3 = revise(heard(), f2, 2, 4, "Nora Bennett")  # across both: they become one
    assert said(f3) == "it was Nora Bennett on the phone" and len(f3) == 1 and f3[0]["was"] == "nona bennett"
    second = [w for c in apply_fixes(heard(), f3) for w in c["words"]][3]
    f4 = revise(heard(), f3, second["s"], second["e"], "Bennet")  # one word inside a correction: the other is kept
    assert said(f4) == "it was Nora Bennet on the phone" and len(f4) == 1
    assert revise(heard(), f4, 2, 2.5, None) == []  # restoring any part restores the whole correction
    assert said(revise(heard(), [], 5, 6, "")) == "it was nona bennett on […] phone"  # deleted words leave a marker
    assert revise(heard(), [], 2, 3, "nona") == []  # retyping what was there stores nothing
    spoken = [{"s": i, "e": i + 0.5} for i in range(100)] + [{"s": 200 + i, "e": 200.5 + i} for i in range(30)] + [{"s": 400, "e": 401}]
    # 100 words, a silence, 30 words, a silence, one stray word (too short to be a passage)
    assert passages(spoken) == [(0, 80), (40, 100), (100, 130)], passages(spoken)
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        for name in ["top.WAV", "notes.txt", ".hidden.wav", "10. b/Side A.wav", "2. a/x/deep.mp3", ".cache/skip.wav"]:
            (Path(d) / name).parent.mkdir(parents=True, exist_ok=True)
            (Path(d) / name).touch()
        assert recordings(d, 0) == ["top.WAV"], recordings(d, 0)
        assert recordings(d, 1) == ["10. b/Side A.wav", "top.WAV"], recordings(d, 1)
        assert recordings(d, 2) == ["2. a/x/deep.mp3", "10. b/Side A.wav", "top.WAV"], recordings(d, 2)
    assert id_of("1. Mr. Smith 1966/Side A.wav") == "1. Mr. Smith 1966/Side A"  # a dot in a folder name is not an extension
    import numpy as np
    t, rng = np.arange(20 * RATE) / RATE, np.random.default_rng(0)
    tune = np.sin(2 * np.pi * np.array([262, 330, 392, 523, 440])[(t / 0.4).astype(int) % 5] * t)
    hum, hiss = np.sin(2 * np.pi * 440 * t) + 0.05 * rng.standard_normal(len(t)), rng.standard_normal(len(t))
    # A tune, a steady hum, hiss, and the tune again with someone talking over it: only the first is marked.
    got = music(sound(np.concatenate([tune, hum, hiss, tune]).astype(np.float32)), [{"s": 60 + i, "e": 60.8 + i} for i in range(20)])
    assert len(got) == 1 and got[0][0] < 2 and 17 < got[0][1] < 22, got
    assert music(sound(np.zeros(100, np.float32)), []) == []  # too short to measure
    print("ok")


if __name__ == "__main__":
    if "--check" in sys.argv:
        check()
        sys.exit()
    if "--record" in sys.argv:
        recorder(json.loads(sys.argv[sys.argv.index("--record") + 1]))
        sys.exit()
    if "--monitor" in sys.argv:
        monitor(json.loads(sys.argv[sys.argv.index("--monitor") + 1]))
        sys.exit()
    def answering(port):
        with socket.socket() as s:
            return s.connect_ex(("127.0.0.1", port)) == 0

    port = PORT
    while answering(port):
        try:
            ours = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=2)).get("app") == "transclip"
        except Exception:
            ours = False
        if ours:  # already running: show it rather than start a second one
            QUIET or webbrowser.open(f"http://127.0.0.1:{port}")
            sys.exit()
        port += 1  # some other program has that port: take the next
    last = BASE / "session.txt"
    last = last.read_text(encoding="utf-8").strip() if last.exists() else ""
    open_session(last if last == safe(last) and (BASE / "sessions" / last).is_dir() else "")
    # Get the meaning index ready in the background, so the first such search does not wait for it.
    threading.Thread(target=lambda: (meaning_index(), embedder()), daemon=True).start()
    url, serve = f"http://127.0.0.1:{port}", lambda: app.run(host="127.0.0.1", port=port, threaded=True)
    try:
        # ponytail: a window on the Mac only, the one place it has been tried. pywebview also draws one on Windows
        # (with Edge) and Linux (GTK or Qt): lift the platform limit on the dependency above and try it there.
        import webview
    except ImportError:
        webview = None
    if QUIET or not webview:
        QUIET or threading.Timer(0.8, webbrowser.open, [url]).start()
        serve()
    else:
        threading.Thread(target=serve, daemon=True).start()
        for _ in range(100):  # the window shows whatever answers when it opens, so the page has to be there first
            if answering(port):
                break
            time.sleep(0.05)
        window(webview, url)
        os._exit(0)  # the window is the app: closed means quit, and the transcriber goes with us
