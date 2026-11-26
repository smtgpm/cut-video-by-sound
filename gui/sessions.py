"""
Sessions: everything in progress, saved as it changes and reopened at start.

A session is one JSON file in sessions/ next to the app. It holds the videos
with their cuts, what was reviewed or sent back, the sample set and which
samples are ticked, and the search and export settings. Nothing in it is
cleared unless you do it: unreview, remove the videos, or delete the session.

A video whose file is gone is left out when the session opens; one whose file
changed (size or date) comes back without its cuts, to be searched again.
"""

import json
import os
import tempfile
from dataclasses import asdict, fields

from gui.state import PROJECT, SAVED, Episode
from pipeline import Cut, safe_name

FOLDER = os.path.join(PROJECT, 'sessions')
LAST = os.path.join(FOLDER, '.last')  # name of the session open when the app was closed
VERSION = 1
_CUT_FIELDS = {f.name for f in fields(Cut)}


def path_of(name):
    return os.path.join(FOLDER, f'{name}.json')


def names():
    if not os.path.isdir(FOLDER):
        return []
    return sorted((entry[:-5] for entry in os.listdir(FOLDER)
                   if entry.endswith('.json') and not entry.startswith('.')), key=str.lower)


def clean_name(text):
    return safe_name(text or '')[:80]


def last_name():
    try:
        with open(LAST, encoding='utf-8') as handle:
            name = handle.read().strip()
        return name if os.path.exists(path_of(name)) else None
    except OSError:
        return None


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix='.session-', dir=os.path.dirname(path))
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as target:
            target.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


# ---------- Episodes ----------
def _cuts(items):
    return [Cut(**{k: v for k, v in item.items() if k in _CUT_FIELDS}) for item in items or []]


def _settings_to_json(settings):
    return settings if settings in (None, SAVED) else list(settings)


def _settings_from_json(value):
    if value is None or value == SAVED:
        return value
    samples, *others = value
    if len(others) == 5:  # search windows
        others[4] = tuple((path, tuple(window)) for path, window in others[4])
    return (tuple((path, mtime) for path, mtime in samples), *others)


def _episode_to_json(episode):
    try:
        info = os.stat(episode.path)
        size, mtime = info.st_size, round(info.st_mtime, 3)
    except OSError:
        size = mtime = None
    return {
        'path': episode.path, 'subfolder': episode.subfolder,
        'duration': episode.duration, 'subtitles': episode.subtitles,
        'size': size, 'mtime': mtime,
        'included': episode.included, 'reviewed': episode.reviewed,
        'needs_rescan': episode.needs_rescan,
        'skip_samples': sorted(episode.skip_samples) if episode.skip_samples else None,
        'searched': sorted(episode.searched),
        'scanned_with': _settings_to_json(episode.scanned_with),
        'status': episode.status,
        'cuts': [asdict(cut) for cut in episode.cuts],
        'candidates': [asdict(cut) for cut in episode.candidates],
    }


def _episode_from_json(item):
    """(episode, problem): problem is 'missing', 'changed' or ''."""
    path = item['path']
    try:
        info = os.stat(path)
    except OSError:
        return None, 'missing'
    episode = Episode(path, item.get('subfolder', ''), float(item.get('duration', 0.0)),
                      int(item.get('subtitles', 0)))
    episode.included = bool(item.get('included', True))
    if (item.get('size'), item.get('mtime')) != (info.st_size, round(info.st_mtime, 3)):
        episode.status = 'Changed on disk, search again'
        return episode, 'changed'
    episode.cuts, episode.candidates = _cuts(item.get('cuts')), _cuts(item.get('candidates'))
    episode.reviewed = bool(item.get('reviewed'))
    episode.needs_rescan = bool(item.get('needs_rescan'))
    skip = item.get('skip_samples')
    episode.skip_samples = frozenset(skip) if skip else None
    episode.searched = frozenset(item.get('searched') or ())
    episode.scanned_with = _settings_from_json(item.get('scanned_with'))
    episode.status = item.get('status') or episode.describe()
    return episode, ''


# ---------- Whole session ----------
def snapshot(app):
    """Everything worth keeping, as JSON-ready data."""
    samples, videos, export = app.panels['samples'], app.panels['videos'], app.panels['export']
    return {
        'version': VERSION,
        'browse_dir': app.browse_dir,
        'samples': {'folder': samples.folder, 'unticked': sorted(samples.unchecked)},
        'search': {'threshold': videos.threshold.get(), 'intro_max': videos.intro_max.get(),
                   'min_match': videos.min_match.get(), 'several': bool(videos.several.get()),
                   'workers': videos.workers.get()},
        'export': {'folder': export.folder.get(), 'in_place': bool(export.in_place.get()),
                   'copy_untouched': bool(export.copy_untouched.get()),
                   'preset': export.preset.get(), 'crf': export.crf.get()},
        'videos': [_episode_to_json(episode) for episode in app.episodes],
    }


def save(app, name):
    _write(path_of(name), json.dumps(snapshot(app), indent=1))
    _write(LAST, name)


def read(name):
    with open(path_of(name), encoding='utf-8') as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError('not a session file')
    return data


def apply(app, data):
    """Put a session's content in the app. Returns (missing, changed) video counts."""
    samples, videos, export = app.panels['samples'], app.panels['videos'], app.panels['export']
    app.browse_dir = data.get('browse_dir') or app.browse_dir
    for variable, key in ((videos.threshold, 'threshold'), (videos.intro_max, 'intro_max'),
                          (videos.min_match, 'min_match'), (videos.several, 'several'),
                          (videos.workers, 'workers')):
        if key in data.get('search', {}):
            variable.set(data['search'][key])
    for variable, key in ((export.folder, 'folder'), (export.in_place, 'in_place'),
                          (export.copy_untouched, 'copy_untouched'), (export.preset, 'preset'),
                          (export.crf, 'crf')):
        if key in data.get('export', {}):
            variable.set(data['export'][key])
    sample_data = data.get('samples', {})
    samples.unchecked = set(sample_data.get('unticked', []))
    folder = sample_data.get('folder')
    samples.refresh_sets(folder if folder and os.path.isdir(folder) else None)

    episodes, missing, changed = [], 0, 0
    for item in data.get('videos', []):
        try:
            episode, problem = _episode_from_json(item)
        except (KeyError, TypeError, ValueError):
            continue
        missing += problem == 'missing'
        changed += problem == 'changed'
        if episode:
            episodes.append(episode)
    app.episodes[:] = episodes
    return missing, changed


def rename(old, new):
    os.replace(path_of(old), path_of(new))
    if last_name() in (None, old):
        _write(LAST, new)


def delete(name):
    os.unlink(path_of(name))
