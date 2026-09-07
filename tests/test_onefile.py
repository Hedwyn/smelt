from __future__ import annotations

import ast
import importlib.util
import os
import platform
import re
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from smelt.onefile import (
    ONEFILE_COMPRESSIONS,
    PAYLOAD_MEMBER_NAME,
    SENTINEL_NAME,
    TRAILER_MAGIC,
    TRAILER_SIZE,
    OnefileCompression,
    OnefileError,
    build_payload_archive,
    cache_name,
    encode_trailer,
    extracting_main,
    must_extract,
    pack_executable,
    pack_zip_application,
    posix_preamble,
    resolve_compression,
)
from smelt.utils import assert_path_exists

LAUNCHER_SOURCE = Path(__file__).parent.parent / "src" / "smelt" / "launcher" / "launcher.zig"

is_linux = platform.system() == "Linux"


def _distribution(root: Path, *, executable: str = "print('payload main')\n") -> Path:
    """
    A distribution folder in miniature: a payload directory, a `bin/python` that is
    executable, a symlinked library and a manifest at the root.
    """
    (root / "app").mkdir(parents=True)
    (root / "app" / "__main__.py").write_text(executable)
    (root / "bin").mkdir()
    interpreter = root / "bin" / "python"
    interpreter.write_text('#!/bin/sh\necho "interpreter argv: $@"\n')
    interpreter.chmod(0o755)
    (root / "lib").mkdir()
    (root / "lib" / "libpython3.12.so").write_bytes(b"\x7fELF" + b"\x00" * 128)
    os.symlink("libpython3.12.so", root / "lib" / "libpython3.12.so.1.0")
    (root / "smelt-dist.json").write_text("{}")
    return root


# --------------------------------------------------------------------------------
# The trailer: a format read by two implementations, so both halves are checked.
# --------------------------------------------------------------------------------


def _trailer_field(trailer: bytes, offset: int) -> str:
    raw = trailer[offset : offset + 64]
    return raw.split(b"\x00", 1)[0].decode()


def test_trailer_carries_what_the_launcher_needs() -> None:
    trailer = encode_trailer(
        payload_offset=1234,
        payload_size=5678,
        compression="xz",
        cache_directory="myapp-0123456789abcdef",
        exec_rel_path="bin/python",
        payload_dir="app",
    )
    assert len(trailer) == TRAILER_SIZE
    assert trailer.startswith(TRAILER_MAGIC)
    assert int.from_bytes(trailer[8:16], "little") == 1234
    assert int.from_bytes(trailer[16:24], "little") == 5678
    assert trailer[24] == 1
    assert _trailer_field(trailer, 32) == "myapp-0123456789abcdef"
    assert _trailer_field(trailer, 96) == "bin/python"
    assert _trailer_field(trailer, 160) == "app"


def test_trailer_refuses_a_field_that_would_not_survive_the_round_trip() -> None:
    """
    Fixed-width fields truncate silently, and a truncated interpreter path is a
    launcher that cannot find its own interpreter on the target machine.
    """
    with pytest.raises(OnefileError, match="does not fit"):
        encode_trailer(
            payload_offset=0,
            payload_size=0,
            compression="xz",
            cache_directory="x" * 65,
            exec_rel_path="bin/python",
            payload_dir="app",
        )


def test_the_launcher_reads_the_trailer_this_module_writes() -> None:
    """
    The one contract with no shared source of truth: the offsets are spelled out in
    `onefile.py` and again in `launcher.zig`, and a silent disagreement between them
    produces a launcher that misreads its own payload rather than a build failure.
    """
    source = LAUNCHER_SOURCE.read_text()
    declared = {
        name: int(value)
        for name, value in re.findall(r"const (\w+_off|field_size) = (\d+);", source)
    }
    assert declared == {
        "magic_off": 0,
        "payload_offset_off": 8,
        "payload_size_off": 16,
        "compression_off": 24,
        "cache_name_off": 32,
        "exec_rel_off": 96,
        "payload_dir_off": 160,
        "field_size": 64,
    }
    assert 'const MAGIC = "SMELTPK\\x01"' in source
    assert f"const TRAILER_SIZE = {TRAILER_SIZE};" in source
    assert f'const SENTINEL = "{SENTINEL_NAME}";' in source


# --------------------------------------------------------------------------------
# The payload archive.
# --------------------------------------------------------------------------------


def test_payload_archive_is_reproducible(tmp_path: Path) -> None:
    """
    Two builds of one folder produce the same bytes. Not a nicety: the extraction
    directory is named after a digest of this archive, so an archive that varied with
    the clock would re-extract on every rebuild and never share a cache entry.
    """
    root = _distribution(tmp_path / "dist")
    first = build_payload_archive(assert_path_exists(root), tmp_path / "first.tar.xz")
    second = build_payload_archive(assert_path_exists(root), tmp_path / "second.tar.xz")
    assert first.digest == second.digest
    assert (tmp_path / "first.tar.xz").read_bytes() == (tmp_path / "second.tar.xz").read_bytes()


