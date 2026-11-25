"""
The player shared by every step: video, timeline and cut preview.

VLC is only used to play. Whenever paused, the exact frame at the playhead is
shown by gui/stills.py, so what is marked is what is seen.
"""

import os
import sys
import time
import tkinter as tk
from tkinter import ttk

from PIL import ImageTk

from gui.common import ACCENT, BORDER, FIELD, MUTED, TEXT, format_time
from gui.stills import StillFrames
from pipeline import media_info

try:
    import vlc
    vlc.libvlc_get_version()
    VLC_PROBLEM = ''
except Exception as exc:  # missing package, or missing libVLC
    vlc = None
    VLC_PROBLEM = (f'Playback unavailable ({exc}). Install VLC, then run: '
                   f'{sys.executable} -m pip install python-vlc')

LEAD = 3.0  # seconds of context before and after a previewed cut


class Player(ttk.Frame):
    def __init__(self, parent, report):
        super().__init__(parent, padding=(8, 8, 4, 8))
        self.report = report
        self.path = None
        self.duration, self.fps, self.position = 0.0, 25.0, 0.0
        self.playing = False
        self.bands = []  # (start, end, colour)
        self.skips = []  # removed ranges, for playing as if already cut
        self.stops = []  # where "Next cut" goes
        self._range_end = None
        self._preview_skip = None
        self._ended = False
        self._pending = None  # a seek made before VLC could take one
        self._target = None  # (time, when asked) of the last seek
        self._anchor = (0.0, 0.0)  # last time reported by VLC, and when
        self._volume_set = False
        self._started = False  # VLC has this file open
        self._reveal = False  # swap the still for the video once it moves
        self._generation = self._request = 0
        self._still_image = self._still = None
        self.stills = StillFrames()

        self.instance = self.player = None
        if vlc:
            try:
                self.instance = vlc.Instance('--quiet', '--no-video-title-show')
                self.player = self.instance.media_player_new()
                self.player.video_set_key_input(False)
                self.player.video_set_mouse_input(False)
            except Exception as exc:
                self.player = None
                self.problem = f'Could not start VLC: {exc}'
        if self.player:
            self.problem = ''
        elif not vlc:
            self.problem = VLC_PROBLEM

        self._build()
        self._tick()

    def _build(self):
        self.title = ttk.Label(self, text='Nothing loaded', style='Title.TLabel', width=1)
        self.title.pack(fill='x', pady=(0, 6))
        if self.problem:
            ttk.Label(self, text=self.problem, style='Warning.TLabel',
                      wraplength=560).pack(fill='x', pady=(0, 6))

        # Everything below the picture is packed first, so it is never pushed out.
        bottom = ttk.Frame(self)
        bottom.pack(side='bottom', fill='x')
        area = tk.Frame(self, bg='black', width=320, height=180)
        area.pack(fill='both', expand=True)
        # Siblings, as VLC draws over any child of the window it is given.
        self.screen = tk.Frame(area, bg='black')
        self.screen.place(relwidth=1, relheight=1)
        self.still = tk.Label(area, bg='black', fg=MUTED, bd=0, highlightthickness=0)
        self.still.place(relwidth=1, relheight=1)
        self.still.bind('<Configure>', lambda _event: self._draw_still())

        self.line = tk.Canvas(bottom, height=44, bg=FIELD, highlightthickness=1,
                              highlightbackground=BORDER, cursor='hand2')
        self.line.pack(fill='x', pady=(8, 4))
        self.line.bind('<Button-1>', self._scrub)
        self.line.bind('<B1-Motion>', self._scrub)
        self.line.bind('<Configure>', lambda _event: self.redraw())

        row = ttk.Frame(bottom)
        row.pack(fill='x', pady=2)
        self.play_button = ttk.Button(row, text='▶ Play', width=9, command=self.toggle)
        self.play_button.pack(side='left')
        for text, step in (('−5s', -5), ('−1s', -1), ('−0.1', -0.1), ('◂ frame', 'back'),
                           ('frame ▸', 'forward'), ('+0.1', 0.1), ('+1s', 1), ('+5s', 5)):
            ttk.Button(row, text=text, width=len(text), style='Transport.TButton',
                       command=lambda step=step: self.step(step)).pack(side='left', padx=(3, 0))

        row = ttk.Frame(bottom)
        row.pack(fill='x', pady=(4, 0))
        self.clock = ttk.Label(row, text='00:00.000 / 00:00.000', font=('TkFixedFont', 11))
        self.clock.pack(side='left')
        self.next_button = ttk.Button(row, text='Next cut ⇥', command=self.next_stop)
        self.next_button.pack(side='left', padx=10)
        self.skip_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text='Skip cuts while playing',
                        variable=self.skip_var).pack(side='left')
        self.volume = tk.IntVar(value=80)
        ttk.Scale(row, from_=0, to=100, variable=self.volume, length=85,
                  command=self._set_volume).pack(side='right')
        ttk.Label(row, text='Volume', style='Muted.TLabel').pack(side='right', padx=5)

        ttk.Label(
            bottom, style='Muted.TLabel',
            text='Space play/pause   ←/→ 0.1 s   Shift+←/→ 1 s   , / . one frame   I / O set start / end',
        ).pack(anchor='w', pady=(6, 0))

    # ---------- Loading ----------
    def load(self, path, position=0.0, play=False):
        path = os.path.abspath(path)
        duration, fps = media_info(path)
        self.close_media()
        self.path, self.duration, self.fps = path, duration, fps
        self.title.config(text=os.path.basename(path))
        self.position = min(max(0.0, position), duration)
        self.show_still()
        if play:
            self.play()
        self.redraw()

    def close_media(self):
        if self.player:
            self.player.stop()
        self._generation += 1
        self.path, self.playing, self._started, self._ended = None, False, False, False
        self._pending = self._range_end = self._preview_skip = None
        self.duration = self.position = 0.0
        self.bands, self.skips, self.stops = [], [], []
        self._still_image = self._still = None
        self.still.config(image='', text='')
        self.still.lift()
        self.title.config(text='Nothing loaded')
        self.play_button.config(text='▶ Play')
        self.redraw()

    def _start(self):
        """Fresh media: the only reliable start from stopped or ended in libVLC."""
        self.player.stop()
        media = self.instance.media_new_path(self.path)
        media.add_option(f':start-time={self.position:.3f}')
        self.player.set_media(media)
        self.screen.update_idletasks()
        handle = self.screen.winfo_id()
        if sys.platform.startswith('win'):
            self.player.set_hwnd(handle)
        elif sys.platform == 'darwin':
            self.player.set_nsobject(handle)
        else:
            self.player.set_xwindow(handle)
        self.player.play()
        self._started, self._ended, self._volume_set, self._pending = True, False, False, None

    # ---------- Transport ----------
    def seek(self, position, keep_preview=False):
        if not self.path:
            return
        if not keep_preview:
            self._range_end = self._preview_skip = None
        self.position = min(max(0.0, position), self.duration)
        if self.playing:
            now = time.monotonic()
            self._target, self._anchor = (self.position, now), (self.position, now)
            if self.player.get_state() in (vlc.State.Playing, vlc.State.Paused):
                self.player.set_time(int(self.position * 1000))
            else:
                self._pending = self.position
        else:
            self.show_still()
        self.redraw()

    def step(self, step):
        if step in ('back', 'forward'):
            self.pause()
            step = (1 if step == 'forward' else -1) / self.fps
        self.seek(self.position + step)

    def toggle(self):
        if self.playing:
            self._range_end = self._preview_skip = None
            self.pause()
        else:
            self.play()

    def play(self):
        if not self.path:
            return
        if not self.player:
            self.report(self.problem)
            return
        if self.position >= self.duration - 0.05:
            self.position = 0.0
        if self._started and not self._ended and self.player.get_state() == vlc.State.Paused:
            self.player.set_time(int(self.position * 1000))
            self.player.set_pause(0)
        else:
            self._start()
        now = time.monotonic()
        self._target, self._anchor = (self.position, now), (self.position, now)
        self.playing, self._reveal = True, True
        self.play_button.config(text='⏸ Pause')

    def pause(self):
        if not self.playing:
            return
        self.playing, self._pending = False, None
        self.play_button.config(text='▶ Play')
        if not self._ended:
            self.player.set_pause(1)
        self.show_still()

    def play_range(self, start, end):
        self.seek(start)
        self._range_end = min(end, self.duration)
        self.play()

    def preview_without(self, start, end):
        """Play across a cut the way the exported video will."""
        self.seek(start - LEAD)
        self._preview_skip = (start, end)
        self._range_end = min(end + LEAD, self.duration)
        self.play()

    def next_stop(self):
        if not self.stops:
            self.report('There are no cuts in this file.')
            return
        points = sorted(max(0.0, point - LEAD) for point in self.stops)
        self.seek(next((p for p in points if p > self.position + 0.2), points[0]))

    # ---------- State ----------
    def _tick(self):
        self.after(30, self._tick)
        self._take_stills()
        if not self.player or not self.path or not self._started or self._ended:
            return
        state, now = self.player.get_state(), time.monotonic()
        if state in (vlc.State.Error, vlc.State.Ended):
            was_playing = self.playing
            self._ended, self.playing = True, False
            self._range_end = self._preview_skip = None
            self.play_button.config(text='▶ Play')
            if state == vlc.State.Error:
                self.report('VLC cannot play this file.')
            elif was_playing:
                self.position = self.duration
                self.show_still()
                self.redraw()
            return
        if state not in (vlc.State.Playing, vlc.State.Paused):
            return

        # play() or pause() may have been asked while the file was still opening.
        if not self.playing:
            if state == vlc.State.Playing:
                self.player.set_pause(1)
            return
        if state == vlc.State.Paused:
            self.player.set_pause(0)
        if self._pending is not None:
            self.player.set_time(int(self._pending * 1000))
            self._pending = None
        if not self._volume_set:
            self._volume_set = self.player.audio_set_volume(int(self.volume.get())) == 0

        reported = self.player.get_time() / 1000
        if self._target:
            asked, when = self._target
            if abs(reported - asked) > 0.75 and now - when < 1.5:
                return  # still reporting the position from before the seek
            self._target = None
            self._anchor = (max(reported, 0.0), now)
        elif abs(reported - self._anchor[0]) > 1e-4:
            self._anchor = (reported, now)
            if self._reveal and self.player.has_vout():
                self._reveal = False
                self.screen.master.tk.call('raise', self.screen._w)
        self.position = min(self._anchor[0] + min(now - self._anchor[1], 0.35), self.duration)

        skips = [self._preview_skip] if self._preview_skip else []
        if self.skip_var.get() and self._range_end is None:
            skips = self.skips
        for start, end in skips:
            if start <= self.position < end - 0.05:
                if end >= self.duration - 0.05:
                    self._range_end = self._preview_skip = None
                    self.pause()
                else:
                    if self._preview_skip:
                        self._preview_skip = None
                    self.seek(end, keep_preview=True)
                break
        else:
            if self._range_end is not None and self.position >= self._range_end:
                self.position, self._range_end = self._range_end, None
                self._preview_skip = None
                self.pause()
        self.redraw()

    def _set_volume(self, _value=None):
        if self.player:
            self.player.audio_set_volume(int(self.volume.get()))

    # ---------- Timeline ----------
    def set_marks(self, bands, skips=(), stops=()):
        self.bands, self.skips, self.stops = list(bands), list(skips), list(stops)
        self.redraw()

    def _x(self, seconds):
        width = max(1, self.line.winfo_width() - 16)
        return 8 + width * min(1.0, max(0.0, seconds / self.duration if self.duration else 0.0))

    def _scrub(self, event):
        if self.path:
            width = max(1, self.line.winfo_width() - 16)
            self.seek((event.x - 8) / width * self.duration)

    def redraw(self):
        self.line.delete('all')
        self.line.create_rectangle(8, 17, self.line.winfo_width() - 8, 27, fill=BORDER, width=0)
        for start, end, colour in self.bands:
            left = self._x(start)
            self.line.create_rectangle(left, 10, max(left + 3, self._x(end)), 34, fill=colour, width=0)
        if self.path:
            x = self._x(self.position)
            self.line.create_rectangle(8, 19, x, 25, fill=ACCENT, width=0)
            self.line.create_line(x, 3, x, 41, fill=TEXT, width=2)
        else:
            self.line.create_text(14, 7, anchor='nw', fill=MUTED, text='Timeline')
        self.clock.config(text=f'{format_time(self.position)} / {format_time(self.duration)}')

    # ---------- Paused picture ----------
    def show_still(self):
        self._request += 1
        # At the very end there is no frame any more: show the last one.
        when = min(self.position, max(0.0, self.duration - 0.001))
        self.stills.request((self._generation, self._request), self.path, when)

    def _take_stills(self):
        while not self.stills.results.empty():
            key, image, error = self.stills.results.get()
            # An older frame of this file is still worth showing while dragging.
            if key[0] != self._generation or self.playing:
                continue
            self._still_image = image
            self._draw_still()
            if image is None:
                self.still.config(text=f'No picture: {error}' if error else 'Audio only')
            self.still.lift()

    def _draw_still(self):
        if self._still_image is None:
            self._still = None
            self.still.config(image='')
            return
        width, height = self.still.winfo_width(), self.still.winfo_height()
        scale = min(width / self._still_image.width, height / self._still_image.height)
        size = (max(1, int(self._still_image.width * scale)),
                max(1, int(self._still_image.height * scale)))
        self._still = ImageTk.PhotoImage(self._still_image.resize(size))
        self.still.config(image=self._still, text='')

    def shutdown(self):
        self.stills.stop()
        if self.player:
            self.player.stop()
            self.player.release()
            self.instance.release()
            self.player = None
