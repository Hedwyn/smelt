"""
Packs an assembled distribution folder (see `smelt.dist`) into a single file.

Two shapes, and which one applies is decided by the distribution, not by an option:

* mode `byo` -- the target brings its own interpreter -- packs into an **executable
  zip application**: a `/bin/sh` preamble that locates a CPython, followed by a zip
  Python itself knows how to run. No compiler is involved and the artifact stays
  architecture-independent whenever its contents are. `python myapp` works on it just
  as well as `./myapp`, so the same file serves both ways of starting it.
* mode `own` -- the interpreter ships inside the folder -- packs into a **compiled
  launcher** (`launcher/launcher.zig`, built with `zig build-exe`) carrying the whole
  folder as an appended, compressed archive. There is no system interpreter to hand a
  zip to, so the artifact has to be directly executable, and a real executable is the
  only thing that can be.

Both shapes reconstitute (or run in place) *exactly* the folder `build_dist`
assembled: packing is a post-step over a finished distribution, never a second way of
assembling one. That is what keeps everything already verified about the folder --
RPATHs, interpreter tailoring, the entrypoint guards -- true of the single file too.

Where a shape has to extract before it can run, it extracts to a **content-addressed
cache** (`<cache root>/<app>-<digest of the payload>`), which gives caching and
invalidation for the price of a directory name: a rebuilt payload is a different
directory, and an unchanged one is extracted once and then only stat'ed. Extraction
goes to a scratch sibling and is `rename`d into place, so a half-extracted tree is
never visible -- not to a later run, and not to a second process starting
concurrently.

Nothing here is a *policy* decision point: the isolation flags and the interpreter
version check live in the generated `app/__main__.py` (see
`smelt.backend.isolation_guard` / `python_version_guard`) and run whichever way the
payload is reached. A launcher only has to produce a directory and hand it over.

@date: 05.09.2026
@author: Baptiste Pestourie
"""

from __future__ import annotations

import gzip
import hashlib
import logging
import lzma
import shutil
import stat
import sys
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Iterable, Literal

from smelt.backend import python_version_guard
from smelt.native_deps import describe_command_failure
from smelt.own_python import is_windows_zig_target
from smelt.process import call_command
from smelt.utils import PathExists, SmeltError, assert_path_exists

_logger = logging.getLogger(__name__)


class OnefileError(SmeltError):
    """
    Raised when a distribution folder cannot be packed into a single file.
    """


#: How the payload is compressed. `"xz"` is the smallest and the slowest to inflate,
#: `"gzip"` roughly halves the inflation time for a larger file, `"none"` stores the
#: payload as-is -- worth having for a payload that is already compressed at rest, and
#: for telling a packing problem apart from a compression one.
type OnefileCompression = Literal["xz", "gzip", "none"]

ONEFILE_COMPRESSIONS: Final[tuple[OnefileCompression, ...]] = ("xz", "gzip", "none")

DEFAULT_ONEFILE_COMPRESSION: Final[OnefileCompression] = "xz"

#: Whether a distribution is additionally packed into a single file. Off by default:
#: the folder is the shape that is inspectable, and packing is what you ask for when
#: the artifact has to travel rather than be looked at.
DEFAULT_ONEFILE: Final[bool] = False

#: `lzma` preset used for the payload. Deliberately the library default rather than
#: `9`: on a 30 MB interpreter tree the extra presets buy single-digit percentages of
#: size for several times the packing time, and the inflation cost is paid on the
#: target machine's first run.
XZ_PRESET: Final[int] = 6

#: Name of the archive member holding the payload in an extracting zip application.
#: Stored (never deflated) inside the zip: it is already compressed.
PAYLOAD_MEMBER_NAME: Final[str] = "smelt-payload.tar"

#: Written into an extracted directory once, last of all, and checked before that
#: directory is reused. Its absence is what tells a later run that an extraction was
#: interrupted rather than completed.
SENTINEL_NAME: Final[str] = ".smelt-complete"

#: Environment variable overriding where payloads are extracted. Read by both
#: launchers, and the answer to a read-only or otherwise unusual home directory.
CACHE_ENV_VAR: Final[str] = "SMELT_ONEFILE_CACHE"

#: Environment variable naming the interpreter a mode `byo` single file should use,
#: ahead of anything it would find on `PATH`.
PYTHON_ENV_VAR: Final[str] = "SMELT_PYTHON"

