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
import zipfile
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
    # A tape to record from, standing in for a sound input: the deck stopped, six seconds of sound, the deck stopped for good.
    tone(tmp / "sound.wav")
    with wave.open(str(tmp / "deck.wav"), "wb") as w:
        w.setnchannels(2), w.setsampwidth(2), w.setframerate(RATE)
        w.writeframes(bytes(2 * RATE * 4) + frames(tmp / "sound.wav") + bytes(35 * RATE * 4))
    env = {**os.environ, "TRANSCLIP_HOME": str(tmp / "home"), "TRANSCLIP_PORT": str(port), "TRANSCLIP_TEST_INPUT": str(tmp / "deck.wav")}
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
        assert before["music"] == [] and call("GET", "/api/transcript/Tape%201/Side%20A")["music"] == [], before["music"]  # a speech and a steady tone
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

        # Sharing. Collected without the originals, a recording is what the app keeps of it, and says
        # nothing of where things are on this computer.
        plain = call("POST", "/api/share", {"tapes": ["speech"], "raw": False})
        with zipfile.ZipFile(plain["file"]) as z:
            assert set(z.namelist()) == {"tapes/speech" + e for e in (".json", ".fixes", ".m4a", ".peaks")}, z.namelist()
            assert json.loads(z.read("tapes/speech.json"))["source"] == "speech.flac"
        assert Path(plain["file"]).name.startswith("speech (Transclip "), plain["file"]
        # Collected with them, forgotten here, and taken back in as on a computer that has no recordings folder:
        # the one still here is left alone, the other returns as it was, and clips are cut from the original that came along.
        whole = call("POST", "/api/share", {"tapes": ["speech", "Tape 1/Side A", "no such recording"], "raw": True})
        assert whole["recordings"] == 2 and whole["bytes"] > (folder / "speech.flac").stat().st_size, whole
        for f in (tmp / "home" / "tapes").glob("speech.*"):
            f.unlink()
        assert set(t["id"] for t in call("GET", "/api/tapes")) == {"Tape 1/Side A"}
        call("PUT", "/api/library", {"folder": str(tmp / "nowhere"), "depth": 1})
        assert call("POST", "/api/import", {"file": whole["file"]}) == {"added": 1, "kept": 1, "bad": 0}
        back = call("GET", "/api/transcript/speech")
        assert {**back, "source": ""} == {**before, "source": ""}, "the transcript changed on the way"
        assert Path(back["source"]) == tmp / "home" / "originals" / "speech.flac", back["source"]
        out = call("POST", "/api/export", {"handle": 0, "target": 0, "bins": [{"name": "Shared", "clips": [clip("speech", hit["t"], hit["end"], "")]}]})
        assert (out["clips"], out["failed"]) == (1, 0), out
        # A file from someone else is not trusted: nothing lands outside the library, and a transcript
        # of the wrong shape is refused rather than left to trip up every other recording.
        junk = tmp / "junk.zip"
        with zipfile.ZipFile(junk, "w") as z:
            z.writestr("tapes/../../escaped.json", "{}"), z.writestr("tapes/broken.json", '{"chunks": 5}'), z.writestr("tapes/note.txt", "hello")
        assert call("POST", "/api/import", {"file": str(junk)}) == {"added": 0, "kept": 0, "bad": 1}
        assert not list(tmp.rglob("escaped.json")) and not list(tmp.rglob("broken.json"))
        assert set(t["id"] for t in call("GET", "/api/tapes")) == {"Tape 1/Side A", "speech"}
        try:
            call("POST", "/api/import", {"file": str(tmp / "app.log")})
            raise AssertionError("a file that is no collection was taken in")
        except urllib.error.HTTPError as e:
            assert e.code == 409, e.code
        print("sharing", flush=True)

        # Sessions: another project is another library, with its own recordings, clips, folder and search.
        lib = call("PUT", "/api/session", {"name": "Second / project"})
        assert (lib["session"], lib["sessions"], lib["transcribed"], lib["folder"]) == ("Second - project", ["Second - project"], 0, ""), lib
        assert call("GET", "/api/tapes") == [] and call("GET", "/api/search?q=country")["total"] == 0
        assert call("GET", "/api/selects")["bins"][0]["clips"] == []
        assert call("POST", "/api/import", {"file": plain["file"]})["added"] == 1
        assert [t["id"] for t in call("GET", "/api/tapes")] == ["speech"] and call("GET", "/api/search?q=country")["total"] >= 1

        # It came without its original and there is no recordings folder here: export names the file it
        # wants, and takes being told where it is. A file of another length is not taken without insisting.
        def refused(path, body):
            try:
                call("POST", path, body)
            except urllib.error.HTTPError as e:
                assert e.code == 409, e.code
                return json.loads(e.read())
            raise AssertionError(f"{path} was not refused")
        cut = {"handle": 0, "target": 0, "bins": [{"name": "Located", "clips": [clip("speech", hit["t"], hit["end"], "")]}]}
        assert refused("/api/export", cut)["missing"] == [{"tape": "speech", "name": "speech.flac"}]
        assert "error" in refused("/api/locate", {"tape": "speech", "file": str(tmp / "app.log")})
        other = refused("/api/locate", {"tape": "speech", "file": str(folder / "Tape 1" / "Side A.wav")})
        assert other["mismatch"] and round(other["runs"]) == 6 and round(other["was"]) == 11, other
        assert refused("/api/export", cut)["missing"], "a refused file was kept all the same"
        moved = tmp / "renamed copy.flac"
        shutil.copy(folder / "speech.flac", moved)
        assert call("POST", "/api/locate", {"tape": "speech", "file": str(moved)}) == {"source": str(moved)}
        out = call("POST", "/api/export", cut)
        assert (out["clips"], out["failed"]) == (1, 0) and [p.suffix for p in (Path(out["dir"]) / "Located").iterdir()] == [".flac"], out
        assert (tmp / "home" / "sessions" / "Second - project" / "tapes" / "speech.json").exists()
        lib = call("PUT", "/api/session", {"name": ""})
        assert (lib["session"], lib["sessions"], lib["transcribed"]) == ("", ["Second - project"], 2), lib
        assert call("GET", "/api/selects")["bins"][0]["name"] == "Shared"  # the first library's clips, as they were left
        assert call("PUT", "/api/session", {"name": "Second - project"})["transcribed"] == 1
        print("sessions", flush=True)

        # Recording a side: nowhere to put it at first, then named, started, and left to end by itself.
        assert call("GET", "/api/inputs") == [{"name": "Test input", "channels": 2, "rate": RATE, "usual": True}]
        # No sound is played in a test: with no output to play through, monitoring is refused.
        assert call("GET", "/api/outputs") == [] and call("GET", "/api/monitor") == {"on": False}
        try:
            call("PUT", "/api/monitor", {"input": "Test input", "output": "Speakers"})
            raise AssertionError("monitoring started with nowhere to play")
        except urllib.error.HTTPError as e:
            assert e.code == 409, e.code
        assert call("PUT", "/api/record", {"device": "Test input", "left": 0, "right": 1})["on"]
        assert "recordings folder" in refused("/api/record", {"tape": "Reel", "side": "Side A"})["error"]
        (tmp / "Transfers").mkdir()
        call("PUT", "/api/library", {"folder": str(tmp / "Transfers"), "depth": 3})
        assert "name" in refused("/api/record", {"tape": " ", "side": "Side A"})["error"]
        assert call("POST", "/api/record", {"tape": "Reel: one", "side": "Side A"})["state"] == "waiting"
        try:
            done = wait("the side to end by itself", lambda: call("GET", "/api/record")["last"], 1)
        except AssertionError:  # what the recorder last said is the clue, and it is not in the app's log
            raise AssertionError(f"the side did not end; the recorder's state: {call('GET', '/api/record')}")
        master = tmp / "Transfers" / "Reel_ one" / "Side A.wav"
        # A second of the stopped deck before the sound, all six seconds of it, and three after.
        assert (done["why"], done["seconds"], done["auto"], done["clipped"], done["lost"], Path(done["file"])) == ("the side ended", 10.0, True, [], [], master), done
        with wave.open(str(master)) as w:
            assert (w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()) == (RATE, 2, 3, 10 * RATE)
            kept = w.readframes(10 * RATE)
        played = frames(tmp / "sound.wav")  # 16 bits a sample: in the master each has a silent byte beneath it
        assert kept[RATE * 6:RATE * 6 + 9000] == b"".join(b"\0" + played[i:i + 2] for i in range(0, 6000, 2)), "the master is not what was played"
        assert not any(kept[:RATE * 6]) and not any(kept[-3 * RATE * 6:]) and [p.name for p in master.parent.iterdir()] == ["Side A.wav"]
        # It is transcribed without being asked, and its name cannot be used twice.
        wait("the side to be transcribed", lambda: "Reel_ one/Side A" in {t["id"] for t in call("GET", "/api/tapes")}, 10)
        assert call("PUT", "/api/record", {"device": "Test input", "left": 0, "right": 1})["on"]
        again = refused("/api/record", {"tape": "Reel: one", "side": "Side A"})
        assert "already" in again["error"] and again["exists"] is True, again
        # Unless it is said that the side is to be recorded again: the old master is then set aside, not destroyed,
        # and this time the stop level is given, not learned.
        assert call("POST", "/api/record", {"tape": "Reel: one", "side": "Side A", "replace": True, "below": -90})["state"] == "waiting"
        done = wait("the side to be recorded again", lambda: call("GET", "/api/record")["last"], 1)
        assert (done["why"], done["seconds"], Path(done["file"])) == ("the side ended", 10.0, master), done
        assert sorted(p.name.split(".replaced")[0] for p in master.parent.iterdir()) == ["Side A.wav", "Side A.wav"], list(master.parent.iterdir())
        wait("the new side to be transcribed", lambda: "Reel_ one/Side A" in {t["id"] for t in call("GET", "/api/tapes")}, 10)
        call("DELETE", "/api/record?all")
        print("recording", flush=True)

        assert (tmp / "home" / "session.txt").read_text(encoding="utf-8") == "Second - project"
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
