import sys, os
import ctypes
from pypy.jit.backend.llsupport import symbolic
from pypy.jit.metainterp.history import Const, Box, BoxPtr, INT, REF, FLOAT
from pypy.jit.metainterp.history import AbstractFailDescr
from pypy.rpython.lltypesystem import lltype, rffi, ll2ctypes, rstr, llmemory
from pypy.rpython.lltypesystem.rclass import OBJECT
from pypy.rpython.lltypesystem.lloperation import llop
from pypy.rpython.annlowlevel import llhelper
from pypy.tool.uid import fixid
from pypy.jit.backend.x86.regalloc import RegAlloc, WORD, lower_byte,\
     X86RegisterManager, X86XMMRegisterManager, get_ebp_ofs, FRAME_FIXED_SIZE,\
     FORCE_INDEX_OFS
from pypy.rlib.objectmodel import we_are_translated, specialize
from pypy.jit.backend.x86 import codebuf
from pypy.jit.backend.x86.ri386 import *
from pypy.jit.metainterp.resoperation import rop
from pypy.jit.backend.x86.support import NonmovableGrowableArrayFloat,\
     NonmovableGrowableArraySigned, NonmovableGrowableArrayGCREF,\
     CHUNK_SIZE
from pypy.rlib.debug import debug_print

# our calling convention - we pass first 6 args in registers
# and the rest stays on the stack

if sys.platform == 'darwin':
    # darwin requires the stack to be 16 bytes aligned on calls
    CALL_ALIGN = 4
else:
    CALL_ALIGN = 1


def align_stack_words(words):
    return (words + CALL_ALIGN - 1) & ~(CALL_ALIGN-1)

class MachineCodeBlockWrapper(object):
    MC_DEFAULT_SIZE = 1024*1024

    def __init__(self, bigsize):
        self.old_mcs = [] # keepalive
        self.bigsize = bigsize
        self._mc = codebuf.MachineCodeBlock(bigsize)

    def bytes_free(self):
        return self._mc._size - self._mc._pos

    def make_new_mc(self):
        new_mc = codebuf.MachineCodeBlock(self.bigsize)
        debug_print('[new machine code block at', new_mc.tell(), ']')
        self._mc.JMP(rel32(new_mc.tell()))
        self._mc.done()
        self.old_mcs.append(self._mc)
        self._mc = new_mc
    make_new_mc._dont_inline_ = True

    def tell(self):
        return self._mc.tell()

    def done(self):
        self._mc.done()

def _new_method(name):
    def method(self, *args):
        # XXX er.... pretty random number, just to be sure
        #     not to write half-instruction
        if self.bytes_free() < 64:
            self.make_new_mc()
        getattr(self._mc, name)(*args)    
    method.func_name = name
    return method

for name in dir(codebuf.MachineCodeBlock):
    if name.upper() == name:
        setattr(MachineCodeBlockWrapper, name, _new_method(name))

