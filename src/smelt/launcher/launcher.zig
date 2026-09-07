//! Onefile launcher for a mode `own` smelt distribution.
//!
//! The whole distribution folder is appended to this executable as a compressed
//! archive; this program inflates it into a content-addressed cache directory the
//! first time it runs, then replaces itself with the bundled interpreter.
//!
//! Deliberately small in scope: everything a distribution has to *enforce* -- the
//! isolation flags, the interpreter version check -- already lives in the generated
//! `app/__main__.py`, which runs whichever way the payload is reached. So this stub
//! only has to produce a directory and exec.

const std = @import("std");
const builtin = @import("builtin");
const Io = std.Io;
const fatal = std.process.fatal;
const native_os = builtin.os.tag;

/// Trailer magic. The final byte is the trailer format version: a stub and a payload
/// built by different smelt versions must not silently half-understand each other.
const MAGIC = "SMELTPK\x01";

/// Size of the fixed-layout trailer at the very end of the file. Read backwards from
/// the end so that neither the ELF nor the PE header has to be parsed to find it.
const TRAILER_SIZE = 256;

/// Written into the extracted directory once, last, and checked before reusing it.
/// Its absence is what tells a later run that an extraction was interrupted.
const SENTINEL = ".smelt-complete";

const Compression = enum(u8) {
    none = 0,
    xz = 1,
    gzip = 2,
    _,
};

const Trailer = struct {
    payload_offset: u64,
    payload_size: u64,
    compression: Compression,
    /// Directory name the payload is extracted under, `<app>-<digest>`.
    cache_name: []const u8,
    /// The interpreter to exec, relative to the extracted directory.
    exec_rel: []const u8,
    /// The directory handed to that interpreter, relative to the extracted directory.
    payload_dir: []const u8,

    /// Field offsets. Little-endian, fixed positions: the writing side is
    /// `smelt.onefile.encode_trailer`, and the two must be read together.
    const magic_off = 0;
    const payload_offset_off = 8;
    const payload_size_off = 16;
    const compression_off = 24;
    const cache_name_off = 32;
    const exec_rel_off = 96;
    const payload_dir_off = 160;
    const field_size = 64;

    fn parse(bytes: *const [TRAILER_SIZE]u8) ?Trailer {
        if (!std.mem.eql(u8, bytes[magic_off..][0..MAGIC.len], MAGIC)) return null;
        return .{
            .payload_offset = std.mem.readInt(u64, bytes[payload_offset_off..][0..8], .little),
            .payload_size = std.mem.readInt(u64, bytes[payload_size_off..][0..8], .little),
            .compression = @enumFromInt(bytes[compression_off]),
            .cache_name = field(bytes, cache_name_off),
            .exec_rel = field(bytes, exec_rel_off),
            .payload_dir = field(bytes, payload_dir_off),
        };
    }

    /// A nul-padded fixed-width string field.
    fn field(bytes: *const [TRAILER_SIZE]u8, offset: usize) []const u8 {
        const raw = bytes[offset..][0..field_size];
        const end = std.mem.indexOfScalar(u8, raw, 0) orelse field_size;
        return raw[0..end];
    }
};

