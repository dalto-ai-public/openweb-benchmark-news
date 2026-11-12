"""Canonical hashes and durable private files."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
import stat
from pathlib import Path
import tempfile
from concurrent.futures import ThreadPoolExecutor
from collections import deque


def encoded(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n").encode()


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else encoded(value)).hexdigest()


def file_digest(path):
    """Fingerprint closed evidence without decoding or retaining its contents."""
    path = Path(path)
    if path.is_symlink():
        raise ValueError('evidence file must not be a symlink')
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError('evidence must be a regular file')
        h = hashlib.sha256()
        while chunk := stream.read(1024 * 1024):
            h.update(chunk)
        after = os.fstat(stream.fileno())
    current = path.stat()
    signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    if signature(before) != signature(after) or signature(after) != signature(current):
        raise ValueError('evidence changed while hashing')
    return h.hexdigest()


def parallel_map(function, values, *, workers=8):
    """Ordered independent checks, with bounded work and no retained cache."""
    if type(workers) is not int or workers < 1:
        raise ValueError('positive check worker count required')
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = deque()
        for value in values:
            pending.append(pool.submit(function, value))
            if len(pending) >= 2 * workers:
                yield pending.popleft().result()
        for job in pending:
            yield job.result()


def file_manifest(paths, *, workers=8):
    """Bounded parallel I/O; results and failures do not depend on worker order."""
    paths = sorted({str(Path(p).absolute()) for p in paths})
    hashes = list(parallel_map(file_digest, paths, workers=workers))
    return [dict(path=p, sha256=h) for p, h in zip(paths, hashes)]


def verify_manifest(refs, *, workers=8):
    if len({r['path'] for r in refs}) != len(refs):
        raise ValueError('duplicate evidence file binding')
    actual = file_manifest([r['path'] for r in refs], workers=workers)
    if actual != sorted(refs, key=lambda r: r['path']):
        raise ValueError('verified evidence file changed')
    return actual


def now():
    return datetime.now(timezone.utc).isoformat()


def load(path):
    return json.loads(Path(path).read_bytes())


def records(path):
    with Path(path).open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def private_directory(path):
    path = Path(path).expanduser().absolute()
    if path.is_symlink():
        raise ValueError("private output directory must not be a symlink")
    missing = []
    parent = path
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700, exist_ok=True)
    path.chmod(0o700)
    return path


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_private(path, value, *, replace=False):
    """Write and fsync bytes before exposing a complete file; never overwrite by default."""
    path = Path(path)
    private_directory(path.parent)
    raw = value if isinstance(value, bytes) else encoded(value)
    fd, temp = tempfile.mkstemp(prefix=".writing-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            if path.is_symlink():
                raise ValueError("refusing symlink output")
            os.replace(temp, path)
        else:
            os.link(temp, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def asset_path(name):
    return Path(__file__).parent / "assets" / name


def asset_bindings():
    root = asset_path("")
    return {p.relative_to(root).as_posix(): digest(p.read_bytes())
            for p in sorted(root.rglob("*")) if p.is_file() and
            p.suffix in {".json", ".md"} and not any(part.startswith(".") for part in p.relative_to(root).parts)}


def code_bindings():
    root = Path(__file__).parent
    return {p.relative_to(root).as_posix(): digest(p.read_bytes())
            for p in sorted(root.rglob("*.py"))}
