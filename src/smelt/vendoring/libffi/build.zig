//! Thin wrapper exposing `allyourcodebase/libffi`'s `ffi` artifact for a
//! standalone `zig build --prefix <dir>` -- meta-python (`meta-python/build.zig`)
//! already depends on the exact same package (see its own `build.zig.zon`) to
//! link libffi into CPython's `_ctypes`, but only as an internal build-graph
//! artifact it never installs anywhere outside its own `zig-out`: there is no
//! path/env var smelt can already read a built libffi from (see
//! cffi_vendoring_plan.md), hence this standalone copy of the same dependency
//! edge.
//!
//! NOTE: this has not been exercised against the real `allyourcodebase/libffi`
//! package yet (no network access to fetch it while this was written) --
//! `libffi_dep.artifact("ffi")` mirrors exactly what `meta-python/build.zig`
//! already does successfully, so the dependency fetch and artifact name should
//! be correct, but whether `b.installArtifact` alone also carries `ffi.h`/
//! `ffitarget.h` into `<prefix>/include` is unverified: some Zig packages expose
//! public headers through a separate `installHeader`/`installHeadersDirectory`
//! step of their own rather than automatically alongside the artifact. If
//! `smelt.vendoring._libffi_build.build_libffi` reports a missing include
//! directory, check https://github.com/allyourcodebase/libffi's own `build.zig`
//! and add the matching header-install call here.

const std = @import("std");

pub fn build(b: *std.Build) void {
    const target = b.standardTargetOptions(.{});
    const optimize = b.standardOptimizeOption(.{});

    const libffi_dep = b.dependency("libffi", .{ .target = target, .optimize = optimize });
    const ffi_lib = libffi_dep.artifact("ffi");

    b.installArtifact(ffi_lib);
}
