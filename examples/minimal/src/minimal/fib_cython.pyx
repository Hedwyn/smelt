def fibx(int n):
    cdef int i, a, b
    a, b = 0, 1
    for i in range(n):
        a, b = a + b, a
    return a