#: Characters kept in the cache directory name. Everything else is replaced, so that a
#: distribution named after a package with unusual characters cannot produce a path
#: neither launcher can spell.
_CACHE_NAME_ALLOWED: Final[str] = (
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)

#: How much of the payload digest goes into the cache directory name. 16 hex
#: characters of SHA-256: the digest identifies a build, and does not have to resist
#: anything -- a payload is only ever compared against payloads of the same
#: application on the same machine.
_DIGEST_CHARS: Final[int] = 16

# --------------------------------------------------------------------------------
# The compiled launcher's trailer. Read from the end of the file by
# `launcher/launcher.zig`, whose `Trailer` struct is the other half of this layout:
# the two must be changed together, which is why the offsets are spelled out on both
# sides rather than derived.
# --------------------------------------------------------------------------------

#: Magic bytes opening the trailer. The last byte is the trailer's format version, so
#: a launcher and a payload built by different smelt versions refuse each other
#: instead of half-understanding each other.
TRAILER_MAGIC: Final[bytes] = b"SMELTPK\x01"

TRAILER_SIZE: Final[int] = 256

_TRAILER_PAYLOAD_OFFSET: Final[int] = 8
_TRAILER_PAYLOAD_SIZE: Final[int] = 16
_TRAILER_COMPRESSION: Final[int] = 24
_TRAILER_CACHE_NAME: Final[int] = 32
_TRAILER_EXEC_REL: Final[int] = 96
_TRAILER_PAYLOAD_DIR: Final[int] = 160
_TRAILER_FIELD_SIZE: Final[int] = 64

_COMPRESSION_CODES: Final[dict[OnefileCompression, int]] = {"none": 0, "xz": 1, "gzip": 2}

#: The launcher's Zig source, compiled at packing time (see `build_launcher`).
LAUNCHER_SOURCE: Final[Path] = Path(__file__).parent / "launcher" / "launcher.zig"


@dataclass(frozen=True)
class PayloadArchive:
    """
    The distribution folder, as one compressed archive.

    `uncompressed_size` is the tar's own size, not the folder's: it is what the
    launcher's decompressor has to write out, which is the number worth reporting.
    """

    path: PathExists
    compression: OnefileCompression
    size: int
    uncompressed_size: int
    digest: str
    entries: int

    @property
    def ratio(self) -> float:
        if not self.uncompressed_size:
            return 1.0
        return self.size / self.uncompressed_size


@dataclass(frozen=True)
class OnefileArtifact:
    """
    The single file packed out of a distribution folder, and what running it does.

    `extracts` is the distinction that matters at runtime: a zip application whose
    payload is pure Python is imported straight out of the zip and needs no cache
    directory at all, while anything holding a native module, a data file or a
    namespace package has to become real files first (see `must_extract`).
    """

    path: Path
    kind: Literal["pyz", "binary"]
    compression: OnefileCompression
    size_bytes: int
    payload_bytes: int
    uncompressed_bytes: int
    extracts: bool
    cache_name: str | None = None

    def render(self) -> str:
        lines = [
            f"Onefile:      {self.path} ({self.size_bytes / 1e6:.1f} MB, {self.kind})",
            f"  payload:    {self.payload_bytes / 1e6:.1f} MB compressed "
            f"({self.compression}) from {self.uncompressed_bytes / 1e6:.1f} MB",
        ]
        if self.extracts:
            lines.append(f"  first run:  inflates to <cache>/{self.cache_name}")
        else:
            lines.append("  first run:  nothing to inflate, imported from the zip")
        return "\n".join(lines)

    def serialize(self) -> dict[str, object]:
        return {
            "path": self.path.name,
            "kind": self.kind,
            "compression": self.compression,
            "size_bytes": self.size_bytes,
            "payload_bytes": self.payload_bytes,
            "uncompressed_bytes": self.uncompressed_bytes,
            "extracts": self.extracts,
            "cache_name": self.cache_name,
        }


def resolve_compression(declared: str | None) -> OnefileCompression:
    """
    Validates a compression name read from the CLI or from `pyproject.toml`.
    """
    if declared is None:
        return DEFAULT_ONEFILE_COMPRESSION
    for compression in ONEFILE_COMPRESSIONS:
        if declared == compression:
            return compression
    raise OnefileError(
        f"Invalid onefile compression {declared!r}, expected one of {list(ONEFILE_COMPRESSIONS)}."
    )


