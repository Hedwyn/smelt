//! `bin/python` of a mode `own` distribution whose interpreter is linked against musl
//! dynamically: a stub that re-executes the real interpreter through the musl loader
//! shipped in the same folder.
//!
//! The real interpreter (`bin/python-real`) names its loader by absolute path in
//! `PT_INTERP` -- `/lib/ld-musl-<arch>.so.1`, baked in at link time -- and that file
//! is exactly what a machine without musl installed does not have. A musl loader can
//! be run as a program instead (`ld-musl-<arch>.so.1 <program> <args...>`), which is
//! what this stub does with the copy in `lib/`, so the folder needs nothing from the
//! machine it runs on.
//!
//! It has to be `bin/python` itself, not just the folder's launcher: `sys.executable`
//! is re-executed from inside Python -- by the generated `__main__`'s isolation guard,
//! by `multiprocessing`'s `spawn` start method, by any `subprocess` call the
//! application makes on its own interpreter -- and every one of those has to reach a
//! file that starts. Being `bin/python` is also what keeps CPython's prefix detection
//! right: `--argv0` hands the interpreter this stub's own path, so the standard
//! library is looked for next to `bin/`, where it is.
//!
//! Statically linked against musl (it has no `dlopen()` to do), so the stub itself
//! needs no loader.

const std = @import("std");
const Io = std.Io;
const fatal = std.process.fatal;

/// The real interpreter, next to this stub. `stage_interpreter` renames it out of the
/// way when it installs this file as `bin/python`.
const real_name = "python-real";

/// What the shipped musl loader is called. The architecture is part of the name
/// (`ld-musl-x86_64.so.1`), and the name itself comes from whatever Zig wrote into the
/// real interpreter's `PT_INTERP`, so it is matched rather than spelled out here.
const loader_prefix = "ld-musl-";
const loader_suffix = ".so.1";

pub fn main(init: std.process.Init) !void {
    const io = init.io;
    const gpa = init.gpa;
    const arena = init.arena.allocator();

    var args = init.minimal.args.iterateAllocator(gpa) catch |err|
        fatal("cannot read command-line arguments: {t}", .{err});
    defer args.deinit();
    const argv0 = args.next() orelse "";

    const self = selfPath(io, arena, argv0);
    const bin_dir = std.fs.path.dirname(self) orelse
        fatal("{s} has no directory to resolve the interpreter against", .{self});
    const prefix = std.fs.path.dirname(bin_dir) orelse
        fatal("{s} is not inside a distribution folder", .{self});
    const real = try std.fs.path.join(arena, &.{ bin_dir, real_name });
    const loader = findLoader(io, arena, try std.fs.path.join(arena, &.{ prefix, "lib" }));

    var argv: std.ArrayList([]const u8) = .empty;
    // `--argv0` is what makes `sys.executable` this stub rather than the real
    // interpreter: a child process re-executing it has to come back through here.
    try argv.appendSlice(arena, &.{ loader, "--argv0", self, real });
    while (args.next()) |arg| try argv.append(arena, arg);

    const err = std.process.replace(io, .{ .argv = argv.items });
    fatal("cannot start {s} through {s}: {t}", .{ real, loader, err });
}

/// This stub's own path. `/proc/self/exe` is the answer that is right however the stub
/// was reached (a bare name found on PATH, a symlink into `~/.local/bin`); `argv[0]`
/// is the fallback for a `/proc`-less environment, and only usable when it carries a
/// directory of its own.
fn selfPath(io: Io, arena: std.mem.Allocator, argv0: []const u8) []const u8 {
    if (std.process.executablePathAlloc(io, arena)) |path| return path else |_| {}
    if (std.mem.indexOfScalar(u8, argv0, std.fs.path.sep) != null) return argv0;
    fatal(
        "cannot locate this executable: /proc is not readable and argv[0] ({s}) " ++
            "carries no path. Start the distribution by path, e.g. ./bin/python.",
        .{argv0},
    );
}

/// The shipped musl loader in `lib_dir`. A distribution holds exactly one, since it
/// is assembled for one architecture; two is refused rather than guessed at, because
/// running the wrong architecture's loader fails much further away from the cause
/// ("unsupported relocation type" while it tries to relocate the interpreter).
fn findLoader(io: Io, arena: std.mem.Allocator, lib_dir: []const u8) []const u8 {
    var dir = Io.Dir.openDirAbsolute(io, lib_dir, .{ .iterate = true }) catch |err|
        fatal("cannot read {s}: {t}", .{ lib_dir, err });
    defer dir.close(io);

    var found: ?[]const u8 = null;
    var it = dir.iterateAssumeFirstIteration();
    while (it.next(io) catch |err| fatal("cannot list {s}: {t}", .{ lib_dir, err })) |entry| {
        if (entry.kind == .directory) continue;
        if (!std.mem.startsWith(u8, entry.name, loader_prefix)) continue;
        if (!std.mem.endsWith(u8, entry.name, loader_suffix)) continue;
        const path = std.fs.path.join(arena, &.{ lib_dir, entry.name }) catch |err|
            fatal("out of memory: {t}", .{err});
        if (found) |first| fatal(
            "{s} holds more than one musl loader ({s} and {s}): it cannot be a " ++
                "distribution for one architecture.",
            .{ lib_dir, std.fs.path.basename(first), entry.name },
        );
        found = path;
    }
    return found orelse fatal(
        "no {s}*{s} in {s}: the distribution is missing the musl loader its " ++
            "interpreter needs to start.",
        .{ loader_prefix, loader_suffix, lib_dir },
    );
}
