"""Look and small helpers shared by the panels."""

import os
import subprocess
import sys
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

BG, PANEL, FIELD = '#20232a', '#282c34', '#1a1d23'
BORDER, TEXT, MUTED = '#3a404b', '#e6e8ec', '#969daa'
ACCENT, CUT, DISABLED_CUT, SAMPLE, WARNING, GOOD = (
    '#4c8dff', '#e5534b', '#68707d', '#3fb68b', '#e3b341', '#3fb68b')
CHECKED, UNCHECKED = '☑', '☐'


def apply_theme(root):
    style = ttk.Style(root)
    style.theme_use('clam')
    root.configure(bg=BG)
    root.option_add('*tearOff', False)
    root.option_add('*TCombobox*Listbox.background', FIELD)
    root.option_add('*TCombobox*Listbox.foreground', TEXT)

    style.configure('.', background=PANEL, foreground=TEXT, fieldbackground=FIELD,
                    bordercolor=BORDER, lightcolor=PANEL, darkcolor=PANEL,
                    troughcolor=FIELD, insertcolor=TEXT, arrowcolor=TEXT)
    style.map('.', foreground=[('disabled', '#626976')])
    style.configure('TButton', background='#353b46', padding=(8, 4))
    style.map('TButton', background=[('disabled', PANEL), ('active', '#424956')])
    style.configure('Transport.TButton', padding=(3, 4))
    style.configure('Primary.TButton', background=ACCENT, foreground='white', padding=(12, 7))
    style.map('Primary.TButton', background=[('disabled', BORDER), ('active', '#6aa0ff')])
    style.configure('TEntry', padding=3)
    for widget in ('TCheckbutton', 'TRadiobutton'):
        style.map(widget, background=[('active', PANEL)],
                  indicatorbackground=[('selected', ACCENT)])
        style.configure(widget, indicatorbackground=FIELD)
    style.map('TCombobox', fieldbackground=[('readonly', FIELD)],
              foreground=[('readonly', TEXT)], selectbackground=[('readonly', FIELD)],
              selectforeground=[('readonly', TEXT)])
    style.configure('TScrollbar', background='#353b46', gripcount=0)
    style.map('TScrollbar', background=[('disabled', PANEL), ('active', '#424956')])
    style.configure('TNotebook', background=BG, borderwidth=0)
    style.configure('TNotebook.Tab', background=BG, foreground=MUTED, padding=(13, 7))
    style.map('TNotebook.Tab', background=[('selected', PANEL)], foreground=[('selected', TEXT)])
    line = tkfont.nametofont('TkDefaultFont').metrics('linespace')
    style.configure('Treeview', background=FIELD, rowheight=line + 8, borderwidth=0)
    style.map('Treeview', background=[('selected', '#31466b')], foreground=[('selected', TEXT)])
    style.configure('Treeview.Heading', background='#303540', foreground=MUTED, padding=3)
    style.map('Treeview.Heading', background=[('active', '#303540')])
    style.configure('Muted.TLabel', foreground=MUTED)
    style.configure('Warning.TLabel', foreground=WARNING)
    style.configure('Title.TLabel', font=('TkDefaultFont', 11, 'bold'))
    style.configure('Horizontal.TProgressbar', background=ACCENT)

    # Space always means play/pause, not "press the focused button again".
    for widget in ('TButton', 'TCheckbutton'):
        root.bind_class(widget, '<KeyPress-space>', '')
        root.bind_class(widget, '<KeyRelease-space>', '')
    try:
        # These variables only exist once Tk's file dialog has been loaded.
        try:
            root.tk.call('tk_getOpenFile', '-not-an-option')
        except tk.TclError:
            pass
        root.tk.call('set', '::tk::dialog::file::showHiddenBtn', '1')
        root.tk.call('set', '::tk::dialog::file::showHiddenVar', '0')
    except tk.TclError:
        pass


def format_time(seconds):
    ms = round(max(0.0, seconds) * 1000)
    minutes, ms = divmod(ms, 60000)
    hours, minutes = divmod(minutes, 60)
    clock = f'{minutes:02d}:{ms / 1000:06.3f}'
    return f'{hours}:{clock}' if hours else clock


def shorten(text, limit):
    """Cut the middle: episode and sample names differ at both ends."""
    if len(text) <= limit:
        return text
    tail = limit // 2
    return f'{text[:limit - tail - 1]}…{text[-tail:]}'


def parse_time(text):
    """Accepts 83.5, 1:23.5 or 0:01:23.5."""
    parts = text.strip().replace(',', '.').split(':')
    try:
        values = [float(part) for part in parts]
    except ValueError:
        values = []
    if not 1 <= len(values) <= 3 or any(not 0 <= value < 1e7 for value in values):
        raise ValueError(f'"{text}" is not a time. Use 83.5, 1:23.5 or 0:01:23.5.')
    total = 0.0
    for value in values:
        total = total * 60 + value
    return total


def make_tree(parent, columns, heading='', width=200, height=6, show='tree headings', **options):
    """A Treeview with a scrollbar. columns: (id, title, width, anchor)."""
    frame = ttk.Frame(parent)
    tree = ttk.Treeview(frame, columns=[c[0] for c in columns], height=height, show=show, **options)
    tree.heading('#0', text=heading, anchor='w')
    tree.column('#0', width=width, minwidth=120, stretch=True)
    for key, title, size, anchor in columns:
        tree.heading(key, text=title, anchor=anchor)
        tree.column(key, width=size, minwidth=40, anchor=anchor, stretch=False)
    bar = ttk.Scrollbar(frame, orient='vertical', command=tree.yview)
    tree.configure(yscrollcommand=bar.set)
    bar.pack(side='right', fill='y')
    tree.pack(side='left', fill='both', expand=True)
    tree.tag_configure('warning', foreground=WARNING)
    tree.tag_configure('off', foreground=MUTED)
    tree.tag_configure('error', foreground=CUT)
    tree.tag_configure('good', foreground=GOOD)
    return frame, tree


def on_check(tree, toggle):
    """Call toggle(row) for a click on a row's check mark, or Enter on the row."""
    def click(event):
        row = tree.identify_row(event.y)
        if not row or tree.identify_region(event.x, event.y) != 'tree':
            return None
        element = tree.identify_element(event.x, event.y)
        box = tree.bbox(row, '#0')
        depth = 1 if tree.parent(row) else 0
        if 'indicator' in element or not box:
            return None
        if event.x - box[0] <= 20 * depth + 40:
            tree.selection_set(row)
            toggle(row)
            return 'break'
        return None

    def key(_event):
        for row in tree.selection():
            toggle(row)
        return 'break'

    tree.bind('<Button-1>', click)
    tree.bind('<Return>', key)


def open_folder(path):
    if sys.platform.startswith('win'):
        os.startfile(path)
    else:
        command = 'open' if sys.platform == 'darwin' else 'xdg-open'
        subprocess.Popen([command, path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