def cache_name(app_name: str, digest: str) -> str:
    """
    Directory name a payload is extracted under: the application's name, so a user
    looking into the cache can tell what is there, plus a digest of the payload, so
    two builds never share a directory and an unchanged one is never re-extracted.

    Must fit the fixed-width trailer field, hence the truncation of the name half.
    """
    sanitized = "".join(
        character if character in _CACHE_NAME_ALLOWED else "-" for character in app_name
    )
    return f"{sanitized[:40]}-{digest[:_DIGEST_CHARS]}"


def _write_tar(root: PathExists, dest: Path) -> int:
    """
    Writes an uncompressed tar of `root`'s contents to `dest` and returns the number
    of entries.

    GNU tar format: `ustar` cannot hold the path lengths a standard library tree
    reaches, and `pax` writes per-entry extended headers carrying a float timestamp --
    which would cost the reproducibility `_normalized` is there to keep.
    """
    entries = 0
    # `dereference=False` is the default and is load-bearing here (see
    # `_iter_entries`); spelled out so it cannot be flipped by accident.
    with tarfile.open(dest, mode="w", format=tarfile.GNU_FORMAT, dereference=False) as tar:
        for entry in _iter_entries(root):
            info = tar.gettarinfo(str(entry), arcname=entry.relative_to(root).as_posix())
            _normalized(info)
            if info.isreg():
                with entry.open("rb") as handle:
                    tar.addfile(info, handle)
            else:
                tar.addfile(info)
            entries += 1
    return entries


def _compress(source: PathExists, dest: Path, compression: OnefileCompression) -> None:
    """
    Compresses `source` into `dest`.

    Done as a second pass over a finished tar rather than through a compressing
    fileobj underneath `tarfile`, for one reason worth recording: `tarfile`'s own
    `w:gz` mode stamps the current time into the gzip header, and a build whose output
    depends on when it ran gives up the byte-for-byte reproducibility the archive is
    deliberately built to have (`_normalized`). `mtime=0` here is the fix, and it is
    only reachable by driving `gzip` directly.
    """
    with source.open("rb") as handle:
        match compression:
            case "xz":
                with lzma.open(dest, "wb", format=lzma.FORMAT_XZ, preset=XZ_PRESET) as xz_sink:
                    shutil.copyfileobj(handle, xz_sink)
            case "gzip":
                with dest.open("wb") as raw:
                    with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gzip_sink:
                        shutil.copyfileobj(handle, gzip_sink)
            case "none":
                with dest.open("wb") as raw:
                    shutil.copyfileobj(handle, raw)


def _normalized(entry: tarfile.TarInfo) -> tarfile.TarInfo:
    """
    Strips everything from a tar entry that describes *this* machine rather than the
    file: timestamps, ownership, and the permission bits beyond the executable one.

    Byte-for-byte reproducibility of a build is an acceptance criterion of the folder
    (see `bytecode-dist-plan.md`, §11.9) and there is no reason for the packed form to
    give it up. Keeping the executable bit is not cosmetic: `bin/python` and the
    generated shim are unusable without it.
    """
    entry.mtime = 0
    entry.uid = 0
    entry.gid = 0
    entry.uname = ""
    entry.gname = ""
    if entry.isdir() or entry.mode & stat.S_IXUSR:
        entry.mode = 0o755
    else:
        entry.mode = 0o644
    return entry


def _iter_entries(root: Path) -> Iterable[Path]:
    """
    Every path under `root`, deepest-last and sorted at each level, so that two
    archives of the same tree hold their entries in the same order.

    Symbolic links are yielded but never followed: an interpreter tree holds
    `libpython3.X.so.1.0` as a link, and dereferencing it would double its size.
    """
    for entry in sorted(root.iterdir()):
        yield entry
        if entry.is_dir() and not entry.is_symlink():
            yield from _iter_entries(entry)


