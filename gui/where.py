"""
Per-sample options, chosen when creating a sample, in the sample list and when
saving a reviewed cut as a sample (ask_new_sample, the review's dialog):

- WhereField, "Look in": where in each video the sample is looked for
  (see pipeline.read_windows);
- ExtendField, "Also cut": everything before or after its match, up to some
  seconds (see pipeline.read_extensions).

Also the review's "Cut by time" dialog (ask_time_cut).
"""

import tkinter as tk
from tkinter import messagebox, ttk

from gui.common import BG
from pipeline import check_extension, check_window

CHOICES = ('Anywhere', 'Starts between', 'Starts in the last')
EXTENDS = ('Nothing more', 'Everything before, up to', 'Everything after, up to')


def phrase(window):
    """'anywhere', 'where it starts between 0 and 5 s' or 'where it starts in the last 30 s'."""
    if not window:
        return 'anywhere'
    if window[0] == 'first':
        return f'where it starts between {window[1]:g} and {window[2]:g} s'
    return f'where it starts in the last {window[1]:g} s'


class WhereField(ttk.Frame):
    """Anywhere, starts between A and B seconds, or starts in the last X seconds."""

    def __init__(self, parent, window=None):
        super().__init__(parent)
        self.kind = tk.StringVar(value=CHOICES[0])
        box = ttk.Combobox(self, textvariable=self.kind, values=CHOICES, state='readonly', width=17)
        box.pack(side='left')
        box.bind('<<ComboboxSelected>>', lambda _event: self._show())
        self.low, self.high = tk.StringVar(value='0'), tk.StringVar(value='5')
        self.low_entry = ttk.Entry(self, textvariable=self.low, width=6)
        self.dash = ttk.Label(self, text='–')
        self.high_entry = ttk.Entry(self, textvariable=self.high, width=6)
        self.seconds = ttk.Label(self, text='s')
        self.set(window)

    def _show(self):
        """Only the fields the chosen kind needs."""
        parts = (self.low_entry, self.dash, self.high_entry, self.seconds)
        for widget in parts:
            widget.pack_forget()
        kind = CHOICES.index(self.kind.get())
        shown = parts if kind == 1 else (self.high_entry, self.seconds) if kind == 2 else ()
        for widget in shown:
            widget.pack(side='left', padx=(5 if widget is shown[0] else 0, 3))

    def set(self, window):
        if not window:
            self.kind.set(CHOICES[0])
        elif window[0] == 'first':
            self.kind.set(CHOICES[1])
            self.low.set(f'{window[1]:g}')
            self.high.set(f'{window[2]:g}')
        else:
            self.kind.set(CHOICES[2])
            self.high.set(f'{window[1]:g}')
        self._show()

    def get(self):
        """The window chosen (None: anywhere). ValueError if the numbers are wrong."""
        kind = CHOICES.index(self.kind.get())
        try:
            if kind == 1:
                return check_window(('first', float(self.low.get()), float(self.high.get())))
            if kind == 2:
                return check_window(('last', float(self.high.get())))
        except (TypeError, ValueError):
            raise ValueError('Give seconds for "Look in", with the first number lower than the second.')
        return None


class ExtendField(ttk.Frame):
    """Nothing more, or everything before / after the match, up to X seconds."""

    def __init__(self, parent, extension=None):
        super().__init__(parent)
        self.kind = tk.StringVar(value=EXTENDS[0])
        box = ttk.Combobox(self, textvariable=self.kind, values=EXTENDS, state='readonly', width=22)
        box.pack(side='left')
        box.bind('<<ComboboxSelected>>', lambda _event: self._show())
        self.most = tk.StringVar(value='60')
        self.entry = ttk.Entry(self, textvariable=self.most, width=6)
        self.seconds = ttk.Label(self, text='s')
        self.set(extension)

    def _show(self):
        self.entry.pack_forget()
        self.seconds.pack_forget()
        if self.kind.get() != EXTENDS[0]:
            self.entry.pack(side='left', padx=(5, 3))
            self.seconds.pack(side='left')

    def set(self, extension):
        if not extension:
            self.kind.set(EXTENDS[0])
        else:
            self.kind.set(EXTENDS[1] if extension[0] == 'before' else EXTENDS[2])
            self.most.set(f'{extension[1]:g}')
        self._show()

    def get(self):
        """The extension chosen (None: nothing more). ValueError if the number is wrong."""
        kind = EXTENDS.index(self.kind.get())
        if not kind:
            return None
        try:
            return check_extension(('before' if kind == 1 else 'after', float(self.most.get())))
        except (TypeError, ValueError):
            raise ValueError('Give "Also cut" a number of seconds above 0.')


def extension_phrase(extension):
    """'' or ', also cutting everything before it (up to 60 s)'."""
    if not extension:
        return ''
    return f', also cutting everything {extension[0]} it (up to {extension[1]:g} s)'


TIME_SIDES = ('The first', 'The last')


