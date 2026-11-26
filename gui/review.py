"""Step 3: go through the videos one by one, and check, adjust or add their cuts."""

import os
import tkinter as tk
from tkinter import simpledialog, ttk

from gui.common import (ACCENT, BORDER, CHECKED, CUT, DISABLED_CUT, FIELD, MUTED, TEXT,
                        UNCHECKED, WARNING, format_time, make_tree, on_check, parse_time, shorten)
from gui import reviews
from gui.state import SAVED
from pipeline import Cut

NEW_CUT_SECONDS = 10.0
MIN_CUT = 0.1  # seconds
GRAB = 7  # pixels around an edge that pick it up
NUDGES = (('−1s', -1.0), ('−0.1', -0.1), ('◂f', 'back'), ('f▸', 'forward'), ('+0.1', 0.1), ('+1s', 1.0))
TICK_STEPS = (0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800)


def _tick_label(seconds, step):
    minutes, rest = divmod(seconds + 1e-6, 60)
    return f'{int(minutes)}:{rest:04.1f}' if step < 1 else f'{int(minutes)}:{int(rest):02d}'


class CutEditor(tk.Canvas):
    """
    A zoomed timeline around the selected cut. Drag its edges to move them,
    click elsewhere to move the playhead, scroll to zoom.
    """

    def __init__(self, parent, panel):
        super().__init__(parent, height=74, bg=FIELD, highlightthickness=1,
                         highlightbackground=BORDER, cursor='hand2')
        self.panel = panel
        self.view = (0.0, 1.0)
        self._drag = None  # 0 = start, 1 = end, 'seek'
        self.bind('<Configure>', lambda _event: self.draw())
        self.bind('<Motion>', self._hover)
        self.bind('<Button-1>', self._press)
        self.bind('<B1-Motion>', self._move)
        self.bind('<ButtonRelease-1>', self._release)
        self.bind('<MouseWheel>', lambda event: self._zoom(event.x, 0.8 if event.delta > 0 else 1.25))
        self.bind('<Button-4>', lambda event: self._zoom(event.x, 0.8))
        self.bind('<Button-5>', lambda event: self._zoom(event.x, 1.25))

    # ---------- View ----------
    @property
    def duration(self):
        episode = self.panel.episode
        return episode.duration if episode else 0.0

    def fit(self, cut=None):
        if not self.duration:
            self.view = (0.0, 1.0)
        elif cut:
            pad = min(60.0, max(4.0, (cut.end - cut.start) * 0.35))
            self.view = (max(0.0, cut.start - pad), min(self.duration, cut.end + pad))
        else:
            self.view = (0.0, self.duration)
        self.draw()

    def _zoom(self, x, factor):
        if not self.duration:
            return
        at = self._time(x)
        low, high = self.view
        span = min(self.duration, max(1.0, (high - low) * factor))
        low = at - (at - low) * span / (high - low)
        low = min(max(0.0, low), self.duration - span)
        self.view = (low, low + span)
        self.draw()

    def _x(self, seconds):
        low, high = self.view
        width = max(1, self.winfo_width() - 20)
        return 10 + width * (seconds - low) / max(1e-6, high - low)

    def _time(self, x):
        low, high = self.view
        width = max(1, self.winfo_width() - 20)
        return min(max(0.0, low + (x - 10) / width * (high - low)), self.duration)

    # ---------- Drawing ----------
    def draw(self):
        self.delete('all')
        width = self.winfo_width()
        episode, selected = self.panel.episode, self.panel.cut
        if not episode or not self.duration:
            self.create_text(12, 10, anchor='nw', fill=MUTED, text='No video selected')
            return
        low, high = self.view
        span = high - low

        step = next((s for s in TICK_STEPS if span / s <= max(2, width / 75)), TICK_STEPS[-1])
        tick = (low // step) * step
        while tick <= high:
            if tick >= low:
                x = self._x(tick)
                self.create_line(x, 54, x, 60, fill=BORDER)
                self.create_text(x, 62, anchor='n', fill=MUTED, text=_tick_label(tick, step),
                                 font=('TkDefaultFont', 8))
            tick += step
        self.create_rectangle(10, 26, width - 10, 34, fill=BORDER, width=0)

        for cut in sorted(episode.cuts, key=lambda c: c is selected):
            if cut.end < low or cut.start > high:
                continue
            colour = WARNING if cut is selected else (CUT if cut.enabled else DISABLED_CUT)
            left, right = self._x(max(cut.start, low)), self._x(min(cut.end, high))
            self.create_rectangle(left, 14, max(left + 2, right), 46, fill=colour,
                                  stipple='' if cut.enabled else 'gray50', width=0)
            if right - left > 60:
                self.create_text((left + right) / 2, 30, fill='black',
                                 text=f'{cut.end - cut.start:.2f}s', font=('TkDefaultFont', 8, 'bold'))
        if selected:
            for when in (selected.start, selected.end):
                if low <= when <= high:
                    x = self._x(when)
                    self.create_line(x, 6, x, 52, fill=TEXT, width=2)
                    self.create_rectangle(x - 4, 4, x + 4, 12, fill=TEXT, width=0)

        player = self.panel.app.player
        if player.path == episode.path and low <= player.position <= high:
            x = self._x(player.position)
            self.create_line(x, 2, x, 54, fill=ACCENT, width=2)

    # ---------- Mouse ----------
    def _edge_at(self, x):
        cut = self.panel.cut
        if not cut or self.panel.app.tasks.busy:
            return None
        distances = [(abs(self._x(when) - x), index) for index, when in enumerate((cut.start, cut.end))]
        distance, index = min(distances)
        return index if distance <= GRAB else None

    def _hover(self, event):
        self.config(cursor='sb_h_double_arrow' if self._edge_at(event.x) is not None else 'hand2')

    def _press(self, event):
        self._drag = self._edge_at(event.x)
        if self._drag is not None:
            self.panel.app.player.pause()
            return
        at = self._time(event.x)
        if 14 <= event.y <= 46:
            hit = [c for c in self.panel.episode.cuts if c.start <= at <= c.end] if self.panel.episode else []
            if hit and self.panel.cut not in hit:
                self.panel.select_cut(hit[0], seek=False, refit=False)
        self._drag = 'seek'
        self.panel.seek(at)

    def _move(self, event):
        if self._drag == 'seek':
            self.panel.seek(self._time(event.x))
        elif self._drag is not None:
            self.panel.drag_edge(self._drag, self._time(event.x))

    def _release(self, _event):
        if self._drag in (0, 1):
            self.panel.commit()
        self._drag = None


class ReviewPanel(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, padding=10)
        self.app = app
        self.episode = self.cut = None
        self._visible = []
        self.rows = {}
        self._labels = []
        self._last_position = None
        self._build()
        self._watch_playhead()

    def _build(self):
        row = ttk.Frame(self)
        row.pack(fill='x')
        self.prev_button = ttk.Button(row, text='◀', width=3, command=lambda: self.step_video(-1))
        self.prev_button.pack(side='left')
        self.choice = tk.StringVar()
        self.chooser = ttk.Combobox(row, textvariable=self.choice, state='readonly', width=10)
        self.chooser.pack(side='left', fill='x', expand=True, padx=5)
        self.chooser.bind('<<ComboboxSelected>>', self._chosen)
        self.next_button = ttk.Button(row, text='▶', width=3, command=lambda: self.step_video(1))
        self.next_button.pack(side='left')
        self.info = ttk.Label(self, style='Muted.TLabel', wraplength=500, justify='left')
        self.info.pack(fill='x', pady=(5, 4))
        self.stale = ttk.Label(self, style='Warning.TLabel', wraplength=500)

        bottom = ttk.Frame(self)
        bottom.pack(side='bottom', fill='x')
        box = ttk.LabelFrame(bottom, text='Selected cut: drag its edges or nudge them, scroll to zoom', padding=8)
        box.pack(fill='x', pady=(8, 6))
        self.editor = CutEditor(box, self)
        self.editor.pack(fill='x', pady=(0, 8))
        buttons = ttk.Frame(box)
        buttons.pack(fill='x')
        self.start, self.end = tk.StringVar(), tk.StringVar()
        self.controls = []
        for index, (text, variable) in enumerate((('Start', self.start), ('End', self.end))):
            ttk.Label(buttons, text=text, width=5).grid(row=index, column=0, sticky='w')
            entry = ttk.Entry(buttons, textvariable=variable, width=11)
            entry.grid(row=index, column=1, pady=2, padx=(0, 4))
            entry.bind('<Return>', self._entered)
            entry.bind('<FocusOut>', self._entered)
            self.controls.append(entry)
            for column, (label, amount) in enumerate(NUDGES, start=2):
                button = ttk.Button(buttons, text=label, width=len(label) + 1, style='Transport.TButton',
                                    command=lambda index=index, amount=amount: self.nudge(index, amount))
                button.grid(row=index, column=column, padx=1)
                self.controls.append(button)
            button = ttk.Button(buttons, text=f'Here ({"IO"[index]})', style='Transport.TButton',
                                command=lambda index=index: self.set_edge(index))
            button.grid(row=index, column=len(NUDGES) + 2, padx=(4, 0))
            self.controls.append(button)
        row = ttk.Frame(box)
        row.pack(fill='x', pady=(7, 0))
        for text, command in (('▶ What is removed', self.play_removed),
                              ('▶ Result at this cut', self.play_result),
                              ('Delete', self.delete)):
            button = ttk.Button(row, text=text, command=command)
            button.pack(side='left', padx=(0, 5))
            self.controls.append(button)
        self.toggle_button = ttk.Button(row, text='Untick', command=lambda: self.toggle_cut(self.cut))
        self.toggle_button.pack(side='left')
        self.controls.append(self.toggle_button)

        row = ttk.Frame(bottom)
        row.pack(fill='x')
        self.add_button = ttk.Button(row, text='+ Add cut at playhead', command=self.add)
        self.add_button.pack(side='left')
        self.sample_button = ttk.Button(row, text='Save as sample…', command=self.save_as_sample)
        self.sample_button.pack(side='left', padx=5)
        self.next_match_button = ttk.Button(row, text='⇄ Next best match', command=self.next_match)
        self.next_match_button.pack(side='right')

        # The decision on this video.
        row = ttk.Frame(bottom)
        row.pack(fill='x', pady=(8, 0))
        self.done_button = ttk.Button(row, text='✓ Validate, reviewed', style='Primary.TButton',
                                      command=self.reviewed)
        self.done_button.pack(side='right', anchor='s')
        rescan = ttk.Frame(row)
        rescan.pack(side='left')
        self.rescan_now_button = ttk.Button(rescan, text='↻ Re-scan now', command=self.rescan_now)
        self.rescan_now_button.grid(row=0, column=0, sticky='w')
        self.rescan_button = ttk.Button(rescan, text='Needs re-scan', command=self.needs_rescan)
        self.rescan_button.grid(row=0, column=1, sticky='w', padx=5)
        self.keep_selected = tk.BooleanVar(value=False)
        self.keep_check = ttk.Checkbutton(rescan, text='Keep selected samples (search only new ones)',
                                          variable=self.keep_selected)
        self.keep_check.grid(row=1, column=0, columnspan=2, sticky='w', pady=(4, 0))

        frame, self.tree = make_tree(
            self, [('start', 'Start', 78, 'e'), ('end', 'End', 78, 'e'),
                   ('length', 'Length', 58, 'e'), ('match', 'Match', 112, 'w')],
            heading='Cut', width=150, height=4, show='tree headings')
        frame.pack(fill='both', expand=True)
        on_check(self.tree, lambda row: self.toggle_cut(self.rows.get(row)))
        self.tree.bind('<<TreeviewSelect>>', self._selected)

    # ---------- Choosing the video ----------
    def visible(self):
        """The videos in the list: never the reviewed ones (step 2 can unreview them)."""
        return [e for e in self.app.episodes if not e.reviewed]

    def _chosen(self, _event=None):
        index, episodes = self.chooser.current(), self._visible
        if 0 <= index < len(episodes):
            self.show_episode(episodes[index])

    def step_video(self, direction):
        episodes = self.visible()
        if not episodes:
            return
        index = episodes.index(self.episode) if self.episode in episodes else -direction
        index += direction
        if 0 <= index < len(episodes):
            self.show_episode(episodes[index])

    def reviewed(self):
        """Validate this video: kept with its cuts, never shown or searched again."""
        episode = self.episode
        if not episode:
            return
        episode.reviewed, episode.needs_rescan = True, False
        self._done_with(episode, 'Every video is reviewed. Next: export.')

    def needs_rescan(self):
        """Send the video back to step 2, searched by the next "Find matches"."""
        episode = self.episode
        if not episode or self.app.tasks.busy:
            return
        if self._send_back(episode):
            note = ('ticked cuts kept. Only samples it was never searched with will be used: add one, '
                    'then "Find matches" in step 2.')
        else:
            note = 'it will be searched again with every ticked sample by "Find matches" in step 2.'
        self._done_with(episode, f'{episode.name}: {note}')

    def rescan_now(self):
        """Search this video again right away, then show it again."""
        episode = self.episode
        if not episode or self.app.tasks.busy:
            return
        videos = self.app.panels['videos']
        settings = videos.settings()
        if settings and self.keep_selected.get():
            used = episode.used_samples()
            if not [path for path, _ in settings[0] if path not in used]:
                self.app.report('No new sample for this video: save a cut as a sample, '
                                'or add one in step 1, then re-scan.')
                return
        self._send_back(episode)
        episode.status = episode.describe()
        self.app.changed()
        if videos.scan(only=episode) is False:
            return
        self.app.report(f'Searching {episode.name} again…')

    def _send_back(self, episode):
        """
        Mark the video for a new search. With "Keep selected samples" (True
        returned), its ticked cuts are kept, the unticked ones dropped, and the
        samples it was already searched with are never used on it again: only
        newer samples are searched, so dropped suggestions cannot come back.
        Without it, everything is searched again.
        """
        episode.reviewed, episode.needs_rescan = False, True
        episode.output, episode.junctions = '', []
        if not self.keep_selected.get():
            episode.skip_samples = None
            return False
        episode.skip_samples = episode.used_samples()
        episode.cuts = [c for c in episode.cuts if c.enabled or not c.sample]
        self.cut = None
        return True

    def save_as_sample(self):
        """Save the selected cut, as adjusted, as a new sample of the current set."""
        cut, episode = self.cut, self.episode
        if not cut or not episode or self.app.tasks.busy:
            return
        base = os.path.splitext(os.path.basename(cut.sample))[0] if cut.sample else 'cut'
        name = simpledialog.askstring(
            'Save as sample', f'Name of the new sample ({cut.end - cut.start:.2f} s, '
            f'{format_time(cut.start)} – {format_time(cut.end)}):', initialvalue=f'{base}_edited', parent=self)
        if not name or not name.strip():
            return
        samples = self.app.panels['samples']
        samples.add_sample(episode.path, cut.start, cut.end, name.strip(), saved=lambda _path: self.app.report(
            f'{os.path.basename(_path)} saved and ticked. "↻ Re-scan now" searches this video with it '
            '(tick "Keep selected samples" to keep the cuts you already have).'))

    def _done_with(self, episode, finished_text):
        """Save the decision, then open the next video still to review."""
        episode.status = episode.describe()
        try:
            reviews.save(episode)
        except OSError as exc:
            self.app.report(f'Could not be saved for next time: {exc}')
        episodes = self.app.episodes
        index = episodes.index(episode)
        later = episodes[index + 1:] + episodes[:index]
        following = next((e for e in later if not e.reviewed and not e.needs_rescan), None)
        if following:
            self.show_episode(following)
            return
        self.app.report(finished_text)
        if episode.reviewed:
            # Nothing left to review: a reviewed video is never shown again.
            self.episode = self.cut = None
            self.app.show('export')
        self.app.changed()

    def show_episode(self, episode):
        self.episode = episode
        cut = next((c for c in episode.cuts if c.enabled), episode.cuts[0] if episode.cuts else None)
        self.app.changed()
        self.select_cut(cut)

    # ---------- Choosing the cut ----------
    def _selected(self, _event=None):
        selection = self.tree.selection()
        cut = self.rows.get(selection[0]) if selection else None
        if cut is not None and cut is not self.cut:
            self.select_cut(cut)

    def select_cut(self, cut, seek=True, refit=True):
        self.cut = cut
        if refit:
            self.editor.fit(cut)
        self._fill()
        self._select_row()
        if self.app.active() is not self or not self.episode:
            return
        player = self.app.player
        position = cut.start if cut else 0.0
        if player.path != self.episode.path:
            if not self.app.load(self.episode.path, position):
                return
        elif seek and cut:
            player.pause()
            player.seek(position)
        self.draw()

    def _select_row(self):
        row = next((row for row, cut in self.rows.items() if cut is self.cut), None)
        if row:
            if self.tree.selection() != (row,):
                self.tree.selection_set(row)
            self.tree.see(row)
        elif self.tree.selection():
            self.tree.selection_remove(*self.tree.selection())

    def ensure_loaded(self):
        if not self.episode:
            return False
        if self.app.player.path != self.episode.path:
            if not self.app.load(self.episode.path):
                return False
            self.draw()
        return True

    def seek(self, position):
        if self.ensure_loaded():
            self.app.player.pause()
            self.app.player.seek(position)
            self.editor.draw()

    def draw(self):
        episode, player = self.episode, self.app.player
        self.editor.draw()
        if not episode or player.path != episode.path or self.app.active() is not self:
            return
        bands = [(c.start, c.end, DISABLED_CUT) for c in episode.cuts if not c.enabled]
        bands += [(c.start, c.end, WARNING if c is self.cut else CUT) for c in episode.cuts if c.enabled]
        player.set_marks(bands, sorted(episode.segments), [c.start for c in episode.cuts if c.enabled])

    def _watch_playhead(self):
        self.after(60, self._watch_playhead)
        position = self.app.player.position
        if position != self._last_position and self.winfo_ismapped():
            self._last_position = position
            self.editor.draw()

    # ---------- Editing ----------
    def _fill(self):
        cut, busy = self.cut, self.app.tasks.busy
        self.start.set(format_time(cut.start) if cut else '')
        self.end.set(format_time(cut.end) if cut else '')
        state = 'normal' if cut and not busy else 'disabled'
        for control in self.controls:
            control.config(state=state)
        self.toggle_button.config(text='Untick' if cut and cut.enabled else 'Tick')
        self.add_button.config(state='normal' if self.episode and not busy else 'disabled')
        more = cut and self.episode and len(self.episode.alternatives(cut)) > 1 and not busy
        self.next_match_button.config(state='normal' if more else 'disabled')
        self.sample_button.config(state='normal' if cut and not busy else 'disabled')
        for widget in (self.done_button, self.rescan_button, self.rescan_now_button, self.keep_check):
            widget.config(state='normal' if self.episode and not busy else 'disabled')

    def toggle_cut(self, cut):
        if self.app.tasks.busy or cut is None or not self.episode:
            return
        cut.enabled, cut.edited = not cut.enabled, True
        self._edited()

    def _entered(self, _event=None):
        if not self.cut or self.app.tasks.busy:
            return
        # Unchanged fields must not round the exact matched times to the display's.
        if (self.start.get(), self.end.get()) == (format_time(self.cut.start), format_time(self.cut.end)):
            return
        try:
            self.set_times(parse_time(self.start.get()), parse_time(self.end.get()))
        except ValueError as exc:
            self.app.report(str(exc))
            self._fill()

    def set_times(self, start, end, commit=True):
        start, end = max(0.0, start), min(end, self.episode.duration)
        if end - start < MIN_CUT:
            raise ValueError('The end must be after the start.')
        self.cut.start, self.cut.end, self.cut.edited = start, end, True
        if commit:
            self._edited()

    def nudge(self, index, amount):
        if not self.cut or self.app.tasks.busy:
            return
        if amount in ('back', 'forward'):
            fps = self.app.player.fps if self.app.player.path == self.episode.path else 25.0
            amount = (1 if amount == 'forward' else -1) / fps
        times = [self.cut.start, self.cut.end]
        times[index] += amount
        try:
            self.set_times(*times)
        except ValueError as exc:
            self.app.report(str(exc))
            return
        self._keep_in_view(times[index])
        if self.ensure_loaded():
            self.app.player.seek(times[index])

    def drag_edge(self, index, when):
        """While an edge is being dragged: cheap updates only."""
        times = [self.cut.start, self.cut.end]
        if index == 0:
            times[0] = min(when, times[1] - MIN_CUT)
        else:
            times[1] = max(when, times[0] + MIN_CUT)
        self.set_times(*times, commit=False)
        self.start.set(format_time(self.cut.start))
        self.end.set(format_time(self.cut.end))
        row = next((row for row, cut in self.rows.items() if cut is self.cut), None)
        if row:
            self.tree.set(row, 'start', format_time(self.cut.start))
            self.tree.set(row, 'end', format_time(self.cut.end))
            self.tree.set(row, 'length', f'{self.cut.end - self.cut.start:.1f}s')
        if self.ensure_loaded():
            self.app.player.seek(times[index])
        self.draw()

    def commit(self):
        if self.cut and self.episode:
            self._edited()

    def _keep_in_view(self, when):
        low, high = self.editor.view
        if not low <= when <= high:
            span = high - low
            low = min(max(0.0, when - span / 2), max(0.0, self.episode.duration - span))
            self.editor.view = (low, low + span)

    def set_edge(self, index):
        if not self.cut or self.app.tasks.busy:
            return
        if self.app.player.path != self.episode.path:
            self.app.report('Select the cut again to show its video first.')
            return
        times = [self.cut.start, self.cut.end]
        times[index] = self.app.player.position
        try:
            self.set_times(*times)
        except ValueError as exc:
            self.app.report(str(exc))

    def add(self):
        if not self.episode or self.app.tasks.busy or not self.ensure_loaded():
            return
        start = self.app.player.position
        end = min(start + NEW_CUT_SECONDS, self.episode.duration)
        if end - start < MIN_CUT:
            self.app.report('Move the playhead back from the end first.')
            return
        self.cut = Cut(start, end, edited=True)
        self.episode.cuts.append(self.cut)
        self.editor.fit(self.cut)
        self._edited()
        self.app.report('Cut added. Drag its end in the box below, or move the playhead there and press O.')

    def next_match(self):
        """Move the selected cut to the next place its sample was heard, best first, then round."""
        cut, episode = self.cut, self.episode
        if not cut or not episode or self.app.tasks.busy:
            return
        places = episode.alternatives(cut)
        if len(places) < 2:
            self.app.report('The search found this sample nowhere else in this video.')
            return
        here = next((i for i, c in enumerate(places) if abs(c.start - cut.start) < 0.5), -1)
        index = (here + 1) % len(places)
        place = places[index]
        cut.start, cut.end, cut.score, cut.waveform = place.start, place.end, place.score, place.waveform
        cut.edited = False
        self.editor.fit(cut)
        self._edited()
        self.select_cut(cut, refit=False)
        name = os.path.splitext(os.path.basename(cut.sample))[0]
        self.app.report(f'{name}: match {index + 1} of {len(places)}, {cut.waveform:.0%} at '
                        f'{format_time(cut.start)}. Click again for the next one.')

    def delete(self):
        if self.cut and not self.app.tasks.busy:
            self.episode.cuts.remove(self.cut)
            self.cut = None
            self._edited()

    def _edited(self):
        episode = self.episode
        episode.cuts.sort(key=lambda cut: cut.start)
        episode.output, episode.junctions = '', []
        count = len(episode.segments)
        if episode.reviewed or episode.needs_rescan:
            # Changes to a video already decided on are kept too.
            episode.status = episode.describe()
            try:
                reviews.save(episode)
            except OSError as exc:
                self.app.report(f'This change could not be saved for next time: {exc}')
        elif episode.ready:
            episode.status = f'{count} cut{"s" * (count != 1)}' if count else 'No cuts'
        else:
            episode.status = 'Not scanned'
        self.app.changed()

    def play_removed(self):
        if self.cut and self.ensure_loaded():
            self.app.player.play_range(self.cut.start, self.cut.end)

    def play_result(self):
        if self.cut and self.ensure_loaded():
            self.app.player.preview_without(self.cut.start, self.cut.end)

    def _overlaps(self):
        """Pairs of ticked cuts of this video that overlap."""
        cuts = sorted((c for c in self.episode.cuts if c.enabled), key=lambda c: c.start) if self.episode else []
        return [(a, b) for i, a in enumerate(cuts) for b in cuts[i + 1:] if b.start < a.end - 0.001]

    # ---------- Called by the app ----------
    def activate(self):
        if self.episode and not self.episode.reviewed:
            self.select_cut(self.cut, seek=False, refit=False)
            return
        first = self._first_to_review()
        if first:
            self.show_episode(first)
        else:
            self.episode = self.cut = None
            self.app.changed()

    def _first_to_review(self):
        """The first video not reviewed, preferably one not sent back for a new search."""
        todo = [e for e in self.app.episodes if not e.reviewed]
        return next((e for e in todo if not e.needs_rescan), todo[0] if todo else None)

    def refresh(self):
        episodes = self.app.episodes
        if self.episode not in episodes or (self.episode and self.episode.reviewed):
            self.episode = self.cut = None
        if not self.episode and self.app.active() is self and self._first_to_review():
            # Open on the next video to review, e.g. when a search ends while this tab is shown.
            self.after_idle(lambda: self.episode or self.show_episode(self._first_to_review()))
        # Cut compares by value, so identity has to be checked.
        if self.cut and not (self.episode and any(c is self.cut for c in self.episode.cuts)):
            self.cut = None

        self._visible = visible = self.visible()
        self._labels = []
        for number, episode in enumerate(visible, start=1):
            mark = '✓' if episode.reviewed else '↻' if episode.needs_rescan else '•'
            note = f'{len(episode.segments)}/{len(episode.cuts)} cuts' if episode.ready else episode.status
            self._labels.append(f'{mark} {number}/{len(visible)}  {shorten(episode.name, 40)}  ({note})')
        self.chooser.config(values=self._labels)
        if self.episode:
            self.chooser.current(visible.index(self.episode))
        else:
            self.choice.set('Add videos and find matches first' if not episodes else
                            'Every video is reviewed' if not visible else 'Choose a video')
        index = visible.index(self.episode) if self.episode else -1
        self.prev_button.config(state='normal' if index > 0 else 'disabled')
        self.next_button.config(state='normal' if index < len(visible) - 1 else 'disabled')

        self.tree.delete(*self.tree.get_children())
        self.rows = {}
        for index, cut in enumerate(self.episode.cuts if self.episode else []):
            row, tags = f'c{index}', ()
            if not cut.sample:
                match = 'by hand'
            else:
                match = f'{max(0, cut.waveform):.0%}' + (' kept' if self.episode.kept(cut) else
                                                          ' edited' if cut.edited else '')
                places = self.episode.alternatives(cut)
                if len(places) > 1:
                    rank = next((i for i, c in enumerate(places) if abs(c.start - cut.start) < 0.5), None)
                    match += f'  {rank + 1}/{len(places)}' if rank is not None else f'  ·/{len(places)}'
                tags = ('warning',) if cut.doubtful else ()
            if not cut.enabled:
                tags = ('off',)
            name = os.path.splitext(os.path.basename(cut.sample))[0] if cut.sample else 'Manual cut'
            self.tree.insert('', 'end', iid=row, tags=tags,
                             text=f'{CHECKED if cut.enabled else UNCHECKED}  {shorten(name, 16)}',
                             values=(format_time(cut.start), format_time(cut.end),
                                     f'{cut.end - cut.start:.1f}s', match))
            self.rows[row] = cut
        self._select_row()

        reviewed = sum(e.reviewed for e in episodes)
        if self.episode:
            text = (f'{len(self.episode.segments)} of {len(self.episode.cuts)} cut(s) ticked in this video. '
                    f'{reviewed} of {len(episodes)} video(s) reviewed. '
                    'Click a tick to change it, or a row to adjust it below.')
            if self.episode.needs_rescan:
                text = (f'↻ {self.episode.describe()}: sent back to step 2. '
                        'Validate it instead if these cuts are right.')
            elif not self.episode.ready:
                text = f'{self.episode.status}. Run "Find matches" in step 2, or add cuts by hand.'
        elif episodes and not visible:
            text = (f'All {len(episodes)} videos are reviewed. To review one again, select it in '
                    'step 2 and click "Unreview".')
        else:
            text = 'Videos are reviewed one at a time. Choose one above.'
        overlaps = self._overlaps()
        if overlaps:
            first, second = overlaps[0]
            text = (f'⚠ Ticked cuts overlap: {format_time(first.start)} – {format_time(first.end)} and '
                    f'{format_time(second.start)} – {format_time(second.end)}. '
                    'Untick one or move its edge.')
        self.info.config(text=text, style='Warning.TLabel' if overlaps else 'Muted.TLabel')

        settings = self.app.panels['videos'].settings()
        if any(e.scanned_with not in (None, settings, SAVED) and not e.reviewed for e in episodes):
            self.stale.config(text='Samples or settings changed since the search. '
                                   'Run "Find matches" again to update these results.')
            self.stale.pack(fill='x', pady=(0, 6), before=self.tree.master)
        else:
            self.stale.pack_forget()
        self._fill()
        self.draw()