def build_payload_archive(
    root: PathExists,
    dest: Path,
    *,
    compression: OnefileCompression = DEFAULT_ONEFILE_COMPRESSION,
) -> PayloadArchive:
    """
    Archives the contents of `root` into `dest` and returns what went in.

    Paths inside the archive are relative to `root` itself, so unpacking into a
    directory reproduces the distribution folder rather than nesting it one level
    deeper.
    """
    with tempfile.TemporaryDirectory() as scratch:
        plain = Path(scratch) / "payload.tar"
        entries = _write_tar(root, plain)
        _compress(assert_path_exists(plain), dest, compression)
        uncompressed_size = plain.stat().st_size

    digest = hashlib.sha256()
    with dest.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    archive = PayloadArchive(
        path=assert_path_exists(dest),
        compression=compression,
        size=dest.stat().st_size,
        uncompressed_size=uncompressed_size,
        digest=digest.hexdigest(),
        entries=entries,
    )
    _logger.info(
        "Archived %s (%d entries) into %s: %.1f MB -> %.1f MB (%.0f%%)",
        root,
        archive.entries,
        dest.name,
        archive.uncompressed_size / 1e6,
        archive.size / 1e6,
        archive.ratio * 100,
    )
    return archive


def encode_trailer(
    *,
    payload_offset: int,
    payload_size: int,
    compression: OnefileCompression,
    cache_directory: str,
    exec_rel_path: str,
    payload_dir: str,
) -> bytes:
    """
    The fixed-layout block the compiled launcher reads from the end of its own file.

    Placed at the very end and read backwards precisely so that neither the ELF nor
    the PE header has to be understood to find it.
    """
    trailer = bytearray(TRAILER_SIZE)
    trailer[0 : len(TRAILER_MAGIC)] = TRAILER_MAGIC
    trailer[_TRAILER_PAYLOAD_OFFSET : _TRAILER_PAYLOAD_OFFSET + 8] = payload_offset.to_bytes(
        8, "little"
    )
    trailer[_TRAILER_PAYLOAD_SIZE : _TRAILER_PAYLOAD_SIZE + 8] = payload_size.to_bytes(8, "little")
    trailer[_TRAILER_COMPRESSION] = _COMPRESSION_CODES[compression]
    for offset, value, what in (
        (_TRAILER_CACHE_NAME, cache_directory, "cache directory name"),
        (_TRAILER_EXEC_REL, exec_rel_path, "interpreter path"),
        (_TRAILER_PAYLOAD_DIR, payload_dir, "payload directory name"),
    ):
        encoded = value.encode()
        if len(encoded) > _TRAILER_FIELD_SIZE:
            raise OnefileError(
                f"The {what} {value!r} does not fit the launcher's trailer "
                f"({len(encoded)} bytes, {_TRAILER_FIELD_SIZE} available)."
            )
        trailer[offset : offset + len(encoded)] = encoded
    return bytes(trailer)


def build_launcher(dest: Path, *, zig_target: str | None = None) -> PathExists:
    """
    Compiles `launcher/launcher.zig` to `dest` through the `ziglang` wheel.

    `-O ReleaseSmall` and a statically linked build: the resulting executable pulls in
    no libc of its own, which is the one property it cannot do without -- a mode `own`
    distribution exists to run where nothing is installed, and a launcher needing a
    dynamic loader before it can unpack that distribution would be a hole under it.

    Zig's build cache goes next to `dest` rather than into the current directory,
    which is otherwise where `zig build-exe` leaves a `.zig-cache` behind.
    """
    if not LAUNCHER_SOURCE.is_file():
        raise OnefileError(
            f"The launcher source is missing from the installation: {LAUNCHER_SOURCE}"
        )
    cmd = [
        sys.executable,
        "-m",
        "ziglang",
        "build-exe",
        "-O",
        "ReleaseSmall",
        f"-femit-bin={dest}",
        "--cache-dir",
        str(dest.parent / ".zig-cache"),
    ]
    if zig_target is not None:
        cmd.extend(["-target", zig_target])
    cmd.append(str(LAUNCHER_SOURCE))
    _logger.info("Building the onefile launcher: %s", " ".join(cmd))
    cmd_trace = call_command(*cmd)
    if cmd_trace.exit_code != 0 or not dest.is_file():
        raise OnefileError(
            "Could not compile the onefile launcher.\n" + describe_command_failure(cmd_trace, cmd)
        )
    dest.chmod(0o755)
    return assert_path_exists(dest)


