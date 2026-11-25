"""
Start the app: samples, videos, review of the cuts, export.

    python app_extract.py

Needs FFmpeg/FFprobe, VLC, and: pip install -r requirements.txt
"""

import shutil
import sys
import tkinter as tk
from tkinter import messagebox


def _fail(text):
    print(text, file=sys.stderr)
    try:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror('Cut video by sound', text)
        root.destroy()
    except tk.TclError:
        pass
    raise SystemExit(1)


def main():
    for tool in ('ffmpeg', 'ffprobe'):
        if shutil.which(tool) is None:
            _fail(f'{tool} was not found. Install FFmpeg and make sure it is in PATH.')
    try:
        from gui.app import main as run
    except ImportError as exc:
        _fail(f'{exc}\n\nThis Python is missing a dependency:\n{sys.executable}\n\n'
              f'Install them with:\n{sys.executable} -m pip install -r requirements.txt')
    run()


if __name__ == '__main__':
    main()
