"""
Native (ELF) dependency resolution and RPATH rewriting for assembled distributions.

An extension module copied into a distribution is not self-contained: it carries
`DT_NEEDED` entries for the shared libraries it was linked against, and an RPATH that
was computed for wherever it used to live (a wheel typically points at its own
vendored libraries through something like `$ORIGIN/../../pkg.libs`). Once the file
sits in a distribution folder instead, that RPATH resolves to nothing and the library
is looked up in the host's default search path -- if it is there at all.

This module closes that gap: it walks the dependencies of everything placed in the
distribution, copies what should travel with it, and rewrites every RPATH so the
result resolves through `$ORIGIN` alone -- i.e. relative to the distribution folder,
wherever that folder ends up.

Reading a binary is done here, by parsing its own tables (`elf_dynamic_entries`,
`pe_imported_dll_names`) -- not by asking the system. `ldd`, the obvious tool for the
ELF half, works by *running the binary's own loader*, so it answers only for binaries
this machine can already run: it reports nothing at all for a musl target, a
cross-built one, or anything whose loader is not installed here, which is exactly the
case a standalone distribution is built for. Writing a binary is still `patchelf` as a
subprocess (RPATHs, program interpreter): a stable interface, and the whole point is
not to be coupled to the tool that produced the artifacts.

ELF/Linux and PE/Windows only. On other platforms (Darwin) the equivalent tooling
differs entirely (`otool`/`install_name_tool`), so callers get a clear "not resolved
here" answer rather than a silently incomplete folder.

The two platforms differ in one way that matters beyond which file format is walked:
ELF has RPATH, a lever that makes a dependency placed anywhere resolve relative to a
binary that names it explicitly, so Linux bundles everything flat at one destination
and rewrites every consumer to look there. PE has no such lever -- the loader's
"altered search path" for an extension module covers only that module's own
directory -- so the Windows path instead places a copy of each dependency next to
every directory that needs one. Same public contract (`BundledNatives`), different
placement strategy underneath.

@date: 03.09.2026
@author: Baptiste Pestourie
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import struct
import sys
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Final, Literal

from smelt.process import CommandContext, call_command
from smelt.utils import PathExists, SmeltError, assert_path_exists

_logger = logging.getLogger(__name__)


#: Libraries that must always come from the running system, whatever the distribution
#: ships: `linux-vdso` has no backing file at all, and `libnss_*` has to match the
#: host's own `/etc/nsswitch.conf` to resolve users and hosts correctly.
ALWAYS_HOST_DLL_PREFIXES: Final[tuple[str, ...]] = ("linux-vdso.so", "libnss_")

#: glibc's base libraries. Not bundled: the loader and libc are a tightly ABI-coupled
#: pair (internal TLS and data layout, not just public symbol versions), so shipping
#: one without exactly the matching other segfaults inside the loader before any code
#: runs. Making a distribution independent of the host's libc is not a bundling
#: problem -- it needs an interpreter built against a libc that ships as a single
#: combined loader (musl), which is a separate feature.
LINUX_BASE_LIBC_DLL_PREFIXES: Final[tuple[str, ...]] = (
    "ld-linux",
    "libc.so",
    "libpthread.so",
    "libm.so",
    "libdl.so",
    "librt.so",
    "libutil.so",
    "libresolv.so",
    "libnsl.so",
    "libanl.so",
    "libBrokenLocale.so",
    "libcidn.so",
    "libcrypt.so",
    "libmemusage.so",
    "libmvec.so",
    "libpcprofile.so",
    "libSegFault.so",
    "libthread_db",
)

#: libpython, excluded on purpose. The interpreter running the distribution already
#: provides it: where it is dynamically linked, its own `libpython` is loaded before
#: any extension module is imported and satisfies that module's `DT_NEEDED` entry by
#: soname. Bundling the build machine's copy instead would risk loading two different
#: libpythons into one process, which is far worse than the case it would fix (a
#: statically linked host interpreter, where an extension module needing `libpython`
#: cannot be satisfied by anything we could ship either).
HOST_PYTHON_DLL_PREFIXES: Final[tuple[str, ...]] = ("libpython",)

#: What a "bring your own python" distribution does not bundle.
LINUX_SYSTEM_DLL_IGNORE_PREFIXES: Final[tuple[str, ...]] = (
    *ALWAYS_HOST_DLL_PREFIXES,
    *LINUX_BASE_LIBC_DLL_PREFIXES,
    *HOST_PYTHON_DLL_PREFIXES,
)

#: DLLs Windows itself always provides, lowercased. Unlike glibc there is no ABI
#: reason to exclude these -- they are simply never absent, shipped by the OS since
#: long before any target smelt builds for, so bundling a copy would be dead weight
#: rather than a compatibility fix.
WINDOWS_OS_DLL_PREFIXES: Final[tuple[str, ...]] = (
    "kernel32",
    "kernelbase",
    "ntdll",
    "user32",
    "gdi32",
    "advapi32",
    "ole32",
    "oleaut32",
    "shell32",
    "shlwapi",
    "comctl32",
    "comdlg32",
    "ws2_32",
    "winmm",
    "crypt32",
    "cryptsp",
    "secur32",
    "sechost",
    "rpcrt4",
    "msvcrt",
    "bcrypt",
    "version",
    "imm32",
    "setupapi",
    "psapi",
    "userenv",
    "netapi32",
    "wtsapi32",
    "dbghelp",
    "iphlpapi",
    "dnsapi",
    "winhttp",
    "wininet",
    "api-ms-win-",
    "ext-ms-win-",
)

#: `pythonXY.dll`, excluded for the same reason as `HOST_PYTHON_DLL_PREFIXES`: the
#: interpreter running the distribution already provides it.
WINDOWS_HOST_PYTHON_DLL_PREFIXES: Final[tuple[str, ...]] = ("python3",)

#: What a "bring your own python" distribution does not bundle, on Windows. Names are
#: matched lowercased -- see `pe_dependencies`.
WINDOWS_SYSTEM_DLL_IGNORE_PREFIXES: Final[tuple[str, ...]] = (
    *WINDOWS_OS_DLL_PREFIXES,
    *WINDOWS_HOST_PYTHON_DLL_PREFIXES,
)


class NativeDepsError(SmeltError):
    """
    Raised when native dependencies cannot be resolved or rewritten.
    """


def describe_command_failure(cmd_trace: CommandContext, cmd: Iterable[str]) -> str:
    """
    Renders a failed command's exit code alongside its captured stdout/stderr,
    so callers get the actual compiler/tool output rather than just an exit code.
    """
    lines = [f"exitcode {cmd_trace.exit_code}: {' '.join(cmd)}"]
    if cmd_trace.stdout:
        lines.append("stdout:\n" + "\n".join(cmd_trace.stdout))
    if cmd_trace.stderr:
        lines.append("stderr:\n" + "\n".join(cmd_trace.stderr))
    return "\n".join(lines)


#: Which binary format's dependencies are being walked -- `"elf"` (`ldd`/`patchelf`,
#: needs a real Linux loader to run) or `"pe"` (a static import-table parse, needing
#: nothing from the running host). This is a property of the *artifact* being walked,
#: not of the host running smelt: cross-compiling a Windows target from Linux still
#: walks PE files, so callers that know their target ahead of time (like
#: `smelt.own_python`, staging a cross-built interpreter) should pass it explicitly
#: rather than let it default from `platform.system()`.
type BinaryFormat = Literal["elf", "pe"]


def _resolve_binary_format(binary_format: BinaryFormat | None) -> BinaryFormat:
    """
    `binary_format` if given, otherwise the one matching the running host -- the
    right default for the common case (bundling wheel-vendored `.so`/`.pyd` files,
    which are only ever relevant on a target matching the build host today).
    """
    if binary_format is not None:
        return binary_format
    return "pe" if platform.system() == "Windows" else "elf"


def is_supported_platform(binary_format: BinaryFormat | None = None) -> bool:
    """
    Whether native dependency resolution is implemented for `binary_format` (or, left
    unspecified, for the running host).

    PE is always "supported" regardless of host: it is a static parse of the import
    table, needing no loader to run (see `pe_dependencies`). ELF needs an actual Linux
    host, since it shells out to `ldd`, which asks the running loader directly.
    """
    if _resolve_binary_format(binary_format) == "pe":
        return True
    return platform.system() == "Linux"


def bundled_patchelf_dir() -> Path | None:
    """
    Returns the directory holding the PyPI-provided `patchelf` binary, or None.

    `patchelf` is needed to rewrite RPATHs on Linux, and is located by bare name via
    PATH. Distributions ship varied (sometimes known-buggy, e.g. 0.18.0) versions, so
    smelt bundles a known-good one via the `patchelf` PyPI package to stay
    self-contained. This locates that binary independently of PATH ordering so it can
    be put first.
    """
    import importlib.metadata as md

    try:
        dist = md.distribution("patchelf")
    except md.PackageNotFoundError:
        return None
    for entry in dist.files or []:
        if entry.name == "patchelf" or entry.name.startswith("patchelf."):
            located = Path(entry.locate()).resolve()
            if located.exists():
                return located.parent
    return None


@contextmanager
def patchelf_on_path() -> Iterator[None]:
    """
    Prepends the bundled `patchelf` directory to PATH, if that package is installed.
    """
    bindir = bundled_patchelf_dir()
    if bindir is None:
        yield
        return
    previous = os.environ.get("PATH")
    os.environ["PATH"] = f"{bindir}{os.pathsep}{previous}" if previous else str(bindir)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = previous


class _ElfFormatError(Exception):
    """
    Raised internally when a file does not have the shape the ELF reader expects. Never
    escapes `elf_dynamic_entries`: a file it cannot make sense of (a data file mistaken
    for an ELF, a linker script, a statically linked object with no dynamic section)
    simply has no dependencies to walk.
    """


def elf_dynamic_entries(binary_path: str | os.PathLike[str]) -> tuple[list[str], list[str]]:
    """
    Statically reads an ELF file's dynamic section and returns
    `(DT_NEEDED names, DT_RPATH/DT_RUNPATH entries)`.

    Nothing is executed and nothing about the running host is consulted, which is the
    point: this is the ELF counterpart of `pe_imported_dll_names`, and it answers the
    same question for a musl binary, a foreign-architecture one, or a file whose loader
    is not installed here -- none of which `ldd` can read, since `ldd` works by running
    the binary's own loader.

    RPATH entries are returned with `$ORIGIN` already expanded against the file's own
    directory (that is what makes a distribution's `$ORIGIN/../lib` resolvable), and
    with `$LIB`/`$PLATFORM` expanded the way the loader would on this machine -- those
    two are rare and machine-dependent, and getting them slightly wrong only costs a
    directory that would not have matched anyway.

    Addresses are translated through `PT_LOAD` segments rather than through section
    headers: a stripped shared library keeps its program headers but may have lost the
    section table entirely.
    """
    try:
        data = Path(binary_path).read_bytes()
    except OSError:
        return [], []
    try:
        needed, rpaths = _parse_elf_dynamic(data)
    except (_ElfFormatError, struct.error, UnicodeDecodeError):
        return [], []
    origin = str(Path(binary_path).resolve().parent)
    return needed, [_expand_dynamic_tokens(entry, origin) for entry in rpaths]


#: The dynamic-section tags the dependency walk reads: what the file needs, where it
#: says to look for it, and the string table those names live in.
_DT_NEEDED: Final[int] = 1
_DT_STRTAB: Final[int] = 5
_DT_RPATH: Final[int] = 15
_DT_RUNPATH: Final[int] = 29


def _parse_elf_dynamic(data: bytes) -> tuple[list[str], list[str]]:
    """
    The parse behind `elf_dynamic_entries`, raising `_ElfFormatError` on anything that
    is not an ELF file with a dynamic section.
    """
    if len(data) < 64 or data[:4] != b"\x7fELF":
        raise _ElfFormatError("not an ELF file")
    is_64 = data[4] == 2
    if data[4] not in (1, 2):
        raise _ElfFormatError(f"unknown ELF class {data[4]}")
    endian = "<" if data[5] == 1 else ">"

    # Program header table: offset, entry size and count, at class-dependent offsets.
    if is_64:
        (ph_offset,) = struct.unpack_from(f"{endian}Q", data, 0x20)
        ph_entry_size, ph_count = struct.unpack_from(f"{endian}HH", data, 0x36)
    else:
        (ph_offset,) = struct.unpack_from(f"{endian}I", data, 0x1C)
        ph_entry_size, ph_count = struct.unpack_from(f"{endian}HH", data, 0x2A)

    loads: list[tuple[int, int, int]] = []  # (vaddr, file offset, size)
    dynamic: tuple[int, int] | None = None  # (file offset, size)
    for index in range(ph_count):
        base = ph_offset + index * ph_entry_size
        if base + ph_entry_size > len(data):
            raise _ElfFormatError("truncated program header table")
        (p_type,) = struct.unpack_from(f"{endian}I", data, base)
        if is_64:
            p_offset, p_vaddr = struct.unpack_from(f"{endian}QQ", data, base + 0x08)
            (p_filesz,) = struct.unpack_from(f"{endian}Q", data, base + 0x20)
        else:
            p_offset, p_vaddr = struct.unpack_from(f"{endian}II", data, base + 0x04)
            (p_filesz,) = struct.unpack_from(f"{endian}I", data, base + 0x10)
        if p_type == 1:  # PT_LOAD
            loads.append((p_vaddr, p_offset, p_filesz))
        elif p_type == 2:  # PT_DYNAMIC
            dynamic = (p_offset, p_filesz)
    if dynamic is None:
        raise _ElfFormatError("no dynamic section")

    entry_size = 16 if is_64 else 8
    tag_format = f"{endian}{'qQ' if is_64 else 'iI'}"
    entries: list[tuple[int, int]] = []
    offset, size = dynamic
    for position in range(offset, min(offset + size, len(data)) - entry_size + 1, entry_size):
        tag, value = struct.unpack_from(tag_format, data, position)
        if tag == 0:  # DT_NULL terminates the section
            break
        entries.append((tag, value))

    strtab = next((value for tag, value in entries if tag == _DT_STRTAB), None)
    if strtab is None:
        raise _ElfFormatError("no string table")
    strtab_offset = _vaddr_to_offset(loads, strtab)
    if strtab_offset is None:
        raise _ElfFormatError("string table outside every loadable segment")

    needed: list[str] = []
    rpaths: list[str] = []
    for tag, value in entries:
        if tag not in (_DT_NEEDED, _DT_RPATH, _DT_RUNPATH):
            continue
        text = _read_c_string(data, strtab_offset + value)
        if not text:
            continue
        if tag == _DT_NEEDED:
            needed.append(text)
        else:
            rpaths.extend(part for part in text.split(":") if part)
    return needed, rpaths


def _vaddr_to_offset(loads: Iterable[tuple[int, int, int]], vaddr: int) -> int | None:
    """
    Translates a virtual address to a file offset through the `PT_LOAD` segment
    containing it, or None when no segment does.
    """
    for segment_vaddr, segment_offset, segment_size in loads:
        if segment_vaddr <= vaddr < segment_vaddr + segment_size:
            return segment_offset + (vaddr - segment_vaddr)
    return None


def _read_c_string(data: bytes, offset: int) -> str:
    """
    The NUL-terminated string at `offset`, or "" when it runs off the end of the file.
    """
    if not 0 <= offset < len(data):
        return ""
    end = data.find(b"\x00", offset)
    if end == -1:
        return ""
    return data[offset:end].decode("utf-8", errors="replace")


def _expand_dynamic_tokens(entry: str, origin: str) -> str:
    """
    Expands the dynamic string tokens an RPATH entry may carry: `$ORIGIN` (the
    directory of the file that declared it -- what makes a relocatable folder work),
    plus `$LIB` and `$PLATFORM`, whose values are the running machine's.
    """
    replacements = {
        "$ORIGIN": origin,
        "${ORIGIN}": origin,
        "$LIB": "lib64" if sys.maxsize > 2**32 else "lib",
        "${LIB}": "lib64" if sys.maxsize > 2**32 else "lib",
        "$PLATFORM": platform.machine(),
        "${PLATFORM}": platform.machine(),
    }
    for token, value in replacements.items():
        entry = entry.replace(token, value)
    return entry


#: Where the loader looks for a library that neither an RPATH nor the caller's own
#: search set accounts for. Read from `/etc/ld.so.conf` (which is what builds
#: `ld.so.cache` in the first place) plus the directories every glibc system has,
#: whether or not the configuration mentions them.
_FALLBACK_LIBRARY_DIRS: Final[tuple[str, ...]] = (
    "/lib",
    "/lib64",
    "/usr/lib",
    "/usr/lib64",
    "/usr/local/lib",
    "/usr/local/lib64",
)


@lru_cache(maxsize=1)
def system_library_dirs() -> tuple[str, ...]:
    """
    The host's own library directories, in the order the loader would search them.

    Only relevant when walking a *native* binary: a cross-built one's dependencies are
    not installed here at all, and are resolved out of the distribution's own folders
    instead (`collect_native_dependencies`' search set).
    """
    found: list[str] = []
    for directory in _ld_so_conf_dirs(Path("/etc/ld.so.conf")):
        if directory not in found:
            found.append(directory)
    for directory in _FALLBACK_LIBRARY_DIRS:
        if directory not in found and Path(directory).is_dir():
            found.append(directory)
    return tuple(found)


def _ld_so_conf_dirs(conf: Path, _depth: int = 0) -> list[str]:
    """
    The directories listed in an `ld.so.conf`-style file, following its `include`
    directives (glob patterns, which is how `/etc/ld.so.conf.d/*.conf` is reached).

    Recursion is bounded: an `include` cycle is a broken system configuration, not
    something to hang the build on.
    """
    if _depth > 4:
        return []
    try:
        text = conf.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    found: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("include"):
            pattern = line[len("include") :].strip()
            if not pattern:
                continue
            base = conf.parent if not pattern.startswith("/") else Path("/")
            for included in sorted(base.glob(pattern.lstrip("/"))):
                found.extend(_ld_so_conf_dirs(included, _depth + 1))
            continue
        if Path(line).is_dir():
            found.append(line)
    return found


def elf_identity(binary_path: str | os.PathLike[str]) -> tuple[int, int, int] | None:
    """
    `(class, endianness, machine)` out of an ELF header -- 64-bit vs 32-bit, little vs
    big endian, and the architecture -- or None for a file that is not an ELF.

    What it is for: deciding whether a candidate file can actually satisfy another
    binary's `DT_NEEDED` entry. The loader makes the same check, and it is not
    academic -- a 64-bit binary looking for `libc.so.6` on a machine that also ships
    32-bit libraries would otherwise resolve to the wrong one (`/usr/lib32` comes
    first in `ld.so.conf` here), and a cross-built extension module would resolve
    against this host's libraries.

    A libc mismatch is *not* visible here: a musl and a glibc build of the same
    library have identical ELF headers. That one is handled by not searching the
    host's directories at all for a foreign-libc target (see `elf_dependencies`).
    """
    try:
        with Path(binary_path).open("rb") as handle:
            header = handle.read(20)
    except OSError:
        return None
    if len(header) < 20 or header[:4] != b"\x7fELF":
        return None
    endian = "<" if header[5] == 1 else ">"
    (machine,) = struct.unpack_from(f"{endian}H", header, 0x12)
    return header[4], header[5], machine


def elf_dependencies(
    binary_path: str | os.PathLike[str],
    search_dirs: Iterable[str | os.PathLike[str]],
    *,
    include_system_dirs: bool = True,
) -> dict[str, str]:
    """
    Resolves `binary_path`'s `DT_NEEDED` entries, returning `{basename: resolved_path}`
    for every one found.

    Search order mirrors the loader's, minus the parts that would make the answer
    depend on the environment smelt happens to run in (`LD_LIBRARY_PATH` and
    `ld.so.cache` are both deliberately left out): the file's own RPATH/RUNPATH first,
    then the directories the caller knows are worth bundling from (a distribution's own
    folders), then the host's library directories.

    A name found nowhere is skipped rather than raised on, exactly like
    `pe_dependencies`: it is either provided by the target machine (libc and its
    loader, see `LINUX_BASE_LIBC_DLL_PREFIXES`) or belongs to a target whose libraries
    are not installed on this build machine at all.

    `include_system_dirs=False` drops the last step, for walking a binary built for a
    different libc or architecture: this host's `libz.so.1` is not a substitute for the
    target's, and copying it in would produce a folder that fails on the target rather
    than one that is missing a file.
    """
    needed, rpaths = elf_dynamic_entries(binary_path)
    if not needed:
        return {}
    identity = elf_identity(binary_path)
    directories = [
        *rpaths,
        *(str(entry) for entry in search_dirs),
        *(system_library_dirs() if include_system_dirs else ()),
    ]
    deps: dict[str, str] = {}
    for name in needed:
        # A name with a path separator is a path, not a soname to look up (musl's own
        # loader and glibc's agree on this).
        candidates = (
            [Path(name)] if "/" in name else [Path(directory) / name for directory in directories]
        )
        for candidate in candidates:
            if not candidate.is_file():
                continue
            if identity is not None and elf_identity(candidate) != identity:
                continue  # right name, wrong architecture -- see `elf_identity`
            deps[name] = str(candidate.resolve())
            break
    return deps


class _PeFormatError(Exception):
    """
    Raised internally when a file does not have the shape `pe_imported_dll_names`
    expects. Never escapes that function: a file it cannot make sense of (a data file
    mistaken for a PE, a `.pyd` built for a foreign architecture) simply has no
    dependencies to walk, exactly like `elf_dynamic_entries` treats a file with no
    dynamic section.
    """


def _pe_sections(data: bytes) -> tuple[list[tuple[int, int, int]], int, int]:
    """
    Parses just enough of a PE/COFF header to answer one question: where does the
    import directory table live, and how do RVAs (the only kind of address a PE header
    speaks) translate to file offsets.

    Returns `(sections, import_directory_rva, import_directory_size)`, where each
    section is `(virtual_address, virtual_size, pointer_to_raw_data)`.
    """
    if len(data) < 0x40 or data[0:2] != b"MZ":
        raise _PeFormatError("missing MZ signature")
    (e_lfanew,) = struct.unpack_from("<I", data, 0x3C)
    if data[e_lfanew : e_lfanew + 4] != b"PE\x00\x00":
        raise _PeFormatError("missing PE signature")
    coff_off = e_lfanew + 4
    try:
        _machine, num_sections, _timestamp, _symtab, _numsyms, opt_size, _flags = (
            struct.unpack_from("<HHIIIHH", data, coff_off)
        )
    except struct.error as exc:
        raise _PeFormatError("truncated COFF header") from exc
    opt_off = coff_off + 20
    if opt_size < 2:
        raise _PeFormatError("no optional header")
    try:
        (magic,) = struct.unpack_from("<H", data, opt_off)
    except struct.error as exc:
        raise _PeFormatError("truncated optional header") from exc
    if magic == 0x10B:  # PE32
        data_dir_off = opt_off + 96
    elif magic == 0x20B:  # PE32+
        data_dir_off = opt_off + 112
    else:
        raise _PeFormatError(f"unknown optional header magic {magic:#x}")
    try:
        # Data directory index 1 is always the import directory table.
        import_dir_rva, import_dir_size = struct.unpack_from("<II", data, data_dir_off + 8)
    except struct.error as exc:
        raise _PeFormatError("truncated data directories") from exc

    section_table_off = opt_off + opt_size
    sections: list[tuple[int, int, int]] = []
    try:
        for i in range(num_sections):
            off = section_table_off + i * 40
            _name, vsize, vaddr, _rawsize, rawptr = struct.unpack_from("<8sIIII", data, off)
            sections.append((vaddr, vsize, rawptr))
    except struct.error as exc:
        raise _PeFormatError("truncated section table") from exc
    return sections, import_dir_rva, import_dir_size


def _rva_to_offset(sections: Iterable[tuple[int, int, int]], rva: int) -> int | None:
    """
    Translates a relative virtual address into a file offset, by finding which
    section it falls in. `None` when nothing claims it -- a hole in the PE, or one
    this parser misread.
    """
    for vaddr, vsize, rawptr in sections:
        if vaddr <= rva < vaddr + max(vsize, 1):
            return rawptr + (rva - vaddr)
    return None


def pe_imported_dll_names(binary_path: str | os.PathLike[str]) -> list[str]:
    """
    Statically reads the import directory table of a PE file (`.exe`, `.dll`, `.pyd`)
    and returns the DLL names it imports from, in whatever case the linker wrote them.

    Like its ELF counterpart, this never runs anything -- there is no Windows loader to
    run when cross-building from Linux, and the import table names a dependency exactly
    as it will be looked up regardless. Names are returned
    unresolved: the loader's search order is not reproduced here, only the question
    "what does this file say it needs" (see `pe_dependencies` for resolution against a
    known set of candidate directories).
    """
    try:
        data = Path(binary_path).read_bytes()
    except OSError:
        return []
    try:
        sections, import_dir_rva, import_dir_size = _pe_sections(data)
    except _PeFormatError:
        return []
    if not import_dir_rva or not import_dir_size:
        return []
    pos = _rva_to_offset(sections, import_dir_rva)
    if pos is None:
        return []

    names: list[str] = []
    entry_size = 20
    while pos + entry_size <= len(data):
        try:
            _orig_thunk, _timestamp, _fwd_chain, name_rva, _first_thunk = struct.unpack_from(
                "<IIIII", data, pos
            )
        except struct.error:
            break
        if name_rva == 0:
            break
        name_off = _rva_to_offset(sections, name_rva)
        if name_off is not None:
            end = data.find(b"\x00", name_off)
            if end != -1:
                names.append(data[name_off:end].decode("ascii", errors="replace"))
        pos += entry_size
    return names


def pe_dependencies(
    binary_path: str | os.PathLike[str],
    search_dirs: Iterable[str | os.PathLike[str]],
) -> dict[str, str]:
    """
    Resolves `binary_path`'s imported DLLs against `search_dirs`, returning
    `{lowercased_name: resolved_path}` for every one found there.

    A name not found in `search_dirs` is assumed to be provided by the target Windows
    installation itself (or filtered out upstream via `WINDOWS_SYSTEM_DLL_IGNORE_PREFIXES`)
    rather than treated as an error: there is no live loader to ask "would this actually
    resolve on the target", only the directories the caller already knows are worth
    bundling from. Matching (and the returned keys) is lowercased explicitly rather
    than left to the filesystem: NTFS resolves DLL names case-insensitively, but a
    build cross-compiling for Windows from a case-sensitive filesystem would otherwise
    miss a dependency whose on-disk case does not match the import table's.
    """
    deps: dict[str, str] = {}
    listings: dict[Path, dict[str, Path]] = {}
    for name in pe_imported_dll_names(binary_path):
        lname = name.lower()
        for directory in (Path(directory) for directory in search_dirs):
            listing = listings.get(directory)
            if listing is None:
                try:
                    listing = {
                        entry.name.lower(): entry
                        for entry in directory.iterdir()
                        if entry.is_file()
                    }
                except OSError:
                    listing = {}
                listings[directory] = listing
            match = listing.get(lname)
            if match is not None:
                deps[lname] = str(match.resolve())
                break
    return deps


def _binary_dependencies(
    binary_path: str,
    search_dirs: Iterable[str | os.PathLike[str]],
    binary_format: BinaryFormat,
    *,
    include_system_dirs: bool,
) -> dict[str, str]:
    """
    Dispatches to whichever of `elf_dependencies`/`pe_dependencies` matches
    `binary_format`.
    """
    if binary_format == "pe":
        return pe_dependencies(binary_path, search_dirs)
    return elf_dependencies(binary_path, search_dirs, include_system_dirs=include_system_dirs)


def collect_native_dependencies(
    seed_paths: Iterable[str | os.PathLike[str]],
    ignore_prefixes: tuple[str, ...] = LINUX_SYSTEM_DLL_IGNORE_PREFIXES,
    *,
    binary_format: BinaryFormat | None = None,
    include_system_dirs: bool = True,
) -> dict[str, str]:
    """
    Recursively walks every path in `seed_paths` (and every dependency found along the
    way), returning `{basename: resolved_path}` for everything worth bundling -- i.e.
    excluding `ignore_prefixes`.

    `binary_format` defaults to whichever matches the running host (see
    `_resolve_binary_format`); pass it explicitly when walking a foreign target's
    binaries (a cross-built Windows interpreter staged from a Linux host, say).

    Neither format runs anything: both are static parses of the file's own tables (see
    `elf_dynamic_entries`, `pe_imported_dll_names`), resolved against a search set built
    from every seed's own directory plus every directory a dependency was found in
    along the way -- the same folders `bundle_native_dependencies` is about to place
    things into -- and, for ELF, the declared RPATH/RUNPATH plus this host's library
    directories.

    `include_system_dirs=False` drops that last step: pass it when the binaries were
    built for a libc or an architecture this machine does not have, where a
    same-named host library is not a substitute but a broken distribution.

    On a same-basename conflict (two different resolved paths reporting the same file
    name) the first one found is kept, and the rest are logged and dropped: there is
    only one flat destination per name, and picking silently would hide a genuine
    version conflict between two dependencies.
    """
    fmt = _resolve_binary_format(binary_format)
    collected: dict[str, str] = {}
    pending = [str(seed) for seed in seed_paths]
    seen: set[str] = set()
    search_dirs = {str(Path(seed).parent) for seed in pending}
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        search_dirs.add(str(Path(current).parent))
        for basename, resolved in _binary_dependencies(
            current, search_dirs, fmt, include_system_dirs=include_system_dirs
        ).items():
            if basename.startswith(ignore_prefixes):
                continue
            existing = collected.get(basename)
            if existing is not None:
                if existing != resolved:
                    _logger.warning(
                        "Conflicting native dependency %r: keeping %r, ignoring %r",
                        basename,
                        existing,
                        resolved,
                    )
                continue
            collected[basename] = resolved
            pending.append(resolved)
            search_dirs.add(str(Path(resolved).parent))
    return collected


def run_patchelf(*args: str) -> None:
    """
    Runs `patchelf` (preferring smelt's bundled copy) and raises on failure.
    """
    with patchelf_on_path():
        cmd_trace = call_command("patchelf", *args)
    if cmd_trace.exit_code != 0:
        raise NativeDepsError(
            f"patchelf failed: {describe_command_failure(cmd_trace, ('patchelf', *args))}"
        )


def set_rpath(binary_path: Path, rpath: str) -> None:
    """
    Sets `binary_path`'s ELF RPATH to `rpath` (`:`-joined entries).

    Uses `--force-rpath`, i.e. the legacy `DT_RPATH` tag rather than `DT_RUNPATH`:
    unlike `DT_RUNPATH`, `DT_RPATH` is searched before `LD_LIBRARY_PATH` and is
    inherited by the binary's own transitive dependencies. Both matter for a
    distribution that must resolve its own libraries rather than fall back to
    whatever same-named library the host happens to have installed.
    """
    run_patchelf("--force-rpath", "--set-rpath", rpath, str(binary_path))


def set_interpreter(binary_path: Path, interpreter: Path) -> None:
    """
    Sets `binary_path`'s ELF `PT_INTERP` -- the dynamic loader the kernel runs before
    any of the binary's own code.

    Unlike `DT_NEEDED` lookups, this is resolved directly by the kernel and is *not*
    subject to RPATH/`$ORIGIN` expansion, so `interpreter` must be an absolute path
    and the folder cannot be moved afterwards. Only relevant once a distribution
    bundles its own libc.
    """
    run_patchelf("--set-interpreter", str(interpreter), str(binary_path))


def nested_rpath(dest_rel_path: Path) -> str:
    """
    The RPATH an artifact placed at `dest_rel_path` needs to find both its siblings
    and the dependencies bundled flat at the distribution root.

    `$ORIGIN` covers the former (a shared runtime sitting in the same package folder),
    and the walk back up to the root covers the latter.
    """
    depth = len(dest_rel_path.parts) - 1
    if depth == 0:
        return "$ORIGIN"
    return "$ORIGIN:" + "/".join(["$ORIGIN", *([".."] * depth)])


def package_rpath_dirs(dest_rel_paths: Iterable[Path]) -> set[str]:
    """
    The distinct subdirectories (relative to the distribution root, POSIX-style)
    holding at least one native artifact -- i.e. the folders anything at the root has
    to be able to look into.
    """
    return {
        dest_rel_path.parent.as_posix()
        for dest_rel_path in dest_rel_paths
        if dest_rel_path.parent != Path()
    }


@dataclass
class BundledNatives:
    """
    The outcome of resolving a distribution's native dependencies.

    `dependencies` maps each bundled library's file name to where it came from.
    `unsupported` is set when the running platform has no implementation here, in
    which case nothing was resolved and nothing was rewritten.
    """

    dependencies: dict[str, PathExists] = field(default_factory=dict)
    rewritten: list[Path] = field(default_factory=list)
    unsupported: str | None = None

    @property
    def resolved(self) -> bool:
        return self.unsupported is None


def bundle_native_dependencies(
    dist_root: Path,
    placed: Mapping[Path, PathExists],
    *,
    extra_seeds: Iterable[PathExists] = (),
    ignore_prefixes: tuple[str, ...] | None = None,
    binary_format: BinaryFormat | None = None,
    include_system_dirs: bool = True,
) -> BundledNatives:
    """
    Bundles every shared library the distribution's own native artifacts need.

    `include_system_dirs=False` keeps this machine's own libraries out of the answer,
    for a distribution built against a libc or an architecture the build host does not
    have (see `elf_dependencies`).

    `placed` maps each artifact's distribution-relative destination to the *source*
    file it was copied from, and the walk deliberately follows those sources rather
    than the copies: a wheel-shipped extension module usually finds its own vendored
    libraries relative to where the wheel installed it, which resolves to nothing once
    the file sits in the distribution instead. Walking the original location is what
    lets those libraries be found at all -- from the copy, they would be reported as
    `not found` and silently dropped.

    For ELF dependencies are copied flat into `dist_root` and every RPATH is rewritten
    to resolve there -- `dist_root` is a `sys.path` entry already, so one destination
    serves every package. PE has no RPATH-equivalent lever: an extension module's
    "altered search path" covers only its own directory, so for PE a dependency is
    instead copied next to `dist_root` *and* into every package folder that needs it
    (see `package_rpath_dirs`) -- more copies, but each one resolves without needing to
    rewrite anything.

    `binary_format` defaults to whichever matches the running host; pass it explicitly
    when `placed` holds a foreign target's binaries (bundling a cross-built Windows
    interpreter's own dependencies from a Linux host, say -- see `smelt.own_python`).

    `ignore_prefixes` defaults to whichever of `LINUX_SYSTEM_DLL_IGNORE_PREFIXES` /
    `WINDOWS_SYSTEM_DLL_IGNORE_PREFIXES` matches `binary_format`.
    """
    fmt = _resolve_binary_format(binary_format)
    if not is_supported_platform(fmt):
        reason = (
            f"native dependency resolution is only implemented for Linux, not {platform.system()}"
        )
        _logger.warning(
            "Skipping native dependency resolution: %s. Shared libraries the bundled "
            "extension modules need will have to be present on the target.",
            reason,
        )
        return BundledNatives(unsupported=reason)
    if ignore_prefixes is None:
        ignore_prefixes = (
            WINDOWS_SYSTEM_DLL_IGNORE_PREFIXES if fmt == "pe" else LINUX_SYSTEM_DLL_IGNORE_PREFIXES
        )

    seeds = [*placed.values(), *extra_seeds]
    # A library that is already one of the distribution's own artifacts must not be
    # bundled a second time at the root: smelt's shared runtimes are needed by the
    # modules that use them and would otherwise be copied twice, once at the
    # package-relative position they are resolved from and once flat.
    already_placed = {
        (dest_rel_path.name.lower() if fmt == "pe" else dest_rel_path.name)
        for dest_rel_path in placed
    }
    dependencies = {
        basename: assert_path_exists(resolved)
        for basename, resolved in collect_native_dependencies(
            seeds, ignore_prefixes, binary_format=fmt, include_system_dirs=include_system_dirs
        ).items()
        if basename not in already_placed
    }
    for basename, resolved in dependencies.items():
        shutil.copy2(resolved, dist_root / basename)

    if fmt == "pe":
        for folder in package_rpath_dirs(placed):
            for basename, resolved in dependencies.items():
                shutil.copy2(resolved, dist_root / folder / basename)
        return BundledNatives(dependencies=dependencies)

    rewritten: list[Path] = []
    for dest_rel_path in placed:
        set_rpath(dist_root / dest_rel_path, nested_rpath(dest_rel_path))
        rewritten.append(dest_rel_path)
    # A bundled dependency may itself need another one, and they all sit flat at the
    # root -- plus the package folders, for a library vendored next to its own
    # extension module.
    dependency_rpath = ":".join(
        ["$ORIGIN", *(f"$ORIGIN/{folder}" for folder in sorted(package_rpath_dirs(placed)))]
    )
    for basename in dependencies:
        set_rpath(dist_root / basename, dependency_rpath)
        rewritten.append(Path(basename))

    return BundledNatives(dependencies=dependencies, rewritten=rewritten)