#: The mode `byo` preamble: a `/bin/sh` script prepended to the zip application.
#:
#: A zip is read from its *end* (the end-of-central-directory record, whose offsets
#: are corrected for whatever precedes the archive), so arbitrary bytes may sit in
#: front of one. That is what makes a zipapp's `#!` line work at all; this is the same
#: trick with a script in place of the single line, which buys the three things a
#: shebang cannot express: an interpreter search order, `SMELT_PYTHON` as an override,
#: and a readable message when nothing is found instead of the kernel's
#: `bad interpreter: No such file or directory`.
#:
#: `exec "$candidate" "$0"` hands the file back to Python, which skips the preamble
#: and runs the zip. The exact minor version is tried first, because it is the one
#: that is right; the generic names follow so that an installation not carrying a
#: versioned executable still works, and the version guard inside the payload's own
#: `__main__` reports the mismatch if the fallback picks wrong.
_POSIX_PREAMBLE_TEMPLATE: Final[str] = """\
#!/bin/sh
# {name}: a smelt distribution packed into one file. Runs on a system CPython
# {version}; `python {name}` works on this file too.
for candidate in "${python_env}" python{version} python3 python; do
    [ -n "$candidate" ] || continue
    if command -v "$candidate" >/dev/null 2>&1; then
        exec "$candidate" "$0" "$@"
    fi
done
echo "{name}: no CPython {version} found (tried python{version}, python3, python)." >&2
echo "Install one, or point ${python_env} at it." >&2
exit 1
"""


def posix_preamble(name: str, python_version: tuple[int, int]) -> bytes:
    """
    The `/bin/sh` preamble for `name`, requiring CPython `python_version`.
    """
    major, minor = python_version
    return _POSIX_PREAMBLE_TEMPLATE.format(
        name=name, version=f"{major}.{minor}", python_env=PYTHON_ENV_VAR
    ).encode()


#: The `__main__` of an *extracting* zip application: it unpacks the payload into the
#: cache and re-executes the real entrypoint out of it.
#:
#: Its own version guard runs before anything is unpacked, which is the point of
#: having one here as well as in the payload: extracting tens of megabytes only to
#: have the interpreter reject the bytecode inside them would be a slow way to reach
#: the same message.
#:
#: The `os.execv` is isolated (`-I -S -B`) directly rather than left to the payload's
#: `isolation_guard`, which would otherwise re-execute a second time to add the flags.
_EXTRACTING_MAIN_TEMPLATE: Final[str] = '''\
{imports}

{guards}

_ARCHIVE = __loader__.archive
_MEMBER = "{member}"
_CACHE_NAME = "{cache_name}"
_PAYLOAD_DIR = "{payload_dir}"
_SENTINEL = "{sentinel}"
_TAR_MODE = "{tar_mode}"


def _cache_root() -> str:
    """
    Where payloads are extracted. Kept in step with the compiled launcher's own
    `cacheRoot`, deliberately including the last resort: a distribution that has to
    work under `env -i` cannot require HOME to be set.
    """
    override = os.environ.get("{cache_env}")
    if override:
        return override
    xdg_cache = os.environ.get("XDG_CACHE_HOME")
    if xdg_cache:
        return os.path.join(xdg_cache, "smelt")
    home = os.environ.get("HOME")
    if home:
        return os.path.join(home, ".cache", "smelt")
    return os.path.join(tempfile.gettempdir(), "smelt")


def _extract(target: str) -> None:
    """
    Unpacks into a scratch directory and renames it into place, so that neither a
    later run nor a concurrent one can ever see a half-extracted tree.
    """
    root = os.path.dirname(target)
    os.makedirs(root, exist_ok=True)
    scratch = tempfile.mkdtemp(prefix=".tmp-", dir=root)
    try:
        payload = __loader__.get_data(os.path.join(_ARCHIVE, _MEMBER))
        with tarfile.open(fileobj=io.BytesIO(payload), mode=_TAR_MODE) as archive:
            archive.extractall(scratch, filter="tar")
        with open(os.path.join(scratch, _SENTINEL), "wb"):
            pass
        try:
            os.rename(scratch, target)
        except OSError:
            # Another process unpacked the same payload first. The directory is named
            # after a digest of that payload, so its copy and ours are the same tree
            # by construction and there is nothing to reconcile.
            shutil.rmtree(scratch, ignore_errors=True)
    except BaseException:
        shutil.rmtree(scratch, ignore_errors=True)
        raise


if __name__ == "__main__":
    _target = os.path.join(_cache_root(), _CACHE_NAME)
    if not os.path.exists(os.path.join(_target, _SENTINEL)):
        _extract(_target)
    os.execv(
        sys.executable,
        [
            sys.executable,
            "-I",
            "-S",
            "-B",
            os.path.join(_target, _PAYLOAD_DIR),
            *sys.argv[1:],
        ],
    )
'''

