
from pypy.module.micronumpy.arrayimpl import base
from pypy.module.micronumpy import support, loop
from pypy.module.micronumpy.base import convert_to_array
from pypy.module.micronumpy.strides import calc_new_strides, shape_agreement,\
     calculate_broadcast_strides
from pypy.module.micronumpy.iter import Chunk, Chunks, NewAxisChunk, RecordChunk
from pypy.interpreter.error import OperationError, operationerrfmt
from pypy.rlib import jit

class ConcreteArrayIterator(base.BaseArrayIterator):
    def __init__(self, array):
        self.array = array
        self.offset = 0
        self.dtype = array.dtype
        self.skip = self.dtype.itemtype.get_element_size()
        self.size = array.size

    def setitem(self, elem):
        self.array.setitem(self.offset, elem)

    def getitem(self):
        return self.array.getitem(self.offset)

    def next(self):
        self.offset += self.skip

    def done(self):
        return self.offset >= self.size

class OneDimViewIterator(ConcreteArrayIterator):
    def __init__(self, array):
        self.array = array
        self.offset = array.start
        self.skip = array.strides[0]
        self.dtype = array.dtype
        self.index = 0
        self.size = array.shape[0]

    def next(self):
        self.offset += self.skip
        self.index += 1

    def done(self):
        return self.index >= self.size

class MultiDimViewIterator(ConcreteArrayIterator):
    def __init__(self, array, start, strides, backstrides, shape):
        self.indexes = [0] * len(shape)
        self.array = array
        self.shape = shape
        self.offset = start
        self.shapelen = len(shape)
        self._done = False
        self.strides = strides
        self.backstrides = backstrides

    @jit.unroll_safe
    def next(self):
        offset = self.offset
        for i in range(self.shapelen - 1, -1, -1):
            if self.indexes[i] < self.shape[i] - 1:
                self.indexes[i] += 1
                offset += self.strides[i]
                break
            else:
                self.indexes[i] = 0
                offset -= self.backstrides[i]
        else:
            self._done = True
        self.offset = offset

    def done(self):
        return self._done


def int_w(space, w_obj):
    # a special version that respects both __index__ and __int__
    # XXX add __index__ support
    try:
        return space.int_w(space.index(w_obj))
    except OperationError:
        return space.int_w(space.int(w_obj))

