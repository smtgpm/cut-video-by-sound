"""
app_extract_playback.py

Tkinter video/audio clip extraction IDE.

Features:
  - Load a video
  - Play/pause video with synchronized audio using VLC/libVLC
  - Scrub with a timeline
  - Mark start/end points
  - Preview the selected clip
  - Fine/coarse seeking
  - Extract marked clips as mono 22.05 kHz WAV files with ffmpeg

Python dependencies:
  - opencv-python
  - pillow
  - python-vlc

External dependencies:
  - VLC media player (libVLC)
  - ffmpeg available in PATH

Install Python packages:
    pip install opencv-python pillow python-vlc

Install VLC separately:
    https://www.videolan.org/vlc/
"""

import os
import shutil
import subprocess
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import cv2
from PIL import Image, ImageTk

try:
    import vlc
except ImportError:
    vlc = None


class VideoExtractorApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Audio clip extractor")
        self.root.geometry("1000x760")
        self.root.minsize(850, 650)

        self.cap = None
        self.video_path = None
        self.fps = 25.0
        self.duration = 0.0

        self.clips = []
        self.pending_start = None

        self.player = None
        self.vlc_instance = None
        self.playing = False
        self.updating_slider = False
        self.preview_after_id = None
        self.poll_after_id = None

        self.volume_var = tk.IntVar(value=80)
        self.time_var = tk.DoubleVar(value=0.0)

        self._build_ui()
        self._setup_vlc()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- UI ----------
    def _build_ui(self):
        top = ttk.Frame(self.root)
        top.pack(fill="x", padx=8, pady=8)

        ttk.Button(top, text="Open a video",
                   command=self.open_video).pack(side="left")
        self.lbl_path = ttk.Label(top, text="No video loaded")
        self.lbl_path.pack(side="left", padx=10)

        self.canvas = tk.Label(self.root, bg="black")
        self.canvas.pack(fill="both", expand=True, padx=8, pady=8)

        # Playback controls
        playback = ttk.Frame(self.root)
        playback.pack(fill="x", padx=8, pady=3)

        self.btn_play = ttk.Button(
            playback, text="▶ Play", command=self.toggle_play, state="disabled"
        )
        self.btn_play.pack(side="left", padx=3)

        ttk.Button(
            playback, text="⏮ 1s", command=lambda: self.nudge(-1)
        ).pack(side="left", padx=3)
        ttk.Button(
            playback, text="◀ 0.1s", command=lambda: self.nudge(-0.1)
        ).pack(side="left", padx=3)
        ttk.Button(
            playback, text="0.1s ▶", command=lambda: self.nudge(0.1)
        ).pack(side="left", padx=3)
        ttk.Button(
            playback, text="1s ⏭", command=lambda: self.nudge(1)
        ).pack(side="left", padx=3)

        ttk.Label(playback, text="Volume").pack(side="left", padx=(18, 4))
        self.volume_scale = ttk.Scale(
            playback,
            from_=0,
            to=100,
            variable=self.volume_var,
            orient="horizontal",
            command=self._on_volume,
        )
        self.volume_scale.pack(side="left", fill="x", expand=False, ipadx=30)

        self.lbl_time = ttk.Label(playback, text="00:00.000 / 00:00.000")
        self.lbl_time.pack(side="right", padx=4)

        # Timeline
        nav = ttk.Frame(self.root)
        nav.pack(fill="x", padx=8, pady=4)

        self.slider = ttk.Scale(
            nav,
            from_=0,
            to=100,
            orient="horizontal",
            variable=self.time_var,
            command=self._on_slider_move,
            state="disabled",
        )
        self.slider.pack(fill="x")

        # Marking controls
        actions = ttk.Frame(self.root)
        actions.pack(fill="x", padx=8, pady=5)

        ttk.Button(
            actions, text="Mark Start", command=self.mark_start
        ).pack(side="left", padx=3)
        ttk.Button(
            actions, text="Mark End / Add Clip", command=self.mark_end
        ).pack(side="left", padx=3)
        ttk.Button(
            actions, text="Preview Clip", command=self.preview_clip
        ).pack(side="left", padx=3)
        ttk.Button(
            actions, text="Clear Start", command=self.clear_pending_start
        ).pack(side="left", padx=3)

        self.lbl_pending = ttk.Label(actions, text="Pending start: none")
        self.lbl_pending.pack(side="left", padx=18)

        # Marked clips
        listframe = ttk.Frame(self.root)
        listframe.pack(fill="both", expand=False, padx=8, pady=4)

        ttk.Label(listframe, text="Marked clips:").pack(anchor="w")

        listrow = ttk.Frame(listframe)
        listrow.pack(fill="x")

        self.listbox = tk.Listbox(listrow, height=6)
        self.listbox.pack(fill="x", side="left", expand=True)

        buttoncol = ttk.Frame(listrow)
        buttoncol.pack(side="left", padx=6)

        ttk.Button(
            buttoncol, text="Preview selection", command=self.preview_selected
        ).pack(fill="x", pady=2)
        ttk.Button(
            buttoncol, text="Remove selection", command=self.remove_selected
        ).pack(fill="x", pady=2)

        bottom = ttk.Frame(self.root)
        bottom.pack(fill="x", padx=8, pady=10)

        ttk.Button(
            bottom,
            text="Extract audio from all clips",
            command=self.extract_all,
        ).pack(side="left")

        self.lbl_status = ttk.Label(bottom, text="")
        self.lbl_status.pack(side="left", padx=15)

        # Keyboard shortcuts make precise editing easier.
        self.root.bind("<space>", self._space_key)
        self.root.bind("<Left>", lambda _e: self.nudge(-0.1))
        self.root.bind("<Right>", lambda _e: self.nudge(0.1))
        self.root.bind("<Shift-Left>", lambda _e: self.nudge(-1))
        self.root.bind("<Shift-Right>", lambda _e: self.nudge(1))
        self.root.bind("<Escape>", lambda _e: self.clear_pending_start())

    # ---------- VLC ----------
    def _setup_vlc(self):
        if vlc is None:
            self._set_status(
                "VLC support not installed — run: pip install python-vlc"
            )
            return

        try:
            self.vlc_instance = vlc.Instance()
            self.player = self.vlc_instance.media_player_new()
            self.player.audio_set_volume(self.volume_var.get())
        except Exception as exc:
            self.player = None
            self._set_status(f"Could not initialize VLC: {exc}")

    def _attach_vlc_to_canvas(self):
        if self.player is None:
            return False

        self.root.update_idletasks()
        handle = self.canvas.winfo_id()

        try:
            if sys.platform.startswith("win"):
                self.player.set_hwnd(handle)
            elif sys.platform == "darwin":
                self.player.set_nsobject(handle)
            else:
                self.player.set_xwindow(handle)
            return True
        except Exception as exc:
            self._set_status(f"Could not attach VLC video: {exc}")
            return False

    def _load_into_vlc(self, path):
        if self.player is None:
            return False

        try:
            self.player.stop()
            media = self.vlc_instance.media_new(path)
            self.player.set_media(media)
            self._attach_vlc_to_canvas()
            self.player.audio_set_volume(self.volume_var.get())
            return True
        except Exception as exc:
            self._set_status(f"Could not load media in VLC: {exc}")
            return False

    # ---------- Video handling ----------
    def open_video(self):
        path = filedialog.askopenfilename(
            title="Choose a video",
            filetypes=[
                ("Videos", "*.mp4 *.mkv *.avi *.mov *.webm *.m4v"),
                ("All files", "*.*"),
            ],
        )
        if not path:
            return

        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            messagebox.showerror("Error", "Could not open this video.")
            return

        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        duration = frame_count / fps if fps else 0.0
        cap.release()

        if duration <= 0:
            messagebox.showerror(
                "Error", "Could not determine video duration.")
            return

        self.stop_playback()

        self.cap = cv2.VideoCapture(path)
        self.video_path = path
        self.fps = fps
        self.duration = duration

        self.lbl_path.config(text=os.path.basename(path))
        self.slider.config(to=max(self.duration, 0.01), state="normal")
        self.time_var.set(0.0)

        self.clips = []
        self.listbox.delete(0, "end")
        self.pending_start = None
        self._refresh_pending_label()

        vlc_ok = self._load_into_vlc(path)

        self.btn_play.config(state="normal" if vlc_ok else "disabled")

        # Use VLC as the actual display. If VLC isn't available, retain
        # the original OpenCV frame preview as a useful fallback.
        if not vlc_ok:
            self._show_frame_at(0.0)

        self._set_status(
            "Ready — press Play or use the timeline."
            if vlc_ok
            else "Frame preview only: VLC is not available."
        )

    def _on_slider_move(self, _value):
        if self.updating_slider or self.cap is None:
            return

        t = max(0.0, min(self.duration, float(self.time_var.get())))

        if self.player is not None:
            was_playing = self.playing
            try:
                self.player.set_time(int(t * 1000))
            except Exception:
                pass

            if not was_playing:
                self._update_time_display(t)
        else:
            self._show_frame_at(t)

    def nudge(self, delta):
        if self.cap is None:
            return

        t = max(0.0, min(self.duration, float(self.time_var.get()) + delta))
        self.time_var.set(t)

        if self.player is not None:
            try:
                self.player.set_time(int(t * 1000))
            except Exception:
                pass
            self._update_time_display(t)
        else:
            self._show_frame_at(t)

    def _show_frame_at(self, t_seconds):
        """Fallback frame preview when VLC is unavailable."""
        if self.cap is None:
            return

        frame_idx = int(t_seconds * self.fps)
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
        ok, frame = self.cap.read()
        if not ok:
            return

        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w = frame.shape[:2]

        # Fit the frame to the current preview area.
        max_w = max(self.canvas.winfo_width() - 10, 300)
        max_h = max(self.canvas.winfo_height() - 10, 200)
        scale = min(max_w / w, max_h / h, 1.0)

        frame = cv2.resize(
            frame,
            (max(1, int(w * scale)), max(1, int(h * scale))),
        )

        img = ImageTk.PhotoImage(Image.fromarray(frame))
        self.canvas.imgtk = img
        self.canvas.config(image=img)

        self._update_time_display(t_seconds)

    # ---------- Playback ----------
    def toggle_play(self):
        if self.player is None or self.video_path is None:
            return

        try:
            if self.playing:
                self.player.pause()
                self.playing = False
                self.btn_play.config(text="▶ Play")
                self._set_status("Paused")
            else:
                current = self._get_player_time()

                if current >= self.duration - 0.05:
                    self.player.set_time(0)

                self.player.play()
                self.playing = True
                self.btn_play.config(text="⏸ Pause")
                self._set_status("Playing")
                self._start_polling()
        except Exception as exc:
            self._set_status(f"Playback error: {exc}")

    def stop_playback(self):
        self.playing = False

        if self.player is not None:
            try:
                self.player.stop()
            except Exception:
                pass

        self.btn_play.config(text="▶ Play")

        if self.poll_after_id is not None:
            try:
                self.root.after_cancel(self.poll_after_id)
            except Exception:
                pass
            self.poll_after_id = None

    def _start_polling(self):
        if self.poll_after_id is None:
            self._poll_player()

    def _poll_player(self):
        self.poll_after_id = None

        if self.player is None:
            return

        try:
            t = self._get_player_time()

            self.updating_slider = True
            self.time_var.set(max(0.0, min(self.duration, t)))
            self.updating_slider = False

            self._update_time_display(t)

            state = self.player.get_state()

            # VLC reaches Ended when playback naturally finishes.
            if state == vlc.State.Ended:
                self.playing = False
                self.btn_play.config(text="▶ Play")
                self._set_status("Playback finished")
                return

            if self.playing:
                self.poll_after_id = self.root.after(40, self._poll_player)
        except Exception:
            self.updating_slider = False

    def _get_player_time(self):
        if self.player is None:
            return float(self.time_var.get())

        ms = self.player.get_time()
        if ms is None or ms < 0:
            return float(self.time_var.get())
        return ms / 1000.0

    def _on_volume(self, value):
        if self.player is not None:
            try:
                self.player.audio_set_volume(int(float(value)))
            except Exception:
                pass

    def _update_time_display(self, t):
        self.lbl_time.config(
            text=f"{self._fmt(t)} / {self._fmt(self.duration)}"
        )

    # ---------- Clip marking ----------
    def mark_start(self):
        if self.video_path is None:
            return

        self.pending_start = float(self.time_var.get())
        self._refresh_pending_label()
        self._set_status(f"Start marked at {self._fmt(self.pending_start)}")

    def mark_end(self):
        if self.video_path is None:
            return

        if self.pending_start is None:
            messagebox.showwarning("Warning", "Mark a start first.")
            return

        end = float(self.time_var.get())
        start = self.pending_start

        if end <= start:
            messagebox.showwarning("Warning", "End must be after start.")
            return

        self.clips.append({"start": start, "end": end})
        self.listbox.insert(
            "end",
            f"Clip {len(self.clips)}: "
            f"{self._fmt(start)} -> {self._fmt(end)} "
            f"({self._fmt(end - start)})",
        )

        self.pending_start = None
        self._refresh_pending_label()
        self._set_status(
            f"Clip added: {self._fmt(start)} → {self._fmt(end)}"
        )

    def clear_pending_start(self):
        self.pending_start = None
        self._refresh_pending_label()

    def _refresh_pending_label(self):
        txt = (
            "none"
            if self.pending_start is None
            else self._fmt(self.pending_start)
        )
        self.lbl_pending.config(text=f"Pending start: {txt}")

    def preview_clip(self):
        if self.pending_start is None:
            messagebox.showwarning(
                "Preview clip",
                "Mark a start and then move to the end point before previewing.",
            )
            return

        end = float(self.time_var.get())
        if end <= self.pending_start:
            messagebox.showwarning(
                "Preview clip",
                "Move the playhead after the marked start first.",
            )
            return

        self._play_range(self.pending_start, end)

    def preview_selected(self):
        sel = self.listbox.curselection()
        if not sel:
            messagebox.showwarning("Preview clip", "Select a clip first.")
            return

        clip = self.clips[sel[0]]
        self._play_range(clip["start"], clip["end"])

    def _play_range(self, start, end):
        if self.player is None:
            messagebox.showwarning(
                "Preview clip",
                "VLC is required for clip preview.",
            )
            return

        if self.preview_after_id is not None:
            try:
                self.root.after_cancel(self.preview_after_id)
            except Exception:
                pass
            self.preview_after_id = None

        self.stop_playback()

        try:
            self.player.set_time(int(start * 1000))
            self.player.play()
            self.playing = True
            self.btn_play.config(text="⏸ Pause")
            self._set_status(
                f"Previewing {self._fmt(start)} → {self._fmt(end)}"
            )
            self._preview_watch(end)
        except Exception as exc:
            self._set_status(f"Preview error: {exc}")

    def _preview_watch(self, end):
        if self.player is None:
            return

        current = self._get_player_time()

        if current >= end:
            self.stop_playback()
            try:
                self.player.set_time(int(end * 1000))
            except Exception:
                pass
            self.time_var.set(end)
            self._update_time_display(end)
            self._set_status("Clip preview finished")
            return

        if self.playing:
            self.preview_after_id = self.root.after(
                30, lambda: self._preview_watch(end)
            )

    # ---------- Clip list ----------
    def remove_selected(self):
        sel = self.listbox.curselection()
        if not sel:
            return

        idx = sel[0]
        del self.clips[idx]

        self.listbox.delete(0, "end")
        for i, c in enumerate(self.clips, 1):
            self.listbox.insert(
                "end",
                f"Clip {i}: "
                f"{self._fmt(c['start'])} -> {self._fmt(c['end'])} "
                f"({self._fmt(c['end'] - c['start'])})",
            )

    # ---------- Audio extraction ----------
    def extract_all(self):
        if not self.clips:
            messagebox.showwarning("Warning", "No clips marked.")
            return

        if not self.video_path:
            messagebox.showwarning("Warning", "No video loaded.")
            return

        if shutil.which("ffmpeg") is None:
            messagebox.showerror(
                "FFmpeg not found",
                "ffmpeg was not found in PATH.\n\n"
                "Install ffmpeg and make sure the ffmpeg command is available.",
            )
            return

        out_dir = filedialog.askdirectory(
            title="Output folder for .wav files"
        )
        if not out_dir:
            return

        base = os.path.splitext(os.path.basename(self.video_path))[0]
        errors = []

        self.stop_playback()

        for i, clip in enumerate(self.clips, 1):
            out_path = os.path.join(
                out_dir, f"{base}_clip_{i}.wav"
            )
            ok, msg = self._extract_audio_segment(
                self.video_path,
                clip["start"],
                clip["end"],
                out_path,
            )
            if not ok:
                errors.append(msg)

        if errors:
            messagebox.showerror("Errors", "\n".join(errors))
        else:
            messagebox.showinfo(
                "Done",
                f"{len(self.clips)} audio clip(s) saved to:\n{out_dir}",
            )

    @staticmethod
    def _extract_audio_segment(
        video_path, start, end, out_path
    ):
        duration = end - start

        cmd = [
            "ffmpeg",
            "-y",
            "-ss",
            f"{start:.3f}",
            "-i",
            video_path,
            "-t",
            f"{duration:.3f}",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "22050",
            "-acodec",
            "pcm_s16le",
            out_path,
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
            )

            if result.returncode != 0:
                return (
                    False,
                    f"ffmpeg failed for {out_path}:\n"
                    f"{result.stderr[-500:]}",
                )

            return True, "ok"

        except FileNotFoundError:
            return (
                False,
                "ffmpeg not found. Install it and make sure "
                "it is in PATH.",
            )

    # ---------- Helpers ----------
    def _set_status(self, text):
        self.lbl_status.config(text=text)

    @staticmethod
    def _fmt(t):
        t = max(0.0, float(t))
        m = int(t // 60)
        s = t - m * 60
        return f"{m:02d}:{s:06.3f}"

    def _space_key(self, _event):
        self.toggle_play()
        return "break"

    def _on_close(self):
        self.stop_playback()

        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass

        if self.player is not None:
            try:
                self.player.release()
            except Exception:
                pass

        if self.vlc_instance is not None:
            try:
                self.vlc_instance.release()
            except Exception:
                pass

        self.root.destroy()


def main():
    root = tk.Tk()
    VideoExtractorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
