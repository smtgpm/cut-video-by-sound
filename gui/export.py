"""Step 4: write the cleaned videos, then check them."""

import glob
import os
import tkinter as tk
from tkinter import messagebox, ttk

from gui import dialogs, reviews
from gui.common import (CHECKED, CUT, FIELD, TEXT, UNCHECKED, make_tree,
                        on_check, open_folder, shorten)
from gui.state import PROJECT, SAVED
from pipeline import export_video, plan_outputs, replace_video, replaced_path
from remove_segments import Cancelled
from timeline import Timeline

PRESETS = ('ultrafast', 'superfast', 'veryfast', 'faster', 'fast', 'medium', 'slow')


class ExportPanel(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, padding=10)
        self.app = app
        self.rows = {}
        self._build()

    def _build(self):
        self.in_place = tk.BooleanVar(value=False)
        ttk.Radiobutton(self, text='Write cleaned copies to a folder', value=False,
                        variable=self.in_place, command=self.app.changed).pack(anchor='w')
        row = ttk.Frame(self)
        row.pack(fill='x', padx=(22, 0), pady=(3, 0))
        ttk.Label(row, text='Output folder').pack(side='left')
        self.folder = tk.StringVar(value=os.path.join(PROJECT, 'output'))
        self.folder_entry = ttk.Entry(row, textvariable=self.folder, width=10)
        self.folder_entry.pack(side='left', fill='x', expand=True, padx=5)
        self.browse_button = ttk.Button(row, text='Browse…', command=self.ask_folder)
        self.browse_button.pack(side='left')
        self.copy_untouched = tk.BooleanVar(value=True)
        self.copy_check = ttk.Checkbutton(self, text='Also copy videos without cuts',
                                          variable=self.copy_untouched, command=self.app.changed)
        self.copy_check.pack(anchor='w', padx=(22, 0), pady=(3, 0))
        ttk.Radiobutton(self, text='Replace the original files (Episode.mp4 becomes the cut Episode.mkv)',
                        value=True, variable=self.in_place, command=self.app.changed).pack(anchor='w', pady=(6, 0))
        ttk.Label(self, style='Muted.TLabel', wraplength=480, justify='left',
                  text='Only videos with cuts are touched. Their subtitle files are retimed where they are. '
                       'The originals are deleted once each cut video is complete: this cannot be undone.',
                  ).pack(anchor='w', padx=(22, 0))

        row = ttk.Frame(self)
        row.pack(fill='x', pady=7)
        self.preset = tk.StringVar(value='veryfast')
        ttk.Combobox(row, textvariable=self.preset, values=PRESETS, state='readonly',
                     width=9).pack(side='right')
        ttk.Label(row, text='Speed', style='Muted.TLabel').pack(side='right', padx=4)
        self.crf = tk.StringVar(value='20')
        ttk.Spinbox(row, textvariable=self.crf, from_=12, to=32, width=4).pack(side='right')
        ttk.Label(row, text='Edge quality', style='Muted.TLabel').pack(side='right', padx=4)

        bottom = ttk.Frame(self)
        bottom.pack(side='bottom', fill='x')
        self.summary = ttk.Label(bottom, style='Muted.TLabel', wraplength=500, justify='left')
        self.summary.pack(fill='x', pady=6)
        row = ttk.Frame(bottom)
        row.pack(fill='x')
        self.export_button = ttk.Button(row, text='Export', style='Primary.TButton', command=self.export)
        self.export_button.pack(side='right')
        ttk.Button(row, text='▶ Play output', command=self.play).pack(side='left')
        ttk.Button(row, text='Open folder', command=self.show_folder).pack(side='left', padx=5)
        self.log = tk.Text(bottom, height=7, width=20, bg=FIELD, fg=TEXT, relief='flat',
                           wrap='word', font=('TkFixedFont', 9), state='disabled')
        self.log.pack(fill='x', pady=(8, 0))

        frame, self.tree = make_tree(
            self, [('cuts', 'Cuts', 42, 'center'), ('length', 'New length', 90, 'e'),
                   ('result', 'Result', 118, 'w')],
            heading='Video', width=210, height=5)
        frame.pack(fill='both', expand=True)
        on_check(self.tree, self.toggle)
        self.tree.bind('<Double-1>', lambda _event: self.play())

    def ask_folder(self):
        folder = dialogs.ask_directory(self, 'Output folder', self.folder.get())
        if folder:
            self.folder.set(os.path.abspath(folder))

    def toggle(self, row):
        episode = self.rows.get(row)
        if episode and episode.ready and not self.app.tasks.busy:
            episode.included = not episode.included
            self.app.changed()

    def chosen(self):
        keep_untouched = self.copy_untouched.get() and not self.in_place.get()
        return [e for e in self.app.episodes
                if e.included and e.ready and (e.segments or keep_untouched)]

    def write_log(self, text):
        self.log.config(state='normal')
        self.log.insert('end', text.rstrip() + '\n')
        self.log.see('end')
        self.log.config(state='disabled')

    def export(self):
        episodes = self.chosen()
        if not episodes:
            return
        in_place = self.in_place.get()
        folder = self.folder.get().strip()
        try:
            crf = int(self.crf.get())
        except ValueError:
            crf = -1
        if (not folder and not in_place) or not 0 <= crf <= 51:
            messagebox.showwarning('Export', 'Check the output folder and the edge quality (0-51).', parent=self)
            return
        folder = os.path.abspath(os.path.expanduser(folder or '.'))

        settings = self.app.panels['videos'].settings()
        if any(e.scanned_with not in (None, settings, SAVED) and not e.reviewed
               for e in episodes) and not messagebox.askyesno(
                'Export', 'Samples or settings changed since the search. Export the cuts as they are shown?',
                parent=self):
            return
        if in_place:
            self._replace(episodes, crf)
            return

        # Planned over the whole list, so a partial export gets the same names.
        planned = plan_outputs([(e.path, e.subfolder, bool(e.segments)) for e in self.app.episodes], folder)
        existing = [planned[e.path] for e in episodes
                    if glob.glob(glob.escape(os.path.splitext(planned[e.path])[0]) + '.*')]
        if existing:
            names = '\n'.join(os.path.basename(path) for path in existing[:8])
            more = f'\n…and {len(existing) - 8} more' if len(existing) > 8 else ''
            if not messagebox.askyesno(
                    'Replace existing files?',
                    f'These exports and their subtitle files already exist:\n\n{names}{more}\n\n'
                    'Replace them? Your source videos are never changed.', parent=self):
                return

        # The player would otherwise keep an output open while it is replaced.
        if self.app.player.path in {planned[e.path] for e in episodes}:
            self.app.player.close_media()

        # Snapshots: the threads must not see later edits.
        jobs = [(e, e.path, e.segments, planned[e.path]) for e in episodes]
        preset = self.preset.get()
        for episode in episodes:
            episode.status, episode.error, episode.output = 'Queued', '', ''
        results = {'done': 0, 'failed': 0, 'warnings': 0}

        def work(job, progress, log, cancelled):
            _, path, segments, output = job
            name = os.path.basename(path)
            log(f'{name}: ' + ('cutting' if segments else 'copying'))
            return export_video(path, segments, output, crf=crf, preset=preset,
                                log=lambda text: log(f'{name}: {text.strip()}'),
                                progress=progress, cancelled=cancelled)

        def finished(job, result, error):
            episode = job[0]
            if isinstance(error, Cancelled):
                episode.status = 'Export cancelled'
            elif error:
                episode.status, episode.error = 'Export failed', str(error)
                results['failed'] += 1
                self.write_log(f'{episode.name}: ERROR: {error}')
            else:
                episode.output = result.output
                episode.status = 'Done, with warnings' if result.warnings else 'Done'
                timeline: Timeline = result.timeline
                episode.junctions = [timeline.map(a) / 1000 for a, _ in timeline.cuts]
                results['done'] += 1
                results['warnings'] += bool(result.warnings)
                for warning in result.warnings:
                    self.write_log(f'{episode.name}: WARNING: {warning}')
                self.write_log(f'{episode.name}: done, {len(result.subtitles)} subtitle file(s)')
            self.app.changed()

        def done(cancelled):
            text = f'{"Cancelled. " if cancelled else ""}{results["done"]} exported'
            if results['failed']:
                text += f', {results["failed"]} failed'
            if results['warnings']:
                text += f', {results["warnings"]} with warnings'
            self.write_log(text)
            self.app.report(text + ('. See the log.' if results['failed'] or results['warnings'] else '.'))

        self.app.run('Exporting', jobs, work, self.app.panels['videos'].worker_count(),
                     finished, done, self.write_log)
        self.app.changed()

    def _replace(self, episodes, crf):
        blocked = [replaced_path(e.path) for e in episodes
                   if os.path.exists(replaced_path(e.path))
                   and not os.path.samefile(replaced_path(e.path), e.path)]
        if blocked:
            names = '\n'.join(os.path.basename(path) for path in blocked[:8])
            messagebox.showwarning(
                'Replace the originals',
                f'These files already exist next to the video that would become them:\n\n{names}\n\n'
                'Rename or move them first, or export to a folder instead.', parent=self)
            return
        names = '\n'.join(e.name for e in episodes[:8])
        more = f'\n…and {len(episodes) - 8} more' if len(episodes) > 8 else ''
        if not messagebox.askyesno(
                'Replace the originals?',
                f'Cut these {len(episodes)} video(s) in place:\n\n{names}{more}\n\n'
                'Each original is deleted and replaced by the cut .mkv, and its subtitle files are '
                'overwritten with retimed ones. This cannot be undone. Continue?',
                icon='warning', default='no', parent=self):
            return

        if self.app.player.path in {e.path for e in episodes}:
            self.app.player.close_media()
        jobs = [(e, e.path, e.segments) for e in episodes]
        preset = self.preset.get()
        for episode in episodes:
            episode.status, episode.error, episode.output = 'Queued', '', ''
        results = {'done': 0, 'failed': 0, 'warnings': 0}

        def work(job, progress, log, cancelled):
            _, path, segments = job
            name = os.path.basename(path)
            log(f'{name}: cutting in place')
            return replace_video(path, segments, crf=crf, preset=preset,
                                 log=lambda text: log(f'{name}: {text.strip()}'),
                                 progress=progress, cancelled=cancelled)

        def finished(job, result, error):
            episode = job[0]
            if isinstance(error, Cancelled):
                episode.status = 'Export cancelled'
            elif error:
                episode.status, episode.error = 'Replace failed', str(error)
                results['failed'] += 1
                self.write_log(f'{episode.name}: ERROR: {error}')
            else:
                timeline: Timeline = result.timeline
                # The file is now the cut one: the old cuts mean nothing in it.
                try:
                    reviews.forget(job[1])
                except OSError:
                    pass
                episode.duration = timeline.map(round(episode.duration * 1000)) / 1000
                episode.path = episode.output = result.output
                episode.cuts, episode.scanned_with, episode.reviewed = [], None, False
                episode.needs_rescan, episode.rescan_after = False, None
                episode.junctions = [timeline.map(a) / 1000 for a, _ in timeline.cuts]
                episode.status = 'Replaced, with warnings' if result.warnings else 'Replaced'
                results['done'] += 1
                results['warnings'] += bool(result.warnings)
                for warning in result.warnings:
                    self.write_log(f'{episode.name}: WARNING: {warning}')
                self.write_log(f'{episode.name}: replaced, {len(result.subtitles)} subtitle file(s) retimed')
            self.app.changed()

        def done(cancelled):
            text = f'{"Cancelled. " if cancelled else ""}{results["done"]} replaced'
            if results['failed']:
                text += f', {results["failed"]} failed'
            if results['warnings']:
                text += f', {results["warnings"]} with warnings'
            self.write_log(text)
            self.app.report(text + ('. See the log.' if results['failed'] or results['warnings'] else '.'))

        self.app.run('Replacing', jobs, work, self.app.panels['videos'].worker_count(),
                     finished, done, self.write_log)
        self.app.changed()

    def play(self):
        selection = self.tree.selection()
        episode = self.rows.get(selection[0]) if selection else None
        if not episode or not episode.output or not os.path.exists(episode.output):
            self.app.report('Select an exported video first.')
            return
        position = max(0.0, episode.junctions[0] - 3) if episode.junctions else 0.0
        if self.app.load(episode.output, position, play=True):
            self.app.player.set_marks([(t, t, CUT) for t in episode.junctions], (), episode.junctions)

    def show_folder(self):
        folder = os.path.expanduser(self.folder.get().strip())
        selection = self.tree.selection()
        episode = self.rows.get(selection[0]) if selection else None
        if self.in_place.get() and episode:
            folder = os.path.dirname(episode.path)
        if not os.path.isdir(folder):
            self.app.report('The output folder does not exist until the first export.')
            return
        try:
            open_folder(folder)
        except OSError as exc:
            self.app.report(f'Cannot open the folder: {exc}')

    def activate(self):
        pass

    def refresh(self):
        selected = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        self.rows = {}
        for episode in self.app.episodes:
            segments = episode.segments
            kept = Timeline(segments).map(round(episode.duration * 1000)) / 1000
            mark = CHECKED if episode.included and episode.ready else UNCHECKED
            if episode.status.startswith('Replaced'):
                result = episode.status
                tags = ('warning',) if 'warnings' in result else ('good',)
            elif not episode.ready:
                result, tags = episode.status, ('off',)
            elif not episode.included:
                result, tags = 'Skipped', ('off',)
            elif episode.error:
                result, tags = episode.status, ('error',)
            elif episode.status.startswith('Done') and episode.output:
                result = episode.status
                tags = ('warning',) if 'warnings' in result else ('good',)
            elif episode.status in ('Queued', 'Export cancelled'):
                result, tags = episode.status, ()
            elif segments:
                result, tags = 'Will be cut', ()
            elif self.in_place.get():
                result, tags = 'Left as it is', ('off',)
            elif self.copy_untouched.get():
                result, tags = 'Will be copied', ()
            else:
                result, tags = 'Skipped', ('off',)
            self.tree.insert('', 'end', iid=episode.path, text=f'{mark}  {shorten(episode.name, 28)}', tags=tags,
                             values=(len(segments), f'{int(kept // 60)}:{int(kept % 60):02d}', result))
            self.rows[episode.path] = episode
        self.tree.selection_set([row for row in selected if self.tree.exists(row)])

        chosen, in_place = self.chosen(), self.in_place.get()
        unready = sum(not e.ready and not e.status.startswith('Replaced') for e in self.app.episodes)
        if in_place:
            text = f'{len(chosen)} to cut in place. Subtitles are retimed automatically.'
        else:
            text = (f'{sum(bool(e.segments) for e in chosen)} to cut, '
                    f'{sum(not e.segments for e in chosen)} to copy. Subtitles are retimed automatically.')
        if unready:
            text += f' {unready} not scanned: they will not be exported.'
        self.summary.config(text=text)
        verb = 'Replace' if in_place else 'Export'
        self.export_button.config(text=f'{verb} {len(chosen)} video(s)',
                                  state='normal' if chosen and not self.app.tasks.busy else 'disabled')
        state = 'disabled' if in_place else 'normal'
        for widget in (self.folder_entry, self.browse_button, self.copy_check):
            widget.config(state=state)
