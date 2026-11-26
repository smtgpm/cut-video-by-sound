"""The main window: player on the left, the four steps on the right."""

import os
import tkinter as tk
import traceback
from tkinter import messagebox, simpledialog, ttk

from gui import sessions
from gui.common import apply_theme
from gui.export import ExportPanel
from gui.player import Player
from gui.review import ReviewPanel
from gui.samples import SamplesPanel
from gui.tasks import Tasks
from gui.videos import VideosPanel

TEXT_INPUTS = {'TEntry', 'Entry', 'TSpinbox', 'TCombobox', 'Text'}


class App:
    def __init__(self, root):
        self.root = root
        self.episodes = []
        self.panels = {}
        self._changed_job = None
        self._save_job = None
        self._closing = False
        self.session = None  # name of the open session, saved as it changes
        self._loading = False
        self.browse_dir = os.path.expanduser('~')  # where file dialogs open

        root.title('Cut video by sound')
        width = min(1400, root.winfo_screenwidth() - 40)
        height = min(860, root.winfo_screenheight() - 80)
        root.geometry(f'{width}x{height}')
        root.minsize(min(1120, width), min(680, height))
        apply_theme(root)
        root.report_callback_exception = self._unexpected

        bar = ttk.Frame(root, padding=(8, 5))
        bar.pack(side='bottom', fill='x')
        self.cancel_button = ttk.Button(bar, text='Cancel', command=self._cancel, state='disabled')
        self.cancel_button.pack(side='right')
        self.progress = ttk.Progressbar(bar, length=260, maximum=1.0)
        self.progress.pack(side='right', padx=8)
        self.message = ttk.Label(bar, text='Start by opening a video, or pick an existing sample set.', width=1)
        self.message.pack(side='left', fill='x', expand=True)

        body = ttk.Frame(root)
        body.pack(fill='both', expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        self.player = Player(body, self.report)
        self.player.grid(row=0, column=0, sticky='nsew')

        side = ttk.Frame(body, width=560)
        side.grid(row=0, column=1, sticky='nsew')
        side.pack_propagate(False)
        self._build_session_bar(side)
        self.notebook = ttk.Notebook(side)
        self.notebook.pack(fill='both', expand=True, padx=(4, 8), pady=8)
        self.tasks = Tasks(root, self._task_changed)
        for key, title, kind in (('samples', '1  Samples', SamplesPanel),
                                 ('videos', '2  Videos', VideosPanel),
                                 ('review', '3  Review cuts', ReviewPanel),
                                 ('export', '4  Export', ExportPanel)):
            self.panels[key] = kind(self.notebook, self)
            self.notebook.add(self.panels[key], text=title)
        self.notebook.bind('<<NotebookTabChanged>>', self._tab_changed)

        self._bind_keys()
        root.protocol('WM_DELETE_WINDOW', self.close)
        self._open_first_session()
        self.changed()

    # ---------- Sessions ----------
    def _build_session_bar(self, parent):
        bar = ttk.Frame(parent, padding=(4, 8, 8, 0))
        bar.pack(fill='x')
        ttk.Label(bar, text='Session').pack(side='left')
        self.session_choice = tk.StringVar()
        self.session_box = ttk.Combobox(bar, textvariable=self.session_choice, state='readonly', width=18)
        self.session_box.pack(side='left', fill='x', expand=True, padx=6)
        self.session_box.bind('<<ComboboxSelected>>', lambda _event: self.switch_session(self.session_choice.get()))
        self.session_buttons = []
        for text, command in (('New…', self.new_session), ('Rename…', self.rename_session),
                              ('Delete…', self.delete_session)):
            button = ttk.Button(bar, text=text, command=command, width=len(text) + 1)
            button.pack(side='left', padx=(0, 4))
            self.session_buttons.append(button)
        self.session_saved = ttk.Label(bar, style='Muted.TLabel', width=8, anchor='e')
        self.session_saved.pack(side='left')

    def _open_first_session(self):
        name = sessions.last_name() or next(iter(sessions.names()), None)
        if name and self._load_session(name):
            return
        self.session = self._free_name('Session 1')
        self._save_session()

    def _free_name(self, wanted):
        name, number, taken = wanted, 1, {n.lower() for n in sessions.names()}
        while name.lower() in taken:
            number += 1
            name = f'{wanted} ({number})'
        return name

    def _load_session(self, name):
        try:
            data = sessions.read(name)
        except (OSError, ValueError) as exc:
            messagebox.showerror('Open session', f'"{name}" could not be read:\n{exc}', parent=self.root)
            return False
        self._loading = True
        try:
            review = self.panels['review']
            review.episode = review.cut = None
            self.player.close_media()
            missing, changed = sessions.apply(self, data)
        finally:
            self._loading = False
        self.session = name
        self._save_session()
        notes = [f'Session "{name}": {len(self.episodes)} video(s)']
        if missing:
            notes.append(f'{missing} no longer found, left out')
        if changed:
            notes.append(f'{changed} changed on disk, to search again')
        self.report(', '.join(notes) + '.')
        self.changed()
        return True

    def _save_session(self):
        self._save_job = None
        if not self.session or self._loading:
            return
        try:
            sessions.save(self, self.session)
            self.session_saved.config(text='saved ✓')
        except OSError as exc:
            self.session_saved.config(text='not saved')
            self.report(f'The session could not be saved: {exc}')

    def _save_soon(self):
        if self._loading or not self.session:
            return
        if self._save_job is not None:
            self.root.after_cancel(self._save_job)
        self.session_saved.config(text='…')
        self._save_job = self.root.after(800, self._save_session)

    def _ask_session_name(self, title, initial):
        name = simpledialog.askstring(title, 'Session name:', initialvalue=initial, parent=self.root)
        name = sessions.clean_name(name)
        if not name:
            return None
        if name.lower() in {n.lower() for n in sessions.names()} and name != self.session:
            messagebox.showwarning(title, 'A session already has that name.', parent=self.root)
            return None
        return name

    def switch_session(self, name):
        if name == self.session:
            return
        if self.tasks.busy:
            self.report('Wait for the current job to finish, or cancel it, before switching session.')
            self.changed()
            return
        self._save_session()
        if not self._load_session(name):
            self.changed()

    def new_session(self):
        """A new, empty session: same samples and settings, no videos."""
        if self.tasks.busy:
            return
        name = self._ask_session_name('New session', self._free_name('Session 1'))
        if not name:
            return
        self._save_session()
        review = self.panels['review']
        review.episode = review.cut = None
        self.player.close_media()
        self.episodes.clear()
        self.session = name
        self._save_session()
        self.report(f'New session "{name}". The previous one is saved: pick it in the list to go back.')
        self.show('videos')
        self.changed()

    def rename_session(self):
        name = self._ask_session_name('Rename session', self.session)
        if not name or name == self.session:
            return
        self._save_session()
        try:
            sessions.rename(self.session, name)
        except OSError as exc:
            messagebox.showerror('Rename session', str(exc), parent=self.root)
            return
        self.session = name
        self.changed()

    def delete_session(self):
        if self.tasks.busy or not self.session:
            return
        if not messagebox.askyesno(
                'Delete session', f'Delete the session "{self.session}"? Its list of videos, cuts and '
                'reviews is lost. No video or sample file is touched.', parent=self.root):
            return
        if self._save_job is not None:
            self.root.after_cancel(self._save_job)
            self._save_job = None
        try:
            sessions.delete(self.session)
        except OSError as exc:
            messagebox.showerror('Delete session', str(exc), parent=self.root)
            return
        self.session = None
        others = sessions.names()
        if not (others and self._load_session(others[0])):
            review = self.panels['review']
            review.episode = review.cut = None
            self.episodes.clear()
            self.session = self._free_name('Session 1')
            self._save_session()
        self.changed()

    # ---------- Shared helpers ----------
    def report(self, text):
        self.message.config(text=text.replace('\n', ' '))

    def load(self, path, position=0.0, play=False):
        try:
            self.player.load(path, position, play)
            return True
        except Exception as exc:
            messagebox.showerror('Cannot open file', str(exc), parent=self.root)
            return False

    def show(self, key):
        self.notebook.select(self.panels[key])

    def active(self):
        if not self.panels or not self.notebook.select():
            return None
        return self.root.nametowidget(self.notebook.select())

    def _tab_changed(self, _event=None):
        self.player.set_marks([])
        # Otherwise a hidden entry keeps the focus and eats the shortcuts.
        self.notebook.focus_set()
        self.active().activate()

    def changed(self):
        """Redraw the panels, once, after the current event."""
        if self._changed_job is None:
            self._changed_job = self.root.after_idle(self._refresh)

    def _refresh(self):
        self._changed_job = None
        for panel in self.panels.values():
            panel.refresh()
        if self.session:
            self.session_box.config(values=sessions.names())
            self.session_choice.set(self.session)
            self.root.title(f'Cut video by sound — {self.session}')
        for button in self.session_buttons:
            button.config(state='disabled' if self.tasks.busy else 'normal')
        self.session_box.config(state='disabled' if self.tasks.busy else 'readonly')
        self._save_soon()

    # ---------- Background work ----------
    def run(self, label, items, work, workers, on_item, on_done=None, on_log=None):
        def done(cancelled):
            self.report(f'{label}: {"cancelled" if cancelled else "finished"}.')
            if on_done and not self._closing:
                on_done(cancelled)
            self.changed()

        def item_finished(*args):
            if not self._closing:
                on_item(*args)

        if not self.tasks.start(label, items, work, workers, item_finished, done, on_log):
            self.report('Wait for the current job to finish, or cancel it.')
            return
        self.changed()

    def _task_changed(self):
        self.progress['value'] = self.tasks.fraction if self.tasks.busy else 0
        self.cancel_button.config(state='normal' if self.tasks.busy else 'disabled')
        if self.tasks.busy:
            self.report(f'{self.tasks.label}… {self.tasks.fraction:.0%}')

    def _cancel(self):
        self.tasks.cancel()

    # ---------- Keyboard ----------
    def _bind_keys(self):
        player = self.player

        def bind(key, action):
            def handle(event):
                widget = event.widget
                if isinstance(widget, str) or widget.winfo_class() in TEXT_INPUTS:
                    return None
                if widget.winfo_toplevel() is not self.root:
                    return None  # typing in a dialog
                action()
                return 'break'
            self.root.bind_all(key, handle)
            # Lists and tabs use these keys themselves, and would run first.
            for widget_class in ('Treeview', 'TNotebook'):
                self.root.bind_class(widget_class, key, handle)

        bind('<space>', player.toggle)
        bind('<Left>', lambda: player.step(-0.1))
        bind('<Right>', lambda: player.step(0.1))
        bind('<Shift-Left>', lambda: player.step(-1))
        bind('<Shift-Right>', lambda: player.step(1))
        bind('<comma>', lambda: player.step('back'))
        bind('<period>', lambda: player.step('forward'))
        for key, index in (('i', 0), ('I', 0), ('o', 1), ('O', 1)):
            bind(f'<KeyPress-{key}>', lambda index=index: self._mark(index))

    def _mark(self, index):
        panel = self.active()
        if hasattr(panel, 'set_edge'):
            panel.set_edge(index)

    # ---------- Lifecycle ----------
    def _unexpected(self, kind, value, trace):
        traceback.print_exception(kind, value, trace)
        messagebox.showerror('Unexpected error', f'{kind.__name__}: {value}', parent=self.root)

    def close(self):
        if self._closing:
            return
        if self.tasks.busy:
            if not messagebox.askyesno('Quit', 'Cancel the running job and quit?', parent=self.root):
                return
            self.tasks.cancel()
            self.report('Stopping safely…')
        self._closing = True
        if self._save_job is not None:
            self.root.after_cancel(self._save_job)
        self._save_session()
        self._close_when_idle()

    def _close_when_idle(self):
        if self.tasks.busy:
            self.root.after(100, self._close_when_idle)
            return
        self.player.shutdown()
        self.root.destroy()


def main():
    root = tk.Tk(className='cut-video-by-sound')
    App(root)
    root.mainloop()
