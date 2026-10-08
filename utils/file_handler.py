"""
Temporary, per-job storage. A "job" is just a folder:

    temp/<job_id>/
        source.csv (or .xlsx), original_name, owner, cleaned.csv

job_id is a uuid4. Deleted on download or after JOB_TTL_SECONDS.

Only module that touches the filesystem for job storage — routes go
through it, not raw paths, so this is the one seam a future object-
storage swap would touch.

Ownership: every job folder is tagged with a hash of the session that
created it (utils/session.hash_value). Every route must confirm the
current request's session matches before acting on a job_id — see
job_owner_hash() / owns() below.
"""

import os
import re
import shutil
import time
import uuid
import zipfile
from typing import Optional
from werkzeug.utils import secure_filename

from config import Config

# job_id comes from a URL segment, so it's validated as a real uuid4
# before ever being joined into a path — see job_dir_path().
_JOB_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def is_valid_job_id(job_id) -> bool:
    return isinstance(job_id, str) and bool(_JOB_ID_RE.match(job_id))


def allowed_file(filename: str) -> bool:
    return (
        "." in filename
        and filename.rsplit(".", 1)[1].lower() in Config.ALLOWED_EXTENSIONS
    )


# Magic bytes for the two binary formats we accept (CSV has none,
# it's just text — checked separately below).
_XLSX_MAGIC = b"PK\x03\x04"  # .xlsx is a zip archive
_XLS_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # legacy OLE2 container


def validate_upload_content(file_storage, ext: str) -> Optional[str]:
    """Sniffs the actual bytes of an upload against what its extension
    claims, on top of the extension whitelist in allowed_file().
    Returns None if the content looks legitimate for `ext`, or a
    user-facing error string if it doesn't. Reads only a small header
    (and, for .xlsx, the zip's own directory) and always rewinds the
    stream afterward so save_upload() still gets the whole file.
    """
    stream = file_storage.stream
    pos = stream.tell()
    try:
        head = stream.read(8)
    finally:
        stream.seek(pos)

    if ext == "xlsx":
        if not head.startswith(_XLSX_MAGIC):
            return "This file doesn't look like a real .xlsx workbook."
        try:
            stream.seek(pos)
            with zipfile.ZipFile(stream) as zf:
                names = zf.namelist()
                # Zip-bomb guard: a few KB on disk can inflate to gigabytes in
                # memory once pandas/openpyxl parse it. Judge by the declared
                # uncompressed sizes in the zip directory (no extraction).
                infos = zf.infolist()
                if len(infos) > 5000:
                    return "This .xlsx file contains too many internal parts to be a normal workbook."
                total_uncompressed = sum(i.file_size for i in infos)
                if total_uncompressed > Config.MAX_XLSX_UNCOMPRESSED_MB * 1024 * 1024:
                    return "This .xlsx file expands to an unreasonable size and was rejected."
                # Reject macro-enabled .xlsm content disguised as .xlsx.
                if any(n.lower() == "xl/vbaproject.bin" for n in names):
                    return "Macro-enabled workbooks (.xlsm content in a .xlsx file) aren't accepted."
                if not any(n.startswith("xl/") for n in names):
                    return "This .xlsx file doesn't contain a valid workbook structure."
        except zipfile.BadZipFile:
            return "This .xlsx file is corrupted or not a real Excel workbook."
        finally:
            stream.seek(pos)
        return None

    if ext == "xls":
        if not head.startswith(_XLS_MAGIC):
            return "This file doesn't look like a real .xls workbook."
        return None

    if ext == "csv":
        try:
            stream.seek(pos)
            sample = stream.read(8192)
        finally:
            stream.seek(pos)
        if b"\x00" in sample:
            return "This file contains binary data and isn't a valid CSV."
        try:
            sample.decode("utf-8")
        except UnicodeDecodeError:
            try:
                sample.decode("latin-1")
            except UnicodeDecodeError:
                return "This file isn't readable text and isn't a valid CSV."
        return None

    return "Unsupported file type."