@pytest.mark.parametrize("compression", ONEFILE_COMPRESSIONS)
def test_payload_archive_round_trips_modes_and_symlinks(
    tmp_path: Path, compression: OnefileCompression
) -> None:
    """
    The executable bit and symbolic links both have to survive: `bin/python` is
    unusable without the first, and `libpython3.X.so.1.0` is a link an interpreter
    resolves at load time -- dereferencing it would silently double its size.
    """
    root = _distribution(tmp_path / "dist")
    archive = build_payload_archive(
        assert_path_exists(root),
        tmp_path / "payload",
        compression=compression,
    )
    unpacked = tmp_path / "unpacked"
    unpacked.mkdir()
    with tarfile.open(archive.path) as tar:
        tar.extractall(unpacked, filter="tar")
    assert (unpacked / "app" / "__main__.py").read_text() == "print('payload main')\n"
    assert os.access(unpacked / "bin" / "python", os.X_OK)
    assert (unpacked / "lib" / "libpython3.12.so.1.0").is_symlink()
    # relative to the folder, never to the machine that packed it
    assert not any(name.startswith("/") for name in tarfile.open(archive.path).getnames())


def test_payload_archive_strips_the_build_machine(tmp_path: Path) -> None:
    root = _distribution(tmp_path / "dist")
    archive = build_payload_archive(assert_path_exists(root), tmp_path / "payload")
    with tarfile.open(archive.path) as tar:
        for entry in tar.getmembers():
            assert entry.mtime == 0, entry.name
            assert entry.uid == entry.gid == 0, entry.name
            assert entry.uname == entry.gname == "", entry.name


# --------------------------------------------------------------------------------
# Options and naming.
# --------------------------------------------------------------------------------


def test_resolve_compression() -> None:
    assert resolve_compression(None) == "xz"
    assert resolve_compression("gzip") == "gzip"
    with pytest.raises(OnefileError, match="Invalid onefile compression"):
        resolve_compression("brotli")


def test_cache_name_is_spellable_and_unique_per_payload() -> None:
    name = cache_name("my app/v2", "0123456789abcdef" * 4)
    assert "/" not in name and " " not in name
    assert name.endswith("-0123456789abcdef")
    # the digest half is what distinguishes two builds; the name half only says what
    # a directory found in the cache belongs to
    assert cache_name("app", "a" * 64) != cache_name("app", "b" * 64)


def test_must_extract_names_what_zipimport_cannot_do() -> None:
    assert not must_extract(has_natives=False, has_data_files=False, has_namespace_packages=False)
    assert must_extract(has_natives=True, has_data_files=False, has_namespace_packages=False)
    assert must_extract(has_natives=False, has_data_files=True, has_namespace_packages=False)
    assert must_extract(has_natives=False, has_data_files=False, has_namespace_packages=True)


def test_posix_preamble_searches_before_it_gives_up() -> None:
    preamble = posix_preamble("myapp", (3, 12)).decode()
    assert preamble.startswith("#!/bin/sh\n")
    # the exact minor first, because it is the one that is right
    assert preamble.index("python3.12") < preamble.index("python3 python")
    assert "SMELT_PYTHON" in preamble
    assert 'exec "$candidate" "$0" "$@"' in preamble


def test_extracting_main_guards_before_it_unpacks() -> None:
    """
    The version check has to come first: extracting tens of megabytes only to have the
    interpreter reject the bytecode inside them is a slow way to reach the same
    message.
    """
    source = extracting_main(
        cache_directory="myapp-0123456789abcdef",
        payload_dir="app",
        compression="xz",
        python_version=(3, 12),
        magic_number=importlib.util.MAGIC_NUMBER,
    )
    ast.parse(source)
    assert source.index("_REQUIRED_VERSION") < source.index("def _extract")
    assert 'mode=_TAR_MODE' in source
    assert '"r:xz"' in source


# --------------------------------------------------------------------------------
# The two packed shapes, end to end.
# --------------------------------------------------------------------------------


@pytest.mark.skipif(not is_linux, reason="the /bin/sh preamble is POSIX-only")
def test_zip_application_runs_out_of_its_own_zip(tmp_path: Path) -> None:
    """
    The case worth keeping: a pure-Python payload is imported straight out of the zip,
    with no cache directory and no first-run cost.
    """
    root = _distribution(
        tmp_path / "dist",
        executable="import sys\nprint('ran', sys.argv[1:], __loader__.__class__.__name__)\n",
    )
    artifact = pack_zip_application(
        assert_path_exists(root),
        tmp_path / "myapp",
        name="myapp",
        payload_dir="app",
        python_version=sys.version_info[:2],
        magic_number=importlib.util.MAGIC_NUMBER,
        extract=False,
    )
    assert not artifact.extracts and artifact.cache_name is None
    # handed to an interpreter, exactly as the preamble would hand it to one
    answer = subprocess.run(
        [sys.executable, str(artifact.path), "alpha"], capture_output=True, text=True, check=True
    )
    assert answer.stdout.strip() == "ran ['alpha'] zipimporter"


