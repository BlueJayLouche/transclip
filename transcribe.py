# /// script
# requires-python = "==3.12.*"
# dependencies = [
#   "faster-whisper>=1.2",
#   "imageio-ffmpeg>=0.6",
#   "numpy",
#   "mlx-whisper==0.4.3; sys_platform == 'darwin' and platform_machine == 'arm64'",
#   "silero-vad==6.2.3; sys_platform == 'darwin' and platform_machine == 'arm64'",
# ]
# ///
"""The transcriber, started by app.py:  uv run transcribe.py <jobs.json>

jobs.json is a list of [recording, data path without extension]. For each one this writes
  <data path>.m4a    small listening copy, so the app works with the recordings unplugged
  <data path>.peaks  waveform outline, one byte per 10 ms
  <data path>.json   transcript with a time for every word (written last: its
                     presence marks the recording as done)
and reports progress as one JSON object per line on standard output.

Speech is found with a voice detector and packed into chunks of up to 30 s.
Each chunk has its language detected, then is transcribed (English) or
translated into English (anything else). Whatever lies between chunks is
"no speech". The recordings are only ever read.

Whisper large-v3 runs through MLX on Apple Silicon and through faster-whisper
everywhere else. TRANSCLIP_ENGINE=faster forces the second; TRANSCLIP_MODEL names another model.
"""
import contextlib
import io
import json
import os
import platform
import subprocess
import sys
import threading
import time
import warnings
from pathlib import Path

import imageio_ffmpeg
import numpy as np

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()  # comes with the package, so nothing has to be installed by hand
SR = 16000
PEAK = SR // 100  # samples per waveform point
MAX_CHUNK = 30 * SR  # Whisper's window
MAX_GAP = 2 * SR  # a longer stretch without speech ends the chunk
MIN_DETECT = 3 * SR  # less speech than this is too little to guess a language from
MIN_LANG_PROB = 0.5
MLX = (sys.platform == "darwin" and platform.machine() == "arm64"
       and os.environ.get("TRANSCLIP_ENGINE") != "faster")


def told():
    """The bar a download would draw, as words for the app: how much of the speech model has arrived."""
    from tqdm import tqdm  # comes with the libraries that do the downloading

    class Told(tqdm):
        got = size = 0  # two counts are kept, bytes received and bytes written out: the app hears the larger

        def __init__(self, *args, **kw):
            super().__init__(*args, **{**kw, "mininterval": 1})

        def display(self, *args, **kw):
            if self.unit == "B" and self.total >= 1e6:  # not the count of files, nor the few small ones on their own
                Told.got, Told.size = max(Told.got, self.n), max(Told.size, self.total)
                say(note=f"Downloading the speech model: {Told.got / 1e6:,.0f} of {Told.size / 1e6:,.0f} MB")

        def close(self):  # a bar says its piece once more as it is thrown away, which can be long after the download
            self.disable = True

    return Told


class Mlx:
    """Apple Silicon: Whisper on the graphics chip."""
    name = os.environ.get("TRANSCLIP_MODEL") or "mlx-community/whisper-large-v3-mlx"

    def __init__(self):
        import mlx.core as mx
        from huggingface_hub import snapshot_download
        from mlx_whisper.transcribe import ModelHolder
        from silero_vad import load_silero_vad
        # Fetched here and handed over as a folder: left to fetch it itself, mlx_whisper does so without a word.
        self.path = self.name if Path(self.name).exists() else snapshot_download(self.name, tqdm_class=told())
        say(note="Loading the speech model")
        self.model, self.vad = ModelHolder.get_model(self.path, mx.float16), load_silero_vad()

    def spans(self, audio):
        import torch
        from silero_vad import get_speech_timestamps
        return get_speech_timestamps(torch.from_numpy(audio), self.vad, sampling_rate=SR,
                                     max_speech_duration_s=MAX_CHUNK / SR, speech_pad_ms=200)

    def language(self, clip):
        import mlx.core as mx
        from mlx_whisper.audio import N_FRAMES, N_SAMPLES, log_mel_spectrogram, pad_or_trim
        mel = log_mel_spectrogram(clip, n_mels=self.model.dims.n_mels, padding=N_SAMPLES)
        _, probs = self.model.detect_language(pad_or_trim(mel, N_FRAMES, axis=-2).astype(mx.float16))
        lang = max(probs, key=probs.get)
        return lang, probs[lang]

    def words(self, clip, lang):
        import mlx_whisper
        # No initial_prompt with recurring names: tried, it barely helped the
        # spelling and it leaked into unclear passages as invented words.
        result = mlx_whisper.transcribe(
            clip, path_or_hf_repo=self.path, language=lang,
            task="transcribe" if lang == "en" else "translate",
            word_timestamps=True, condition_on_previous_text=False,
        )
        return [(w["start"], w["end"], w["word"]) for seg in result["segments"] for w in seg["words"]]


