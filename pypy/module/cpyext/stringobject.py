from pypy.rpython.lltypesystem import rffi, lltype
from pypy.module.cpyext.api import cpython_api, PyObject, PyVarObjectFields, \
    PyStringObject, Py_ssize_t, cpython_struct, make_ref, from_ref, CANNOT_FAIL, \
    general_check


@cpython_api([PyObject], rffi.INT_real, error=CANNOT_FAIL)
def PyString_Check(space, w_obj):
    """Return true if the object o is a string object or an instance of a subtype of
    the string type.
    
    Allowed subtypes to be accepted."""
    w_type = space.w_str
    return general_check(space, w_obj, w_type)

@cpython_api([rffi.CCHARP, Py_ssize_t], PyStringObject, error=lltype.nullptr(PyStringObject.TO))
def PyString_FromStringAndSize(space, char_p, length):
    if char_p:
        s = rffi.charpsize2str(char_p, length)
        ptr = make_ref(space, space.wrap(s))
        return rffi.cast(PyStringObject, ptr)
    else:
        py_str = lltype.malloc(PyStringObject.TO, flavor='raw')
        py_str.c_ob_refcnt = 1
        
        buflen = length + 1
        py_str.c_buffer = lltype.malloc(rffi.CCHARP.TO, buflen, flavor='raw')
        py_str.c_buffer[buflen-1] = '\0'
        py_str.c_size = length
        py_str.c_ob_type = make_ref(space, space.w_str)
        
        return py_str

@cpython_api([rffi.CCHARP], PyObject)
def PyString_FromString(space, char_p):
    s = rffi.charp2str(char_p)
    return space.wrap(s)

@cpython_api([PyObject], rffi.CCHARP, error=0)
def PyString_AsString(space, ref):
    ref = rffi.cast(PyStringObject, ref)
    if not ref.c_buffer:
        # copy string buffer
        w_str = from_ref(space, ref)
        s = space.str_w(w_str)
        ref.c_buffer = rffi.str2charp(s)
    return ref.c_buffer

@cpython_api([PyObject], Py_ssize_t, error=-1)
def PyString_Size(space, ref):
    if from_ref(space, ref.c_ob_type) is space.w_str:
        ref = rffi.cast(PyStringObject, ref)
        return ref.c_size
    else:
        w_obj = from_ref(space, ref)
        return space.int_w(space.len(w_obj))
