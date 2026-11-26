"""
Review decisions kept between sessions, in reviews.json next to the app.

A video validated in the review is remembered with its cuts: added again, even
after a restart, it comes back reviewed and is not searched again. A video
sent back with "Needs re-scan" is remembered too, with its cuts and, with
"Keep selected samples", the samples it is never searched with again.

An entry only applies to the same file: if its size or date changes, it is
ignored.
"""

import json
import os
import tempfile
from dataclasses import asdict, fields

from gui.state import PROJECT, SAVED
from pipeline import Cut

FILE = os.path.join(PROJECT, 'reviews.json')
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
    """Remember the video's review state and cuts (forgets it if neither reviewed nor sent back)."""
    if not (episode.reviewed or episode.needs_rescan):
        forget(episode.path)
        return
    try:
        size, mtime = _identity(episode.path)
    except OSError:
        return
    data = _read()
    data[episode.path] = {
        'size': size, 'mtime': mtime,
        'state': 'reviewed' if episode.reviewed else 'rescan',
        'skip_samples': sorted(episode.skip_samples) if episode.skip_samples else None,
        'cuts': [asdict(cut) for cut in episode.cuts],
        'candidates': [asdict(cut) for cut in episode.candidates],
        'searched': sorted(episode.searched),
    }
    _write(data)


def forget(path):
    data = _read()
    if data.pop(path, None) is not None:
        _write(data)


def restore(episode):
    """Bring back a saved review state and cuts, if the file has not changed. True if restored."""
    entry = _read().get(episode.path)
    try:
        if not entry or (entry['size'], entry['mtime']) != _identity(episode.path):
            return False
        cuts, candidates = ([Cut(**{k: v for k, v in cut.items() if k in _CUT_FIELDS}) for cut in entry.get(key, [])]
                            for key in ('cuts', 'candidates'))
        rescan = entry.get('state') == 'rescan'
        skip = entry.get('skip_samples')
        if skip is None and entry.get('rescan_after'):
            # Older entries: "not with these samples".
            skip = [path for path, _ in entry['rescan_after'][0]]
        skip = frozenset(skip) if skip else None
    except (OSError, KeyError, TypeError, ValueError):
        return False
    episode.cuts, episode.candidates, episode.scanned_with = cuts, candidates, SAVED
    episode.searched = frozenset(entry.get('searched') or ())
    episode.reviewed, episode.needs_rescan, episode.skip_samples = not rescan, rescan, skip
    episode.status = episode.describe()
    return True