#: Tar mode the extracting `__main__` opens the payload with, per compression.
_TAR_MODES: Final[dict[OnefileCompression, str]] = {"xz": "r:xz", "gzip": "r:gz", "none": "r:"}


def extracting_main(
    *,
    cache_directory: str,
    payload_dir: str,
    compression: OnefileCompression,
    python_version: tuple[int, int],
    magic_number: bytes,
) -> str:
    """
    Source of the bootstrap `__main__` an extracting zip application ships.
    """
    guard = python_version_guard(python_version, magic_number)
    imports = sorted({"io", "os", "shutil", "sys", "tarfile", "tempfile", *guard.imports})
    return _EXTRACTING_MAIN_TEMPLATE.format(
        imports="\n".join(f"import {name}" for name in imports),
        guards=guard.code,
        member=PAYLOAD_MEMBER_NAME,
        cache_name=cache_directory,
        payload_dir=payload_dir,
        sentinel=SENTINEL_NAME,
        tar_mode=_TAR_MODES[compression],
        cache_env=CACHE_ENV_VAR,
    )


def must_extract(*, has_natives: bool, has_data_files: bool, has_namespace_packages: bool) -> bool:
    """
    Whether a mode `byo` single file has to unpack itself before it can run, rather
    than being imported straight out of its own zip.

    Three things force it, each because `zipimport` cannot do it:

    * a **native module** -- `dlopen` needs a real file, and always will;
    * a **data file** -- it is shipped because something reads it relative to
      `__file__`, and inside a zip that path does not exist;
    * a **namespace package** -- `zipimport` implements none of PEP 420, so a portion
      of a namespace inside a zip is simply not found.

    Everything else (bytecode modules, `.dist-info` metadata) is imported from the zip
    directly, which is the case worth keeping: no cache directory, no first-run cost,
    and an artifact that stays architecture-independent.
    """
    return has_natives or has_data_files or has_namespace_packages


def _zip_compression(compression: OnefileCompression) -> int:
    """
    Zip member method for `compression`.

    `zipimport` reads stored and deflated members only, so a payload imported straight
    out of the zip cannot be `xz`-compressed however the build asked for it -- deflate
    is what `"xz"` means here. Stated rather than silently mapped, since it is the one
    place where the compression option does not do quite what it says.
    """
    return zipfile.ZIP_STORED if compression == "none" else zipfile.ZIP_DEFLATED


def _write_zip_application(
    dest: Path,
    *,
    name: str,
    python_version: tuple[int, int],
    members: Iterable[tuple[str, Path]],
    generated: Iterable[tuple[str, str]] = (),
    stored: Iterable[tuple[str, Path]] = (),
    compression: OnefileCompression,
) -> None:
    """
    Writes the `/bin/sh` preamble followed by a zip holding `members` (real files),
    `generated` (name and source of a generated text file) and `stored` (real files
    never compressed, for a payload that already is).
    """
    method = _zip_compression(compression)
    with dest.open("wb") as handle:
        handle.write(posix_preamble(name, python_version))
        with zipfile.ZipFile(handle, "w", compression=method) as archive:
            for arcname, source in members:
                archive.write(source, arcname)
            for arcname, text in generated:
                archive.writestr(arcname, text)
            for arcname, source in stored:
                archive.write(source, arcname, compress_type=zipfile.ZIP_STORED)
    dest.chmod(0o755)