class Faster:
    """Everything else: an NVIDIA card if there is one, otherwise the processor."""
    name = os.environ.get("TRANSCLIP_MODEL") or "large-v3"

    def __init__(self):
        from faster_whisper import WhisperModel
        # ponytail: no count of the download here as there is on the Mac. faster-whisper fetches its own
        # model and takes no bar; fetch it first, as Mlx does, when Windows or Linux gets an app of its own.
        self.model = WhisperModel(self.name, device="auto", compute_type="auto")

    def spans(self, audio):
        from faster_whisper.vad import VadOptions, get_speech_timestamps
        # The same detector and settings as the MLX side uses.
        return get_speech_timestamps(audio, VadOptions(max_speech_duration_s=MAX_CHUNK / SR,
                                                       min_silence_duration_ms=100, speech_pad_ms=200))

    def language(self, clip):
        lang, prob, _ = self.model.detect_language(clip)
        return lang, prob

    def words(self, clip, lang):
        segments, _ = self.model.transcribe(
            clip, language=lang, task="transcribe" if lang == "en" else "translate",
            word_timestamps=True, condition_on_previous_text=False,
        )
        return [(w.start, w.end, w.word) for seg in segments for w in seg.words]


def load_audio(path):
    # ponytail: plain mono downmix. A phase-inverted channel pair would cancel
    # out; pick one channel with -af pan if a tape ever comes back silent.
    cmd = [FFMPEG, "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "1", "-ar", str(SR), "-"]
    audio = np.frombuffer(subprocess.run(cmd, capture_output=True, check=True).stdout, np.float32).copy()
    # Bring quiet transfers up to a working level, or the voice detector hears
    # nothing. The 99.9th percentile ignores clicks that a plain peak would not;
    # the 20 dB ceiling keeps a blank side looking blank instead of like a wall of hiss.
    gain = min(0.5 / max(np.percentile(np.abs(audio), 99.9), 1e-6), 10) if len(audio) else 1
    return np.clip(audio * gain, -1, 1)


def pack(spans):
    """Merge speech spans (in samples) into chunks no longer than MAX_CHUNK,
    starting a new chunk at any gap longer than MAX_GAP."""
    chunks = []
    for s in spans:
        c = chunks[-1] if chunks else None
        if c and s["start"] - c["end"] <= MAX_GAP and s["end"] - c["start"] <= MAX_CHUNK:
            c["end"] = s["end"]
            c["speech"] += s["end"] - s["start"]
        else:
            chunks.append({"start": s["start"], "end": s["end"], "speech": s["end"] - s["start"]})
    return chunks


def transcribe(audio, engine, progress=lambda fraction: None):
    chunks, lang = [], "en"
    for c in pack(engine.spans(audio)):
        clip = audio[c["start"]:c["end"]]
        # Short or uncertain chunks keep the language of the chunk before them.
        prob = None
        if c["speech"] >= MIN_DETECT:
            guess, prob = engine.language(clip)
            if prob >= MIN_LANG_PROB:
                lang = guess
        start = c["start"] / SR
        words = [{"s": round(start + float(s), 2), "e": round(start + float(e), 2), "w": w.strip()}
                 for s, e, w in engine.words(clip, lang)]
        if words:
            chunks.append({
                "start": round(start, 2), "end": round(c["end"] / SR, 2),
                "lang": lang, "lang_prob": prob and round(float(prob), 2), "words": words,
            })
        progress(c["end"] / len(audio))
    return chunks


def write(path, data):
    """Write via a temp file so an interrupted run never leaves a half-written one."""
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def process(src, dst, engine, progress=lambda fraction: None):
    """dst is the data path for this recording without an extension."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    m4a = dst.with_name(dst.name + ".m4a")
    if not m4a.exists():
        part = dst.with_name(dst.name + ".part.m4a")
        subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(src), "-vn", "-c:a", "aac", "-b:a", "96k",
                        "-movflags", "+faststart", str(part)], check=True)
        os.replace(part, m4a)

    audio = load_audio(src)
    n = len(audio) // PEAK
    peaks = (np.abs(audio[:n * PEAK]).reshape(n, PEAK).max(1) * 255).astype(np.uint8)
    write(dst.with_name(dst.name + ".peaks"), peaks.tobytes())

    chunks = transcribe(audio, engine, progress)
    out = {"source": str(src), "duration": round(len(audio) / SR, 2), "model": engine.name, "chunks": chunks}
    write(dst.with_name(dst.name + ".json"), json.dumps(out, ensure_ascii=False).encode())
    return out


def say(**message):
    print(json.dumps(message), flush=True)


def main(jobs):
    # The app holds our standard input open for as long as it wants us running: when it
    # stops the job or goes away the pipe closes, on every system, and so do we.
    if "--stop-with-stdin" in sys.argv:
        threading.Thread(target=lambda: (sys.stdin.read(), os._exit(0)), daemon=True).start()
    # The app shows the last line printed here as its status, and the last line of a warning is
    # "warnings.warn(". None is worth printing; one is worth stopping for, before a disk is filled for nothing.
    warnings.simplefilter("ignore")
    warnings.filterwarnings("error", message="Not enough free disk space")
    say(note="Loading the speech model (the first time, it is downloaded: a few GB)")
    try:
        engine = (Mlx if MLX else Faster)()
    except Exception as e:
        if isinstance(e, Warning):  # the download library's own words, which give both sizes
            say(note=str(e))
        elif "No space left on device" in str(e):
            say(note="The disk filled up while the speech model was downloading. Make some room and transcribe again.")
        else:
            raise
        sys.exit(1)
    for i, (src, dst) in enumerate(jobs, 1):
        t0 = time.time()
        say(i=i, n=len(jobs), file=src, fraction=0)
        try:
            out = process(Path(src), Path(dst), engine, lambda f: say(i=i, n=len(jobs), file=src, fraction=round(f, 3)))
        except Exception as e:  # one bad file must not stop an unattended run
            detail = getattr(e, "stderr", None)
            say(i=i, n=len(jobs), file=src, error=(detail.decode(errors="replace").strip() if detail else str(e))[-300:])
            continue
        say(i=i, n=len(jobs), file=src, done=True, minutes=round(out["duration"] / 60, 1),
            words=sum(len(c["words"]) for c in out["chunks"]), seconds=round(time.time() - t0))


def check():
    spans = [{"start": a * SR, "end": b * SR} for a, b in [(0, 10), (11, 25), (26, 40), (50, 51)]]
    got = [(c["start"] // SR, c["end"] // SR, c["speech"] // SR) for c in pack(spans)]
    # 26-40 would push the first chunk past 30 s; 50 is past the 2 s gap.
    assert got == [(0, 25, 24), (26, 40, 14), (50, 51, 1)], got

    class Stub:  # stands in for Whisper: one word per chunk, French for the second
        name = "stub"
        spans = lambda self, audio: [{"start": 0, "end": 4 * SR}, {"start": 10 * SR, "end": 14 * SR}, {"start": 20 * SR, "end": 21 * SR}]
        language = lambda self, clip: ("fr", 0.9) if clip[0] else ("en", 0.9)
        words = lambda self, clip, lang: [(0.5, 1.0, f" {lang} ")]
    audio = np.zeros(30 * SR, np.float32)
    audio[10 * SR:14 * SR] = 1
    seen = []
    got = transcribe(audio, Stub(), seen.append)
    # Times are moved to the recording's clock; the third chunk is too short to judge, so it stays French.
    assert [(c["start"], c["lang"], c["words"][0]) for c in got] == [
        (0.0, "en", {"s": 0.5, "e": 1.0, "w": "en"}), (10.0, "fr", {"s": 10.5, "e": 11.0, "w": "fr"}),
        (20.0, "fr", {"s": 20.5, "e": 21.0, "w": "fr"})], got
    assert seen[-1] == 0.7 and seen == sorted(seen), seen

    # A download as the library reports it: received runs ahead of written out, and files are counted too.
    Told, heard = told(), io.StringIO()
    with contextlib.redirect_stdout(heard):
        received, written, files = Told(total=0, unit="B"), Told(total=0, unit="B"), Told(total=4)
        received.total = written.total = 3_000_000_000
        received.update(1_500_000_000), written.update(200_000_000), files.update(1)
        for bar in (received, files, written):
            bar.refresh(), bar.close()  # closed here, or each says its piece again as the program ends
    assert json.loads(heard.getvalue().splitlines()[-1]) == {"note": "Downloading the speech model: 1,500 of 3,000 MB"}
    assert "files" not in heard.getvalue() and "it/s" not in heard.getvalue(), heard.getvalue()
    print("ok")


if __name__ == "__main__":
    if sys.argv[1:] == ["--check"]:
        check()
    elif len(sys.argv) >= 2:
        main(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")))
    else:
        sys.exit(__doc__)
