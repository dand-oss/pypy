import py
from pypy.translator.interactive import Translation
from pypy import conftest
from pypy.rpython.lltypesystem import llmemory, lltype
from pypy.rpython.memory import lladdress
from pypy.rlib.objectmodel import ComputedIntSymbolic

from pypy.translator.llvm.test.runtest import *

def test_offsetof():
    STRUCT = lltype.GcStruct("s", ("x", lltype.Signed), ("y", lltype.Signed))
    offsetx = llmemory.offsetof(STRUCT, 'x')
    offsety = llmemory.offsetof(STRUCT, 'y')
    def f():
        s = lltype.malloc(STRUCT)
        s.x = 1
        adr = llmemory.cast_ptr_to_adr(s)
        result = (adr + offsetx).signed[0]
        (adr + offsety).signed[0] = 2
        return result * 10 + s.y
    fn = compile_function(f, [])
    res = fn()
    assert res == 12

def test_sizeof_array_with_no_length():
    py.test.skip("inprogress")
    A = lltype.GcArray(lltype.Signed, hints={'nolength': True})
    a = lltype.malloc(A, 5)
    
    arraysize = llmemory.itemoffsetof(A, 10)
    signedsize = llmemory.sizeof(lltype.Signed)
    def f():
        return a[0] + arraysize-signedsize*10
    fn = compile_function(f, [])
    res = fn()
    assert res == 0

def test_itemoffsetof():
    ARRAY = lltype.GcArray(lltype.Signed)
    itemoffsets = [llmemory.itemoffsetof(ARRAY, i) for i in range(5)]
    def f():
        a = lltype.malloc(ARRAY, 5)
        adr = llmemory.cast_ptr_to_adr(a)
        result = 0
        for i in range(5):
            a[i] = i + 1
        for i in range(5):
            result = result * 10 + (adr + itemoffsets[i]).signed[0]
        for i in range(5):
            (adr + itemoffsets[i]).signed[0] = i
        for i in range(5):
            result = 10 * result + a[i]
        return result
    fn = compile_function(f, [])
    res = fn()
    assert res == 1234501234

def test_itemoffsetof_fixedsizearray():
    ARRAY = lltype.FixedSizeArray(lltype.Signed, 5)
    itemoffsets = [llmemory.itemoffsetof(ARRAY, i) for i in range(5)]
    a = lltype.malloc(ARRAY, immortal=True)
    def f():
        adr = llmemory.cast_ptr_to_adr(a)
        result = 0
        for i in range(5):
            a[i] = i + 1
        for i in range(5):
            result = result * 10 + (adr + itemoffsets[i]).signed[0]
        for i in range(5):
            (adr + itemoffsets[i]).signed[0] = i
        for i in range(5):
            result = 10 * result + a[i]
        return result
    fn = compile_function(f, [])
    res = fn()
    assert res == 1234501234

def test_sizeof_constsize_struct():
    # _not_ a GcStruct, since we want to raw_malloc it
    STRUCT = lltype.Struct("s", ("x", lltype.Signed), ("y", lltype.Signed))
    STRUCTPTR = lltype.Ptr(STRUCT)
    sizeofs = llmemory.sizeof(STRUCT)
    offsety = llmemory.offsetof(STRUCT, 'y')
    def f():
        adr = lladdress.raw_malloc(sizeofs)
        s = llmemory.cast_adr_to_ptr(adr, STRUCTPTR)
        s.y = 5 # does not crash
        result = (adr + offsety).signed[0] * 10 + int(offsety < sizeofs)
        lladdress.raw_free(adr)
        return result

    fn = compile_function(f, [])
    res = fn()
    assert res == 51

def test_computed_int_symbolic():
    llvm_test()
    too_early = True
    def compute_fn():
        assert not too_early
        return 7
    k = ComputedIntSymbolic(compute_fn)
    def f():
        return k*6

    t = Translation(f)
    t.rtype()
    if conftest.option.view:
        t.view()
    too_early = False
    fn = t.compile_llvm()
    res = fn()
    assert res == 42

def offsetofs(TYPE, *fldnames):
    import operator
    offsets = []
    for name in fldnames:
        assert name in TYPE._flds
        offset = llmemory.FieldOffset(TYPE, name)
        offsets.append(offset)
        TYPE = getattr(TYPE, name)
    return reduce(operator.add, offsets)
            
def test_complex_struct():
    A = lltype.Array(lltype.Signed)
    # XXX WHY cant we create a varsize array as last elemen here? :-(
    S2 = lltype.Struct('s2', ('a', lltype.Signed)) # ('a', A)
    S3 = lltype.Struct('s3', ('s', lltype.Signed), ('s2', S2))
    SBASE = lltype.GcStruct('base', ('a', lltype.Signed), ('b', S3))
    SBASEPTR = lltype.Ptr(SBASE)

    sizeofsbase = llmemory.sizeof(SBASE)
    offset_toa = offsetofs(SBASE, 'b', 's2', 'a') 
    def complex_struct():
        adr = lladdress.raw_malloc(sizeofsbase)
        s = llmemory.cast_adr_to_ptr(adr, SBASEPTR)
        s.b.s2.a = 42
        return (adr + offset_toa).signed[0]

    fn = compile_function(complex_struct, [])
    assert fn() == 42

def test_vararray():
    S1 = lltype.Struct('s1', ('s', lltype.Signed))
    A = lltype.Array(S1)
    S2 = lltype.GcStruct('s2', ('b', lltype.Signed), ('a', A))
    S1PTR = lltype.Ptr(S1)

    offset1 = (llmemory.offsetof(S2, 'a') +
               llmemory.ArrayItemsOffset(A) +
               llmemory.ItemOffset(S1, 21) +
               llmemory.offsetof(S1, 's'))
    
    offset2 = (llmemory.offsetof(S2, 'a') +
               llmemory.ArrayItemsOffset(A) +
               llmemory.ItemOffset(S1, 21))
    
    def vararray(n):
        s = lltype.malloc(S2, n)
        adr = llmemory.cast_ptr_to_adr(s)
        s.a[n].s = n
        s1 = llmemory.cast_adr_to_ptr(adr + offset2, S1PTR)
        return (adr + offset1).signed[0] + s1.s
    
    fn = compile_function(vararray, [int])
    assert fn(21) == 42


