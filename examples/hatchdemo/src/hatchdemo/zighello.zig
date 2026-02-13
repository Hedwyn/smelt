// Derived from:  https://github.com/adamserafini/zaml/blob/main/zamlmodule.zig

const py = @cImport({
    @cDefine("Py_LIMITED_API", "3");
    @cDefine("PY_SSIZE_T_CLEAN", {});
    @cInclude("Python.h");
});
const std = @import("std");

const PyObject = py.PyObject;
const PyMethodDef = py.PyMethodDef;
const PyModuleDef = py.PyModuleDef;
const PyModuleDef_Base = py.PyModuleDef_Base;
const Py_BuildValue = py.Py_BuildValue;
const PyModule_Create = py.PyModule_Create;
const METH_NOARGS = py.METH_NOARGS;

fn hello_world(self: [*c]PyObject, args: [*c]PyObject) callconv(.c) [*]PyObject {
    _ = self;
    _ = args;

    std.debug.print("Hello World !", .{});
    return Py_BuildValue("i", @as(c_int, 1));
}

var ZamlMethods = [_]PyMethodDef{
    PyMethodDef{
        .ml_name = "hello",
        .ml_meth = hello_world,
        .ml_flags = METH_NOARGS,
        .ml_doc = "",
    },
    PyMethodDef{
        .ml_name = null,
        .ml_meth = null,
        .ml_flags = 0,
        .ml_doc = null,
    },
};

var hellomodule = PyModuleDef{
    .m_doc = null,
    .m_size = -1,
    .m_methods = &ZamlMethods,
    .m_slots = null,
    .m_traverse = null,
    .m_clear = null,
    .m_free = null,
};

pub export fn PyInit_zighello() [*]PyObject {
    return PyModule_Create(&hellomodule);
}
