# Video Intro Remover

## How it works

The program detects reference audio clips, then removes the matching passages from videos.

### No quality loss

Removal uses FFmpeg exclusively with `-c copy`:

- no video is re-encoded;
- no audio is re-encoded;
- NVENC/QuickSync/VAAPI/VideoToolbox are not used;
- kept streams therefore stay as-is.

Segment boundaries are aligned to **keyframes**. The cut may therefore be slightly offset from the detected timestamp, but video and audio remain copied together.

## Installation

Install FFmpeg and FFprobe, then:

```bash
pip install -r requirements.txt
```

## 1. Create audio references

```bash
python app_extract.py
```

Mark the start and end of the intro, then export the `.wav` clips.

## 2. Clean the videos

Individual files:

```bash
python cli_remove.py --refs refs_folder --videos ep01.mp4 ep02.mp4 --out output_folder
```

Entire folder (writes into `output/season1_cleaned/`):

```bash
python cli_remove.py --refs refs_folder --videos season1 --out output
```

Several folders/files can be mixed:

```bash
python cli_remove.py --refs refs --videos season1 season2 special.mp4 --out output
```

That creates `output/season1_cleaned/`, `output/season2_cleaned/`, and puts `special_cleaned.mp4` directly in `output/`.

Folders are scanned one level deep: only video files directly inside them are taken.

## Options

- `--threshold 0.82`: similarity threshold.
- `--workers 2`: number of videos processed in parallel.

The old GPU/smart-cut options are no longer needed: the pipeline always uses stream copy now.
