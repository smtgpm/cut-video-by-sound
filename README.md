# Cut video by sound

Remove intros, outros, recaps or any other repeated passage from a whole
series at once, by **sound**. You mark the passage once in one episode; the
program finds it in every other episode by listening for it, lets you check
each cut, then writes the cut videos with their subtitles retimed.

- Exact cuts: the audio match gives the start to the sample, and only the few
  frames around each cut are re-encoded. The rest of the video and all the
  audio are copied untouched, so there is no quality loss.
- Several samples at once (say an intro and an outro), never overlapping, even
  when one is heard inside the other.
- A review step to check, adjust, untick or add cuts before anything is
  written, and that remembers what you already reviewed.
- Subtitles (inside the video or in files next to it) and chapters follow the
  cuts.
- Write cleaned copies to a folder, or replace the original files.

There is a desktop app and a command line.

## Contents

- [Installation](#installation)
- [The app, step by step](#the-app-step-by-step)
- [Command line](#command-line)
- [How it works](#how-it-works)
- [Files the program writes](#files-the-program-writes)
- [Project layout](#project-layout)
- [Limits](#limits)

## Installation

You need Python 3.10 or newer, [FFmpeg](https://ffmpeg.org/) (with
`ffprobe`) in your `PATH`, and [VLC](https://www.videolan.org/) for playback
in the app. Tkinter comes with Python on Windows and macOS; on Debian or
Ubuntu, install `python3-tk`. On Linux, if `zenity` is installed (it comes
with GNOME), the app uses the system's own file and folder dialogs.

```bash
git clone https://github.com/smtgpm/cut-video-by-sound.git
cd cut-video-by-sound
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Then start the app:

```bash
python app_extract.py
```

## The app, step by step

The player is on the left, the four steps are tabs on the right.

### 1. Samples

Open an episode, find the passage to remove and mark it: `I` at its start,
`O` at its end (or type the times). Preview it, then save it as a sample.
Samples are grouped in sets, for example one set per series. Tick the
samples to search for.

Start the sample exactly on the passage, or just inside it. If it begins with
a bit of the previous scene, episodes where the passage comes right at 0:00
will not match.

### 2. Videos

Add videos, or a folder: every video in it and in its subfolders is added.
"Find matches" searches the videos for the ticked samples.

- **Threshold**: how similar a passage must sound to count, higher is
  stricter (0.72 by default).
- **Hide matches below**: weaker matches (50 % by default) are dropped and
  never shown in the review.
- **A sample may occur several times per video**: by default each sample is
  cut at most once per video.
- **Videos at once**: how many videos are searched in parallel, about 1.3 GB
  of memory each. The default suits the machine.

The cuts are chosen over all samples together: one per sample, never
overlapping, with the highest total score. So when the intro tune is also
played inside the outro, the intro sample cannot take the copy inside the
outro, even if it scores a little higher there, because the outro would then
be left with a poor match.

The list is split in two: videos **to review**, and videos already
**reviewed**. "Find matches" only searches the videos that need it: never the
reviewed ones, nor those already searched with the same samples and
settings, nor those waiting for a new sample (see below).

To review a video again, select it (or the whole "Reviewed" group) and click
**Unreview**: it goes back to the review with its cuts, without a new search.

### 3. Review cuts

The review goes through the videos not reviewed yet, one at a time (◀ ▶ or
the list at the top). A reviewed video is never shown again, unless you
unreview it in step 2.

For each video, the list shows its cuts and how well each one matches.
Doubtful matches (a similar sound, but not a copy of the sample) start
unticked, and overlapping ticked cuts are flagged.

- Click a tick to keep or drop a cut. Click a row to adjust it.
- The selected cut is shown on a zoomed timeline: drag its edges, scroll to
  zoom, or nudge each edge by 1 s, 0.1 s or one frame. "Here (I/O)" moves an
  edge to the playhead.
- "▶ What is removed" plays the cut, "▶ Result at this cut" plays across the
  junction as it will be.
- "+ Add cut at playhead" adds a cut by hand.
Then decide, and the next video opens:

- **✓ Validate, reviewed**: the cuts are right. The video is saved with its
  cuts: add the same file again, even after restarting the app, and it comes
  back reviewed.
- **↻ Needs re-scan**: the video goes back to step 2, to be searched again by
  the next "Find matches". Cuts you added by hand are kept.
  With **Not with the current samples** ticked, it waits instead: it is only
  searched again once you add a sample in step 1 (or change a search
  setting), since searching again with the same samples would find the same
  thing. This choice is saved too.

| Key | Action |
|---|---|
| Space | play / pause |
| Left / Right | 0.1 s (with Shift: 1 s) |
| `,` / `.` | one frame |
| `I` / `O` | set the start / end to the playhead |

When paused, the picture is the exact frame at the playhead, on the same clock
as the cutter. Subtitles are only drawn during playback.

### 4. Export

Choose one:

- **Write cleaned copies to a folder**: originals are left as they are.
  `Season 1/E05.mp4` becomes `<output>/Season 1_cleaned/E05_cleaned.mkv`,
  subfolders are kept, and a video without cuts is copied as it is.
- **Replace the original files**: each video with cuts is replaced by its cut
  version (`Episode.mp4` becomes `Episode.mkv`) and its subtitle files are
  retimed where they are. The original is only deleted once the cut video is
  complete, but **this cannot be undone**.

"Edge quality" (CRF, 20 by default, lower is better) and "Speed" (encoder
preset) only apply to the few frames re-encoded around each cut.

Cut videos can then be played in the app, with "Next cut" jumping from one
junction to the next.

## Command line

The command line does the same without the review: it cuts the best match of
each sample, doubtful ones included (it prints a warning for those).

```bash
python cli_remove.py --refs refs/my-series --videos "Season 1" "Season 2" special.mp4 --out cleaned
```

Every `.wav` file in `--refs` is a sample: the app saves its samples as
`refs/<set name>/*.wav`, so a set can be used directly. That command creates
`cleaned/Season 1_cleaned/`, `cleaned/Season 2_cleaned/`, and
`cleaned/special_cleaned.mkv`.

| Option | Meaning |
|---|---|
| `--out FOLDER` | write cleaned copies there |
| `--in-place` | replace the videos instead (asks first; `--yes` does not) |
| `--threshold 0.72` | match threshold, higher is stricter |
| `--intro-max 180` | seconds in which an intro is expected, only to rank close candidates |
| `--unlimited-extracts` | allow several cuts per sample in a video |
| `--workers N` | videos processed in parallel, about 1.3 GB of memory each |

## How it works

### Finding the samples

The audio of each video is decoded once, as mono 16 kHz, on the same clock a
player shows (so a delayed audio track does not shift the cuts). Each sample
is then searched for in two ways:

- **waveform correlation**, normalised, which finds exact copies and gives the
  precise start, to the audio sample;
- a **mel-spectrogram fingerprint**, which still recognises a copy that was
  encoded differently.

Candidates from both are refined against the waveform. The waveform score is
the "match %" shown in the app: below 50 % a match counts as doubtful.

### Choosing the cuts

All matches of all samples are considered together. The program keeps, for
each sample, at most one match, so that no two kept matches overlap and the
sum of their scores is the highest (an exact search, since there are only a
few candidates). Matches that touch by less than a second are trimmed instead
of being rejected. With "several times per video", more matches are then
added wherever they overlap nothing.

### Cutting

Times are kept in integer milliseconds, and one timeline maps every source
time to its output time, so video, audio, subtitles and chapters cannot drift
apart.

- **Audio**: never re-encoded. Packets are copied, so a junction is within
  half a packet of the cut (about 10–16 ms for AAC or AC-3).
- **H.264 and HEVC video**: copied, except the frames between each cut and the
  next keyframe, typically a few seconds, which are re-encoded with the same
  codec. Those re-encodes run in parallel.
- **Other video codecs** (VP9, AV1, MPEG-4…): the kept video is re-encoded to
  H.264, because those streams cannot be spliced.

Cut videos are written as Matroska (`.mkv`), with every audio and subtitle
track, the chapters (retimed) and the attachments such as embedded fonts.

### Subtitles

| Where | Formats |
|---|---|
| Inside the video | SRT, ASS/SSA, WebVTT, MOV_TEXT (becomes SRT), PGS, VobSub, DVB |
| Next to the video | `.srt`, `.ass`, `.ssa`, `.vtt`, `.smi`, MicroDVD `.sub`, `.sup`, `.idx` + `.sub` |

Subtitle files next to a video are found by name, and written next to the
cleaned copy:

```
Episode.srt                  -> Episode_cleaned.srt
Episode.en.forced.srt        -> Episode_cleaned.en.forced.srt
Subs/Episode.fr.ass          -> Episode_cleaned.fr.ass
Subs/Episode/2_English.srt   -> Episode_cleaned.2_English.srt
```

When the originals are replaced, the subtitle files are rewritten where they
are.

A cue inside a removed part is dropped, a cue crossing a cut is shortened,
and everything after it moves back by the removed time. Only the timestamps
change: styling, encoding and line endings are kept. Anything that cannot be
retimed is reported as a warning.

## Files the program writes

All next to the program, and not part of the repository:

| Path | What |
|---|---|
| `refs/<set>/*.wav` | your samples, one folder per set |
| `reviews.json` | the videos you validated or sent back for a new search, with their cuts. An entry only applies while the file keeps the same size and date |

## Project layout

| File | Role |
|---|---|
| `app_extract.py` | starts the app, after checking FFmpeg and the dependencies |
| `cli_remove.py` | the command line |
| `pipeline.py` | what the app and the command line share: finding videos, searching them, choosing the cuts, exporting or replacing |
| `audio_match.py` | decoding audio and finding a sample in it |
| `remove_segments.py` | cutting a video: copying packets, re-encoding the frames around each cut |
| `timeline.py` | the mapping from source time to output time |
| `subtitles.py`, `subtitle_bitmap.py` | retiming text and image subtitles |
| `gui/app.py` | the main window |
| `gui/samples.py`, `gui/videos.py`, `gui/review.py`, `gui/export.py` | the four steps |
| `gui/player.py`, `gui/stills.py` | the VLC player and the exact frame shown when paused |
| `gui/reviews.py` | the reviews kept in `reviews.json` |
| `gui/tasks.py`, `gui/state.py`, `gui/common.py` | background work, shared state, look and helpers |

## Limits

- The program needs the passage to sound the same in every video. A new
  arrangement of a theme, or music under dialogue that changes each time, is
  not found.
- Animations inside an ASS cue (`\k`, `\move`, `\t`) restart when a cut trims
  the start of that cue. DVB subtitles are retimed by packet.
- Replacing the originals cannot be undone.