def create_job_dir() -> str:
    job_id = str(uuid.uuid4())
    job_dir = os.path.join(Config.TEMP_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    return job_id


def job_dir_path(job_id: str) -> Optional[str]:
    """Returns None for anything that isn't a well-formed job_id
    rather than blindly joining it into a path, since job_id is
    user-supplied (a URL segment)."""
    if not is_valid_job_id(job_id):
        return None
    return os.path.join(Config.TEMP_DIR, job_id)


def safe_display_name(filename: str, ext: str) -> str:
    """A filename that is safe to store, log and echo back to a browser.

    werkzeug's secure_filename can return an empty string or drop the dot
    entirely (e.g. for a name made only of non-ASCII characters, "名前.csv" ->
    "csv"), so the extension is re-attached from the already-validated `ext`
    instead of trusting whatever survives sanitising.
    """
    cleaned = secure_filename(filename or "")
    stem = cleaned.rsplit(".", 1)[0] if "." in cleaned else cleaned
    stem = stem.strip("._ ")[:100] or "upload"
    return f"{stem}.{ext}"


def save_upload(file_storage, job_id: str, ext: Optional[str] = None) -> str:
    """Saves the incoming file as source.<ext> inside the job dir.

    The original filename (sanitized) is kept in a small `original_name`
    marker file alongside it, the file itself is renamed to
    source.<ext> so the rest of the pipeline never has to deal with
    arbitrary user-supplied names, but the frontend still wants to
    show "customers.csv" rather than "source.csv"."""
    if ext is None:
        raw = file_storage.filename or ""
        ext = raw.rsplit(".", 1)[1].lower() if "." in raw else ""
    if ext not in Config.ALLOWED_EXTENSIONS:
        raise ValueError("unsupported extension")
    job_dir = job_dir_path(job_id)
    if not job_dir:
        raise ValueError("invalid job id")
    original = safe_display_name(file_storage.filename, ext)
    dest = os.path.join(job_dir, f"source.{ext}")
    file_storage.save(dest)
    with open(os.path.join(job_dir, "original_name"), "w", encoding="utf-8") as f:
        f.write(original)
    return dest


def set_job_owner(job_id: str, owner_hash: str) -> None:
    """Tags a freshly created job dir with a hash of the session that
    uploaded it (see utils/session.hash_value). Called once, right
    after create_job_dir(), before the file itself is even saved, so
    a job is never briefly ownerless."""
    job_dir = job_dir_path(job_id)
    if not job_dir:
        return
    with open(os.path.join(job_dir, "owner"), "w") as f:
        f.write(owner_hash)


def job_owner_hash(job_id: str) -> Optional[str]:
    """Owner fingerprint for a job. The local marker file wins (single-instance layout);
    otherwise the shared database (queue mode / object storage, where no local file exists)."""
    d = job_dir_path(job_id)
    if not d:
        return None
    path = os.path.join(d, "owner")
    if os.path.isfile(path):
        try:
            with open(path) as f:
                return f.read().strip() or None
        except OSError:
            return None
    try:
        import db  # local import: avoids a hard dependency for callers that don't need it
        return db.get_job_owner_hash(job_id) or None
    except Exception:
        return None  # fail closed: no owner known -> nobody owns it


def original_filename(job_id: str) -> Optional[str]:
    d = job_dir_path(job_id)
    if not d:
        return None
    path = os.path.join(d, "original_name")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().strip() or None
    except OSError:
        return None


def find_source_file(job_id: str) -> Optional[str]:
    d = job_dir_path(job_id)
    if not d or not os.path.isdir(d):
        return None
    for name in os.listdir(d):
        if name.startswith("source."):
            return os.path.join(d, name)
    return None


def cleaned_file_path(job_id: str, ext: str) -> str:
    d = job_dir_path(job_id)
    if not d:
        raise ValueError(f"Invalid job_id: {job_id!r}")
    return os.path.join(d, f"cleaned.{ext}")


def delete_job(job_id: str) -> None:
    """Discards all temp data for a job. Called after download,
    and by the periodic sweep for abandoned jobs. Safe to call on
    an invalid/already-gone job_id, this is best-effort cleanup,
    not something that should ever crash a request.

    Uses rmtree (not a flat os.remove loop) so a job folder that ever
    contains a sub-directory or a half-written temp file is still fully
    removed instead of leaking user data on disk."""
    d = job_dir_path(job_id)
    if not d or not os.path.isdir(d) or os.path.islink(d):
        return
    shutil.rmtree(d, ignore_errors=True)


def sweep_expired_jobs() -> None:
    """Deletes any job folder older than JOB_TTL_SECONDS. Cheap
    enough to call at the top of every API entry point (V1 has no
    scheduler), see routes/*.py. Never lets a bad individual job
    folder (e.g. deleted out from under it by a concurrent request)
    take down the whole sweep. Also marks those jobs 'expired' in the
    metadata db (db.py) so the admin dashboard's job history still
    shows what happened to them after their temp files are gone.
    """
    now = time.time()
    if not os.path.isdir(Config.TEMP_DIR):
        return
    expired_ids = []
    for job_id in os.listdir(Config.TEMP_DIR):
        if not is_valid_job_id(job_id):
            continue  # not one of ours, never touch it
        d = job_dir_path(job_id)
        try:
            if d and os.path.isdir(d) and (now - os.path.getmtime(d)) > Config.JOB_TTL_SECONDS:
                delete_job(job_id)
                expired_ids.append(job_id)
        except OSError:
            continue  # e.g. another request already deleted it, not fatal

    if expired_ids:
        try:
            import db  # local import: avoids a hard dependency for callers that don't need it
            db.mark_expired(expired_ids)
        except Exception:
            pass  # metadata bookkeeping must never break the actual cleanup


_last_sweep = 0.0


def maybe_sweep() -> None:
    """Request-path cleanup, throttled. The old code listed the whole temp directory on EVERY
    API call. Now at most once per SWEEP_MIN_INTERVAL_SECONDS per process, and never in queue
    mode (the worker reaper owns cleanup there)."""
    global _last_sweep
    if Config.PROCESSING_MODE == "queue":
        return
    now = time.monotonic()
    if now - _last_sweep < Config.SWEEP_MIN_INTERVAL_SECONDS:
        return
    _last_sweep = now
    sweep_expired_jobs()