def ask_time_cut(parent, others):
    """
    ('first' or 'last', seconds, on every video still to review) for a cut set
    by time alone, or None if cancelled. others: how many other videos are
    still to review.
    """
    title = 'Cut by time'
    dialog = _dialog(parent, title)
    frame = ttk.Frame(dialog, padding=14)
    frame.pack(fill='both', expand=True)
    ttk.Label(frame, text='Cut a fixed part of the video, whatever is heard there:\n'
              'the opening seconds, or the end credits for example.',
              wraplength=380, justify='left').grid(row=0, column=0, columnspan=4, sticky='w', pady=(0, 10))
    line = ttk.Frame(frame)
    line.grid(row=1, column=0, columnspan=4, sticky='w')
    side = tk.StringVar(value=TIME_SIDES[1])
    ttk.Label(line, text='Cut').pack(side='left', padx=(0, 8))
    ttk.Combobox(line, textvariable=side, values=TIME_SIDES, state='readonly', width=10).pack(side='left')
    seconds = tk.StringVar(value='70')
    entry = ttk.Entry(line, textvariable=seconds, width=7)
    entry.pack(side='left', padx=5)
    ttk.Label(line, text='seconds').pack(side='left')
    everywhere = tk.BooleanVar(value=False)
    if others:
        ttk.Checkbutton(frame, variable=everywhere,
                        text=f'Also on the {others} other video{"s" * (others != 1)} still to review'
                        ).grid(row=2, column=0, columnspan=4, sticky='w', pady=(10, 0))
    result = {}

    def ok(_event=None):
        try:
            value = float(seconds.get().replace(',', '.'))
            if not 0 < value < 1e6:
                raise ValueError
        except ValueError:
            messagebox.showwarning(title, 'Give a number of seconds above 0.', parent=dialog)
            return
        result['value'] = ('first' if side.get() == TIME_SIDES[0] else 'last', value, bool(everywhere.get()))
        dialog.destroy()

    _buttons(frame, dialog, 'Add cut', ok, row=3, columns=4)
    _show(dialog, parent, entry)
    return result.get('value')


def _dialog(parent, title):
    dialog = tk.Toplevel(parent)
    dialog.title(title)
    dialog.configure(bg=BG)
    dialog.transient(parent.winfo_toplevel())
    dialog.resizable(False, False)
    return dialog


def _buttons(frame, dialog, text, ok, row, columns):
    buttons = ttk.Frame(frame)
    buttons.grid(row=row, column=0, columnspan=columns, sticky='e', pady=(12, 0))
    ttk.Button(buttons, text='Cancel', command=dialog.destroy).pack(side='right')
    ttk.Button(buttons, text=text, style='Primary.TButton', command=ok).pack(side='right', padx=6)
    dialog.bind('<Return>', ok)
    dialog.bind('<Escape>', lambda _event: dialog.destroy())


def _show(dialog, parent, entry):
    """Centred on the app, modal, with the entry ready to type in, until closed."""
    dialog.update_idletasks()
    top = parent.winfo_toplevel()
    x = top.winfo_rootx() + (top.winfo_width() - dialog.winfo_width()) // 2
    y = top.winfo_rooty() + (top.winfo_height() - dialog.winfo_height()) // 3
    dialog.geometry(f'+{max(0, x)}+{max(0, y)}')
    entry.focus_set()
    entry.select_range(0, 'end')
    dialog.wait_visibility()
    dialog.grab_set()
    parent.wait_window(dialog)


def ask_new_sample(parent, title, text, name, window, extension=None):
    """(name, window, extension) for a new sample, or None if cancelled."""
    dialog = _dialog(parent, title)
    frame = ttk.Frame(dialog, padding=14)
    frame.pack(fill='both', expand=True)
    ttk.Label(frame, text=text, wraplength=380, justify='left').grid(row=0, column=0, columnspan=2,
                                                                    sticky='w', pady=(0, 10))
    ttk.Label(frame, text='Name').grid(row=1, column=0, sticky='w', padx=(0, 8))
    value = tk.StringVar(value=name)
    entry = ttk.Entry(frame, textvariable=value, width=28)
    entry.grid(row=1, column=1, sticky='we', pady=3)
    ttk.Label(frame, text='Look in').grid(row=2, column=0, sticky='w', padx=(0, 8))
    where = WhereField(frame, window)
    where.grid(row=2, column=1, sticky='w', pady=3)
    ttk.Label(frame, text='Also cut').grid(row=3, column=0, sticky='w', padx=(0, 8))
    extend = ExtendField(frame, extension)
    extend.grid(row=3, column=1, sticky='w', pady=3)
    result = {}

    def ok(_event=None):
        try:
            chosen = where.get(), extend.get()
        except ValueError as exc:
            messagebox.showwarning(title, str(exc), parent=dialog)
            return
        if not value.get().strip():
            messagebox.showwarning(title, 'Give the sample a name.', parent=dialog)
            return
        result['value'] = (value.get().strip(), *chosen)
        dialog.destroy()

    _buttons(frame, dialog, 'Save sample', ok, row=4, columns=2)
    _show(dialog, parent, entry)
    return result.get('value')
