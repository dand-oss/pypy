import sys
from rpython.jit.metainterp.history import ConstInt
from rpython.jit.metainterp.optimize import InvalidLoop
from rpython.jit.metainterp.optimizeopt.intutils import (IntBound,
    IntLowerBound, IntUpperBound, ConstIntBound)
from rpython.jit.metainterp.optimizeopt.optimizer import (Optimization, CONST_1,
    CONST_0)
from rpython.jit.metainterp.optimizeopt.util import make_dispatcher_method
from rpython.jit.metainterp.resoperation import rop, AbstractResOp
from rpython.jit.metainterp.optimizeopt import vstring
from rpython.rlib.rarithmetic import intmask

def get_integer_min(is_unsigned, byte_size):
    if is_unsigned:
        return 0
    else:
        return -(1 << ((byte_size << 3) - 1))


def get_integer_max(is_unsigned, byte_size):
    if is_unsigned:
        return (1 << (byte_size << 3)) - 1
    else:
        return (1 << ((byte_size << 3) - 1)) - 1


IS_64_BIT = sys.maxint > 2**32

def next_pow2_m1(n):
    """Calculate next power of 2 greater than n minus one."""
    n |= n >> 1
    n |= n >> 2
    n |= n >> 4
    n |= n >> 8
    n |= n >> 16
    if IS_64_BIT:
        n |= n >> 32
    return n


