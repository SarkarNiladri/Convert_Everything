"""Archive converters: ZIP, TAR, TAR.GZ and 7z to each other.

ZIP and TAR use the standard library; 7z uses py7zr (pure Python apart from a
few small compression wheels).

Archives come from strangers, so unpacking is defensive:

* Entry names are cleaned: absolute paths, drive letters and ".." are refused,
  so nothing can be written outside the job's own folder ("zip slip").
* Symlinks, hard links and device files are skipped, never recreated.
* The total unpacked size and the number of entries are capped (zip bombs).
* Password-protected archives are rejected with a clear message.

The source is unpacked into a private staging folder, then packed into the
target format. File contents, folders and modification times are kept.
"""
import os
import re
import shutil
import stat
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path

from errors import ConversionError

ARCHIVE_IN = ["zip", "tar", "tar.gz", "tgz", "7z"]
ARCHIVE_OUT = ["zip", "tar", "tar.gz", "7z"]

MAX_UNPACKED_BYTES = int(os.environ.get("MAX_ARCHIVE_UNPACKED_MB", "2048")) * 1024 * 1024
MAX_ENTRIES = int(os.environ.get("MAX_ARCHIVE_ENTRIES", "20000"))


def targets_for(ext: str) -> list[str]:
    same = "tar.gz" if ext == "tgz" else ext
    return [t for t in ARCHIVE_OUT if t != same]


def archive_stem(src: Path) -> str:
    name = src.name
    for suffix in (".tar.gz", ".tgz", ".tar", ".zip", ".7z"):
        if name.lower().endswith(suffix):
            return name[: -len(suffix)] or "archive"
    return src.stem


def archive_ext(src: Path) -> str:
    name = src.name.lower()
    return "tar.gz" if name.endswith(".tar.gz") else src.suffix.lower().lstrip(".")


# ---------- Unpacking ----------
class _Limits:
    def __init__(self, name: str):
        self.name, self.bytes, self.entries = name, 0, 0

    def entry(self, size: int = 0) -> None:
        self.entries += 1
        self.bytes += max(0, size)
        if self.entries > MAX_ENTRIES:
            raise ConversionError(f"{self.name} has more than {MAX_ENTRIES:,} entries, which is over the limit.")
        if self.bytes > MAX_UNPACKED_BYTES:
            mb = MAX_UNPACKED_BYTES // (1024 * 1024)
            raise ConversionError(f"{self.name} unpacks to more than {mb:,} MB, which is over the limit.")


def safe_relpath(name: str) -> str | None:
    """A clean relative path for an archive entry, or None if it must be skipped."""
    parts = [p for p in name.replace("\\", "/").split("/") if p not in ("", ".")]
    if parts and re.fullmatch(r"[A-Za-z]:", parts[0]):
        parts = parts[1:]
    if not parts or ".." in parts:
        return None
    return "/".join(parts)


def _target_path(stage: Path, rel: str) -> Path | None:
    dest = (stage / rel).resolve()
    root = stage.resolve()
    if dest != root and root not in dest.parents:
        return None
    return dest


def _write_entry(stage: Path, rel: str, reader, mtime: float | None) -> None:
    dest = _target_path(stage, rel)
    if dest is None:
        return
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as f:
            shutil.copyfileobj(reader, f, 1024 * 1024)
    except (IsADirectoryError, NotADirectoryError, FileExistsError):
        return  # a file and a folder with the same name: keep the first one
    if mtime:
        os.utime(dest, (mtime, mtime))


def _make_dir(stage: Path, rel: str) -> None:
    dest = _target_path(stage, rel)
    if dest is not None:
        try:
            dest.mkdir(parents=True, exist_ok=True)
        except (FileExistsError, NotADirectoryError):
            pass


def _unpack_zip(src: Path, stage: Path) -> None:
    limits = _Limits(src.name)
    try:
        with zipfile.ZipFile(src) as zf:
            for info in zf.infolist():
                if info.flag_bits & 0x1:
                    raise ConversionError(f"{src.name} is password-protected. Unlock it first.")
                rel = safe_relpath(info.filename)
                if rel is None:
                    continue
                if info.is_dir():
                    limits.entry()
                    _make_dir(stage, rel)
                    continue
                kind = stat.S_IFMT(info.external_attr >> 16)
                if kind and kind != stat.S_IFREG:  # symlinks and other specials
                    continue
                limits.entry(info.file_size)
                try:
                    mtime = time.mktime(info.date_time + (0, 0, -1))
                except (OverflowError, ValueError):
                    mtime = None
                with zf.open(info) as reader:  # reads at most file_size bytes
                    _write_entry(stage, rel, reader, mtime)
    except zipfile.BadZipFile as exc:
        raise ConversionError(f"{src.name} isn't a valid ZIP file ({exc}).")
    except NotImplementedError as exc:
        raise ConversionError(f"{src.name} uses a ZIP compression method that isn't supported ({exc}).")


