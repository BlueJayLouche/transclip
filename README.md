# Transclip

Transcribe a folder of audio recordings, read and search them, and pull clips out of the originals.

Built for making selects from an archive of digitised cassette tapes: hours of speech, uneven quality, not much time.

- **Records tapes** (on a Mac): the master WAV straight from a sound input, started before the tape and ended by itself when the deck stops, then transcribed without being asked.
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
| macOS 14 or later, Apple Silicon | In use. Double-click app with a window of its own. The libraries it runs on are not built for older versions of macOS. |
| Windows, Linux, Intel Mac | Runs from the command line and shows itself in your browser. CI runs the whole app end to end on Windows and Linux with a small model, but nobody has yet used it on either with real recordings, and the folder chooser there is untried. Without an NVIDIA graphics card, transcription will be much slower than on Apple Silicon. |

## Install

### macOS app

Download `transclip-macos-arm64.dmg` from [Releases](../../releases), open it and drag Transclip to Applications. It needs a Mac with Apple Silicon and macOS 14 or later. Working on recordings someone has transcribed and sent you asks little more than that; transcribing them yourself also takes about 4 GB of memory while it runs, so 8 GB is tight and 16 GB comfortable.

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

## Recording a tape

On a Mac, **Record…** (beside Recordings…) transfers a tape through any sound input and makes the master: a 24-bit, 48 kHz stereo WAV in the session's recordings folder, as `<tape>/<side>.wav`.

1. Choose the **input**, and for an interface with more than two inputs the **channels** the deck is on. The meter is live from then on.
2. Play the loudest part of the tape and set the gain on your interface or deck until "loudest so far" sits near the **target max** (the orange mark, −6 dBFS unless you move it). Transclip only measures; the master is exactly what arrives.
3. Type the **tape**'s name. The **side** starts as "Side A".
4. With the deck stopped, press **Record** and wait for **ARMED**. Then press Play on the deck: the panel turns to **● RECORDING**.

What it keeps, and when it ends:

- The master begins one second before the tape's first sound, so the wait before you press Play is not in it. A click is not taken for the tape: the sound has to last.
- The side **ends by itself** once the deck has been stopped for 30 seconds, and the master ends three seconds after the last sound. A blank stretch of tape, however long, does not end the side; a pause of the deck shorter than 30 seconds stays in the master. While it counts down the panel says **DECK STOPPED** and the button reads **End side now**.
- **Stop** ends a side at any time. While sound is still coming it asks to be pressed twice, so a stray press does not end a transfer. **Cancel**, while armed, records nothing.
- If the sound clipped, the panel says where. If the sound system ever lost sound, it says that too: such a master has gaps and should be done again.

**The stop level** (the blue mark) is the level under which the deck counts as stopped. A deck standing still is far quieter than blank tape running, and the level has to lie between the two.

- With the slider all the way left it is **learned**: from the moments between Record and the tape starting. That needs Record before Play; if the tape was already running, nothing could be learned, the side does not end by itself, and the panel says so.
- Anywhere else, it is what you set: read the meter with the deck stopped ("quietest" shows it), read it on blank tape, and put the mark between. It can be moved while a side is recording.

**Monitor** plays the input through to an output you choose, so the tape can be heard: as recorded, or with one track in both ears for a tape recorded on one. It is a separate program from the recorder and has no part in the master, so it can be switched on and off while a side is recording. It is never on unless you tick it.

**Recording a side again.** A name that is taken is refused; the panel then offers to record it again, and the old master goes to the Mac's bin.

When a side ends it is transcribed straight away, or as soon as a transcription already running has finished, and the panel offers the next side of the same tape. The panel keeps the window until the side is over, and the session and its recordings folder cannot be changed meanwhile.

The sound comes first. The recorder is a program of its own that nothing else in the app can hold up; every buffer the sound system delivers is queued, never skipped; the file on disk is a playable WAV at every moment; quitting asks first and finishes the file; and after a crash or a power cut, what had been recorded is given its name the next time the Record panel is opened. It will not start without room for two hours on the disk.

Not yet: the transcript appearing while the tape is still playing, and recording on Windows or Linux.

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

**macOS asks whether "python3.12" may read a folder, or use the microphone.** That is Transclip (see [Install](#macos-app)). It can ask again for Downloads, Documents or Desktop when a file is imported or located there. "The microphone" is how macOS speaks of every sound input: it is asked the first time the Record panel is opened, and recording cannot work without it.

**A side did not end by itself.** Either the tape was already running when Record was pressed (the panel said so), or the deck does not go quiet enough when it stops. Press Stop; nothing is lost. For the next side, set the stop level by hand.

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

[Whisper](https://github.com/openai/whisper) via [MLX](https://github.com/ml-explore/mlx-examples/tree/main/whisper) and [faster-whisper](https://github.com/SYSTRAN/faster-whisper), [Silero VAD](https://github.com/snakers4/silero-vad), [fastembed](https://github.com/qdrant/fastembed), [wavesurfer.js](https://wavesurfer.xyz), [FFmpeg](https://ffmpeg.org) through [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg), [Flask](https://flask.palletsprojects.com), [pywebview](https://pywebview.flowrl.com) for the Mac window, [sounddevice](https://python-sounddevice.readthedocs.io) and [PortAudio](https://www.portaudio.com) for recording, [Pillow](https://python-pillow.org) to draw the icon, and [uv](https://docs.astral.sh/uv/), a copy of which is inside the Mac app.