class ConcreteArray(base.BaseArrayImplementation):
    start = 0
    parent = None
    
    def __init__(self, shape, dtype, order, strides, backstrides, storage=None):
        self.shape = shape
        self.size = support.product(shape) * dtype.get_size()
        if storage is None:
            storage = dtype.itemtype.malloc(self.size)
        self.storage = storage
        self.order = order
        self.dtype = dtype
        self.strides = strides
        self.backstrides = backstrides

    def get_shape(self):
        return self.shape

    def create_iter(self, shape):
        if shape == self.shape:
            return ConcreteArrayIterator(self)
        r = calculate_broadcast_strides(self.strides, self.backstrides,
                                        self.shape, shape)
        return MultiDimViewIterator(self, 0, r[0], r[1], shape)

    def getitem(self, index):
        return self.dtype.getitem(self, index)

    def setitem(self, index, value):
        self.dtype.setitem(self, index, value)

    def fill(self, box):
        self.dtype.fill(self.storage, box, 0, self.size)

    def copy(self):
        impl = ConcreteArray(self.shape, self.dtype, self.order, self.strides,
                             self.backstrides)
        return loop.setslice(self.shape, impl, self)

    def setslice(self, space, arr):
        impl = arr.implementation
        if impl.is_scalar():
            self.fill(impl.get_scalar_value())
            return
        shape = shape_agreement(space, self.shape, arr)
        if impl.storage == self.storage:
            impl = impl.copy()
        loop.setslice(shape, self, impl)

    def get_size(self):
        return self.size // self.dtype.itemtype.get_element_size()

    def reshape(self, space, new_shape):
        # Since we got to here, prod(new_shape) == self.size
        new_strides = None
        if self.size > 0:
            new_strides = calc_new_strides(new_shape, self.shape,
                                           self.strides, self.order)
        if new_strides:
            # We can create a view, strides somehow match up.
            ndims = len(new_shape)
            new_backstrides = [0] * ndims
            for nd in range(ndims):
                new_backstrides[nd] = (new_shape[nd] - 1) * new_strides[nd]
            return SliceArray(self.start, new_strides, new_backstrides,
                              new_shape, self)
        else:
            return None

    # -------------------- applevel get/setitem -----------------------

    @jit.unroll_safe
    def _lookup_by_index(self, space, view_w):
        item = self.start
        for i, w_index in enumerate(view_w):
            if space.isinstance_w(w_index, space.w_slice):
                raise IndexError
            idx = int_w(space, w_index)
            if idx < 0:
                idx = self.shape[i] + idx
            if idx < 0 or idx >= self.shape[i]:
                raise operationerrfmt(space.w_IndexError,
                      "index (%d) out of range (0<=index<%d", i, self.shape[i],
                )
            item += idx * self.strides[i]
        return item

    def _single_item_index(self, space, w_idx):
        """ Return an index of single item if possible, otherwise raises
        IndexError
        """
        if (space.isinstance_w(w_idx, space.w_str) or
            space.isinstance_w(w_idx, space.w_slice) or
            space.is_w(w_idx, space.w_None)):
            raise IndexError
        shape_len = len(self.shape)
        if shape_len == 0:
            raise OperationError(space.w_IndexError, space.wrap(
                "0-d arrays can't be indexed"))
        if space.isinstance_w(w_idx, space.w_tuple):
            view_w = space.fixedview(w_idx)
            if len(view_w) < shape_len:
                raise IndexError
            if len(view_w) > shape_len:
                # we can allow for one extra None
                count = len(view_w)
                for w_item in view_w:
                    if space.is_w(w_item, space.w_None):
                        count -= 1
                if count == shape_len:
                    raise IndexError # but it's still not a single item
                raise OperationError(space.w_IndexError,
                                     space.wrap("invalid index"))
            return self._lookup_by_index(space, view_w)
        idx = int_w(space, w_idx)
        return self._lookup_by_index(space, [space.wrap(idx)])

    @jit.unroll_safe
    def _prepare_slice_args(self, space, w_idx):
        if space.isinstance_w(w_idx, space.w_str):
            idx = space.str_w(w_idx)
            dtype = self.find_dtype()
            if not dtype.is_record_type() or idx not in dtype.fields:
                raise OperationError(space.w_ValueError, space.wrap(
                    "field named %s not defined" % idx))
            return RecordChunk(idx)
        if (space.isinstance_w(w_idx, space.w_int) or
            space.isinstance_w(w_idx, space.w_slice)):
            return Chunks([Chunk(*space.decode_index4(w_idx, self.shape[0]))])
        elif space.is_w(w_idx, space.w_None):
            return Chunks([NewAxisChunk()])
        result = []
        i = 0
        for w_item in space.fixedview(w_idx):
            if space.is_w(w_item, space.w_None):
                result.append(NewAxisChunk())
            else:
                result.append(Chunk(*space.decode_index4(w_item,
                                                         self.shape[i])))
                i += 1
        return Chunks(result)

    def descr_getitem(self, space, w_index):
        try:
            item = self._single_item_index(space, w_index)
            return self.getitem(item)
        except IndexError:
            # not a single result
            chunks = self._prepare_slice_args(space, w_index)
            return chunks.apply(self)

    def descr_setitem(self, space, w_index, w_value):
        try:
            item = self._single_item_index(space, w_index)
            self.setitem(item, self.dtype.coerce(space, w_value))
        except IndexError:
            w_value = convert_to_array(space, w_value)
            chunks = self._prepare_slice_args(space, w_index)
            view = chunks.apply(self)
            view.implementation.setslice(space, w_value)

    #def setshape(self, space, new_shape):
    #    self.shape = new_shape
    #    self.calc_strides(new_shape)

    def transpose(self):
        if len(self.shape) < 2:
            return self
        strides = []
        backstrides = []
        shape = []
        for i in range(len(self.shape) - 1, -1, -1):
            strides.append(self.strides[i])
            backstrides.append(self.backstrides[i])
            shape.append(self.shape[i])
        return SliceArray(self.start, strides,
                          backstrides, shape, self)

class SliceArray(ConcreteArray):
    def __init__(self, start, strides, backstrides, shape, parent):
        self.strides = strides
        self.backstrides = backstrides
        self.shape = shape
        if isinstance(parent, SliceArray):
            parent = parent.parent # one level only
        self.parent = parent
        self.storage = parent.storage
        self.order = parent.order
        self.dtype = parent.dtype
        self.size = support.product(shape) * self.dtype.itemtype.get_element_size()
        self.start = start

    def fill(self, box):
        loop.fill(self, box)

    def create_iter(self, shape):
        if shape != self.shape:
            r = calculate_broadcast_strides(self.strides, self.backstrides,
                                            self.shape, shape)
            return MultiDimViewIterator(self.parent,
                                        self.start, r[0], r[1], shape)
        if len(self.shape) == 1:
            return OneDimViewIterator(self)
        return MultiDimViewIterator(self.parent, self.start, self.strides,
                                    self.backstrides, self.shape)

    def set_shape(self, space, new_shape):
        if len(self.shape) < 2 or self.size == 0:
            # TODO: this code could be refactored into calc_strides
            # but then calc_strides would have to accept a stepping factor
            strides = []
            backstrides = []
            dtype = self.dtype
            s = self.strides[0] // dtype.get_size()
            if self.order == 'C':
                new_shape.reverse()
            for sh in new_shape:
                strides.append(s * dtype.get_size())
                backstrides.append(s * (sh - 1) * dtype.get_size())
                s *= max(1, sh)
            if self.order == 'C':
                strides.reverse()
                backstrides.reverse()
                new_shape.reverse()
            return SliceArray(self.start, strides, backstrides, new_shape,
                              self)
        new_strides = calc_new_strides(new_shape, self.shape, self.strides,
                                       self.order)
        if new_strides is None:
            raise OperationError(space.w_AttributeError, space.wrap(
                          "incompatible shape for a non-contiguous array"))
        new_backstrides = [0] * len(new_shape)
        for nd in range(len(new_shape)):
            new_backstrides[nd] = (new_shape[nd] - 1) * new_strides[nd]
        xxx
        self.strides = new_strides[:]
        self.backstrides = new_backstrides
        self.shape = new_shape[:]
