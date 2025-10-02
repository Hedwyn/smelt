/**
    A minimalist C extension providing a single sum function.
    Meant for testing.
 */

#define PY_SSIZE_T_CLEAN
#include <Python.h>

static PyObject* hello(PyObject* self, PyObject* args) {
    return PyUnicode_FromString("Hello World!");
}

static PyMethodDef HelloMethods[] = {
    {"hello", hello, METH_NOARGS, "Returns a greeting."},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef hellomodule = {
    PyModuleDef_HEAD_INIT,
    "hello",    // name of module
    NULL,       // module documentation
    -1,         // size of per-interpreter state of the module
    HelloMethods
};

PyMODINIT_FUNC PyInit_hello(void) {
    return PyModule_Create(&hellomodule);
}
