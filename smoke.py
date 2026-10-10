# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""End-to-end check on a throwaway library:  uv run smoke.py

Makes a small folder of recordings, starts the app on it, and goes through everything a
person would: choose the folder and depth, transcribe, search, correct, export, quit.
TRANSCLIP_ENGINE=faster TRANSCLIP_MODEL=tiny keeps it small and is what CI runs on every
system; without them it uses the real model for this machine.

tests/jfk.flac is eleven seconds of President Kennedy's 1961 inaugural address (public
domain), the sample the Whisper project itself tests with.
"""
import json
import math
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

ROOT = Path(__file__).parent
RATE = 48000


def tone(path, seconds=6):
    """A stereo WAV in which no two frames are alike, so a cut can be checked to the sample."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2), w.setsampwidth(2), w.setframerate(RATE)
        w.writeframes(b"".join(struct.pack("<hh", int(8000 * math.sin(i * 0.05)), i % 30000) for i in range(seconds * RATE)))


def frames(path):
    with wave.open(str(path), "rb") as w:
        return w.readframes(w.getnframes())


def main():
    sys.stdout.reconfigure(errors="replace")  # a progress bar's characters must not trip up a plain console
    tmp = Path(tempfile.mkdtemp(prefix="transclip smoke "))
    folder = tmp / "My recordings"
    tone(folder / "Tape 1" / "Side A.wav")
    tone(folder / "a" / "b" / "c" / "too deep.wav")
    shutil.copy(ROOT / "tests" / "jfk.flac", folder / "speech.flac")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    env = {**os.environ, "TRANSCLIP_HOME": str(tmp / "home"), "TRANSCLIP_PORT": str(port)}
    start = [os.environ.get("UV") or shutil.which("uv"), "run", "--script", str(ROOT / "app.py"), "--no-open"]
    log = open(tmp / "app.log", "w+", encoding="utf-8")
    app = subprocess.Popen(start, env=env, stdout=log, stderr=subprocess.STDOUT)

    def call(method, path, body=None, origin=None):
        headers = {"Content-Type": "application/json", **({"Origin": origin} if origin else {})}
        req = urllib.request.Request(base + path, method=method, headers=headers,
                                     data=None if body is None else json.dumps(body).encode())
        with urllib.request.urlopen(req, timeout=300) as r:
            raw = r.read()
            return json.loads(raw) if raw else None

    def wait(what, done, minutes):
        end = time.time() + minutes * 60
        while time.time() < end:
            assert app.poll() is None, "the app stopped"
            try:
                got = done()
                if got:
                    return got
            except (urllib.error.URLError, ConnectionError):
                pass
            time.sleep(1)
        raise AssertionError(f"gave up waiting for {what}")

    try:
        wait("the app to start", lambda: call("GET", "/api/ping"), 15)
        print("started", flush=True)
        try:
            call("POST", "/api/quit", origin="http://elsewhere.example")
            raise AssertionError("a request from another site was let in")
        except urllib.error.HTTPError as e:
            assert e.code == 403, e.code

        lib = call("PUT", "/api/library", {"folder": str(folder), "depth": 3})
        assert (lib["found"], lib["todo"], lib["reachable"]) == (3, 3, True), lib
        lib = call("PUT", "/api/library", {"folder": str(folder), "depth": 1})
        assert (lib["found"], lib["todo"]) == (2, 2), lib
        assert call("PUT", "/api/library", {"folder": str(tmp / "nowhere"), "depth": 1})["reachable"] is False
        call("PUT", "/api/library", {"folder": str(folder), "depth": 1})
        print("folder and depth", flush=True)

        call("POST", "/api/transcribe")
        seen = []

        def finished():
            job = call("GET", "/api/library")["job"]
            if job.get("note") and job["note"] not in seen:
                seen.append(job["note"])
                print("  ", job["note"], flush=True)
            return not job["running"] and job
        job = wait("transcription", finished, 40)
        lib = call("GET", "/api/library")
        assert not job["failed"] and (lib["todo"], lib["transcribed"]) == (0, 2), (job, lib)
        tapes = {t["id"]: t for t in call("GET", "/api/tapes")}
        assert set(tapes) == {"Tape 1/Side A", "speech"}, tapes
        assert tapes["Tape 1/Side A"]["words"] == 0 and tapes["speech"]["words"] > 10, tapes
        assert abs(tapes["speech"]["duration"] - 11) < 0.1, tapes
        print("transcribed:", tapes["speech"]["words"], "words", flush=True)

        hit = call("GET", "/api/search?q=country")
        assert hit["total"] >= 1 and hit["hits"][0]["tape"] == "speech", hit
        hit = hit["hits"][0]
        close = wait("search by meaning", lambda: call("GET", "/api/meaning?q=serving+the+nation+rather+than+yourself")["hits"], 10)
        assert close[0]["tape"] == "speech", close
        print("search", flush=True)

        before = call("GET", "/api/transcript/speech")
        fixed = call("POST", "/api/fix", {"tape": "speech", "s": hit["t"], "e": hit["end"], "text": "homeland"})
        assert fixed != before and call("GET", "/api/search?q=homeland")["hits"][0]["exact"]
        assert call("POST", "/api/fix", {"tape": "speech", "s": hit["t"], "e": hit["end"], "text": None}) == before
        assert call("GET", "/api/search?q=homeland")["total"] == 0
        print("corrections", flush=True)

        clip = lambda tape, a, b, name: {"id": name, "tape": tape, "start": a, "end": b, "name": name}
        out = call("POST", "/api/export", {"handle": 0.5, "target": 0, "bins": [{"name": "Bin / one", "clips": [
            clip("Tape 1/Side A", 2.0, 3.5, 'what: a "test"?'), {**clip("Tape 1/Side A", 4.0, 5.0, "not yet"), "review": True},
            clip("speech", hit["t"], hit["end"], "")]}]})
        assert (out["clips"], out["failed"]) == (2, 0), out
        made = sorted(p.name for p in (Path(out["dir"]) / "Bin - one").iterdir())
        # The one marked for review is left out, and the clip after it keeps its number.
        assert len(made) == 2 and made[0].endswith(".wav") and made[1].endswith(".flac") and made[1].startswith("03_"), made
        cut, whole = frames(Path(out["dir"]) / "Bin - one" / made[0]), frames(folder / "Tape 1" / "Side A.wav")
        at = int(1.5 * RATE) * 4  # the clip's start less the handle, in bytes
        # The original's own samples from exactly there; a copy may run on by a fraction of a second, never fall short.
        assert whole[at:at + len(cut)] == cut and 2.5 <= len(cut) / 4 / RATE < 2.7, len(cut) / 4 / RATE
        assert len((Path(out["dir"]) / "log.csv").read_text(encoding="utf-8-sig").splitlines()) == 3
        print("export", flush=True)

        assert subprocess.run(start, env=env, timeout=300).returncode == 0  # a second launch finds the first and bows out
        call("POST", "/api/quit")
        assert app.wait(timeout=30) == 0
        print("ok")
    except BaseException:
        app.kill()
        log.seek(0)
        print("---- the app said:", log.read()[-3000:], sep="\n", file=sys.stderr)
        raise
    finally:
        log.close()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
