"""
"Look in": where in each video a sample is looked for (see pipeline.read_windows).

WhereField is the choice itself, used when creating a sample, in the sample
list and when saving a reviewed cut as a sample; ask_new_sample is the
dialog of the review's "Save as sample…".
"""

import tkinter as tk
from tkinter import messagebox, ttk

from gui.common import BG
from pipeline import check_window

CHOICES = ('Anywhere', 'Starts between', 'Starts in the last')


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


def ask_new_sample(parent, title, text, name, window):
    """A name and a window for a new sample, or None if cancelled."""
    dialog = tk.Toplevel(parent)
    dialog.title(title)
    dialog.configure(bg=BG)
    dialog.transient(parent.winfo_toplevel())
    dialog.resizable(False, False)
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
    result = {}

    def ok(_event=None):
        try:
            chosen = where.get()
        except ValueError as exc:
            messagebox.showwarning(title, str(exc), parent=dialog)
            return
        if not value.get().strip():
            messagebox.showwarning(title, 'Give the sample a name.', parent=dialog)
            return
        result['value'] = value.get().strip(), chosen
        dialog.destroy()

    buttons = ttk.Frame(frame)
    buttons.grid(row=3, column=0, columnspan=2, sticky='e', pady=(12, 0))
    ttk.Button(buttons, text='Cancel', command=dialog.destroy).pack(side='right')
    ttk.Button(buttons, text='Save sample', style='Primary.TButton', command=ok).pack(side='right', padx=6)
    dialog.bind('<Return>', ok)
    dialog.bind('<Escape>', lambda _event: dialog.destroy())

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
    return result.get('value')
