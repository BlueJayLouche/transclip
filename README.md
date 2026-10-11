# Transclip

Transcribe a folder of audio recordings, read and search them, and pull clips out of the originals.

Built for making selects from an archive of digitised cassette tapes: hours of speech, uneven quality, not much time.

- **Transcribes on your own computer.** Whisper large-v3, with a time for every word. Stretches in another language are marked and roughly translated into English; stretches with no speech are marked too.
- **Music is marked** where there are no words: in the transcript ("♪ Music · 3 min 20 s") and as a green band on the waveform. It goes by the sound alone, so take it as a guide: a busy room with a hum in it can be taken for music, and music under speech is not marked.
- **Transcript and waveform, linked.** Click a word to play from it. Drag across words to make a clip, drag the clip's edges on the waveform to trim it.
- **Search every recording** by the words spoken (small misspellings and word beginnings count) and by meaning ("working abroad without a permit").
- **Correct the transcript** where the machine misheard. The original wording is kept and corrected words are marked.
- **Mark a passage to review** when you are not yet sure of it: it gets its own colour and is kept out of the export until you clear the mark.
- **Export** clips into named bins: each clip is cut from the original recording, in the original's format, with handles, plus a log sheet (`log.csv`) of tape, in and out points and words.
- **A session per project**, each with its own recordings, clips and search, so one job's tapes are not in the way of another's.
- **Hand recordings to someone else** already transcribed: collect them into one file, with or without the originals, and they import it. Nothing has to be transcribed twice.

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

The first launch downloads Python and the app's libraries (a minute or two) before its window opens. The first transcription downloads the transcriber's libraries and the speech model, about 5 GB in all, and the status in **Recordings…** counts the model as it arrives. After that it works offline. If it does not get that far, see [If something goes wrong](#if-something-goes-wrong).

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

## Sessions

A session is one project: its own recordings, its own recordings folder, its own clips and exports. Search looks through every recording of the open session and none of any other.

- **Start one:** in **Recordings…**, type a name beside **Session** and press **New session**. It starts empty; choose its recordings folder and transcribe, or import recordings into it.
- **Switch:** pick another from the **Session** menu. The open session is named on the Recordings… button, and Transclip reopens the one you left.
- **Main library** is what you had before there were sessions. Nothing in it moves.
- **Saving** happens as you work; there is nothing to save or close.
- **Moving recordings into a session:** collect them in the session they are in, switch, and import the file (below). The first session keeps its copy.
- **Renaming or deleting** a session is renaming or deleting its folder in `sessions/` (see [Where things are kept](#where-things-are-kept)), with Transclip closed.

A session cannot be changed while it is transcribing: stop the transcription first.

## Sharing recordings

Recordings you have transcribed can be handed to someone else, who then has nothing to transcribe.

**Collect and export** (in **Recordings…**) writes one `.zip` into the exports folder and opens the folder. The menu beside it says what goes in: the open recording, the folder it is in, or everything in the session. The file holds each recording's transcript, your corrections, the listening copy and the waveform. That is enough to read, play, search and choose clips. It does not hold your clips, or the place the recordings are kept on your computer.

Tick **with the originals** to put the recordings themselves in as well. The file is then as big as they are, and clips can be cut from them wherever it goes.

**Import…** takes such a file into the open session. Recordings already there are left as they are. Originals that came in the file are kept in `originals/`.

### Where clips are cut from

A clip is always cut from an original recording, never from the listening copy. For each recording Transclip looks, in this order:

1. In the session's recordings folder, for a file with the recording's own name in the same folders below it. For someone who imported your recordings, that means pointing **Recordings… → Folder containing the recordings** at their copies, named and arranged as yours were. The panel then counts them as transcribed, with none to do.
2. Where it was last told the original is: where it was when transcribed, where an import put it, or the file chosen with **Locate it…**.

When an export cannot find an original it names the recording and the file it wants and offers **Locate it…**: choose the file, under any name and in any folder, and the export carries on. A file that runs more than a second longer or shorter than the recording is questioned first, since it is very likely a different recording; **Use it anyway** takes it.

## If something goes wrong

**The first transcription.** It needs about 5 GB of free disk: about 1.5 GB of libraries and a 3.1 GB speech model. On a Mac the status reads "Downloading the speech model: 1,240 of 3,084 MB" as it comes. With too little room it stops before starting and says how much is needed and how much is free; if the disk fills part-way it says so. Either way nothing is damaged: make room and press **Transcribe** again.

**It sits on "Loading the speech model" for minutes with no count.** Another transcriber may be holding the download. Quit Transclip, run `pkill -f transcribe.py` in Terminal, and open it again.

**Deleting and reinstalling the app changes nothing** about a download: the model and libraries are kept outside it, in `~/.cache/huggingface` and `~/.cache/uv`.

**To watch the download happen**, from a copy of this repository:

```
echo '[]' > /tmp/none.json && uv run transcribe.py /tmp/none.json && echo "The speech model is ready."
```

It fetches the same model to the same place, and the app finds it there. If that stalls too, put `HF_HUB_DISABLE_XET=1 ` in front of `uv` to fetch it by the plain route.

**macOS asks whether "python3.12" may read a folder.** That is Transclip (see [Install](#macos-app)). It can ask again for Downloads, Documents or Desktop when a file is imported or located there.

**Anything else:** the app's own messages are in `Library/Logs/Transclip.log` in your home folder.

## Where things are kept

Everything lives in one folder, `Transclip` in your home folder (set `TRANSCLIP_HOME` to put it elsewhere):

| | |
|---|---|
| `tapes/` | per recording: the transcript (which also says where the original is), a small listening copy, the waveform outline and the measurements music is found from, so everything but export works with the recordings' drive unplugged |
| `selects.json` | your bins and clips, saved on every change, with one backup per day |
| `exports/` | a new dated folder per export, and the files made by Collect and export |
| `originals/` | recordings that arrived inside an imported file |
| `settings.json` | the recordings folder and depth |
| `models/` | the small model that search by meaning uses |
| `sessions/<name>/` | each other session, laid out like the folder itself: `tapes/`, `selects.json`, `exports/`, `settings.json`, `originals/` |
| `session.txt` | which session to reopen |

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