@pytest.mark.skipif(not is_linux, reason="the /bin/sh preamble is POSIX-only")
def test_zip_application_extracts_when_it_has_to(tmp_path: Path) -> None:
    root = _distribution(
        tmp_path / "dist",
        executable="import sys\nprint('ran', sys.argv[1:], __file__)\n",
    )
    artifact = pack_zip_application(
        assert_path_exists(root),
        tmp_path / "myapp",
        name="myapp",
        payload_dir="app",
        python_version=sys.version_info[:2],
        magic_number=importlib.util.MAGIC_NUMBER,
        extract=True,
    )
    assert artifact.extracts and artifact.cache_name is not None
    cache = tmp_path / "cache"
    environment = {**os.environ, "SMELT_ONEFILE_CACHE": str(cache)}
    answer = subprocess.run(
        [sys.executable, str(artifact.path), "alpha"],
        capture_output=True,
        text=True,
        check=True,
        env=environment,
    )
    target = cache / artifact.cache_name
    assert (target / SENTINEL_NAME).is_file()
    assert str(target / "app") in answer.stdout
    # a second run finds the directory and unpacks nothing; the sentinel's timestamp
    # is what would change if it did
    stamp = (target / SENTINEL_NAME).stat().st_mtime_ns
    subprocess.run(
        [sys.executable, str(artifact.path)], capture_output=True, check=True, env=environment
    )
    assert (target / SENTINEL_NAME).stat().st_mtime_ns == stamp


@pytest.mark.skipif(not is_linux, reason="the compiled launcher is verified on Linux only")
def test_compiled_launcher_unpacks_and_starts_the_bundled_interpreter(tmp_path: Path) -> None:
    """
    The whole mode `own` contract in one run: the payload is inflated out of the
    executable itself, into a directory named after its digest, and the *bundled*
    interpreter is started with the payload directory and the caller's arguments.

    Compiles the launcher, so it is the slow test in this file. It is also the only
    one that checks the trailer against the implementation that has to read it.
    """
    root = _distribution(tmp_path / "dist")
    artifact = pack_executable(
        assert_path_exists(root),
        tmp_path / "myapp",
        name="myapp",
        payload_dir="app",
        exec_rel_path=Path("bin/python"),
    )
    assert artifact.kind == "binary" and artifact.cache_name is not None
    cache = tmp_path / "cache"
    answer = subprocess.run(
        [str(artifact.path), "alpha", "beta"],
        capture_output=True,
        text=True,
        check=True,
        # `env -i` but for the one variable that says where to unpack: a mode `own`
        # distribution's claim is that it needs nothing from the environment.
        env={"SMELT_ONEFILE_CACHE": str(cache)},
    )
    target = cache / artifact.cache_name
    assert answer.stdout.strip() == f"interpreter argv: {target / 'app'} alpha beta"
    assert (target / SENTINEL_NAME).is_file()
    assert (target / "lib" / "libpython3.12.so.1.0").is_symlink()

    # moved somewhere else entirely, it still runs: nothing was recorded about where
    # the executable sat when it was packed
    moved = tmp_path / "elsewhere" / "myapp"
    moved.parent.mkdir()
    moved.write_bytes(artifact.path.read_bytes())
    moved.chmod(0o755)
    relocated = subprocess.run(
        [str(moved), "gamma"],
        capture_output=True,
        text=True,
        check=True,
        env={"SMELT_ONEFILE_CACHE": str(cache)},
    )
    assert relocated.stdout.strip() == f"interpreter argv: {target / 'app'} gamma"


@pytest.mark.skipif(not is_linux, reason="the compiled launcher is verified on Linux only")
def test_compiled_launcher_reports_a_file_carrying_no_payload(tmp_path: Path) -> None:
    """
    The launcher is a real executable and can be run before anything is appended to
    it, or after being truncated. It has to say so rather than misread whatever bytes
    it finds at its own end.
    """
    from smelt.onefile import build_launcher

    launcher = build_launcher(tmp_path / "bare")
    answer = subprocess.run([str(launcher)], capture_output=True, text=True)
    assert answer.returncode != 0
    assert "no smelt payload" in answer.stderr


def test_payload_member_is_stored_rather_than_compressed_twice(tmp_path: Path) -> None:
    import zipfile

    root = _distribution(tmp_path / "dist")
    artifact = pack_zip_application(
        assert_path_exists(root),
        tmp_path / "myapp",
        name="myapp",
        payload_dir="app",
        python_version=sys.version_info[:2],
        magic_number=importlib.util.MAGIC_NUMBER,
        extract=True,
    )
    with zipfile.ZipFile(artifact.path) as archive:
        assert archive.getinfo(PAYLOAD_MEMBER_NAME).compress_type == zipfile.ZIP_STORED
