"""
Reviews kept between sessions, in reviews.json next to the app.

A video ticked off with "Reviewed, next video" is remembered with its cuts, so
it comes back reviewed when it is added again, and is not searched again. The
entry only applies to the same file: if its size or date changes, it is ignored.
"""

import json
import os
import tempfile
from dataclasses import asdict, fields

from gui.state import PROJECT
from pipeline import Cut

FILE = os.path.join(PROJECT, 'reviews.json')
SAVED = 'saved review'  # scanned_with of a video restored from the file
_CUT_FIELDS = {f.name for f in fields(Cut)}


def _read():
    try:
        with open(FILE, encoding='utf-8') as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(data):
    handle, temporary = tempfile.mkstemp(prefix='.reviews-', dir=os.path.dirname(FILE))
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as target:
            json.dump(data, target, indent=1)
        os.replace(temporary, FILE)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _identity(path):
    info = os.stat(path)
    return info.st_size, round(info.st_mtime, 3)


def save(episode):
    """Remember a reviewed video and its cuts."""
    try:
        size, mtime = _identity(episode.path)
    except OSError:
        return
    data = _read()
    data[episode.path] = {'size': size, 'mtime': mtime,
                          'cuts': [asdict(cut) for cut in episode.cuts]}
    _write(data)


def forget(path):
    data = _read()
    if data.pop(path, None) is not None:
        _write(data)


def restore(episode):
    """Mark the episode reviewed, with its saved cuts, if it has a valid saved review."""
    entry = _read().get(episode.path)
    try:
        if not entry or (entry['size'], entry['mtime']) != _identity(episode.path):
            return False
        cuts = [Cut(**{k: v for k, v in cut.items() if k in _CUT_FIELDS}) for cut in entry['cuts']]
    except (OSError, KeyError, TypeError):
        return False
    episode.cuts, episode.reviewed, episode.scanned_with = cuts, True, SAVED
    count = sum(cut.enabled for cut in cuts)
    episode.status = f'Reviewed, {count} cut{"s" * (count != 1)}'
    return True
