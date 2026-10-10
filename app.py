# /// script
# requires-python = "==3.12.*"
# dependencies = [
#   "flask>=3", "fastembed>=0.9", "imageio-ffmpeg>=0.6", "numpy",
#   "pywebview>=6; sys_platform == 'darwin'",
# ]
# ///
"""Transclip:  uv run app.py

Finds the recordings in a folder you choose, has transcribe.py transcribe them, and
shows the result in a window of its own on a Mac and in your browser elsewhere: read,
search, select clips, export them.

Everything it keeps is in one folder, Transclip in your home folder (TRANSCLIP_HOME
moves it): tapes/ holds each recording's transcript, listening copy and waveform
outline, selects.json the clips, exports/ what you export. Exports are cut from the
original recordings, which are only ever read. Corrections to a transcript are kept
beside it in <tape>.fixes and laid over the machine's words when it is read; the
machine's own transcript is never rewritten. Search by meaning uses a small language
model kept in models/. Nothing is sent anywhere, and only this computer can reach the app.
"""
import csv
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import imageio_ffmpeg
from flask import Flask, abort, jsonify, request, send_from_directory
from werkzeug.security import safe_join

ROOT = Path(__file__).parent
HOME = Path(os.environ.get("TRANSCLIP_HOME") or Path.home() / "Transclip")
TAPES, SELECTS, SETTINGS, EXPORTS = HOME / "tapes", HOME / "selects.json", HOME / "settings.json", HOME / "exports"
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
# Music is looked for in the sound itself, about a quarter of a second at a time: where there are no words,
# and the sound is made of tones rather than hiss (FLAT) and keeps changing rather than humming on (CHANGE).
# ponytail: two measurements with limits found by trial on one archive of cassettes, checked against spectrograms.
# They cannot tell music from other tonal, changing sound without words (faint voices, a busy room with a hum
# in it), and music under words is never marked. A trained sound classifier is the step up if that is not enough.
RATE, FRAME, HOP, PER_BLOCK = 16000, 1024, 512, 8
BLOCKS = RATE / HOP / PER_BLOCK  # measurements a second
FLAT, CHANGE = 0.1, 0.15
SMOOTH, BRIDGE, SHORTEST = 5, 3, 10  # seconds: judged over, a lapse a piece may contain, and the least that counts

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
        _embedder = TextEmbedding("BAAI/bge-small-en-v1.5", cache_dir=str(HOME / "models"))
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
        elif state["running"]:
            state["note"] = "The transcriber stopped unexpectedly: " + state["note"]
        state["running"] = False

    threading.Thread(target=watch, daemon=True).start()


def stop_job():
    proc, state = _job["proc"], _job["state"]
    if proc and state["running"]:
        state.update(running=False, note="Stopped. Recordings already finished are kept.")
        proc.stdin.close()
        proc.terminate()  # in case it had not got as far as listening


def status():
    folder, found, todo = pending()
    return {**settings(), "reachable": folder is not None, "found": len(found), "todo": len(todo),
            "transcribed": len(library()["tapes"]), "job": _job["state"], "home": str(HOME)}


def window(webview, url):
    """Show the page in a window of the app's own. Returns when the window has been closed."""
    from Foundation import NSBundle
    NSBundle.mainBundle().infoDictionary()["CFBundleName"] = "Transclip"  # or the menus say "Quit" where they should say "Quit Transclip"
    # Words are selected by dragging across them, which a window does not allow unless told to.
    w = webview.create_window("Transclip", url, width=1440, height=900, min_size=(900, 560), text_select=True)
    # Closing the window quits, and quitting stops the transcriber: ask first while it is at work.
    w.events.closing += lambda: setattr(w, "confirm_close", _job["state"]["running"])
    webview.start(private_mode=False,  # a private window would forget which recording was open
                  localization={"global.quitConfirmation": "A transcription is still running. Quitting stops it; "
                                "recordings already finished are kept."})


def choose_folder():
    """Ask for a folder with the system's own dialog. Empty if none was chosen."""
    if sys.platform == "darwin":
        cmd = ["osascript", "-e", "activate", "-e", 'POSIX path of (choose folder with prompt "The folder containing your recordings")']
    else:
        # ponytail: not tried on Windows or Linux yet; the folder can always be typed or pasted instead.
        cmd = [sys.executable, "-c", "import tkinter as t, tkinter.filedialog as f; r = t.Tk(); r.withdraw(); "
               "r.attributes('-topmost', True); print(f.askdirectory() or '')"]
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
    try:
        depth = max(0, min(int(body.get("depth", 3)), 20))
    except (TypeError, ValueError):
        abort(400, "depth must be a whole number")
    dump(SETTINGS, {"folder": str(body.get("folder") or "").strip(), "depth": depth})
    return jsonify(status())


@app.post("/api/choose-folder")
def choose():
    return jsonify({"folder": choose_folder()})


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
    missing = sorted({str(src) for *_, src in jobs if not src.exists()})
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
    TAPES.mkdir(parents=True, exist_ok=True)
    backup = SELECTS.with_name(f"selects.{time.strftime('%Y-%m-%d')}.bak.json")
    if SELECTS.exists() and not backup.exists():
        shutil.copy2(SELECTS, backup)  # one copy per day, as things stood at first launch
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
