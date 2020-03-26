from rpython.rtyper.lltypesystem import lltype, rffi
from pypy.interpreter.error import OperationError, oefmt
from pypy.module._hpy_universal.apiset import API
from pypy.module._hpy_universal import handles

def make_unary(name, spacemeth):
    assert spacemeth.startswith('space.')
    spacemeth = spacemeth[len('space.'):]
    #
    @API.func("HPy HPy_unary(HPyContext ctx, HPy h1)", func_name=name)
    def HPy_unary(space, ctx, h1):
        w_obj1 = handles.deref(space, h1)
        meth = getattr(space, spacemeth)
        w_res = meth(w_obj1)
        return handles.new(space, w_res)
    #
    globals()[name] = HPy_unary

def make_binary(name, spacemeth):
    assert spacemeth.startswith('space.')
    spacemeth = spacemeth[len('space.'):]
    #
    @API.func("HPy HPy_binary(HPyContext ctx, HPy h1, HPy h2)", func_name=name)
    def HPy_binary(space, ctx, h1, h2):
        w_obj1 = handles.deref(space, h1)
        w_obj2 = handles.deref(space, h2)
        meth = getattr(space, spacemeth)
        w_res = meth(w_obj1, w_obj2)
        return handles.new(space, w_res)
    #
    globals()[name] = HPy_binary


make_unary('HPy_Negative', 'space.neg')
make_unary('HPy_Positive', 'space.pos')
make_unary('HPy_Absolute', 'space.abs')
make_unary('HPy_Invert', 'space.invert')
make_unary('HPy_Index', 'space.index')

make_binary('HPy_Add', 'space.add')
make_binary('HPy_Subtract', 'space.sub')
make_binary('HPy_Multiply', 'space.mul')
make_binary('HPy_FloorDivide', 'space.floordiv')
make_binary('HPy_TrueDivide', 'space.truediv')
make_binary('HPy_Remainder', 'space.mod')
make_binary('HPy_Divmod', 'space.divmod')
make_binary('HPy_Lshift', 'space.lshift')
make_binary('HPy_Rshift', 'space.rshift')
make_binary('HPy_And', 'space.and_')
make_binary('HPy_Xor', 'space.xor')
make_binary('HPy_Or', 'space.or_')
make_binary('HPy_MatrixMultiply', 'space.matmul')


@API.func("HPy HPy_Long(HPyContext ctx, HPy h1)")
def HPy_Long(space, ctx, h1):
    w_obj1 = handles.deref(space, h1)
    w_res = space.call_function(space.w_int, w_obj1)
    return handles.new(space, w_res)


@API.func("HPy HPy_Float(HPyContext ctx, HPy h1)")
def HPy_Float(space, ctx, h1):
    w_obj1 = handles.deref(space, h1)
    w_res = space.call_function(space.w_float, w_obj1)
    return handles.new(space, w_res)


@API.func("HPy HPy_Power(HPyContext ctx, HPy h1, HPy h2, HPy h3)")
def HPy_Power(space, ctx, h1, h2, h3):
    w_o1 = handles.deref(space, h1)
    w_o2 = handles.deref(space, h2)
    w_o3 = handles.deref(space, h3)
    w_res = space.pow(w_o1, w_o2, w_o3)
    return handles.new(space, w_res)


@API.func("HPy HPy_InPlaceAdd(HPyContext ctx, HPy h1, HPy h2)")
def HPy_InPlaceAdd(space, ctx, h1, h2):
    from rpython.rlib.nonconst import NonConstant # for the annotator
    if NonConstant(False): return 0
    raise NotImplementedError

@API.func("HPy HPy_InPlaceSubtract(HPyContext ctx, HPy h1, HPy h2)")
def HPy_InPlaceSubtract(space, ctx, h1, h2):
    from rpython.rlib.nonconst import NonConstant # for the annotator
    if NonConstant(False): return 0
    raise NotImplementedError

@API.func("HPy HPy_InPlaceMultiply(HPyContext ctx, HPy h1, HPy h2)")
def HPy_InPlaceMultiply(space, ctx, h1, h2):
    from rpython.rlib.nonconst import NonConstant # for the annotator
    if NonConstant(False): return 0
    raise NotImplementedError

@API.func("HPy HPy_InPlaceMatrixMultiply(HPyContext ctx, HPy h1, HPy h2)")
def HPy_InPlaceMatrixMultiply(space, ctx, h1, h2):
    from rpython.rlib.nonconst import NonConstant # for the annotator
    if NonConstant(False): return 0
    raise NotImplementedError

@API.func("HPy HPy_InPlaceFloorDivide(HPyContext ctx, HPy h1, HPy h2)")
def HPy_InPlaceFloorDivide(space, ctx, h1, h2):
    from rpython.rlib.nonconst import NonConstant # for the annotator
    if NonConstant(False): return 0
    raise NotImplementedError

@API.func("HPy HPy_InPlaceTrueDivide(HPyContext ctx, HPy h1, HPy h2)")
def HPy_InPlaceTrueDivide(space, ctx, h1, h2):
    from rpython.rlib.nonconst import NonConstant # for the annotator
    if NonConstant(False): return 0
    raise NotImplementedError

@API.func("HPy HPy_InPlaceRemainder(HPyContext ctx, HPy h1, HPy h2)")
def HPy_InPlaceRemainder(space, ctx, h1, h2):
    from rpython.rlib.nonconst import NonConstant # for the annotator
    if NonConstant(False): return 0
    raise NotImplementedError

@API.func("HPy HPy_InPlacePower(HPyContext ctx, HPy h1, HPy h2, HPy h3)")
def HPy_InPlacePower(space, ctx, h1, h2, h3):
    from rpython.rlib.nonconst import NonConstant # for the annotator
    if NonConstant(False): return 0
    raise NotImplementedError

@API.func("HPy HPy_InPlaceLshift(HPyContext ctx, HPy h1, HPy h2)")
def HPy_InPlaceLshift(space, ctx, h1, h2):
    from rpython.rlib.nonconst import NonConstant # for the annotator
    if NonConstant(False): return 0
    raise NotImplementedError

@API.func("HPy HPy_InPlaceRshift(HPyContext ctx, HPy h1, HPy h2)")
def HPy_InPlaceRshift(space, ctx, h1, h2):
    from rpython.rlib.nonconst import NonConstant # for the annotator
    if NonConstant(False): return 0
    raise NotImplementedError

@API.func("HPy HPy_InPlaceAnd(HPyContext ctx, HPy h1, HPy h2)")
def HPy_InPlaceAnd(space, ctx, h1, h2):
    from rpython.rlib.nonconst import NonConstant # for the annotator
    if NonConstant(False): return 0
    raise NotImplementedError

@API.func("HPy HPy_InPlaceXor(HPyContext ctx, HPy h1, HPy h2)")
def HPy_InPlaceXor(space, ctx, h1, h2):
    from rpython.rlib.nonconst import NonConstant # for the annotator
    if NonConstant(False): return 0
    raise NotImplementedError

@API.func("HPy HPy_InPlaceOr(HPyContext ctx, HPy h1, HPy h2)")
def HPy_InPlaceOr(space, ctx, h1, h2):
    from rpython.rlib.nonconst import NonConstant # for the annotator
    if NonConstant(False): return 0
    raise NotImplementedError