pub fn main(init: std.process.Init) !void {
    const io = init.io;
    const gpa = init.gpa;
    const arena = init.arena.allocator();
    const environ = init.minimal.environ;

    var self = std.process.openExecutable(io, .{}) catch |err|
        fatal("cannot read own executable: {t}", .{err});
    defer self.close(io);

    const size = self.length(io) catch |err|
        fatal("cannot size own executable: {t}", .{err});
    if (size < TRAILER_SIZE) fatal("this executable carries no payload", .{});

    var raw_trailer: [TRAILER_SIZE]u8 = undefined;
    _ = self.readPositionalAll(io, &raw_trailer, size - TRAILER_SIZE) catch |err|
        fatal("cannot read the payload trailer: {t}", .{err});
    const trailer = Trailer.parse(&raw_trailer) orelse
        fatal("this executable carries no smelt payload, or one this launcher is too old to read", .{});

    const cache_root = try cacheRoot(arena, environ);
    const target = try std.fs.path.join(arena, &.{ cache_root, trailer.cache_name });

    const sentinel = try std.fs.path.join(arena, &.{ target, SENTINEL });
    const cached = Io.Dir.accessAbsolute(io, sentinel, .{}) != error.FileNotFound;
    if (!cached) try extract(io, gpa, arena, self, trailer, cache_root, target);

    const interpreter = try std.fs.path.join(arena, &.{ target, trailer.exec_rel });
    const payload = try std.fs.path.join(arena, &.{ target, trailer.payload_dir });

    var argv: std.ArrayList([]const u8) = .empty;
    // `argv[0]` is what `replace` execs; the application's own name is not preserved
    // through it, which is a known cost of exec'ing the interpreter directly.
    try argv.append(arena, interpreter);
    try argv.append(arena, payload);
    // `iterate()` alone is POSIX/WASI-only (see its doc comment); Windows args are
    // read from `GetCommandLineW` and need an allocator to reassemble.
    var args = init.minimal.args.iterateAllocator(gpa) catch |err|
        fatal("cannot read command-line arguments: {t}", .{err});
    defer args.deinit();
    _ = args.next();
    while (args.next()) |arg| try argv.append(arena, arg);

    if (std.process.can_replace) {
        const err = std.process.replace(io, .{ .argv = argv.items });
        fatal("cannot start the bundled interpreter {s}: {t}", .{ interpreter, err });
    }
    // Windows has no exec(): the closest equivalent is spawning the bundled
    // interpreter as a child, waiting for it, and exiting with its own code. Argv[0]
    // stays lost either way, same as the POSIX branch above.
    var child = std.process.spawn(io, .{ .argv = argv.items }) catch |err|
        fatal("cannot start the bundled interpreter {s}: {t}", .{ interpreter, err });
    const term = child.wait(io) catch |err|
        fatal("cannot wait for the bundled interpreter {s}: {t}", .{ interpreter, err });
    std.process.exit(switch (term) {
        .exited => |code| code,
        else => 1,
    });
}

/// Reads a WTF-16 Windows environment variable and re-encodes it as WTF-8, the string
/// encoding every other path in this file (and `Io.Dir`) is in.
fn envGetWindows(arena: std.mem.Allocator, environ: std.process.Environ, comptime key: []const u8) !?[]const u8 {
    const key_w = comptime std.unicode.wtf8ToWtf16LeStringLiteral(key);
    const value_w = environ.getWindows(key_w) orelse return null;
    return try std.unicode.wtf16LeToWtf8Alloc(arena, value_w);
}

/// Where extracted payloads are kept. `SMELT_ONEFILE_CACHE` wins so that a read-only
/// or unusual home directory can be worked around without rebuilding the executable.
fn cacheRoot(arena: std.mem.Allocator, environ: std.process.Environ) ![]const u8 {
    if (native_os == .windows) {
        if (try envGetWindows(arena, environ, "SMELT_ONEFILE_CACHE")) |dir| return dir;
        if (try envGetWindows(arena, environ, "LOCALAPPDATA")) |dir|
            return std.fs.path.join(arena, &.{ dir, "smelt" });
        if (try envGetWindows(arena, environ, "TEMP")) |dir|
            return std.fs.path.join(arena, &.{ dir, "smelt" });
        if (try envGetWindows(arena, environ, "TMP")) |dir|
            return std.fs.path.join(arena, &.{ dir, "smelt" });
        // No usable temp directory at all -- as unlikely on Windows as no HOME is on
        // POSIX, and the same reasoning applies: fail soft rather than not at all.
        return "C:/Windows/Temp/smelt";
    }
    if (environ.getPosix("SMELT_ONEFILE_CACHE")) |dir| return dir;
    if (environ.getPosix("XDG_CACHE_HOME")) |dir| return std.fs.path.join(arena, &.{ dir, "smelt" });
    if (environ.getPosix("HOME")) |dir| return std.fs.path.join(arena, &.{ dir, ".cache", "smelt" });
    // No HOME at all -- `env -i` is a case this is verified against, and failing there
    // would defeat the point of a distribution that needs nothing installed.
    return "/tmp/smelt";
}