class Assembler386(object):
    mc = None
    mc2 = None
    mc_size = MachineCodeBlockWrapper.MC_DEFAULT_SIZE

    def __init__(self, cpu, translate_support_code=False):
        self.cpu = cpu
        self.verbose = False
        self.rtyper = cpu.rtyper
        self.malloc_func_addr = 0
        self.malloc_array_func_addr = 0
        self.malloc_str_func_addr = 0
        self.malloc_unicode_func_addr = 0
        self.fail_boxes_int = NonmovableGrowableArraySigned()
        self.fail_boxes_ptr = NonmovableGrowableArrayGCREF()
        self.fail_boxes_float = NonmovableGrowableArrayFloat()
        self.fail_ebp = 0
        self.setup_failure_recovery()

    def leave_jitted_hook(self):
        i = 0
        while i < self.fail_boxes_ptr.lgt:
            chunk = self.fail_boxes_ptr.chunks[i]
            llop.gc_assume_young_pointers(lltype.Void,
                                      llmemory.cast_ptr_to_adr(chunk))
            i += 1

    def make_sure_mc_exists(self):
        if self.mc is None:
            # the address of the function called by 'new'
            gc_ll_descr = self.cpu.gc_ll_descr
            gc_ll_descr.initialize()
            ll_new = gc_ll_descr.get_funcptr_for_new()
            self.malloc_func_addr = rffi.cast(lltype.Signed, ll_new)
            if gc_ll_descr.get_funcptr_for_newarray is not None:
                ll_new_array = gc_ll_descr.get_funcptr_for_newarray()
                self.malloc_array_func_addr = rffi.cast(lltype.Signed,
                                                        ll_new_array)
            if gc_ll_descr.get_funcptr_for_newstr is not None:
                ll_new_str = gc_ll_descr.get_funcptr_for_newstr()
                self.malloc_str_func_addr = rffi.cast(lltype.Signed,
                                                      ll_new_str)
            if gc_ll_descr.get_funcptr_for_newunicode is not None:
                ll_new_unicode = gc_ll_descr.get_funcptr_for_newunicode()
                self.malloc_unicode_func_addr = rffi.cast(lltype.Signed,
                                                          ll_new_unicode)
            # done
            # we generate the loop body in 'mc'
            # 'mc2' is for guard recovery code
            self.mc = MachineCodeBlockWrapper(self.mc_size)
            self.mc2 = MachineCodeBlockWrapper(self.mc_size)
            self._build_failure_recovery(False)
            self._build_failure_recovery(True)
            if self.cpu.supports_floats:
                self._build_failure_recovery(False, withfloats=True)
                self._build_failure_recovery(True, withfloats=True)

    def assemble_loop(self, inputargs, operations, looptoken):
        """adds the following attributes to looptoken:
               _x86_loop_code       (an integer giving an address)
               _x86_bootstrap_code  (an integer giving an address)
               _x86_frame_depth
               _x86_param_depth
               _x86_arglocs
        """
        self.make_sure_mc_exists()
        regalloc = RegAlloc(self, self.cpu.translate_support_code)
        arglocs = regalloc.prepare_loop(inputargs, operations, looptoken)
        looptoken._x86_arglocs = arglocs
        looptoken._x86_bootstrap_code = self.mc.tell()
        adr_stackadjust = self._assemble_bootstrap_code(inputargs, arglocs)
        looptoken._x86_loop_code = self.mc.tell()
        looptoken._x86_frame_depth = -1     # temporarily
        looptoken._x86_param_depth = -1     # temporarily        
        frame_depth, param_depth = self._assemble(regalloc, operations)
        self._patch_stackadjust(adr_stackadjust, frame_depth+param_depth)
        looptoken._x86_frame_depth = frame_depth
        looptoken._x86_param_depth = param_depth

    def assemble_bridge(self, faildescr, inputargs, operations):
        self.make_sure_mc_exists()
        arglocs = self.rebuild_faillocs_from_descr(
            faildescr._x86_failure_recovery_bytecode)
        if not we_are_translated():
            assert ([loc.assembler() for loc in arglocs] ==
                    [loc.assembler() for loc in faildescr._x86_debug_faillocs])
        regalloc = RegAlloc(self, self.cpu.translate_support_code)
        fail_depths = faildescr._x86_current_depths
        regalloc.prepare_bridge(fail_depths, inputargs, arglocs,
                                operations)
        adr_bridge = self.mc.tell()
        adr_stackadjust = self._patchable_stackadjust()
        frame_depth, param_depth = self._assemble(regalloc, operations)
        self._patch_stackadjust(adr_stackadjust, frame_depth+param_depth)
        if not we_are_translated():
            # for the benefit of tests
            faildescr._x86_bridge_frame_depth = frame_depth
            faildescr._x86_bridge_param_depth = param_depth
        # patch the jump from original guard
        self.patch_jump(faildescr, adr_bridge)

    def patch_jump(self, faildescr, adr_new_target):
        adr_jump_offset = faildescr._x86_adr_jump_offset
        mc = codebuf.InMemoryCodeBuilder(adr_jump_offset, adr_jump_offset + 4)
        mc.write(packimm32(adr_new_target - adr_jump_offset - 4))
        mc.valgrind_invalidated()
        mc.done()

    def _assemble(self, regalloc, operations):
        self._regalloc = regalloc
        regalloc.walk_operations(operations)        
        self.mc.done()
        self.mc2.done()
        if we_are_translated() or self.cpu.dont_keepalive_stuff:
            self._regalloc = None   # else keep it around for debugging
        frame_depth = regalloc.fm.frame_depth
        param_depth = regalloc.param_depth
        jump_target_descr = regalloc.jump_target_descr
        if jump_target_descr is not None:
            target_frame_depth = jump_target_descr._x86_frame_depth
            target_param_depth = jump_target_descr._x86_param_depth
            frame_depth = max(frame_depth, target_frame_depth)
            param_depth = max(param_depth, target_param_depth)
        return frame_depth, param_depth

    def _patchable_stackadjust(self):
        # stack adjustment LEA
        self.mc.LEA(esp, fixedsize_ebp_ofs(0))
        return self.mc.tell() - 4

    def _patch_stackadjust(self, adr_lea, reserved_depth):
        # patch stack adjustment LEA
        # possibly align, e.g. for Mac OS X        
        mc = codebuf.InMemoryCodeBuilder(adr_lea, adr_lea + 4)
        # Compute the correct offset for the instruction LEA ESP, [EBP-4*words].
        # Given that [EBP] is where we saved EBP, i.e. in the last word
        # of our fixed frame, then the 'words' value is:
        words = (FRAME_FIXED_SIZE - 1) + reserved_depth
        mc.write(packimm32(-WORD * words))
        mc.done()

    def _assemble_bootstrap_code(self, inputargs, arglocs):
        nonfloatlocs, floatlocs = arglocs
        self.mc.PUSH(ebp)
        self.mc.MOV(ebp, esp)
        self.mc.PUSH(ebx)
        self.mc.PUSH(esi)
        self.mc.PUSH(edi)
        # NB. the shape of the frame is hard-coded in get_basic_shape() too.
        # Also, make sure this is consistent with FRAME_FIXED_SIZE.
        adr_stackadjust = self._patchable_stackadjust()
        tmp = X86RegisterManager.all_regs[0]
        xmmtmp = X86XMMRegisterManager.all_regs[0]
        for i in range(len(nonfloatlocs)):
            loc = nonfloatlocs[i]
            if loc is None:
                continue
            if isinstance(loc, REG):
                target = loc
            else:
                target = tmp
            if inputargs[i].type == REF:
                # This uses XCHG to put zeroes in fail_boxes_ptr after
                # reading them
                self.mc.XOR(target, target)
                adr = self.fail_boxes_ptr.get_addr_for_num(i)
                self.mc.XCHG(target, heap(adr))
            else:
                adr = self.fail_boxes_int.get_addr_for_num(i)
                self.mc.MOV(target, heap(adr))
            if target is not loc:
                self.mc.MOV(loc, target)
        for i in range(len(floatlocs)):
            loc = floatlocs[i]
            if loc is None:
                continue
            adr = self.fail_boxes_float.get_addr_for_num(i)
            if isinstance(loc, REG):
                self.mc.MOVSD(loc, heap64(adr))
            else:
                self.mc.MOVSD(xmmtmp, heap64(adr))
                self.mc.MOVSD(loc, xmmtmp)
        return adr_stackadjust

    def dump(self, text):
        if not self.verbose:
            return
        _prev = Box._extended_display
        try:
            Box._extended_display = False
            print >> sys.stderr, ' 0x%x  %s' % (fixid(self.mc.tell()), text)
        finally:
            Box._extended_display = _prev

    # ------------------------------------------------------------

    def mov(self, from_loc, to_loc):
        if isinstance(from_loc, XMMREG) or isinstance(to_loc, XMMREG):
            self.mc.MOVSD(to_loc, from_loc)
        else:
            self.mc.MOV(to_loc, from_loc)

    regalloc_mov = mov # legacy interface

    def regalloc_push(self, loc):
        if isinstance(loc, XMMREG):
            self.mc.SUB(esp, imm(2*WORD))
            self.mc.MOVSD(mem64(esp, 0), loc)
        elif isinstance(loc, MODRM64):
            # XXX evil trick
            self.mc.PUSH(mem(ebp, get_ebp_ofs(loc.position)))
            self.mc.PUSH(mem(ebp, get_ebp_ofs(loc.position + 1)))
        else:
            self.mc.PUSH(loc)

    def regalloc_pop(self, loc):
        if isinstance(loc, XMMREG):
            self.mc.MOVSD(loc, mem64(esp, 0))
            self.mc.ADD(esp, imm(2*WORD))
        elif isinstance(loc, MODRM64):
            # XXX evil trick
            self.mc.POP(mem(ebp, get_ebp_ofs(loc.position + 1)))
            self.mc.POP(mem(ebp, get_ebp_ofs(loc.position)))
        else:
            self.mc.POP(loc)

    def regalloc_perform(self, op, arglocs, resloc):
        genop_list[op.opnum](self, op, arglocs, resloc)

    def regalloc_perform_discard(self, op, arglocs):
        genop_discard_list[op.opnum](self, op, arglocs)

    def regalloc_perform_with_guard(self, op, guard_op, faillocs,
                                    arglocs, resloc, current_depths):
        faildescr = guard_op.descr
        assert isinstance(faildescr, AbstractFailDescr)
        faildescr._x86_current_depths = current_depths
        failargs = guard_op.fail_args
        guard_opnum = guard_op.opnum
        failaddr = self.implement_guard_recovery(guard_opnum,
                                                 faildescr, failargs,
                                                 faillocs)
        if op is None:
            dispatch_opnum = guard_opnum
        else:
            dispatch_opnum = op.opnum
        adr_jump_offset = genop_guard_list[dispatch_opnum](self, op,
                                                           guard_op,
                                                           failaddr, arglocs,
                                                           resloc)
        faildescr._x86_adr_jump_offset = adr_jump_offset

    def regalloc_perform_guard(self, guard_op, faillocs, arglocs, resloc,
                               current_depths):
        self.regalloc_perform_with_guard(None, guard_op, faillocs, arglocs,
                                         resloc, current_depths)

    def load_effective_addr(self, sizereg, baseofs, scale, result):
        self.mc.LEA(result, addr_add(imm(0), sizereg, baseofs, scale))

    def _unaryop(asmop):
        def genop_unary(self, op, arglocs, resloc):
            getattr(self.mc, asmop)(arglocs[0])
        return genop_unary

    def _binaryop(asmop, can_swap=False):
        def genop_binary(self, op, arglocs, result_loc):
            getattr(self.mc, asmop)(arglocs[0], arglocs[1])
        return genop_binary

    def _cmpop(cond, rev_cond):
        def genop_cmp(self, op, arglocs, result_loc):
            if isinstance(op.args[0], Const):
                self.mc.CMP(arglocs[1], arglocs[0])
                self.mc.MOV(result_loc, imm8(0))
                getattr(self.mc, 'SET' + rev_cond)(lower_byte(result_loc))
            else:
                self.mc.CMP(arglocs[0], arglocs[1])
                self.mc.MOV(result_loc, imm8(0))
                getattr(self.mc, 'SET' + cond)(lower_byte(result_loc))
        return genop_cmp

    def _cmpop_float(cond):
        def genop_cmp(self, op, arglocs, result_loc):
            self.mc.UCOMISD(arglocs[0], arglocs[1])
            self.mc.MOV(result_loc, imm8(0))
            getattr(self.mc, 'SET' + cond)(lower_byte(result_loc))
        return genop_cmp

    def _cmpop_guard(cond, rev_cond, false_cond, false_rev_cond):
        def genop_cmp_guard(self, op, guard_op, addr, arglocs, result_loc):
            guard_opnum = guard_op.opnum
            if isinstance(op.args[0], Const):
                self.mc.CMP(arglocs[1], arglocs[0])
                if guard_opnum == rop.GUARD_FALSE:
                    name = 'J' + rev_cond
                    return self.implement_guard(addr, getattr(self.mc, name))
                else:
                    name = 'J' + false_rev_cond
                    return self.implement_guard(addr, getattr(self.mc, name))
            else:
                self.mc.CMP(arglocs[0], arglocs[1])
                if guard_opnum == rop.GUARD_FALSE:
                    name = 'J' + cond
                    return self.implement_guard(addr, getattr(self.mc, name))
                else:
                    name = 'J' + false_cond
                    return self.implement_guard(addr, getattr(self.mc, name))
        return genop_cmp_guard

