"""Step 1: mark samples in a video and manage sample sets."""

import os
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from gui import dialogs
from gui.common import (CHECKED, SAMPLE, UNCHECKED, format_time, make_tree,
                        on_check, parse_time, shorten)
from gui.state import PROJECT
from pipeline import VIDEO_EXTENSIONS, list_samples, media_info, safe_name, save_sample

REFS = os.path.join(PROJECT, 'refs')


class SamplesPanel(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, padding=10)
        self.app = app
        self.video = None
        self.folder = ''
        self.sets = {}
        self.samples = []
        self.unchecked = set()
        self._build()
        self.refresh_sets()

    def _build(self):
        box = ttk.LabelFrame(self, text='Create a sample', padding=8)
        box.pack(fill='x')
        row = ttk.Frame(box)
        row.pack(fill='x')
        ttk.Button(row, text='Open a video…', command=self.ask_video).pack(side='left')
        self.back = ttk.Button(row, text='Back to it', command=self.show_video, state='disabled')
        self.back.pack(side='left', padx=5)
        self.video_label = ttk.Label(box, text='Open any episode that contains the part to remove.',
                                     style='Muted.TLabel', width=1)
        self.video_label.pack(fill='x', pady=6)

        grid = ttk.Frame(box)
        grid.pack(fill='x')
        self.start, self.end, self.name = tk.StringVar(), tk.StringVar(), tk.StringVar(value='intro')
        for index, (text, variable) in enumerate((('Start', self.start), ('End', self.end))):
            ttk.Label(grid, text=text, width=7).grid(row=index, column=0, sticky='w')
            entry = ttk.Entry(grid, textvariable=variable, width=13)
            entry.grid(row=index, column=1, pady=2)
            entry.bind('<Return>', lambda _event: self.focus_set())
            ttk.Button(grid, text=f'Set to playhead ({"IO"[index]})',
                       command=lambda index=index: self.set_edge(index)).grid(row=index, column=2, padx=5)
            ttk.Button(grid, text='Go', width=3,
                       command=lambda variable=variable: self.go(variable)).grid(row=index, column=3)
            variable.trace_add('write', lambda *_: self.draw())
        self.length = ttk.Label(grid, text='', style='Muted.TLabel')
        self.length.grid(row=0, column=4, rowspan=2, padx=8)
        ttk.Label(grid, text='Name', width=7).grid(row=2, column=0, sticky='w')
        ttk.Entry(grid, textvariable=self.name, width=13).grid(row=2, column=1, pady=2)

        ttk.Label(box, style='Muted.TLabel', wraplength=480, justify='left',
                  text='Tip: start exactly on the intro or just inside it. With even a little of '
                       'the scene before it, episodes that open on the intro will not match.',
                  ).pack(fill='x', pady=(6, 0))
        row = ttk.Frame(box)
        row.pack(fill='x', pady=(8, 0))
        ttk.Button(row, text='▶ Preview', command=self.preview).pack(side='left')
        self.save_button = ttk.Button(row, text='Save sample', style='Primary.TButton', command=self.save)
        self.save_button.pack(side='left', padx=6)

        box = ttk.LabelFrame(self, text='Samples to search for', padding=8)
        box.pack(fill='both', expand=True, pady=(10, 0))
        row = ttk.Frame(box)
        row.pack(fill='x', pady=(0, 6))
        ttk.Label(row, text='Set').pack(side='left')
        self.set_choice = ttk.Combobox(row, state='readonly', width=20)
        self.set_choice.pack(side='left', padx=5, fill='x', expand=True)
        self.set_choice.bind('<<ComboboxSelected>>',
                             lambda _event: self.use_folder(self.sets[self.set_choice.get()]))
        ttk.Button(row, text='New…', command=self.new_set).pack(side='left')
        ttk.Button(row, text='Other folder…', command=self.ask_folder).pack(side='left', padx=(5, 0))

        buttons = ttk.Frame(box)
        buttons.pack(side='bottom', fill='x', pady=(6, 0))
        ttk.Button(buttons, text='▶ Listen', command=self.listen).pack(side='left')
        ttk.Button(buttons, text='Rename…', command=self.rename).pack(side='left', padx=5)
        ttk.Button(buttons, text='Delete…', command=self.delete).pack(side='left')
        self.next_button = ttk.Button(buttons, text='Next: videos →', style='Primary.TButton',
                                      command=lambda: self.app.show('videos'))
        self.next_button.pack(side='right')

        frame, self.tree = make_tree(box, [('length', 'Length', 75, 'e')], heading='Sample', height=4)
        frame.pack(fill='both', expand=True)
        on_check(self.tree, self.toggle)
        self.tree.bind('<Double-1>', lambda _event: self.listen())
        self.tree.bind('<Delete>', lambda _event: self.delete())

    # ---------- Sets ----------
    def refresh_sets(self, select=None):
        self.sets = {}
        if list_samples(REFS):
            self.sets['refs'] = REFS
        if os.path.isdir(REFS):
            for name in sorted(os.listdir(REFS)):
                if os.path.isdir(os.path.join(REFS, name)):
                    self.sets[name] = os.path.join(REFS, name)
        folder = select or self.folder or next(iter(self.sets.values()), os.path.join(REFS, 'default'))
        if folder not in self.sets.values():
            name = os.path.basename(folder) or folder
            while name in self.sets:
                name += ' (other)'
            self.sets[name] = folder
        self.set_choice.config(values=list(self.sets))
        self.set_choice.set(next(name for name, path in self.sets.items() if path == folder))
        self.use_folder(folder)

    def use_folder(self, folder):
        self.folder = folder
        self.samples = list_samples(folder)
        self.tree.delete(*self.tree.get_children())
        for index, path in enumerate(self.samples):
            try:
                length = f'{media_info(path)[0]:.2f} s'
            except RuntimeError:
                length = 'unreadable'
                self.unchecked.add(path)
            self.tree.insert('', 'end', iid=str(index), values=(length,))
            self._label(index)
        self.app.changed()

    def _label(self, index):
        path = self.samples[index]
        mark = UNCHECKED if path in self.unchecked else CHECKED
        self.tree.item(str(index), text=f'{mark}  {shorten(os.path.splitext(os.path.basename(path))[0], 44)}')

    def selected_samples(self):
        return [path for path in self.samples if path not in self.unchecked]

    def toggle(self, row):
        self.unchecked ^= {self.samples[int(row)]}
        self._label(int(row))
        self.app.changed()

    def new_set(self):
        name = simpledialog.askstring('New sample set', 'Name, for example the series:', parent=self)
        if name and safe_name(name):
            folder = os.path.join(REFS, safe_name(name))
            os.makedirs(folder, exist_ok=True)
            self.refresh_sets(folder)

    def ask_folder(self):
        folder = dialogs.ask_directory(self, 'Folder containing .wav samples')
        if folder:
            self.refresh_sets(os.path.abspath(folder))

    # ---------- Marking ----------
    def ask_video(self):
        patterns = ' '.join(f'*{ext} *{ext.upper()}' for ext in sorted(VIDEO_EXTENSIONS))
        chosen = dialogs.ask_open_files(self, 'Choose a video', [('Videos', patterns), ('All files', '*')],
                                        self.app.browse_dir, multiple=False)
        path = chosen[0] if chosen else ''
        if path:
            self.app.browse_dir = os.path.dirname(path)
            self.open_video(path)

    def open_video(self, path):
        if self.app.load(path):
            self.video = os.path.abspath(path)
            self.video_label.config(text=shorten(os.path.basename(path), 64))
            self.back.config(state='normal')
            self.start.set('')
            self.end.set('')
            self.draw()

    def show_video(self):
        if self.video and self.app.player.path != self.video:
            if not self.app.load(self.video):
                return False
            self.draw()
        return bool(self.video)

    def set_edge(self, index):
        if not self.video or self.app.player.path != self.video:
            self.app.report('Open a video, or press "Back to it", before marking.')
            return
        (self.start, self.end)[index].set(format_time(self.app.player.position))

    def go(self, variable):
        try:
            position = parse_time(variable.get())
        except ValueError as exc:
            self.app.report(str(exc))
            return
        if self.show_video():
            self.app.player.seek(position)

    def marked(self):
        if not self.video:
            raise ValueError('Open a video first.')
        start, end = parse_time(self.start.get()), parse_time(self.end.get())
        if end <= start:
            raise ValueError('The end must be after the start.')
        return start, end

    def draw(self):
        try:
            start, end = self.marked()
        except ValueError:
            self.length.config(text='')
            bands = []
        else:
            self.length.config(text=f'= {end - start:.2f} s')
            bands = [(start, end, SAMPLE)]
        player = self.app.player
        if self.video and player.path == self.video and self.app.active() is self:
            player.set_marks(bands)

    def preview(self):
        try:
            start, end = self.marked()
        except ValueError as exc:
            self.app.report(str(exc))
            return
        if self.show_video():
            self.app.player.play_range(start, end)

    def save(self):
        try:
            start, end = self.marked()
        except ValueError as exc:
            messagebox.showwarning('Save sample', str(exc), parent=self)
            return
        video, folder, name = self.video, self.folder, self.name.get()

        def work(_item, _progress, _log, _cancelled):
            return save_sample(video, start, end, folder, name)

        def finished(_item, path, error):
            if error:
                messagebox.showerror('Save sample', str(error), parent=self)
            else:
                self.refresh_sets(folder)
                self.app.report(f'Saved {os.path.basename(path)} ({end - start:.2f} s).')

        self.app.run('Saving sample', [video], work, 1, finished)

    # ---------- Managing ----------
    def current(self):
        selection = self.tree.selection()
        if not selection:
            self.app.report('Select a sample first.')
            return None
        return self.samples[int(selection[0])]

    def listen(self):
        path = self.current()
        if path:
            self.app.load(path, play=True)

    def rename(self):
        path = self.current()
        if not path or self.app.tasks.busy:
            return
        old = os.path.splitext(os.path.basename(path))[0]
        name = safe_name(simpledialog.askstring('Rename sample', 'New name:',
                                                initialvalue=old, parent=self) or '')
        if not name or name == old:
            return
        new = os.path.join(self.folder, name + '.wav')
        if os.path.exists(new):
            messagebox.showwarning('Rename sample', 'That name is already used.', parent=self)
            return
        if self.app.player.path == os.path.abspath(path):
            self.app.player.close_media()
        try:
            os.rename(path, new)
        except OSError as exc:
            messagebox.showerror('Rename sample', str(exc), parent=self)
            return
        self.use_folder(self.folder)

    def delete(self):
        """Delete every selected sample, after one confirmation."""
        paths = [self.samples[int(row)] for row in self.tree.selection()]
        if not paths:
            self.app.report('Select one or more samples first.')
            return
        if self.app.tasks.busy:
            return
        names = '\n'.join(os.path.basename(path) for path in paths[:15])
        if len(paths) > 15:
            names += f'\n… and {len(paths) - 15} more'
        question = (f'Permanently delete this file?\n\n{paths[0]}' if len(paths) == 1 else
                    f'Permanently delete these {len(paths)} samples?\n\n{names}')
        if not messagebox.askyesno('Delete samples', question, parent=self):
            return
        failed = []
        for path in paths:
            if self.app.player.path == os.path.abspath(path):
                self.app.player.close_media()
            try:
                os.unlink(path)
                self.unchecked.discard(path)
            except OSError as exc:
                failed.append(f'{os.path.basename(path)}: {exc}')
        if failed:
            messagebox.showerror('Delete samples', '\n'.join(failed[:12]), parent=self)
        self.use_folder(self.folder)
        deleted = len(paths) - len(failed)
        self.app.report(f'{deleted} sample{"s" * (deleted != 1)} deleted.')

    # ---------- Called by the app ----------
    def activate(self):
        self.draw()

    def refresh(self):
        busy = self.app.tasks.busy
        self.save_button.config(state='disabled' if busy else 'normal')
