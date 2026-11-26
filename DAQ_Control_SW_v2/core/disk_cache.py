"""On-disk caches of values read from ROOT files (FinalResult / production), so a restarted app does not
re-read unchanged files. One pickle per table under ~/.cache/precal/routine_results; an entry is used only
while the file's mtime and size are unchanged, so a reprocessed file is read again."""
import atexit
import os
import pickle
import threading

DIR = os.path.expanduser("~/.cache/precal/routine_results")
_lock = threading.Lock()
_tables = {}     # name -> {path: (mtime, size, value)}
_dirty = set()


def stamp(path):
    """(mtime, size) of a file; raises OSError when it is missing. Take it before reading the file."""
    st = os.stat(path)
    return st.st_mtime, st.st_size


def _table(name):
    t = _tables.get(name)
    if t is None:
        try:
            with open(os.path.join(DIR, name + ".pkl"), "rb") as f:
                t = pickle.load(f)
        except Exception:
            t = {}
        _tables[name] = t if isinstance(t, dict) else {}
    return _tables[name]


def get(name, path, st):
    """The cached value of path for this stamp, or None."""
    with _lock:
        hit = _table(name).get(path)
    return hit[2] if hit and hit[:2] == tuple(st) else None


def put(name, path, st, value):
    with _lock:
        _table(name)[path] = tuple(st) + (value,)
        _dirty.add(name)


def flush():
    """Write the tables changed since the last flush (atomic replace)."""
    with _lock:
        for name in list(_dirty):
            try:
                os.makedirs(DIR, exist_ok=True)
                path = os.path.join(DIR, name + ".pkl")
                tmp = f"{path}.{os.getpid()}.tmp"
                with open(tmp, "wb") as f:
                    pickle.dump(_tables[name], f, protocol=4)
                os.replace(tmp, path)
                _dirty.discard(name)
            except OSError:
                pass


atexit.register(flush)
