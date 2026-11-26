"""
Choosing files and folders.

On Linux, the system's own dialog is used when zenity is installed (GNOME
and most desktops have it): it scrolls with the mouse wheel and has the
bookmarks and recent folders. Otherwise Tk's dialog is used, with the
mouse wheel added: Tk 8.6 only scrolls it with Shift held.
"""

import os
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog

_ZENITY = shutil.which('zenity') if sys.platform.startswith('linux') else None
_open = [False]  # one dialog at a time


def ask_directory(parent, title, initialdir=None):
    """A folder, or '' if cancelled."""
    if _ZENITY:
        chosen = _zenity(parent, ['--directory'], title, initialdir)
        return chosen[0] if chosen else ''
    _fix_wheel(parent)
    return filedialog.askdirectory(parent=parent, title=title, initialdir=initialdir) or ''


def ask_open_files(parent, title, filetypes, initialdir=None, multiple=True):
    """
    The chosen files ([] if cancelled). filetypes: [(name, '*.a *.b'), ...]
    """
    if _ZENITY:
        extra = ['--multiple', '--separator=\n'] if multiple else []
        extra += [f'--file-filter={name} | {patterns}' for name, patterns in filetypes]
        return _zenity(parent, extra, title, initialdir)
    _fix_wheel(parent)
    ask = filedialog.askopenfilenames if multiple else filedialog.askopenfilename
    chosen = ask(parent=parent, title=title, initialdir=initialdir, filetypes=filetypes)
    if not chosen:
        return []
    return [chosen] if isinstance(chosen, str) else list(chosen)


def _zenity(parent, options, title, initialdir):
    """Runs zenity while the app keeps redrawing (a frozen window would look crashed)."""
    if _open[0]:
        return []
    command = [_ZENITY, '--file-selection', f'--title={title}', *options]
    if initialdir and os.path.isdir(initialdir):
        command.append('--filename=' + os.path.join(os.path.abspath(initialdir), ''))
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    except OSError:
        return []

    top = parent.winfo_toplevel()
    _open[0] = True
    finished = tk.BooleanVar(master=top, value=False)
    output = []
    # Read on a thread: a long list of files must not fill the pipe and block zenity.
    reader = threading.Thread(target=lambda: output.append(process.communicate()[0]), daemon=True)
    reader.start()

    def check():
        if reader.is_alive():
            top.after(50, check)
        else:
            finished.set(True)

    check()
    try:
        top.wait_variable(finished)
    finally:
        _open[0] = False
    text = output[0] if output and process.returncode == 0 else ''
    return [line for line in text.splitlines() if line]


def _fix_wheel(widget):
    """Lets the plain mouse wheel scroll Tk's file and folder dialogs."""
    root = widget.winfo_toplevel()
    if getattr(root, '_dialog_wheel', False):
        return
    root._dialog_wheel = True

    def scroll(event, step):
        # The dialogs are '.__tk_filedialog' or '.__tk_choosedir'; their file
        # list is a canvas that scrolls sideways, one column at a time.
        # Made by Tk itself, so Python only gets the widget's name.
        if '__tk_' in str(event.widget):
            root.tk.call(str(event.widget), 'xview', 'scroll', step, 'units')

    root.bind_class('Canvas', '<Button-4>', lambda event: scroll(event, -1), add='+')
    root.bind_class('Canvas', '<Button-5>', lambda event: scroll(event, 1), add='+')
    root.bind_class('Canvas', '<MouseWheel>',
                    lambda event: scroll(event, -1 if event.delta > 0 else 1), add='+')
