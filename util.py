"""Small shared helpers used by both app.py and model.py."""
import os
import json
import time


def atomic_write(path, write_func, retries=8, delay=0.05):
    """Write to `path` atomically via a temp-file-then-replace pattern.

    On POSIX, os.replace() is atomic and succeeds even if another process
    has the destination open. On Windows it can raise PermissionError
    (WinError 5) if another thread briefly holds an open handle to the
    destination -- e.g. a concurrent GET /train_status read. That's a
    transient condition (the reader closes the handle immediately after
    reading), so a short retry loop resolves it without weakening the
    atomicity guarantee.
    """
    tmp_path = path + ".tmp"
    write_func(tmp_path)
    last_error = None
    for _ in range(retries):
        try:
            os.replace(tmp_path, path)
            return
        except PermissionError as e:
            last_error = e
            time.sleep(delay)
    raise last_error


def atomic_write_json(path, data, **kwargs):
    def _write(tmp_path):
        with open(tmp_path, "w") as f:
            json.dump(data, f)
    atomic_write(path, _write, **kwargs)