/// Inflates the payload into `target`, atomically: it is written to a sibling
/// directory and renamed into place, so a second process starting concurrently either
/// sees no directory or sees a complete one -- never a half-extracted tree.
fn extract(
    io: Io,
    gpa: std.mem.Allocator,
    arena: std.mem.Allocator,
    self: Io.File,
    trailer: Trailer,
    cache_root: []const u8,
    target: []const u8,
) !void {
    const cwd: Io.Dir = .cwd();
    cwd.createDirPath(io, cache_root) catch |err|
        fatal("cannot create the cache directory {s}: {t}", .{ cache_root, err });

    const scratch = try std.fmt.allocPrint(arena, "{s}{c}.tmp-{d}-{s}", .{
        cache_root, std.fs.path.sep, currentPid(), trailer.cache_name,
    });
    cwd.deleteTree(io, scratch) catch {};
    var scratch_dir = cwd.createDirPathOpen(io, scratch, .{}) catch |err|
        fatal("cannot create {s}: {t}", .{ scratch, err });
    defer scratch_dir.close(io);

    var read_buffer: [64 * 1024]u8 = undefined;
    var file_reader = self.reader(io, &read_buffer);
    file_reader.seekTo(trailer.payload_offset) catch |err|
        fatal("cannot seek to the payload: {t}", .{err});

    switch (trailer.compression) {
        .none => try untar(io, scratch_dir, &file_reader.interface),
        .xz => {
            const window = try gpa.alloc(u8, 1 << 20);
            defer gpa.free(window);
            var decompress = std.compress.xz.Decompress.init(&file_reader.interface, gpa, window) catch |err|
                fatal("the payload is not a valid xz stream: {t}", .{err});
            defer decompress.deinit();
            try untar(io, scratch_dir, &decompress.reader);
        },
        .gzip => {
            var window: [std.compress.flate.max_window_len]u8 = undefined;
            var decompress: std.compress.flate.Decompress = .init(&file_reader.interface, .gzip, &window);
            try untar(io, scratch_dir, &decompress.reader);
        },
        _ => fatal("unknown payload compression", .{}),
    }

    scratch_dir.writeFile(io, .{ .sub_path = SENTINEL, .data = "" }) catch |err|
        fatal("cannot finalize {s}: {t}", .{ scratch, err });

    cwd.rename(scratch, cwd, target, io) catch |err| switch (err) {
        // Another process extracted the same payload first. Its copy is as good as
        // ours by construction -- the directory name is a digest of the payload -- so
        // there is nothing to reconcile, only our own scratch tree to remove.
        error.DirNotEmpty, error.AccessDenied, error.PermissionDenied => cwd.deleteTree(io, scratch) catch {},
        else => fatal("cannot move the extracted payload into {s}: {t}", .{ target, err }),
    };
}

fn untar(io: Io, dir: Io.Dir, reader: *Io.Reader) !void {
    std.tar.extract(io, dir, reader, .{ .mode_mode = .executable_bit_only }) catch |err|
        fatal("cannot unpack the payload: {t}", .{err});
}

/// The scratch directory an extraction writes into is named after the process doing
/// it, so two processes unpacking the same payload at the same time cannot write into
/// each other's tree -- and so a scratch tree left behind by a killed process is
/// eventually reclaimed rather than accumulating forever.
fn currentPid() u32 {
    return switch (native_os) {
        .windows => std.os.windows.GetCurrentProcessId(),
        else => @intCast(std.posix.system.getpid()),
    };
}