def pack_zip_application(
    dist_root: PathExists,
    dest: Path,
    *,
    name: str,
    payload_dir: str,
    python_version: tuple[int, int],
    magic_number: bytes,
    extract: bool,
    compression: OnefileCompression = DEFAULT_ONEFILE_COMPRESSION,
    extra_root_files: Iterable[Path] = (),
) -> OnefileArtifact:
    """
    Packs a mode `byo` distribution into one executable zip application.

    With `extract` False the payload's own files go in at the zip root, so the
    application is imported straight out of the archive and its generated `__main__`
    -- guards included -- is the zip's `__main__`. With `extract` True the zip instead
    holds a compressed archive of the whole folder and a bootstrap `__main__` that
    unpacks it (see `extracting_main` and `must_extract`).

    `extra_root_files` are distribution-root files worth carrying into the
    non-extracting form (the manifest), which would otherwise be left behind with the
    folder.
    """
    payload_root = assert_path_exists(dist_root / payload_dir)
    if not extract:
        members = [
            (entry.relative_to(payload_root).as_posix(), entry)
            for entry in _iter_entries(payload_root)
            if entry.is_file()
        ]
        members.extend((entry.name, entry) for entry in extra_root_files if entry.is_file())
        _write_zip_application(
            dest,
            name=name,
            python_version=python_version,
            members=members,
            compression=compression,
        )
        size = dest.stat().st_size
        artifact = OnefileArtifact(
            path=dest,
            kind="pyz",
            compression=compression,
            size_bytes=size,
            payload_bytes=size,
            uncompressed_bytes=sum(source.stat().st_size for _, source in members),
            extracts=False,
        )
        _logger.info("Packed %s: %s", dest, artifact.render())
        return artifact

    with tempfile.TemporaryDirectory() as scratch:
        archive = build_payload_archive(
            dist_root, Path(scratch) / PAYLOAD_MEMBER_NAME, compression=compression
        )
        directory = cache_name(name, archive.digest)
        _write_zip_application(
            dest,
            name=name,
            python_version=python_version,
            members=(),
            generated=[
                (
                    "__main__.py",
                    extracting_main(
                        cache_directory=directory,
                        payload_dir=payload_dir,
                        compression=compression,
                        python_version=python_version,
                        magic_number=magic_number,
                    ),
                )
            ],
            stored=[(PAYLOAD_MEMBER_NAME, archive.path)],
            compression=compression,
        )
        artifact = OnefileArtifact(
            path=dest,
            kind="pyz",
            compression=compression,
            size_bytes=dest.stat().st_size,
            payload_bytes=archive.size,
            uncompressed_bytes=archive.uncompressed_size,
            extracts=True,
            cache_name=directory,
        )
    _logger.info("Packed %s: %s", dest, artifact.render())
    return artifact


def pack_executable(
    dist_root: PathExists,
    dest: Path,
    *,
    name: str,
    payload_dir: str,
    exec_rel_path: Path,
    compression: OnefileCompression = DEFAULT_ONEFILE_COMPRESSION,
    zig_target: str | None = None,
) -> OnefileArtifact:
    """
    Packs a mode `own` distribution into one executable: the compiled launcher, the
    compressed folder, and the trailer that ties them together.

    `exec_rel_path` is the bundled interpreter's path *within* the folder
    (`bin/python`); the launcher joins it onto the extracted directory, so the
    interpreter it starts is the shipped one and never the target's.

    `dest` gains a `.exe` suffix when `zig_target` names a Windows target and does not
    already carry one: Windows resolves what is executable by extension, unlike POSIX's
    permission bit, so the artifact needs the right name to be runnable at all.
    """
    if is_windows_zig_target(zig_target) and dest.suffix.lower() != ".exe":
        dest = dest.with_name(dest.name + ".exe")
    with tempfile.TemporaryDirectory() as scratch:
        archive = build_payload_archive(
            dist_root, Path(scratch) / PAYLOAD_MEMBER_NAME, compression=compression
        )
        launcher = build_launcher(Path(scratch) / "launcher", zig_target=zig_target)
        directory = cache_name(name, archive.digest)
        payload_offset = launcher.stat().st_size
        with dest.open("wb") as handle:
            with launcher.open("rb") as stub:
                shutil.copyfileobj(stub, handle)
            with archive.path.open("rb") as payload:
                shutil.copyfileobj(payload, handle)
            handle.write(
                encode_trailer(
                    payload_offset=payload_offset,
                    payload_size=archive.size,
                    compression=compression,
                    cache_directory=directory,
                    exec_rel_path=exec_rel_path.as_posix(),
                    payload_dir=payload_dir,
                )
            )
        dest.chmod(0o755)
        artifact = OnefileArtifact(
            path=dest,
            kind="binary",
            compression=compression,
            size_bytes=dest.stat().st_size,
            payload_bytes=archive.size,
            uncompressed_bytes=archive.uncompressed_size,
            extracts=True,
            cache_name=directory,
        )
    _logger.info("Packed %s: %s", dest, artifact.render())
    return artifact