class OptIntBounds(Optimization):
    """Keeps track of the bounds placed on integers by guards and remove
       redundant guards"""

    def propagate_forward(self, op):
        dispatch_opt(self, op)

    def opt_default(self, op):
        assert not op.is_ovf()
        self.emit_operation(op)

    def propagate_bounds_backward(self, box):
        # FIXME: This takes care of the instruction where box is the reuslt
        #        but the bounds produced by all instructions where box is
        #        an argument might also be tighten
        b = self.getintbound(box)
        if b.has_lower and b.has_upper and b.lower == b.upper:
            self.make_constant_int(box, b.lower)

        if isinstance(box, AbstractResOp):
            dispatch_bounds_ops(self, box)

    def _optimize_guard_true_false_value(self, op):
        self.emit_operation(op)
        if op.getarg(0).type == 'i':
            self.propagate_bounds_backward(op.getarg(0))

    optimize_GUARD_TRUE = _optimize_guard_true_false_value
    optimize_GUARD_FALSE = _optimize_guard_true_false_value
    optimize_GUARD_VALUE = _optimize_guard_true_false_value

    def optimize_INT_OR_or_XOR(self, op):
        v1 = self.get_box_replacement(op.getarg(0))
        b1 = self.getintbound(v1)
        v2 = self.get_box_replacement(op.getarg(1))
        b2 = self.getintbound(v2)
        if v1 is v2:
            if op.getopnum() == rop.INT_OR:
                self.make_equal_to(op, v1)
            else:
                self.make_constant_int(op, 0)
            return
        self.emit_operation(op)
        if b1.known_ge(IntBound(0, 0)) and \
           b2.known_ge(IntBound(0, 0)):
            r = self.getintbound(op)
            mostsignificant = b1.upper | b2.upper
            r.intersect(IntBound(0, next_pow2_m1(mostsignificant)))

    optimize_INT_OR = optimize_INT_OR_or_XOR
    optimize_INT_XOR = optimize_INT_OR_or_XOR

    def optimize_INT_AND(self, op):
        b1 = self.getintbound(op.getarg(0))
        b2 = self.getintbound(op.getarg(1))
        self.emit_operation(op)

        r = self.getintbound(op)
        if b2.is_constant():
            val = b2.lower
            if val >= 0:
                r.intersect(IntBound(0, val))
        elif b1.is_constant():
            val = b1.lower
            if val >= 0:
                r.intersect(IntBound(0, val))
        elif b1.known_ge(IntBound(0, 0)) and b2.known_ge(IntBound(0, 0)):
            lesser = min(b1.upper, b2.upper)
            r.intersect(IntBound(0, next_pow2_m1(lesser)))

    def optimize_INT_SUB(self, op):
        self.emit_operation(op)
        b1 = self.getintbound(op.getarg(0))
        b2 = self.getintbound(op.getarg(1))
        b = b1.sub_bound(b2)
        if b.bounded():
            self.getintbound(op).intersect(b)

    def optimize_INT_ADD(self, op):
        arg1 = self.get_box_replacement(op.getarg(0))
        arg2 = self.get_box_replacement(op.getarg(1))
        v1 = self.getintbound(arg1)
        v2 = self.getintbound(arg2)

        # Optimize for addition chains in code "b = a + 1; c = b + 1" by
        # detecting the int_add chain, and swapping with "b = a + 1;
        # c = a + 2". If b is not used elsewhere, the backend eliminates
        # it.

        # either v1 or v2 can be a constant, swap the arguments around if
        # v1 is the constant
        if v1.is_constant():
            arg1, arg2 = arg2, arg1
            v1, v2 = v2, v1
        # if both are constant, the pure optimization will deal with it
        if v2.is_constant() and not v1.is_constant():
            if not self.optimizer.is_inputarg(arg1):
                if arg1.getopnum() == rop.INT_ADD:
                    prod_arg1 = arg1.getarg(0)
                    prod_arg2 = arg1.getarg(1)
                    prod_v1 = self.getintbound(prod_arg1)
                    prod_v2 = self.getintbound(prod_arg2)

                    # same thing here: prod_v1 or prod_v2 can be a
                    # constant
                    if prod_v1.is_constant():
                        prod_arg1, prod_arg2 = prod_arg2, prod_arg1
                        prod_v1, prod_v2 = prod_v2, prod_v1
                    if prod_v2.is_constant():
                        sum = intmask(arg2.getint() + prod_arg2.getint())
                        arg1 = prod_arg1
                        arg2 = ConstInt(sum)
                        op = self.replace_op_with(op, rop.INT_ADD, args=[arg1, arg2])

        self.emit_operation(op)
        if self.is_raw_ptr(op.getarg(0)) or self.is_raw_ptr(op.getarg(1)):
            return
        b1 = self.getintbound(op.getarg(0))
        b2 = self.getintbound(op.getarg(1))
        r = self.getintbound(op)
        b = b1.add_bound(b2)
        if b.bounded():
            r.intersect(b)

    def optimize_INT_MUL(self, op):
        b1 = self.getintbound(op.getarg(0))
        b2 = self.getintbound(op.getarg(1))
        self.emit_operation(op)
        r = self.getintbound(op)
        b = b1.mul_bound(b2)
        if b.bounded():
            r.intersect(b)

    def optimize_INT_FLOORDIV(self, op):
        b1 = self.getintbound(op.getarg(0))
        b2 = self.getintbound(op.getarg(1))
        self.emit_operation(op)
        r = self.getintbound(op)
        r.intersect(b1.div_bound(b2))

    def optimize_INT_MOD(self, op):
        b1 = self.getintbound(op.getarg(0))
        b2 = self.getintbound(op.getarg(1))
        known_nonneg = (b1.known_ge(IntBound(0, 0)) and
                        b2.known_ge(IntBound(0, 0)))
        if known_nonneg and b2.is_constant():
            val = b2.getint()
            if (val & (val-1)) == 0:
                # nonneg % power-of-two ==> nonneg & (power-of-two - 1)
                arg1 = op.getarg(0)
                arg2 = ConstInt(val-1)
                op = self.replace_op_with(op, rop.INT_AND,
                                          args=[arg1, arg2])
        self.emit_operation(op)
        if b2.is_constant():
            val = b2.getint()
            r = self.getintbound(op)
            if val < 0:
                if val == -sys.maxint-1:
                    return     # give up
                val = -val
            if known_nonneg:
                r.make_ge(IntBound(0, 0))
            else:
                r.make_gt(IntBound(-val, -val))
            r.make_lt(IntBound(val, val))

    def optimize_INT_LSHIFT(self, op):
        arg0 = self.get_box_replacement(op.getarg(0))
        b1 = self.getintbound(arg0)
        arg1 = self.get_box_replacement(op.getarg(1))
        b2 = self.getintbound(arg1)
        self.emit_operation(op)
        r = self.getintbound(op)
        b = b1.lshift_bound(b2)
        r.intersect(b)
        # intbound.lshift_bound checks for an overflow and if the
        # lshift can be proven not to overflow sets b.has_upper and
        # b.has_lower
        if b.has_lower and b.has_upper:
            # Synthesize the reverse op for optimize_default to reuse
            self.pure_from_args(rop.INT_RSHIFT,
                                [op, arg1], arg0)

    def optimize_INT_RSHIFT(self, op):
        b1 = self.getintbound(op.getarg(0))
        b2 = self.getintbound(op.getarg(1))
        b = b1.rshift_bound(b2)
        if b.has_lower and b.has_upper and b.lower == b.upper:
            # constant result (likely 0, for rshifts that kill all bits)
            self.make_constant_int(op, b.lower)
        else:
            self.emit_operation(op)
            r = self.getintbound(op)
            r.intersect(b)

    def optimize_GUARD_NO_OVERFLOW(self, op):
        lastop = self.last_emitted_operation
        if lastop is not None:
            opnum = lastop.getopnum()
            args = lastop.getarglist()
            result = lastop
            # If the INT_xxx_OVF was replaced with INT_xxx or removed
            # completely, then we can kill the GUARD_NO_OVERFLOW.
            if (opnum != rop.INT_ADD_OVF and
                opnum != rop.INT_SUB_OVF and
                opnum != rop.INT_MUL_OVF):
                return
            # Else, synthesize the non overflowing op for optimize_default to
            # reuse, as well as the reverse op
            elif opnum == rop.INT_ADD_OVF:
                #self.pure(rop.INT_ADD, args[:], result)
                self.pure_from_args(rop.INT_SUB, [result, args[1]], args[0])
                self.pure_from_args(rop.INT_SUB, [result, args[0]], args[1])
            elif opnum == rop.INT_SUB_OVF:
                #self.pure(rop.INT_SUB, args[:], result)
                self.pure_from_args(rop.INT_ADD, [result, args[1]], args[0])
                self.pure_from_args(rop.INT_SUB, [args[0], result], args[1])
            #elif opnum == rop.INT_MUL_OVF:
            #    self.pure(rop.INT_MUL, args[:], result)
        self.emit_operation(op)

    def optimize_GUARD_OVERFLOW(self, op):
        # If INT_xxx_OVF was replaced by INT_xxx, *but* we still see
        # GUARD_OVERFLOW, then the loop is invalid.
        lastop = self.last_emitted_operation
        if lastop is None:
            raise InvalidLoop('An INT_xxx_OVF was proven not to overflow but' +
                              'guarded with GUARD_OVERFLOW')
        opnum = lastop.getopnum()
        if opnum not in (rop.INT_ADD_OVF, rop.INT_SUB_OVF, rop.INT_MUL_OVF):
            raise InvalidLoop('An INT_xxx_OVF was proven not to overflow but' +
                              'guarded with GUARD_OVERFLOW')

        self.emit_operation(op)

    def optimize_INT_ADD_OVF(self, op):
        b1 = self.getintbound(op.getarg(0))
        b2 = self.getintbound(op.getarg(1))
        resbound = b1.add_bound(b2)
        if resbound.bounded():
            # Transform into INT_ADD.  The following guard will be killed
            # by optimize_GUARD_NO_OVERFLOW; if we see instead an
            # optimize_GUARD_OVERFLOW, then InvalidLoop.
            op = self.replace_op_with(op, rop.INT_ADD)
        self.emit_operation(op) # emit the op
        r = self.getintbound(op)
        r.intersect(resbound)

    def optimize_INT_SUB_OVF(self, op):
        arg0 = self.get_box_replacement(op.getarg(0))
        arg1 = self.get_box_replacement(op.getarg(1))
        b0 = self.getintbound(arg0)
        b1 = self.getintbound(arg1)
        if arg0.same_box(arg1):
            self.make_constant_int(op, 0)
            return
        resbound = b0.sub_bound(b1)
        if resbound.bounded():
            op = self.replace_op_with(op, rop.INT_SUB)
        self.emit_operation(op) # emit the op
        r = self.getintbound(op)
        r.intersect(resbound)

    def optimize_INT_MUL_OVF(self, op):
        b1 = self.getintbound(op.getarg(0))
        b2 = self.getintbound(op.getarg(1))
        resbound = b1.mul_bound(b2)
        if resbound.bounded():
            op = self.replace_op_with(op, rop.INT_MUL)
        self.emit_operation(op)
        r = self.getintbound(op)
        r.intersect(resbound)

    def optimize_INT_LT(self, op):
        arg1 = self.get_box_replacement(op.getarg(0))
        arg2 = self.get_box_replacement(op.getarg(1))
        b1 = self.getintbound(arg1)
        b2 = self.getintbound(arg2)
        if b1.known_lt(b2):
            self.make_constant_int(op, 1)
        elif b1.known_ge(b2) or arg1 is arg2:
            self.make_constant_int(op, 0)
        else:
            self.emit_operation(op)

    def optimize_INT_GT(self, op):
        arg1 = self.get_box_replacement(op.getarg(0))
        arg2 = self.get_box_replacement(op.getarg(1))
        b1 = self.getintbound(arg1)
        b2 = self.getintbound(arg2)
        if b1.known_gt(b2):
            self.make_constant_int(op, 1)
        elif b1.known_le(b2) or arg1 is arg2:
            self.make_constant_int(op, 0)
        else:
            self.emit_operation(op)

    def optimize_INT_LE(self, op):
        arg1 = self.get_box_replacement(op.getarg(0))
        arg2 = self.get_box_replacement(op.getarg(1))
        b1 = self.getintbound(arg1)
        b2 = self.getintbound(arg2)
        if b1.known_le(b2) or arg1 is arg2:
            self.make_constant_int(op, 1)
        elif b1.known_gt(b2):
            self.make_constant_int(op, 0)
        else:
            self.emit_operation(op)

    def optimize_INT_GE(self, op):
        arg1 = self.get_box_replacement(op.getarg(0))
        arg2 = self.get_box_replacement(op.getarg(1))
        b1 = self.getintbound(arg1)
        b2 = self.getintbound(arg2)
        if b1.known_ge(b2) or arg1 is arg2:
            self.make_constant_int(op, 1)
        elif b1.known_lt(b2):
            self.make_constant_int(op, 0)
        else:
            self.emit_operation(op)

    def optimize_INT_EQ(self, op):
        arg0 = self.get_box_replacement(op.getarg(0))
        arg1 = self.get_box_replacement(op.getarg(1))
        b1 = self.getintbound(op.getarg(0))
        b2 = self.getintbound(op.getarg(1))
        if b1.known_gt(b2):
            self.make_constant_int(op, 0)
        elif b1.known_lt(b2):
            self.make_constant_int(op, 0)
        elif arg0.same_box(arg1):
            self.make_constant_int(op, 1)
        else:
            self.emit_operation(op)

    def optimize_INT_NE(self, op):
        arg0 = self.get_box_replacement(op.getarg(0))
        b1 = self.getintbound(arg0)
        arg1 = self.get_box_replacement(op.getarg(1))
        b2 = self.getintbound(arg1)
        if b1.known_gt(b2):
            self.make_constant_int(op, 1)
        elif b1.known_lt(b2):
            self.make_constant_int(op, 1)
        elif arg0 is arg1:
            self.make_constant_int(op, 0)
        else:
            self.emit_operation(op)

    def optimize_INT_FORCE_GE_ZERO(self, op):
        b = self.getintbound(op.getarg(0))
        if b.known_ge(IntBound(0, 0)):
            self.make_equal_to(op, op.getarg(0))
        else:
            self.emit_operation(op)

    def optimize_INT_SIGNEXT(self, op):
        b = self.getintbound(op.getarg(0))
        numbits = op.getarg(1).getint() * 8
        start = -(1 << (numbits - 1))
        stop = 1 << (numbits - 1)
        bounds = IntBound(start, stop - 1)
        if bounds.contains_bound(b):
            self.make_equal_to(op, op.getarg(0))
        else:
            self.emit_operation(op)
            bres = self.getintbound(op)
            bres.intersect(bounds)

    def optimize_ARRAYLEN_GC(self, op):
        array = self.getptrinfo(op.getarg(0))
        result = self.getintbound(op)
        result.make_ge(IntLowerBound(0))
        self.emit_operation(op)
        #array.make_len_gt(MODE_ARRAY, op.getdescr(), -1)
        #array.getlenbound().bound.intersect(result.getintbound())

    def optimize_STRLEN(self, op):
        self.emit_operation(op)
        self.make_nonnull_str(op.getarg(0), vstring.mode_string)
        array = self.getptrinfo(op.getarg(0))
        new_op = self.get_box_replacement(op)
        if not new_op.is_constant():
            new_op.set_forwarded(array.getlenbound(vstring.mode_string))

    def optimize_UNICODELEN(self, op):
        self.emit_operation(op)
        self.make_nonnull_str(op.getarg(0), vstring.mode_unicode)
        array = self.getptrinfo(op.getarg(0))
        new_op = self.get_box_replacement(op)
        if not new_op.is_constant():
            new_op.set_forwarded(array.getlenbound(vstring.mode_unicode))

    def optimize_STRGETITEM(self, op):
        self.emit_operation(op)
        v1 = self.getintbound(op)
        v1.make_ge(IntLowerBound(0))
        v1.make_lt(IntUpperBound(256))

    def optimize_GETFIELD_RAW_I(self, op):
        self.emit_operation(op)
        descr = op.getdescr()
        if descr.is_integer_bounded():
            b1 = self.getintbound(op)
            b1.make_ge(IntLowerBound(descr.get_integer_min()))
            b1.make_le(IntUpperBound(descr.get_integer_max()))

    optimize_GETFIELD_RAW_F = optimize_GETFIELD_RAW_I
    optimize_GETFIELD_RAW_R = optimize_GETFIELD_RAW_I
    optimize_GETFIELD_GC_I = optimize_GETFIELD_RAW_I
    optimize_GETFIELD_GC_R = optimize_GETFIELD_RAW_I
    optimize_GETFIELD_GC_F = optimize_GETFIELD_RAW_I

    optimize_GETINTERIORFIELD_GC_I = optimize_GETFIELD_RAW_I
    optimize_GETINTERIORFIELD_GC_R = optimize_GETFIELD_RAW_I
    optimize_GETINTERIORFIELD_GC_F = optimize_GETFIELD_RAW_I

    def optimize_GETARRAYITEM_RAW_I(self, op):
        self.emit_operation(op)
        descr = op.getdescr()
        if descr and descr.is_item_integer_bounded():
            intbound = self.getintbound(op)
            intbound.make_ge(IntLowerBound(descr.get_item_integer_min()))
            intbound.make_le(IntUpperBound(descr.get_item_integer_max()))

    optimize_GETARRAYITEM_RAW_F = optimize_GETARRAYITEM_RAW_I
    optimize_GETARRAYITEM_GC_I = optimize_GETARRAYITEM_RAW_I
    optimize_GETARRAYITEM_GC_F = optimize_GETARRAYITEM_RAW_I
    optimize_GETARRAYITEM_GC_R = optimize_GETARRAYITEM_RAW_I

    def optimize_UNICODEGETITEM(self, op):
        self.emit_operation(op)
        b1 = self.getintbound(op)
        b1.make_ge(IntLowerBound(0))

    def make_int_lt(self, box1, box2):
        b1 = self.getintbound(box1)
        b2 = self.getintbound(box2)
        if b1.make_lt(b2):
            self.propagate_bounds_backward(box1)
        if b2.make_gt(b1):
            self.propagate_bounds_backward(box2)

    def make_int_le(self, box1, box2):
        b1 = self.getintbound(box1)
        b2 = self.getintbound(box2)
        if b1.make_le(b2):
            self.propagate_bounds_backward(box1)
        if b2.make_ge(b1):
            self.propagate_bounds_backward(box2)

    def make_int_gt(self, box1, box2):
        self.make_int_lt(box2, box1)

    def make_int_ge(self, box1, box2):
        self.make_int_le(box2, box1)

    def propagate_bounds_INT_LT(self, op):
        r = self.getintbound(op)
        if r.is_constant():
            if r.getint() == 1:
                self.make_int_lt(op.getarg(0), op.getarg(1))
            else:
                assert r.getint() == 0
                self.make_int_ge(op.getarg(0), op.getarg(1))

    def propagate_bounds_INT_GT(self, op):
        r = self.getintbound(op)
        if r.is_constant():
            if r.getint() == 1:
                self.make_int_gt(op.getarg(0), op.getarg(1))
            else:
                assert r.getint() == 0
                self.make_int_le(op.getarg(0), op.getarg(1))

    def propagate_bounds_INT_LE(self, op):
        r = self.getintbound(op)
        if r.is_constant():
            if r.getint() == 1:
                self.make_int_le(op.getarg(0), op.getarg(1))
            else:
                assert r.getint() == 0
                self.make_int_gt(op.getarg(0), op.getarg(1))

    def propagate_bounds_INT_GE(self, op):
        r = self.getintbound(op)
        if r.is_constant():
            if r.getint() == 1:
                self.make_int_ge(op.getarg(0), op.getarg(1))
            else:
                assert r.getint() == 0
                self.make_int_lt(op.getarg(0), op.getarg(1))

    def propagate_bounds_INT_EQ(self, op):
        r = self.getintbound(op)
        if r.is_constant():
            if r.equal(1):
                b1 = self.getintbound(op.getarg(0))
                b2 = self.getintbound(op.getarg(1))
                if b1.intersect(b2):
                    self.propagate_bounds_backward(op.getarg(0))
                if b2.intersect(b1):
                    self.propagate_bounds_backward(op.getarg(1))

    def propagate_bounds_INT_NE(self, op):
        r = self.getintbound(op)
        if r.is_constant():
            if r.equal(0):
                b1 = self.getintbound(op.getarg(0))
                b2 = self.getintbound(op.getarg(1))
                if b1.intersect(b2):
                    self.propagate_bounds_backward(op.getarg(0))
                if b2.intersect(b1):
                    self.propagate_bounds_backward(op.getarg(1))

    def _propagate_int_is_true_or_zero(self, op, valnonzero, valzero):
        r = self.getintbound(op)
        if r.is_constant():
            if r.getint() == valnonzero:
                b1 = self.getintbound(op.getarg(0))
                if b1.known_ge(IntBound(0, 0)):
                    b1.make_gt(IntBound(0, 0))
                    self.propagate_bounds_backward(op.getarg(0))
            elif r.getint() == valzero:
                b1 = self.getintbound(op.getarg(0))
                # XXX remove this hack maybe?
                # Clever hack, we can't use self.make_constant_int yet because
                # the args aren't in the values dictionary yet so it runs into
                # an assert, this is a clever way of expressing the same thing.
                b1.make_ge(IntBound(0, 0))
                b1.make_lt(IntBound(1, 1))
                self.propagate_bounds_backward(op.getarg(0))

    def propagate_bounds_INT_IS_TRUE(self, op):
        self._propagate_int_is_true_or_zero(op, 1, 0)

    def propagate_bounds_INT_IS_ZERO(self, op):
        self._propagate_int_is_true_or_zero(op, 0, 1)

    def propagate_bounds_INT_ADD(self, op):
        if self.is_raw_ptr(op.getarg(0)) or self.is_raw_ptr(op.getarg(1)):
            return
        b1 = self.getintbound(op.getarg(0))
        b2 = self.getintbound(op.getarg(1))
        r = self.getintbound(op)
        b = r.sub_bound(b2)
        if b1.intersect(b):
            self.propagate_bounds_backward(op.getarg(0))
        b = r.sub_bound(b1)
        if b2.intersect(b):
            self.propagate_bounds_backward(op.getarg(1))

    def propagate_bounds_INT_SUB(self, op):
        b1 = self.getintbound(op.getarg(0))
        b2 = self.getintbound(op.getarg(1))
        r = self.getintbound(op)
        b = r.add_bound(b2)
        if b1.intersect(b):
            self.propagate_bounds_backward(op.getarg(0))
        b = r.sub_bound(b1).mul(-1)
        if b2.intersect(b):
            self.propagate_bounds_backward(op.getarg(1))

    def propagate_bounds_INT_MUL(self, op):
        b1 = self.getintbound(op.getarg(0))
        b2 = self.getintbound(op.getarg(1))
        r = self.getintbound(op)
        b = r.div_bound(b2)
        if b1.intersect(b):
            self.propagate_bounds_backward(op.getarg(0))
        b = r.div_bound(b1)
        if b2.intersect(b):
            self.propagate_bounds_backward(op.getarg(1))

    def propagate_bounds_INT_LSHIFT(self, op):
        b1 = self.getintbound(op.getarg(0))
        b2 = self.getintbound(op.getarg(1))
        r = self.getintbound(op)
        b = r.rshift_bound(b2)
        if b1.intersect(b):
            self.propagate_bounds_backward(op.getarg(0))

    propagate_bounds_INT_ADD_OVF = propagate_bounds_INT_ADD
    propagate_bounds_INT_SUB_OVF = propagate_bounds_INT_SUB
    propagate_bounds_INT_MUL_OVF = propagate_bounds_INT_MUL


dispatch_opt = make_dispatcher_method(OptIntBounds, 'optimize_',
        default=OptIntBounds.opt_default)
dispatch_bounds_ops = make_dispatcher_method(OptIntBounds, 'propagate_bounds_')
