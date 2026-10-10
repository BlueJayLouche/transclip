# Transclip

Transcribe a folder of audio recordings, read and search them, and pull clips out of the originals.

Built for making selects from an archive of digitised cassette tapes: hours of speech, uneven quality, not much time.

- **Transcribes on your own computer.** Whisper large-v3, with a time for every word. Stretches in another language are marked and roughly translated into English; stretches with no speech are marked too.
- **Transcript and waveform, linked.** Click a word to play from it. Drag across words to make a clip, drag the clip's edges on the waveform to trim it.
- **Search every recording** by the words spoken (small misspellings and word beginnings count) and by meaning ("working abroad without a permit").
- **Correct the transcript** where the machine misheard. The original wording is kept and corrected words are marked.
- **Mark a passage to review** when you are not yet sure of it: it gets its own colour and is kept out of the export until you clear the mark.
- **Export** clips into named bins: each clip is cut from the original recording, in the original's format, with handles, plus a log sheet (`log.csv`) of tape, in and out points and words.

Nothing is uploaded. The recordings are only ever read, and the app answers only to the computer it runs on.

## Status

| System | State |
|---|---|
| macOS, Apple Silicon | In use. Double-click app with a window of its own. |
| Windows, Linux, Intel Mac | Runs from the command line and shows itself in your browser. CI runs the whole app end to end on Windows and Linux with a small model, but nobody has yet used it on either with real recordings, and the folder chooser there is untried. Without an NVIDIA graphics card, transcription will be much slower than on Apple Silicon. |

## Install

### macOS app

Download `transclip-macos-arm64.dmg` from [Releases](../../releases), open it and drag Transclip to Applications.

The app is not signed by a registered Apple developer, so macOS will refuse to open a downloaded copy until you allow it, once:

```
xattr -dr com.apple.quarantine /Applications/Transclip.app
```

The first launch downloads Python and the app's libraries (a minute or two) before its window opens. The first transcription downloads the speech model (a few GB). After that it works offline.

If your recordings are on an external drive, macOS asks once whether to allow reading it, and the Recordings panel waits until you answer. The question names "python3.12" rather than Transclip: the app runs on the Python it downloaded, and that is the program macOS sees reading the drive.

### From source, any system

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then:

```
git clone https://github.com/BlueJayLouche/transclip.git
cd transclip
uv run app.py
```

uv fetches Python and everything else the first time, ffmpeg included. On a Mac it opens in a window; elsewhere in your browser.

## Use

1. **Recordings…** (top left): choose the folder your recordings are in and how many levels of folders inside it to look through, then **Transcribe**. Recordings appear in the list as each one finishes, under a heading for the folder they are in; you can start working on the first while the rest are done.
2. Click a word to play from it. Space plays and pauses; ← and → skip two seconds.
3. Drag across words and press Enter to make a clip, or mark by ear with I and O. Press R instead of Enter to mark the passage for review; ✓ on a marked clip clears the mark, ⚑ sets it.
4. Double-click a word to correct it, or select several and press E.
5. `/` jumps to search.
6. **Export** writes every clip that is not marked for review, a folder per bin, and opens the folder.

Closing the window quits Transclip; while a transcription is running it asks first, because quitting stops it. Where it runs in the browser instead, starting it again just shows it, and **Recordings… → Quit Transclip** stops it.

## Where things are kept

Everything lives in one folder, `Transclip` in your home folder (set `TRANSCLIP_HOME` to put it elsewhere, or to keep a second, separate library):

| | |
|---|---|
| `tapes/` | per recording: the transcript, a small listening copy and the waveform outline, so everything but export works with the recordings' drive unplugged |
| `selects.json` | your bins and clips, saved on every change, with one backup per day |
| `exports/` | a new dated folder per export |
| `settings.json` | the recordings folder and depth |
| `models/` | the small model that search by meaning uses |

Deleting a recording's files from `tapes/` makes the app transcribe it again.

## Development

```
uv run app.py --check          # logic checks
uv run transcribe.py --check
uv run smoke.py                # the whole app on a throwaway folder, with this machine's real model
./package-macos.sh 0.1.0       # builds dist/Transclip.app
uv run packaging/icon.py icon.png   # draws the icon on its own, to look at
```

`TRANSCLIP_ENGINE=faster TRANSCLIP_MODEL=tiny uv run smoke.py` is what CI runs on macOS, Linux and Windows.

Pushing a `v*` tag builds the Mac app in CI and attaches it to a GitHub release. Running the Release workflow by hand builds the same thing as a downloadable artifact without publishing.

After changing a script's dependencies: `uv lock --script app.py` (or `transcribe.py`).

## Built with

[Whisper](https://github.com/openai/whisper) via [MLX](https://github.com/ml-explore/mlx-examples/tree/main/whisper) and [faster-whisper](https://github.com/SYSTRAN/faster-whisper), [Silero VAD](https://github.com/snakers4/silero-vad), [fastembed](https://github.com/qdrant/fastembed), [wavesurfer.js](https://wavesurfer.xyz), [FFmpeg](https://ffmpeg.org) through [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg), [Flask](https://flask.palletsprojects.com), [pywebview](https://pywebview.flowrl.com) for the Mac window, [Pillow](https://python-pillow.org) to draw the icon, and [uv](https://docs.astral.sh/uv/), a copy of which is inside the Mac app.
