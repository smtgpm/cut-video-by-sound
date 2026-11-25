"""Step 2: choose the videos and search them for the samples."""

import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from gui import reviews
from gui.common import make_tree, shorten
from gui.state import Episode
from pipeline import VIDEO_EXTENSIONS, default_workers, expand_video_inputs, media_info, scan_video
from remove_segments import Cancelled
from subtitles import find_sidecars


class VideosPanel(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, padding=10)
        self.app = app
        self._build()

    def _build(self):
        row = ttk.Frame(self)
        row.pack(fill='x')
        self.editing = []
        for text, command in (('Add folder…', self.ask_folder), ('Add files…', self.ask_files),
                              ('Remove', self.remove), ('Clear', self.clear)):
            button = ttk.Button(row, text=text, command=command)
            button.pack(side='left', padx=(0, 5))
            self.editing.append(button)
        ttk.Label(self, style='Muted.TLabel',
                  text='A folder adds every video in it and in its subfolders. Double-click to play. '
                       'Reviewed videos are kept apart and not searched again.',
                  wraplength=500, justify='left',
                  ).pack(anchor='w', pady=6)

        bottom = ttk.Frame(self)
        bottom.pack(side='bottom', fill='x')
        box = ttk.LabelFrame(bottom, text='Search settings', padding=8)
        box.pack(fill='x', pady=8)
        self.threshold = tk.StringVar(value='0.72')
        self.intro_max = tk.StringVar(value='180')
        self.workers = tk.StringVar(value=str(default_workers()))
        self.min_match = tk.StringVar(value='50')
        self.several = tk.BooleanVar(value=False)
        for index, (text, variable, low, high, step, note) in enumerate((
            ('Threshold', self.threshold, 0.3, 0.99, 0.01, 'higher is stricter'),
            ('Intro expected before', self.intro_max, 10, 3600, 10, 'seconds, only used for ranking'),
            ('Hide matches below', self.min_match, 0, 99, 5, '% match, never shown in the review'),
            ('Videos at once', self.workers, 1, 8, 1, 'about 1.3 GB of RAM each'),
        )):
            ttk.Label(box, text=text).grid(row=index, column=0, sticky='w', pady=2)
            spin = ttk.Spinbox(box, textvariable=variable, from_=low, to=high, increment=step, width=7)
            spin.grid(row=index, column=1, padx=6)
            ttk.Label(box, text=note, style='Muted.TLabel').grid(row=index, column=2, sticky='w')
        ttk.Checkbutton(box, text='A sample may occur several times per video',
                        variable=self.several).grid(row=4, column=0, columnspan=3, sticky='w', pady=(4, 0))
        for variable in (self.threshold, self.intro_max, self.min_match, self.several):
            variable.trace_add('write', lambda *_: self.app.changed())

        row = ttk.Frame(bottom)
        row.pack(fill='x')
        self.summary = ttk.Label(row, style='Muted.TLabel', width=1)
        self.summary.pack(side='left', fill='x', expand=True)
        self.scan_button = ttk.Button(row, text='Find matches →', style='Primary.TButton', command=self.scan)
        self.scan_button.pack(side='right')

        frame, self.tree = make_tree(
            self, [('length', 'Length', 62, 'e'), ('subs', 'Sub files', 66, 'center'),
                   ('status', 'Status', 125, 'w')],
            heading='Video', width=190, height=5, selectmode='extended')
        frame.pack(fill='both', expand=True)
        self.tree.bind('<Double-1>', lambda _event: self.play())

    def ask_folder(self):
        folder = filedialog.askdirectory(parent=self, initialdir=self.app.browse_dir, title='Folder containing videos')
        if folder:
            self.app.browse_dir = folder
            self.add([folder])

    def ask_files(self):
        patterns = ' '.join(f'*{ext} *{ext.upper()}' for ext in sorted(VIDEO_EXTENSIONS))
        paths = filedialog.askopenfilenames(parent=self, initialdir=self.app.browse_dir, title='Choose videos',
                                            filetypes=[('Videos', patterns), ('All files', '*')])
        if paths:
            self.app.browse_dir = os.path.dirname(paths[0])
            self.add(paths)

    def add(self, inputs):
        try:
            found = expand_video_inputs(inputs)
        except OSError as exc:
            messagebox.showerror('Add videos', str(exc), parent=self)
            return
        known = {episode.path for episode in self.app.episodes}
        new = [item for item in found if item[0] not in known]
        if not new:
            text = ('All the videos there are already in the list.' if found else
                    'No video files were found there, nor in its subfolders.\n\n'
                    f'Recognised: {", ".join(sorted(VIDEO_EXTENSIONS))}')
            messagebox.showinfo('Add videos', text, parent=self)
            return
        self.app.report(f'Reading {len(new)} video(s)…')
        added, failed = {}, []

        def work(item, progress, _log, _cancelled):
            path, subfolder = item
            duration, _fps = media_info(path)
            subtitles = len(find_sidecars(path, VIDEO_EXTENSIONS))
            progress(1.0)
            episode = Episode(path, subfolder, duration, subtitles)
            reviews.restore(episode)  # reviewed in an earlier session
            return episode

        def finished(item, episode, error):
            if error:
                failed.append(f'{os.path.basename(item[0])}: {str(error) or "cancelled"}')
            else:
                added[item] = episode

        def done(_cancelled):
            self.app.episodes.extend(added[item] for item in new if item in added)
            restored = sum(added[item].reviewed for item in new if item in added)
            if restored:
                self.app.report(f'{restored} video(s) were already reviewed: their cuts are back, '
                                'and they will not be searched again.')
            if failed:
                messagebox.showwarning('Some files were not added', '\n'.join(failed[:12]), parent=self)
            self.app.changed()

        self.app.run('Reading videos', new, work, 4, finished, done)

    def remove(self):
        if self.app.tasks.busy:
            return
        paths = set(self.tree.selection())
        self._drop([e for e in self.app.episodes if e.path in paths])

    def clear(self):
        if not self.app.tasks.busy:
            self._drop(list(self.app.episodes))

    def _drop(self, episodes):
        if not episodes:
            return
        if any(e.cuts for e in episodes) and not messagebox.askyesno(
                'Remove from the list',
                'Remove these videos from the list? No file is deleted.\n\n'
                'Videos you ticked off as reviewed keep their saved review: '
                'added again, they come back reviewed.', parent=self):
            return
        for episode in episodes:
            self.app.episodes.remove(episode)
        self.app.changed()

    def play(self):
        paths = {e.path for e in self.app.episodes}
        selection = [row for row in self.tree.selection() if row in paths]
        if selection:
            self.app.load(selection[0])

    def settings(self):
        """Everything a scan's results depend on."""
        try:
            threshold, intro_max = float(self.threshold.get()), float(self.intro_max.get())
            min_match = float(self.min_match.get()) / 100
        except ValueError:
            return None
        if not (0 < threshold <= 1 and 0 < intro_max < 1e6 and 0 <= min_match < 1):
            return None
        samples = []
        for path in self.app.panels['samples'].selected_samples():
            try:
                samples.append((path, os.path.getmtime(path)))
            except OSError:
                continue
        return tuple(samples), threshold, intro_max, bool(self.several.get()), min_match

    def worker_count(self):
        try:
            return min(8, max(1, int(self.workers.get())))
        except ValueError:
            return 1

    def scan(self):
        settings = self.settings()
        if settings is None:
            messagebox.showwarning('Find matches', 'The threshold, the intro time or the hidden match level is not valid.', parent=self)
            return
        if not settings[0]:
            messagebox.showwarning('Find matches', 'Save or tick at least one sample in step 1.', parent=self)
            self.app.show('samples')
            return
        if not self.app.episodes:
            return
        # Reviewed videos are done: only "Needs re-scan" in the review sends one back.
        episodes = [e for e in self.app.episodes if not e.reviewed]
        if not episodes:
            messagebox.showinfo(
                'Find matches', 'Every video is already reviewed, so there is nothing to search.\n\n'
                'To search one again, open it in step 3 and click "Needs re-scan".', parent=self)
            return
        if any(c.sample and c.edited for e in episodes for c in e.cuts) and not messagebox.askyesno(
                'Find matches',
                'Searching again replaces the matches you edited. Cuts added by hand are kept. Continue?',
                parent=self):
            return
        samples, threshold, intro_max, several, min_match = settings
        paths = [path for path, _ in samples]
        for episode in episodes:
            episode.status, episode.error = 'Queued', ''

        def work(episode, progress, _log, cancelled):
            return scan_video(episode.path, paths, threshold=threshold, intro_max=intro_max,
                              several_per_sample=several, min_waveform=min_match, progress=progress, cancelled=cancelled)

        def finished(episode, cuts, error):
            if isinstance(error, Cancelled):
                episode.status = 'Scan cancelled'
            elif error:
                episode.status, episode.error = 'Scan failed', str(error)
                # Old results must not pass for current ones.
                episode.scanned_with = None
            else:
                episode.cuts = sorted([c for c in episode.cuts if not c.sample] + cuts,
                                      key=lambda c: c.start)
                episode.scanned_with = settings
                episode.reviewed = episode.needs_rescan = False
                count = len(episode.segments)
                episode.status = f'{count} cut{"s" * (count != 1)}' if count else 'No match'
            self.app.changed()

        def done(cancelled):
            failures = [e for e in episodes if e.error]
            if failures:
                messagebox.showerror(
                    'Some scans failed',
                    '\n\n'.join(f'{e.name}: {e.error[-400:]}' for e in failures[:4]), parent=self)
            if not cancelled:
                self.app.show('review')

        self.app.run('Searching', episodes, work, self.worker_count(), finished, done)
        self.app.changed()

    def activate(self):
        pass

    def refresh(self):
        episodes, busy = self.app.episodes, self.app.tasks.busy
        selected = self.tree.selection()
        closed = {row for row in ('group:todo', 'group:done')
                  if self.tree.exists(row) and not self.tree.item(row, 'open')}
        self.tree.delete(*self.tree.get_children())
        todo = [e for e in episodes if not e.reviewed]
        done = [e for e in episodes if e.reviewed]
        groups = [('group:todo', f'To search and review ({len(todo)})', todo, ''),
                  ('group:done', f'Reviewed ✓ ({len(done)}), not searched again', done, 'good')]
        for group, title, members, tag in groups:
            if not members:
                continue
            self.tree.insert('', 'end', iid=group, text=title, open=group not in closed,
                             tags=(tag,) if tag else ())
            for episode in members:
                tags = ('error',) if episode.error else ('warning',) if episode.needs_rescan else ()
                self.tree.insert(group, 'end', iid=episode.path, text=shorten(episode.name, 26),
                                 values=(f'{int(episode.duration // 60)}:{int(episode.duration % 60):02d}',
                                         episode.subtitles or '–', episode.status),
                                 tags=tags)
        self.tree.selection_set([row for row in selected if self.tree.exists(row)])
        samples = len(self.app.panels['samples'].selected_samples())
        self.summary.config(text=f'{len(todo)} to search, {len(done)} reviewed, {samples} sample(s) ticked')
        for button in self.editing:
            button.config(state='disabled' if busy else 'normal')
        self.scan_button.config(text=f'Find matches in {len(todo)} →',
                                state='disabled' if busy or not todo else 'normal')