##    XXX redo me
##    def align_stack_for_call(self, nargs):
##        # xxx do something when we don't use push anymore for calls
##        extra_on_stack = align_stack_words(nargs)
##        for i in range(extra_on_stack-nargs):
##            self.mc.PUSH(imm(0))   --- or just use a single SUB(esp, imm)
##        return extra_on_stack

    def call(self, addr, args, res):
        nargs = len(args)
        extra_on_stack = nargs #self.align_stack_for_call(nargs)
        for i in range(nargs-1, -1, -1):
            self.mc.PUSH(args[i])
        self.mc.CALL(rel32(addr))
        self.mark_gc_roots()
        self.mc.ADD(esp, imm(extra_on_stack * WORD))
        assert res is eax

    genop_int_neg = _unaryop("NEG")
    genop_int_invert = _unaryop("NOT")
    genop_int_add = _binaryop("ADD", True)
    genop_int_sub = _binaryop("SUB")
    genop_int_mul = _binaryop("IMUL", True)
    genop_int_and = _binaryop("AND", True)
    genop_int_or  = _binaryop("OR", True)
    genop_int_xor = _binaryop("XOR", True)
    genop_float_add = _binaryop("ADDSD", True)
    genop_float_sub = _binaryop('SUBSD')
    genop_float_mul = _binaryop('MULSD', True)
    genop_float_truediv = _binaryop('DIVSD')

    genop_int_mul_ovf = genop_int_mul
    genop_int_sub_ovf = genop_int_sub
    genop_int_add_ovf = genop_int_add

    genop_int_lt = _cmpop("L", "G")
    genop_int_le = _cmpop("LE", "GE")
    genop_int_eq = _cmpop("E", "E")
    genop_oois = genop_int_eq
    genop_int_ne = _cmpop("NE", "NE")
    genop_ooisnot = genop_int_ne
    genop_int_gt = _cmpop("G", "L")
    genop_int_ge = _cmpop("GE", "LE")

    genop_float_lt = _cmpop_float('B')
    genop_float_le = _cmpop_float('BE')
    genop_float_eq = _cmpop_float('E')
    genop_float_ne = _cmpop_float('NE')
    genop_float_gt = _cmpop_float('A')
    genop_float_ge = _cmpop_float('AE')

    genop_uint_gt = _cmpop("A", "B")
    genop_uint_lt = _cmpop("B", "A")
    genop_uint_le = _cmpop("BE", "AE")
    genop_uint_ge = _cmpop("AE", "BE")

    genop_guard_int_lt = _cmpop_guard("L", "G", "GE", "LE")
    genop_guard_int_le = _cmpop_guard("LE", "GE", "G", "L")
    genop_guard_int_eq = _cmpop_guard("E", "E", "NE", "NE")
    genop_guard_int_ne = _cmpop_guard("NE", "NE", "E", "E")
    genop_guard_int_gt = _cmpop_guard("G", "L", "LE", "GE")
    genop_guard_int_ge = _cmpop_guard("GE", "LE", "L", "G")

    genop_guard_uint_gt = _cmpop_guard("A", "B", "BE", "AE")
    genop_guard_uint_lt = _cmpop_guard("B", "A", "AE", "BE")
    genop_guard_uint_le = _cmpop_guard("BE", "AE", "A", "B")
    genop_guard_uint_ge = _cmpop_guard("AE", "BE", "B", "A")

    # for now all chars are being considered ints, although we should make
    # a difference at some point
    xxx_genop_char_eq = genop_int_eq

    def genop_float_neg(self, op, arglocs, resloc):
        self.mc.XORPD(arglocs[0], arglocs[1])

    def genop_float_abs(self, op, arglocs, resloc):
        self.mc.ANDPD(arglocs[0], arglocs[1])

    def genop_float_is_true(self, op, arglocs, resloc):
        loc0, loc1 = arglocs
        self.mc.XORPD(loc0, loc0)
        self.mc.UCOMISD(loc0, loc1)
        self.mc.SETNE(lower_byte(resloc))
        self.mc.MOVZX(resloc, lower_byte(resloc))

    def genop_cast_float_to_int(self, op, arglocs, resloc):
        self.mc.CVTTSD2SI(resloc, arglocs[0])

    def genop_cast_int_to_float(self, op, arglocs, resloc):
        self.mc.CVTSI2SD(resloc, arglocs[0])

    def genop_bool_not(self, op, arglocs, resloc):
        self.mc.XOR(arglocs[0], imm8(1))

    def genop_int_lshift(self, op, arglocs, resloc):
        loc, loc2 = arglocs
        if loc2 is ecx:
            loc2 = cl
        self.mc.SHL(loc, loc2)

    def genop_int_rshift(self, op, arglocs, resloc):
        loc, loc2 = arglocs
        if loc2 is ecx:
            loc2 = cl
        self.mc.SAR(loc, loc2)

    def genop_uint_rshift(self, op, arglocs, resloc):
        loc, loc2 = arglocs
        if loc2 is ecx:
            loc2 = cl
        self.mc.SHR(loc, loc2)

    def genop_guard_int_is_true(self, op, guard_op, addr, arglocs, resloc):
        guard_opnum = guard_op.opnum
        loc = arglocs[0]
        self.mc.TEST(loc, loc)
        if guard_opnum == rop.GUARD_TRUE:
            return self.implement_guard(addr, self.mc.JZ)
        else:
            return self.implement_guard(addr, self.mc.JNZ)

    def genop_int_is_true(self, op, arglocs, resloc):
        self.mc.CMP(arglocs[0], imm8(0))
        self.mc.MOV(resloc, imm8(0))
        self.mc.SETNE(lower_byte(resloc))

    def genop_same_as(self, op, arglocs, resloc):
        self.mov(arglocs[0], resloc)
    genop_cast_ptr_to_int = genop_same_as

    def genop_int_mod(self, op, arglocs, resloc):
        self.mc.CDQ()
        self.mc.IDIV(ecx)

    genop_int_floordiv = genop_int_mod

    def genop_new_with_vtable(self, op, arglocs, result_loc):
        assert result_loc is eax
        loc_vtable = arglocs[-1]
        assert isinstance(loc_vtable, IMM32)
        arglocs = arglocs[:-1]
        self.call(self.malloc_func_addr, arglocs, eax)
        # xxx ignore NULL returns for now
        self.set_vtable(eax, loc_vtable)

    def set_vtable(self, loc, loc_vtable):
        if self.cpu.vtable_offset is not None:
            self.mc.MOV(mem(loc, self.cpu.vtable_offset), loc_vtable)

    # XXX genop_new is abused for all varsized mallocs with Boehm, for now
    # (instead of genop_new_array, genop_newstr, genop_newunicode)
    def genop_new(self, op, arglocs, result_loc):
        assert result_loc is eax
        self.call(self.malloc_func_addr, arglocs, eax)

    def genop_new_array(self, op, arglocs, result_loc):
        assert result_loc is eax
        self.call(self.malloc_array_func_addr, arglocs, eax)

    def genop_newstr(self, op, arglocs, result_loc):
        assert result_loc is eax
        self.call(self.malloc_str_func_addr, arglocs, eax)

    def genop_newunicode(self, op, arglocs, result_loc):
        assert result_loc is eax
        self.call(self.malloc_unicode_func_addr, arglocs, eax)

    def genop_getfield_gc(self, op, arglocs, resloc):
        base_loc, ofs_loc, size_loc = arglocs
        assert isinstance(size_loc, IMM32)
        size = size_loc.value
        if size == 1:
            self.mc.MOVZX(resloc, addr8_add(base_loc, ofs_loc))
        elif size == 2:
            self.mc.MOVZX(resloc, addr_add(base_loc, ofs_loc))
        elif size == WORD:
            self.mc.MOV(resloc, addr_add(base_loc, ofs_loc))
        elif size == 8:
            self.mc.MOVSD(resloc, addr64_add(base_loc, ofs_loc))
        else:
            raise NotImplementedError("getfield size = %d" % size)

    genop_getfield_raw = genop_getfield_gc
    genop_getfield_raw_pure = genop_getfield_gc
    genop_getfield_gc_pure = genop_getfield_gc

    def genop_getarrayitem_gc(self, op, arglocs, resloc):
        base_loc, ofs_loc, scale, ofs = arglocs
        assert isinstance(ofs, IMM32)
        assert isinstance(scale, IMM32)
        if op.result.type == FLOAT:
            self.mc.MOVSD(resloc, addr64_add(base_loc, ofs_loc, ofs.value,
                                             scale.value))
        else:
            if scale.value == 0:
                self.mc.MOVZX(resloc, addr8_add(base_loc, ofs_loc, ofs.value,
                                                scale.value))
            elif scale.value == 2:
                self.mc.MOV(resloc, addr_add(base_loc, ofs_loc, ofs.value,
                                             scale.value))
            else:
                print "[asmgen]setarrayitem unsupported size: %d" % scale.value
                raise NotImplementedError()

    genop_getarrayitem_gc_pure = genop_getarrayitem_gc

    def genop_discard_setfield_gc(self, op, arglocs):
        base_loc, ofs_loc, size_loc, value_loc = arglocs
        assert isinstance(size_loc, IMM32)
        size = size_loc.value
        if size == WORD * 2:
            self.mc.MOVSD(addr64_add(base_loc, ofs_loc), value_loc)
        elif size == WORD:
            self.mc.MOV(addr_add(base_loc, ofs_loc), value_loc)
        elif size == 2:
            self.mc.MOV16(addr_add(base_loc, ofs_loc), value_loc)
        elif size == 1:
            self.mc.MOV(addr8_add(base_loc, ofs_loc), lower_byte(value_loc))
        else:
            print "[asmgen]setfield addr size %d" % size
            raise NotImplementedError("Addr size %d" % size)

    def genop_discard_setarrayitem_gc(self, op, arglocs):
        base_loc, ofs_loc, value_loc, scale_loc, baseofs = arglocs
        assert isinstance(baseofs, IMM32)
        assert isinstance(scale_loc, IMM32)
        if op.args[2].type == FLOAT:
            self.mc.MOVSD(addr64_add(base_loc, ofs_loc, baseofs.value,
                                     scale_loc.value), value_loc)
        else:
            if scale_loc.value == 2:
                self.mc.MOV(addr_add(base_loc, ofs_loc, baseofs.value,
                                     scale_loc.value), value_loc)
            elif scale_loc.value == 0:
                self.mc.MOV(addr8_add(base_loc, ofs_loc, baseofs.value,
                                      scale_loc.value), lower_byte(value_loc))
            else:
                raise NotImplementedError("scale = %d" % scale_loc.value)

    def genop_discard_strsetitem(self, op, arglocs):
        base_loc, ofs_loc, val_loc = arglocs
        basesize, itemsize, ofs_length = symbolic.get_array_token(rstr.STR,
                                              self.cpu.translate_support_code)
        assert itemsize == 1
        self.mc.MOV(addr8_add(base_loc, ofs_loc, basesize),
                    lower_byte(val_loc))

    def genop_discard_unicodesetitem(self, op, arglocs):
        base_loc, ofs_loc, val_loc = arglocs
        basesize, itemsize, ofs_length = symbolic.get_array_token(rstr.UNICODE,
                                              self.cpu.translate_support_code)
        if itemsize == 4:
            self.mc.MOV(addr_add(base_loc, ofs_loc, basesize, 2), val_loc)
        elif itemsize == 2:
            self.mc.MOV16(addr_add(base_loc, ofs_loc, basesize, 1), val_loc)
        else:
            assert 0, itemsize

    genop_discard_setfield_raw = genop_discard_setfield_gc
    genop_discard_setarrayitem_raw = genop_discard_setarrayitem_gc

    def genop_strlen(self, op, arglocs, resloc):
        base_loc = arglocs[0]
        basesize, itemsize, ofs_length = symbolic.get_array_token(rstr.STR,
                                             self.cpu.translate_support_code)
        self.mc.MOV(resloc, addr_add_const(base_loc, ofs_length))

    def genop_unicodelen(self, op, arglocs, resloc):
        base_loc = arglocs[0]
        basesize, itemsize, ofs_length = symbolic.get_array_token(rstr.UNICODE,
                                             self.cpu.translate_support_code)
        self.mc.MOV(resloc, addr_add_const(base_loc, ofs_length))

    def genop_arraylen_gc(self, op, arglocs, resloc):
        base_loc, ofs_loc = arglocs
        assert isinstance(ofs_loc, IMM32)
        self.mc.MOV(resloc, addr_add_const(base_loc, ofs_loc.value))

    def genop_strgetitem(self, op, arglocs, resloc):
        base_loc, ofs_loc = arglocs
        basesize, itemsize, ofs_length = symbolic.get_array_token(rstr.STR,
                                             self.cpu.translate_support_code)
        assert itemsize == 1
        self.mc.MOVZX(resloc, addr8_add(base_loc, ofs_loc, basesize))

    def genop_unicodegetitem(self, op, arglocs, resloc):
        base_loc, ofs_loc = arglocs
        basesize, itemsize, ofs_length = symbolic.get_array_token(rstr.UNICODE,
                                             self.cpu.translate_support_code)
        if itemsize == 4:
            self.mc.MOV(resloc, addr_add(base_loc, ofs_loc, basesize, 2))
        elif itemsize == 2:
            self.mc.MOVZX(resloc, addr_add(base_loc, ofs_loc, basesize, 1))
        else:
            assert 0, itemsize

    def genop_guard_guard_true(self, ign_1, guard_op, addr, locs, ign_2):
        loc = locs[0]
        self.mc.TEST(loc, loc)
        return self.implement_guard(addr, self.mc.JZ)
    genop_guard_guard_nonnull = genop_guard_guard_true

    def genop_guard_guard_no_exception(self, ign_1, guard_op, addr,
                                       locs, ign_2):
        self.mc.CMP(heap(self.cpu.pos_exception()), imm(0))
        return self.implement_guard(addr, self.mc.JNZ)

    def genop_guard_guard_exception(self, ign_1, guard_op, addr,
                                    locs, resloc):
        loc = locs[0]
        loc1 = locs[1]
        self.mc.MOV(loc1, heap(self.cpu.pos_exception()))
        self.mc.CMP(loc1, loc)
        addr = self.implement_guard(addr, self.mc.JNE)
        if resloc is not None:
            self.mc.MOV(resloc, heap(self.cpu.pos_exc_value()))
        self.mc.MOV(heap(self.cpu.pos_exception()), imm(0))
        self.mc.MOV(heap(self.cpu.pos_exc_value()), imm(0))
        return addr

    def genop_guard_guard_no_overflow(self, ign_1, guard_op, addr,
                                      locs, resloc):
        return self.implement_guard(addr, self.mc.JO)

    def genop_guard_guard_overflow(self, ign_1, guard_op, addr,
                                   locs, resloc):
        return self.implement_guard(addr, self.mc.JNO)

    def genop_guard_guard_false(self, ign_1, guard_op, addr, locs, ign_2):
        loc = locs[0]
        self.mc.TEST(loc, loc)
        return self.implement_guard(addr, self.mc.JNZ)
    genop_guard_guard_isnull = genop_guard_guard_false

    def genop_guard_guard_value(self, ign_1, guard_op, addr, locs, ign_2):
        if guard_op.args[0].type == FLOAT:
            assert guard_op.args[1].type == FLOAT
            self.mc.UCOMISD(locs[0], locs[1])
        else:
            self.mc.CMP(locs[0], locs[1])
        return self.implement_guard(addr, self.mc.JNE)

    def _cmp_guard_class(self, mc, locs):
        offset = self.cpu.vtable_offset
        if offset is not None:
            mc.CMP(mem(locs[0], offset), locs[1])
        else:
            # XXX hard-coded assumption: to go from an object to its class
            # we use the following algorithm:
            #   - read the typeid from mem(locs[0]), i.e. at offset 0
            #   - keep the lower 16 bits read there
            #   - multiply by 4 and use it as an offset in type_info_group.
            loc = locs[1]
            assert isinstance(loc, IMM32)
            classptr = loc.value
            # here, we have to go back from 'classptr' to the value expected
            # from reading the 16 bits in the object header
            type_info_group = llop.gc_get_type_info_group(llmemory.Address)
            type_info_group = rffi.cast(lltype.Signed, type_info_group)
            expected_typeid = (classptr - type_info_group) >> 2
            mc.CMP16(mem(locs[0], 0), imm32(expected_typeid))

    def genop_guard_guard_class(self, ign_1, guard_op, addr, locs, ign_2):
        self._cmp_guard_class(self.mc._mc, locs)
        return self.implement_guard(addr, self.mc.JNE)

    def genop_guard_guard_nonnull_class(self, ign_1, guard_op,
                                        addr, locs, ign_2):
        mc = self.mc._mc
        mc.CMP(locs[0], imm8(1))
        mc.write(constlistofchars('\x72\x00'))             # JB later
        jb_location = mc.get_relative_pos()
        self._cmp_guard_class(mc, locs)
        # patch the JB above
        offset = mc.get_relative_pos() - jb_location
        assert 0 < offset <= 127
        mc.overwrite(jb_location-1, [chr(offset)])
        #
        return self.implement_guard(addr, self.mc.JNE)

    def implement_guard_recovery(self, guard_opnum, faildescr, failargs,
                                                               fail_locs):
        exc = (guard_opnum == rop.GUARD_EXCEPTION or
               guard_opnum == rop.GUARD_NO_EXCEPTION or
               guard_opnum == rop.GUARD_NOT_FORCED)
        return self.generate_quick_failure(faildescr, failargs, fail_locs, exc)

    def generate_quick_failure(self, faildescr, failargs, fail_locs, exc):
        """Generate the initial code for handling a failure.  We try to
        keep it as compact as possible.  The idea is that this code is
        executed at most once (and very often, zero times); when
        executed, it generates a more complete piece of code which can
        really handle recovery from this particular failure.
        """
        fail_index = self.cpu.get_fail_descr_number(faildescr)
        bytes_needed = 20 + 5 * len(failargs)    # conservative estimate
        if self.mc2.bytes_free() < bytes_needed:
            self.mc2.make_new_mc()
        mc = self.mc2._mc
        addr = mc.tell()
        withfloats = False
        for box in failargs:
            if box is not None and box.type == FLOAT:
                withfloats = True
                break
        mc.CALL(rel32(self.failure_recovery_code[exc + 2 * withfloats]))
        # write tight data that describes the failure recovery
        faildescr._x86_failure_recovery_bytecode = mc.tell()
        self.write_failure_recovery_description(mc, failargs, fail_locs)
        # write the fail_index too
        mc.write(packimm32(fail_index))
        # for testing the decoding, write a final byte 0xCC
        if not we_are_translated():
            mc.writechr(0xCC)
            faildescr._x86_debug_faillocs = [loc for loc in fail_locs
                                                 if loc is not None]
        return addr

    DESCR_REF       = 0x00
    DESCR_INT       = 0x01
    DESCR_FLOAT     = 0x02
    DESCR_SPECIAL   = 0x03
    DESCR_FROMSTACK = 8
    DESCR_STOP      = 0 | DESCR_SPECIAL
    DESCR_HOLE      = 4 | DESCR_SPECIAL

    def write_failure_recovery_description(self, mc, failargs, locs):
        for i in range(len(failargs)):
            arg = failargs[i]
            if arg is not None:
                if arg.type == REF:
                    kind = self.DESCR_REF
                elif arg.type == INT:
                    kind = self.DESCR_INT
                elif arg.type == FLOAT:
                    kind = self.DESCR_FLOAT
                else:
                    raise AssertionError("bogus kind")
                loc = locs[i]
                if isinstance(loc, MODRM):
                    n = self.DESCR_FROMSTACK + loc.position
                else:
                    assert isinstance(loc, REG)
                    n = loc.op
                n = kind + 4*n
                while n > 0x7F:
                    mc.writechr((n & 0x7F) | 0x80)
                    n >>= 7
            else:
                n = self.DESCR_HOLE
            mc.writechr(n)
        mc.writechr(self.DESCR_STOP)
        # preallocate the fail_boxes
        i = len(failargs) - 1
        if i >= 0:
            self.fail_boxes_int.get_addr_for_num(i)
            self.fail_boxes_ptr.get_addr_for_num(i)
            if self.cpu.supports_floats:
                self.fail_boxes_float.get_addr_for_num(i)

    def rebuild_faillocs_from_descr(self, bytecode):
        from pypy.jit.backend.x86.regalloc import X86FrameManager
        bytecode = rffi.cast(rffi.UCHARP, bytecode)
        arglocs = []
        while 1:
            # decode the next instruction from the bytecode
            code = rffi.cast(lltype.Signed, bytecode[0])
            bytecode = rffi.ptradd(bytecode, 1)
            if code >= 4*self.DESCR_FROMSTACK:
                # 'code' identifies a stack location
                if code > 0x7F:
                    shift = 7
                    code &= 0x7F
                    while True:
                        nextcode = rffi.cast(lltype.Signed, bytecode[0])
                        bytecode = rffi.ptradd(bytecode, 1)
                        code |= (nextcode & 0x7F) << shift
                        shift += 7
                        if nextcode <= 0x7F:
                            break
                kind = code & 3
                code = (code >> 2) - self.DESCR_FROMSTACK
                if kind == self.DESCR_FLOAT:
                    size = 2
                else:
                    size = 1
                loc = X86FrameManager.frame_pos(code, size)
            elif code == self.DESCR_STOP:
                break
            elif code == self.DESCR_HOLE:
                continue
            else:
                # 'code' identifies a register
                kind = code & 3
                code >>= 2
                if kind == self.DESCR_FLOAT:
                    loc = xmm_registers[code]
                else:
                    loc = registers[code]
            arglocs.append(loc)
        return arglocs[:]

    def grab_frame_values(self, bytecode, frame_addr, allregisters):
        # no malloc allowed here!!
        self.fail_ebp = allregisters[16 + ebp.op]
        num = 0
        value_hi = 0
        while 1:
            # decode the next instruction from the bytecode
            code = rffi.cast(lltype.Signed, bytecode[0])
            bytecode = rffi.ptradd(bytecode, 1)
            if code >= 4*self.DESCR_FROMSTACK:
                if code > 0x7F:
                    shift = 7
                    code &= 0x7F
                    while True:
                        nextcode = rffi.cast(lltype.Signed, bytecode[0])
                        bytecode = rffi.ptradd(bytecode, 1)
                        code |= (nextcode & 0x7F) << shift
                        shift += 7
                        if nextcode <= 0x7F:
                            break
                # load the value from the stack
                kind = code & 3
                code = (code >> 2) - self.DESCR_FROMSTACK
                stackloc = frame_addr + get_ebp_ofs(code)
                value = rffi.cast(rffi.LONGP, stackloc)[0]
                if kind == self.DESCR_FLOAT:
                    value_hi = value
                    value = rffi.cast(rffi.LONGP, stackloc - 4)[0]
            else:
                # 'code' identifies a register: load its value
                kind = code & 3
                if kind == self.DESCR_SPECIAL:
                    if code == self.DESCR_HOLE:
                        num += 1
                        continue
                    assert code == self.DESCR_STOP
                    break
                code >>= 2
                if kind == self.DESCR_FLOAT:
                    value = allregisters[2*code]
                    value_hi = allregisters[2*code + 1]
                else:
                    value = allregisters[16 + code]

            # store the loaded value into fail_boxes_<type>
            if kind == self.DESCR_INT:
                tgt = self.fail_boxes_int.get_addr_for_num(num)
            elif kind == self.DESCR_REF:
                tgt = self.fail_boxes_ptr.get_addr_for_num(num)
            elif kind == self.DESCR_FLOAT:
                tgt = self.fail_boxes_float.get_addr_for_num(num)
                rffi.cast(rffi.LONGP, tgt)[1] = value_hi
            else:
                assert 0, "bogus kind"
            rffi.cast(rffi.LONGP, tgt)[0] = value
            num += 1
        #
        if not we_are_translated():
            assert bytecode[4] == 0xCC
        fail_index = rffi.cast(rffi.LONGP, bytecode)[0]
        return fail_index

    def setup_failure_recovery(self):

        def failure_recovery_func(registers):
            # 'registers' is a pointer to a structure containing the
            # original value of the registers, optionally the original
            # value of XMM registers, and finally a reference to the
            # recovery bytecode.  See _build_failure_recovery() for details.
            stack_at_ebp = registers[ebp.op]
            bytecode = rffi.cast(rffi.UCHARP, registers[8])
            allregisters = rffi.ptradd(registers, -16)
            return self.grab_frame_values(bytecode, stack_at_ebp, allregisters)

        self.failure_recovery_func = failure_recovery_func
        self.failure_recovery_code = [0, 0, 0, 0]

    _FAILURE_RECOVERY_FUNC = lltype.Ptr(lltype.FuncType([rffi.LONGP],
                                                        lltype.Signed))

    def _build_failure_recovery(self, exc, withfloats=False):
        failure_recovery_func = llhelper(self._FAILURE_RECOVERY_FUNC,
                                         self.failure_recovery_func)
        failure_recovery_func = rffi.cast(lltype.Signed,
                                          failure_recovery_func)
        mc = self.mc2._mc
        # Assume that we are called at the beginning, when there is no risk
        # that 'mc' runs out of space.  Checked by asserts in mc.write().
        recovery_addr = mc.tell()
        mc.PUSH(edi)
        mc.PUSH(esi)
        mc.PUSH(ebp)
        mc.PUSH(esp)  # <-- not really used, but needed to take up the space
        mc.PUSH(ebx)
        mc.PUSH(edx)
        mc.PUSH(ecx)
        mc.PUSH(eax)
        mc.MOV(esi, esp)
        if withfloats:
            mc.SUB(esp, imm(8*8))
            for i in range(8):
                mc.MOVSD(mem64(esp, 8*i), xmm_registers[i])

        # we call a provided function that will
        # - call our on_leave_jitted_hook which will mark
        #   the fail_boxes_ptr array as pointing to young objects to
        #   avoid unwarranted freeing
        # - optionally save exception depending on the flag
        addr = self.cpu.get_on_leave_jitted_int(save_exception=exc)
        mc.CALL(rel32(addr))

        # the following call saves all values from the stack and from
        # registers to the right 'fail_boxes_<type>' location.
        # Note that the registers are saved so far in esi[0] to esi[7],
        # as pushed above, plus optionally in esi[-16] to esi[-1] for
        # the XMM registers.  Moreover, esi[8] is a pointer to the recovery
        # bytecode, pushed just before by the CALL instruction written by
        # generate_quick_failure().
        mc.PUSH(esi)
        mc.CALL(rel32(failure_recovery_func))
        # returns in eax the fail_index

        # now we return from the complete frame, which starts from
        # _assemble_bootstrap_code().  The LEA below throws away most
        # of the frame, including all the PUSHes that we did just above.
        mc.LEA(esp, addr_add(ebp, imm(-3 * WORD)))
        mc.POP(edi)    # [ebp-12]
        mc.POP(esi)    # [ebp-8]
        mc.POP(ebx)    # [ebp-4]
        mc.POP(ebp)    # [ebp]
        mc.RET()
        self.mc2.done()
        self.failure_recovery_code[exc + 2 * withfloats] = recovery_addr

    def generate_failure(self, mc, fail_index, locs, exc, locs_are_ref):
        for i in range(len(locs)):
            loc = locs[i]
            if isinstance(loc, REG):
                if loc.width == 8:
                    adr = self.fail_boxes_float.get_addr_for_num(i)
                    mc.MOVSD(heap64(adr), loc)
                else:
                    if locs_are_ref[i]:
                        adr = self.fail_boxes_ptr.get_addr_for_num(i)
                    else:
                        adr = self.fail_boxes_int.get_addr_for_num(i)
                    mc.MOV(heap(adr), loc)
        for i in range(len(locs)):
            loc = locs[i]
            if not isinstance(loc, REG):
                if loc.width == 8:
                    mc.MOVSD(xmm0, loc)
                    adr = self.fail_boxes_float.get_addr_for_num(i)
                    mc.MOVSD(heap64(adr), xmm0)
                else:
                    if locs_are_ref[i]:
                        adr = self.fail_boxes_ptr.get_addr_for_num(i)
                    else:
                        adr = self.fail_boxes_int.get_addr_for_num(i)
                    mc.MOV(eax, loc)
                    mc.MOV(heap(adr), eax)

        # we call a provided function that will
        # - call our on_leave_jitted_hook which will mark
        #   the fail_boxes_ptr array as pointing to young objects to
        #   avoid unwarranted freeing
        # - optionally save exception depending on the flag
        addr = self.cpu.get_on_leave_jitted_int(save_exception=exc)
        mc.CALL(rel32(addr))

        # don't break the following code sequence!   xxx no reason any more?
        mc = mc._mc
        mc.LEA(esp, addr_add(ebp, imm(-3 * WORD)))
        mc.MOV(eax, imm(fail_index))
        mc.POP(edi)    # [ebp-12]
        mc.POP(esi)    # [ebp-8]
        mc.POP(ebx)    # [ebp-4]
        mc.POP(ebp)    # [ebp]
        mc.RET()

    @specialize.arg(2)
    def implement_guard(self, addr, emit_jump):
        emit_jump(rel32(addr))
        return self.mc.tell() - 4

    def genop_call(self, op, arglocs, resloc):
        sizeloc = arglocs[0]
        assert isinstance(sizeloc, IMM32)
        size = sizeloc.value
        nargs = len(op.args)-1
        extra_on_stack = 0
        for arg in range(2, nargs + 2):
            extra_on_stack += round_up_to_4(arglocs[arg].width)

        self._regalloc.reserve_param(extra_on_stack//4)
        
        #extra_on_stack = self.align_stack_for_call(extra_on_stack)
        if isinstance(op.args[0], Const):
            x = rel32(op.args[0].getint())
        else:
            x = arglocs[1]
        if x is eax:
            tmp = ecx
        else:
            tmp = eax
        p = 0
        for i in range(2, nargs + 2):
            loc = arglocs[i]
            if isinstance(loc, REG):
                if isinstance(loc, XMMREG):
                    self.mc.MOVSD(mem64(esp, p), loc)
                else:
                    self.mc.MOV(mem(esp, p), loc)
            p += round_up_to_4(loc.width)
        p = 0
        for i in range(2, nargs + 2):
            loc = arglocs[i]
            if not isinstance(loc, REG):
                if isinstance(loc, MODRM64):
                    self.mc.MOVSD(xmm0, loc)
                    self.mc.MOVSD(mem64(esp, p), xmm0)
                else:
                    self.mc.MOV(tmp, loc)
                    self.mc.MOV(mem(esp, p), tmp)
            p += round_up_to_4(loc.width)
        self.mc.CALL(x)
        self.mark_gc_roots()
        if isinstance(resloc, MODRM64):
            self.mc.FSTP(resloc)
        elif size == 1:
            self.mc.AND(eax, imm(0xff))
        elif size == 2:
            self.mc.AND(eax, imm(0xffff))

    genop_call_pure = genop_call
    
    def genop_guard_call_may_force(self, op, guard_op, addr,
                                   arglocs, result_loc):
        faildescr = guard_op.descr
        fail_index = self.cpu.get_fail_descr_number(faildescr)
        self.mc.MOV(mem(ebp, FORCE_INDEX_OFS), imm(fail_index))
        self.genop_call(op, arglocs, result_loc)
        self.mc.CMP(mem(ebp, FORCE_INDEX_OFS), imm(0))
        return self.implement_guard(addr, self.mc.JL)

    def genop_discard_cond_call_gc_wb(self, op, arglocs):
        # use 'mc._mc' directly instead of 'mc', to avoid
        # bad surprizes if the code buffer is mostly full
        loc_cond = arglocs[0]
        loc_mask = arglocs[1]
        mc = self.mc._mc
        mc.TEST(loc_cond, loc_mask)
        mc.write(constlistofchars('\x74\x00'))             # JZ after_the_call
        jz_location = mc.get_relative_pos()
        # the following is supposed to be the slow path, so whenever possible
        # we choose the most compact encoding over the most efficient one.
        for i in range(len(arglocs)-1, 2, -1):
            mc.PUSH(arglocs[i])
        mc.CALL(rel32(op.args[2].getint()))
        pop_count = 0
        for i in range(3, len(arglocs)):
            loc = arglocs[i]
            pop_count += 1
            if isinstance(loc, REG):
                while pop_count > 0:
                    mc.POP(loc)
                    pop_count -= 1
        if pop_count:
            mc.ADD(esp, imm(WORD * pop_count))
        # patch the JZ above
        offset = mc.get_relative_pos() - jz_location
        assert 0 < offset <= 127
        mc.overwrite(jz_location-1, [chr(offset)])

    def genop_force_token(self, op, arglocs, resloc):
        self.mc.LEA(resloc, mem(ebp, FORCE_INDEX_OFS))

    def not_implemented_op_discard(self, op, arglocs):
        msg = "not implemented operation: %s" % op.getopname()
        print msg
        raise NotImplementedError(msg)

    def not_implemented_op(self, op, arglocs, resloc):
        msg = "not implemented operation with res: %s" % op.getopname()
        print msg
        raise NotImplementedError(msg)

    def not_implemented_op_guard(self, op, guard_op,
                                 failaddr, arglocs, resloc):
        msg = "not implemented operation (guard): %s" % guard_op.getopname()
        print msg
        raise NotImplementedError(msg)

    def mark_gc_roots(self):
        gcrootmap = self.cpu.gc_ll_descr.gcrootmap
        if gcrootmap:
            mark = self._regalloc.get_mark_gc_roots(gcrootmap)
            gcrootmap.put(rffi.cast(llmemory.Address, self.mc.tell()), mark)

    def target_arglocs(self, loop_token):
        return loop_token._x86_arglocs

    def closing_jump(self, loop_token):
        self.mc.JMP(rel32(loop_token._x86_loop_code))

    def malloc_cond_fixedsize(self, nursery_free_adr, nursery_top_adr,
                              size, tid, slowpath_addr):
        # don't use self.mc
        mc = self.mc._mc
        mc.MOV(eax, heap(nursery_free_adr))
        mc.LEA(edx, addr_add(eax, imm(size)))
        mc.CMP(edx, heap(nursery_top_adr))
        mc.write(constlistofchars('\x76\x00')) # JNA after the block
        jmp_adr = mc.get_relative_pos()
        mc.PUSH(imm(size))
        mc.CALL(rel32(slowpath_addr))
        self.mark_gc_roots()
        # note that slowpath_addr returns a "long long", or more precisely
        # two results, which end up in eax and edx.
        # eax should contain the result of allocation, edx new value
        # of nursery_free_adr
        mc.ADD(esp, imm(4))
        offset = mc.get_relative_pos() - jmp_adr
        assert 0 < offset <= 127
        mc.overwrite(jmp_adr-1, [chr(offset)])
        mc.MOV(addr_add(eax, imm(0)), imm(tid))
        mc.MOV(heap(nursery_free_adr), edx)
        
genop_discard_list = [Assembler386.not_implemented_op_discard] * rop._LAST
genop_list = [Assembler386.not_implemented_op] * rop._LAST
genop_guard_list = [Assembler386.not_implemented_op_guard] * rop._LAST

for name, value in Assembler386.__dict__.iteritems():
    if name.startswith('genop_discard_'):
        opname = name[len('genop_discard_'):]
        num = getattr(rop, opname.upper())
        genop_discard_list[num] = value
    elif name.startswith('genop_guard_') and name != 'genop_guard_exception': 
        opname = name[len('genop_guard_'):]
        num = getattr(rop, opname.upper())
        genop_guard_list[num] = value
    elif name.startswith('genop_'):
        opname = name[len('genop_'):]
        num = getattr(rop, opname.upper())
        genop_list[num] = value

def new_addr_add(heap, mem, memsib):
    def addr_add(reg_or_imm1, reg_or_imm2, offset=0, scale=0):
        if isinstance(reg_or_imm1, IMM32):
            if isinstance(reg_or_imm2, IMM32):
                return heap(reg_or_imm1.value + offset +
                            (reg_or_imm2.value << scale))
            else:
                return memsib(None, reg_or_imm2, scale, reg_or_imm1.value + offset)
        else:
            if isinstance(reg_or_imm2, IMM32):
                return mem(reg_or_imm1, offset + (reg_or_imm2.value << scale))
            else:
                return memsib(reg_or_imm1, reg_or_imm2, scale, offset)
    return addr_add

addr8_add = new_addr_add(heap8, mem8, memSIB8)
addr_add = new_addr_add(heap, mem, memSIB)
addr64_add = new_addr_add(heap64, mem64, memSIB64)

def addr_add_const(reg_or_imm1, offset):
    if isinstance(reg_or_imm1, IMM32):
        return heap(reg_or_imm1.value + offset)
    else:
        return mem(reg_or_imm1, offset)

def round_up_to_4(size):
    if size < 4:
        return 4
    return size