def _unpack_tar(src: Path, stage: Path) -> None:
    limits = _Limits(src.name)
    try:
        with tarfile.open(src, "r:*") as tf:
            for member in tf:  # streams, so a huge member list is never held at once
                rel = safe_relpath(member.name)
                if rel is None:
                    continue
                if member.isdir():
                    limits.entry()
                    _make_dir(stage, rel)
                elif member.isreg():
                    limits.entry(member.size)
                    reader = tf.extractfile(member)
                    if reader is not None:
                        with reader:
                            _write_entry(stage, rel, reader, member.mtime)
                # symlinks, hard links, devices and FIFOs are skipped
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise ConversionError(f"{src.name} isn't a valid TAR archive ({exc}).")


def _unpack_7z(src: Path, stage: Path) -> None:
    import py7zr
    limits = _Limits(src.name)
    try:
        with py7zr.SevenZipFile(src, "r") as z:
            if z.needs_password():
                raise ConversionError(f"{src.name} is password-protected. Unlock it first.")
            wanted = []
            for info in z.list():
                rel = safe_relpath(info.filename)
                if rel is None or info.is_symlink:
                    continue
                if not (info.is_directory or info.is_file):
                    continue
                limits.entry(0 if info.is_directory else info.uncompressed or 0)
                wanted.append(info.filename)
            if wanted:
                z.extract(path=stage, targets=wanted)
    except ConversionError:
        raise
    except py7zr.exceptions.PasswordRequired:
        raise ConversionError(f"{src.name} is password-protected. Unlock it first.")
    except py7zr.exceptions.DecompressionBombError:
        raise ConversionError(f"{src.name} unpacks to far more data than it holds, so it was refused.")
    except (py7zr.exceptions.ArchiveError, py7zr.exceptions.Bad7zFile, py7zr.exceptions.CrcError,
            py7zr.exceptions.DecompressionError, py7zr.exceptions.UnsupportedCompressionMethodError,
            py7zr.exceptions.AbsolutePathError, EOFError) as exc:
        raise ConversionError(f"{src.name} isn't a 7z archive that can be read ({exc}).")
    # Belt and braces: nothing that isn't a plain file or folder survives.
    for root, dirs, files in os.walk(stage):
        for name in dirs + files:
            p = Path(root) / name
            if p.is_symlink():
                p.unlink()


UNPACK = {"zip": _unpack_zip, "tar": _unpack_tar, "tar.gz": _unpack_tar, "tgz": _unpack_tar, "7z": _unpack_7z}


# ---------- Packing ----------
def _entries(stage: Path) -> list[tuple[Path, str]]:
    """Every folder and file under the stage, parents before children, as (path, archive name)."""
    out = []
    for root, dirs, files in os.walk(stage):
        dirs.sort()
        for name in dirs + sorted(files):
            p = Path(root) / name
            if p.is_symlink():
                continue
            out.append((p, p.relative_to(stage).as_posix()))
    out.sort(key=lambda e: e[1].split("/"))
    return out


def _tar_clean(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def _pack(stage: Path, target: str, out: Path) -> None:
    entries = _entries(stage)
    if target == "zip":
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            for path, arc in entries:
                zf.write(path, arcname=arc + ("/" if path.is_dir() else ""))
    elif target in ("tar", "tar.gz"):
        mode = "w:gz" if target == "tar.gz" else "w"
        with tarfile.open(out, mode, format=tarfile.PAX_FORMAT) as tf:
            for path, arc in entries:
                tf.add(path, arcname=arc, recursive=False, filter=_tar_clean)
    elif target == "7z":
        import py7zr
        with py7zr.SevenZipFile(out, "w") as z:
            for path, arc in entries:
                z.write(path, arcname=arc)
    else:
        raise ConversionError(f"Can't create {target.upper()} archives.")


# ---------- Entry point ----------
def convert_archive(src: Path, target: str, out_dir: Path) -> list[Path]:
    ext = archive_ext(src)
    if ext not in UNPACK:
        raise ConversionError(f"{src.name} isn't a supported archive.")
    stage = Path(tempfile.mkdtemp(prefix="_unpack_", dir=str(out_dir)))
    out = out_dir / f"{archive_stem(src)}.{target}"
    try:
        UNPACK[ext](src, stage)
        _pack(stage, target, out)
    except ConversionError:
        out.unlink(missing_ok=True)
        raise
    except Exception as exc:
        out.unlink(missing_ok=True)
        raise ConversionError(f"Couldn't convert {src.name} to {target.upper()} ({exc}).")
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return [out]
