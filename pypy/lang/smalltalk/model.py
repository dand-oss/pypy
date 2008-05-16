"""
Squeak model.

    W_Object
        W_SmallInteger        
        W_Float
        W_AbstractObjectWithIdentityHash
            W_AbstractObjectWithClassReference
                W_PointersObject 
                W_BytesObject
                W_WordsObject
            W_CompiledMethod

W_BlockContext and W_MethodContext classes have been replaced by functions
that create W_PointersObjects of correct size with attached shadows.
"""
import sys
from pypy.rlib import rrandom, objectmodel
from pypy.rlib.rarithmetic import intmask
from pypy.lang.smalltalk import constants
from pypy.tool.pairtype import extendabletype
from pypy.rlib.objectmodel import instantiate
from pypy.lang.smalltalk.tool.bitmanipulation import splitter

class W_Object(object):
    """Root of Squeak model, abstract."""
    __slots__ = ()    # no RPython-level instance variables allowed in W_Object

    def size(self):
        """Return bytesize that conforms to Blue Book.
        
        The reported size may differ from the actual size in Spy's object
        space, as memory representation varies depending on PyPy translation."""
        return 0

    def varsize(self):
        """Return bytesize of variable-sized part.

        Variable sized objects are those created with #new:."""
        return self.size()

    def primsize(self):
        # TODO remove this method
        return self.size()

    def getclass(self):
        """Return Squeak class."""
        raise NotImplementedError()

    def gethash(self):
        """Return 31-bit hash value."""
        raise NotImplementedError()

    def at0(self, index0):
        """Access variable-sized part, as by Object>>at:.

        Return value depends on layout of instance. Byte objects return bytes,
        word objects return words, pointer objects return pointers. Compiled method are
        treated special, if index0 within the literalsize returns pointer to literal,
        otherwise returns byte (ie byte code indexing starts at literalsize)."""
        raise NotImplementedError()

    def atput0(self, index0, w_value):
        """Access variable-sized part, as by Object>>at:put:.

        Semantics depend on layout of instance. Byte objects set bytes,
        word objects set words, pointer objects set pointers. Compiled method are
        treated special, if index0 within the literalsize sets pointer to literal,
        otherwise patches bytecode (ie byte code indexing starts at literalsize)."""
        raise NotImplementedError()

    def fetch(self, n0):
        """Access fixed-size part, maybe also variable-sized part (we have to
        consult the Blue Book)."""
        # TODO check the Blue Book
        raise NotImplementedError()
        
    def store(self, n0, w_value):    
        """Access fixed-size part, maybe also variable-sized part (we have to
        consult the Blue Book)."""
        raise NotImplementedError()

    def invariant(self):
        return True

    def shadow_of_my_class(self):
        """Return internal representation of Squeak class."""
        return self.getclass().as_class_get_shadow()

    def equals(self, other):
        """Compare object identity"""
        return self == other

    def become(self, w_old, w_new):
        """Exchange object identity."""
        # TODO use PyPy's become 
        pass

class W_SmallInteger(W_Object):
    """Boxed integer value"""
    # TODO can we tell pypy that its never larger then 31-bit?
    __slots__ = ('value',)     # the only allowed slot here

    def __init__(self, value):
        self.value = value

    def getclass(self):
        """Return SmallInteger from special objects array."""
        from pypy.lang.smalltalk.classtable import w_SmallInteger
        return w_SmallInteger

    def gethash(self):
        return self.value

    def invariant(self):
        return isinstance(self.value, int) and self.value < 0x8000

    def __repr__(self):
        return "W_SmallInteger(%d)" % self.value

    def equals(self, other):
        # TODO what is correct terminology to say that identity is by value?
        if not isinstance(other, W_SmallInteger):
            return False
        return self.value == other.value

class W_Float(W_Object):
    """Boxed float value."""
    def __init__(self, value):
        self.value = value

    def getclass(self):
        """Return Float from special objects array."""
        from pypy.lang.smalltalk.classtable import w_Float
        return w_Float

    def gethash(self):
        return 41    # XXX check this

    def invariant(self):
        return self.value is not None        # XXX but later:
        #return isinstance(self.value, float)
    def __repr__(self):
        return "W_Float(%f)" % self.value

    def equals(self, other):
        if not isinstance(other, W_Float):
            return False
        # TODO is that correct in Squeak?
        return self.value == other.value

class W_AbstractObjectWithIdentityHash(W_Object):
    """Object with explicit hash (ie all except small
    ints and floats)."""
    #XXX maybe this is too extreme, but it's very random
    hash_generator = rrandom.Random()
    UNASSIGNED_HASH = sys.maxint

    hash = UNASSIGNED_HASH # default value

    def gethash(self):
        if self.hash == self.UNASSIGNED_HASH:
            self.hash = hash = intmask(self.hash_generator.genrand32()) // 2
            return hash
        return self.hash

    def invariant(self):
        return isinstance(self.hash, int)

class W_AbstractObjectWithClassReference(W_AbstractObjectWithIdentityHash):
    """Objects with arbitrary class (ie not CompiledMethod, SmallInteger or
    Float)."""

    def __init__(self, w_class):
        if w_class is not None:     # it's None only for testing
            assert isinstance(w_class, W_PointersObject)
        self.w_class = w_class

    def getclass(self):
        assert self.w_class is not None
        return self.w_class

    def __repr__(self):
        return "<%s %s>" % (self.__class__.__name__, self)

    def __str__(self):
        if isinstance(self, W_PointersObject) and self._shadow is not None:
            return self._shadow.getname()
        else:
            return "a %s" % (self.shadow_of_my_class().name or '?',)

    def invariant(self):
        return (W_AbstractObjectWithIdentityHash.invariant(self) and
                isinstance(self.w_class, W_PointersObject))

    def become(self, w_old, w_new):
        # TODO to be eventually replaced by PyPy's become
        if self.w_class == w_old:
            self.w_class = w_new
        elif self.w_class == w_new:
            self.w_class = w_old

class W_PointersObject(W_AbstractObjectWithClassReference):
    """Common object."""
    
    _shadow = None # Default value

    def __init__(self, w_class, size):
        """Create new object with size = fixed + variable size."""
        W_AbstractObjectWithClassReference.__init__(self, w_class)
        self._vars = [w_nil] * size

    def at0(self, index0):
        # To test, at0 = in varsize part
        return self.fetch(index0+self.instsize())

    def atput0(self, index0, w_value):
        # To test, at0 = in varsize part
        self.store(index0+self.instsize(), w_value)

    def fetch(self, n0):
        if self._shadow is not None:
            self._shadow.sync_w_self()
        return self._vars[n0]
        
    def store(self, n0, w_value):    
        if self._shadow is not None:
            self._shadow.sync_w_self()
            self._shadow.invalidate_shadow()
        self._vars[n0] = w_value

    # def fetchvarpointer(self, idx):
    #    return self._vars[idx+self.instsize()]

    # def storevarpointer(self, idx, value):
    #    self._vars[idx+self.instsize()] = value

    def varsize(self):
        return self.size() - self.instsize()

    def instsize(self):
        return self.shadow_of_my_class().instsize()

    def primsize(self):
        return self.varsize()

    def size(self):
        return len(self._vars)

    def invariant(self):
        return (W_AbstractObjectWithClassReference.invariant(self) and
                isinstance(self._vars, list))

    # XXX XXX
    # Find better way of overloading shadows...
    def setshadow(self, shadow):
        self._shadow = shadow

    @objectmodel.specialize.arg(1)
    def as_special_get_shadow(self, TheClass, invalid=True):
        shadow = self._shadow
        if shadow is None:
            shadow = TheClass(self, invalid)
            self._shadow = shadow
        elif not isinstance(shadow, TheClass):
            shadow.sync_w_self()
            shadow.invalidate_shadow()
            shadow = TheClass(self, invalid)
            self._shadow = shadow
        shadow.sync_shadow()
        return shadow

    def get_shadow(self):
        from pypy.lang.smalltalk.shadow import AbstractShadow
        return self.as_special_get_shadow(AbstractShadow)

    def as_class_get_shadow(self):
        from pypy.lang.smalltalk.shadow import ClassShadow
        return self.as_special_get_shadow(ClassShadow)

    def as_link_get_shadow(self):
        from pypy.lang.smalltalk.shadow import LinkShadow
        return self.as_special_get_shadow(LinkShadow)
    
    def as_semaphore_get_shadow(self):
        from pypy.lang.smalltalk.shadow import SemaphoreShadow
        return self.as_special_get_shadow(SemaphoreShadow)

    def as_linkedlist_get_shadow(self):
        from pypy.lang.smalltalk.shadow import LinkedListShadow
        return self.as_special_get_shadow(LinkedListShadow)

    def as_process_get_shadow(self):
        from pypy.lang.smalltalk.shadow import ProcessShadow
        return self.as_special_get_shadow(ProcessShadow)

    def as_scheduler_get_shadow(self):
        from pypy.lang.smalltalk.shadow import SchedulerShadow
        return self.as_special_get_shadow(SchedulerShadow)

    def as_association_get_shadow(self):
        from pypy.lang.smalltalk.shadow import AssociationShadow
        return self.as_special_get_shadow(AssociationShadow)

    def as_blockcontext_get_shadow(self, invalid=True):
        from pypy.lang.smalltalk.shadow import BlockContextShadow
        return self.as_special_get_shadow(BlockContextShadow, invalid)

    def as_methodcontext_get_shadow(self, invalid=True):
        from pypy.lang.smalltalk.shadow import MethodContextShadow
        return self.as_special_get_shadow(MethodContextShadow, invalid)

    def as_context_get_shadow(self, invalid=True):
        from pypy.lang.smalltalk.shadow import ContextPartShadow
        return self.as_special_get_shadow(ContextPartShadow, invalid)

    def as_methoddict_get_shadow(self):
        from pypy.lang.smalltalk.shadow import MethodDictionaryShadow
        return self.as_special_get_shadow(MethodDictionaryShadow)

    def become(self, w_old, w_new):
        W_AbstractObjectWithClassReference.become(self, w_old, w_new)
        for i in range(len(self._vars)):
            w_test = self.fetch(i)
            if w_test == w_old:
                self.store(i, w_new)
            elif w_test == w_new:
                self.store(i, w_old)

class W_BytesObject(W_AbstractObjectWithClassReference):
    def __init__(self, w_class, size):
        W_AbstractObjectWithClassReference.__init__(self, w_class)
        self.bytes = ['\x00'] * size

    def at0(self, index0):
        from pypy.lang.smalltalk import utility
        return utility.wrap_int(ord(self.getchar(index0)))
       
    def atput0(self, index0, w_value):
        from pypy.lang.smalltalk import utility
        self.setchar(index0, chr(utility.unwrap_int(w_value)))

    def getchar(self, n0):
        return self.bytes[n0]
    
    def setchar(self, n0, character):
        assert len(character) == 1
        self.bytes[n0] = character

    def size(self):
        return len(self.bytes)    

    def __str__(self):
        return self.as_string()

    def __repr__(self):
        return "<W_BytesObject %r>" % (self.as_string(),)

    def as_string(self):
        return "".join(self.bytes)

    def invariant(self):
        if not W_AbstractObjectWithClassReference.invariant(self):
            return False
        for c in self.bytes:
            if not isinstance(c, str) or len(c) != 1:
                return False
        return True

    def equals(self, other):
        if not isinstance(other, W_BytesObject):
            return False
        return self.bytes == other.bytes

class W_WordsObject(W_AbstractObjectWithClassReference):
    def __init__(self, w_class, size):
        W_AbstractObjectWithClassReference.__init__(self, w_class)
        self.words = [0] * size
        
    def at0(self, index0):
        from pypy.lang.smalltalk import utility
        return utility.wrap_int(self.getword(index0))
       
    def atput0(self, index0, w_value):
        from pypy.lang.smalltalk import utility
        self.setword(index0, utility.unwrap_int(w_value))

    def getword(self, n):
        return self.words[n]
        
    def setword(self, n, word):
        self.words[n] = word        

    def size(self):
        return len(self.words)   

    def invariant(self):
        return (W_AbstractObjectWithClassReference.invariant(self) and
                isinstance(self.words, list))

# XXX Shouldn't compiledmethod have class reference for subclassed compiled
# methods?
class W_CompiledMethod(W_AbstractObjectWithIdentityHash):
    """My instances are methods suitable for interpretation by the virtual machine.  This is the only class in the system whose instances intermix both indexable pointer fields and indexable integer fields.

    The current format of a CompiledMethod is as follows:

        header (4 bytes)
        literals (4 bytes each)
        bytecodes  (variable)
    """

### Extension from Squeak 3.9 doc, which we do not implement:
###        trailer (variable)
###    The trailer has two variant formats.  In the first variant, the last
###    byte is at least 252 and the last four bytes represent a source pointer
###    into one of the sources files (see #sourcePointer).  In the second
###    variant, the last byte is less than 252, and the last several bytes
###    are a compressed version of the names of the method's temporary
###    variables.  The number of bytes used for this purpose is the value of
###    the last byte in the method.

    def __init__(self, bytecount=0, header=0):
        self.setheader(header)
        self.bytes = "\x00"*bytecount

    def compiledin(self):  
        if self.w_compiledin is None:
            # (Blue book, p 607) All CompiledMethods that contain
            # extended-super bytecodes have the clain which they are found as
            # their last literal variable.   
            # Last of the literals is an association with compiledin
            # as a class
            w_association = self.literals[-1]
            assert isinstance(w_association, W_PointersObject)
            s_association = w_association.as_association_get_shadow()
            self.w_compiledin = s_association.value()
        return self.w_compiledin

    def getclass(self):
        from pypy.lang.smalltalk.classtable import w_CompiledMethod
        return w_CompiledMethod

    def getliteral(self, index):
                                    # We changed this part
        return self.literals[index] #+ constants.LITERAL_START]

    def getliteralsymbol(self, index):
        w_literal = self.getliteral(index)
        assert isinstance(w_literal, W_BytesObject)
        return w_literal.as_string()    # XXX performance issue here

    def create_frame(self, receiver, arguments, sender = None):
        from pypy.lang.smalltalk import objtable
        assert len(arguments) == self.argsize
        w_new = W_MethodContext(self, receiver, arguments, sender)
        return w_new

    def __str__(self):
        from pypy.lang.smalltalk.interpreter import BYTECODE_TABLE
        j = 1
        retval = "\n\nBytecode:\n---------------------\n"
        for i in self.bytes:
            retval += str(j) + ": " + BYTECODE_TABLE[ord(i)].__name__ + " " + str(ord(i)) + "\n"
            j += 1
        return retval + "\n---------------------\n"

    def invariant(self):
        return (W_Object.invariant(self) and
                hasattr(self, 'literals') and
                self.literals is not None and 
                hasattr(self, 'bytes') and
                self.bytes is not None and 
                hasattr(self, 'argsize') and
                self.argsize is not None and 
                hasattr(self, 'tempsize') and
                self.tempsize is not None and 
                hasattr(self, 'primitive') and
                self.primitive is not None)       

    def size(self):
        return self.headersize() + self.getliteralsize() + len(self.bytes) 

    def getliteralsize(self):
        return self.literalsize * constants.BYTES_PER_WORD

    def bytecodeoffset(self):
        return self.getliteralsize() + self.headersize()

    def headersize(self):
        return constants.BYTES_PER_WORD

    def getheader(self):
        return self.header

    def setheader(self, header):
        """Decode 30-bit method header and apply new format.

        (index 0)  9 bits: main part of primitive number   (#primitive)
        (index 9)  8 bits: number of literals (#numLiterals)
        (index 17) 1 bit:  whether a large frame size is needed (#frameSize)
        (index 18) 6 bits: number of temporary variables (#numTemps)
        (index 24) 4 bits: number of arguments to the method (#numArgs)
        (index 28) 1 bit:  high-bit of primitive number (#primitive)
        (index 29) 1 bit:  flag bit, ignored by the VM  (#flag)
        """
        primitive, literalsize, islarge, tempsize, numargs, highbit = (
            splitter[9,8,1,6,4,1](header))
        primitive = primitive + (highbit << 10) ##XXX todo, check this
        self.literalsize = literalsize
        self.literals = [w_nil] * self.literalsize
        self.header = header
        self.argsize = numargs
        self.tempsize = tempsize
        self.primitive = primitive
        self.w_compiledin = None
        self.islarge = islarge

    def tempframesize(self):
        return self.argsize + self.tempsize

    def literalat0(self, index0):
        if index0 == 0:
            from pypy.lang.smalltalk import utility
            return utility.wrap_int(self.getheader())
        else:
            return self.literals[index0-1]

    def literalatput0(self, index0, w_value):
        if index0 == 0:
            from pypy.lang.smalltalk import utility
            header = utility.unwrap_int(w_value)
            self.setheader(header)
        else:
            self.literals[index0-1] = w_value

    def store(self, index0, w_v):
        self.atput0(index0, w_v)

    def at0(self, index0):
        from pypy.lang.smalltalk import utility
        if index0 <= self.getliteralsize():
            return self.literalat0(index0/constants.BYTES_PER_WORD)
        else:
            # From blue book:
            # The literal count indicates the size of the
            # CompiledMethod's literal frame.
            # This, in turn, indicates where the 
            # CompiledMethod's bytecodes start. 
            index0 = index0 - self.getliteralsize() - self.headersize()
            assert index0 < len(self.bytes)
            return utility.wrap_int(ord(self.bytes[index0]))
        
    def atput0(self, index0, w_value):
        from pypy.lang.smalltalk import utility
        if index0 <= self.getliteralsize():
            self.literalatput0(index0/constants.BYTES_PER_WORD, w_value)
        else:
            # XXX use to-be-written unwrap_char
            index0 = index0 - self.getliteralsize() - self.headersize()
            assert index0 < len(self.bytes)
            self.setchar(index0, chr(utility.unwrap_int(w_value)))

    def setchar(self, index0, character):
        assert index0 >= 0
        self.bytes = (self.bytes[:index0] + character +
                      self.bytes[index0 + 1:])

def W_BlockContext(w_home, w_sender, argcnt, initialip):
    from pypy.lang.smalltalk.classtable import w_BlockContext
    w_result = W_PointersObject(w_BlockContext, w_home.size())
    # Only home-brewed shadows are not invalid from start.
    s_result = w_result.as_blockcontext_get_shadow(invalid=False)
    s_result.store_expected_argument_count(argcnt)
    s_result.store_initialip(initialip)
    s_result.store_w_home(w_home)
    s_result._stack = []
    s_result._pc = initialip
    s_result.invalidate_w_self()
    return w_result

def W_MethodContext(w_method, w_receiver,
                    arguments, w_sender=None):
    from pypy.lang.smalltalk.classtable import w_MethodContext
    # From blue book: normal mc have place for 12 temps+maxstack
    # mc for methods with islarge flag turned on 32
    size = 12 + w_method.islarge * 20 + w_method.argsize
    w_result = w_MethodContext.as_class_get_shadow().new(size)
    assert isinstance(w_result, W_PointersObject)
    # Only home-brewed shadows are not invalid from start.
    s_result = w_result.as_methodcontext_get_shadow(invalid=False)
    s_result.store_w_method(w_method)
    if w_sender:
        s_result.store_w_sender(w_sender)
    s_result.store_w_receiver(w_receiver)
    s_result.store_pc(0)
    for i in range(len(arguments)):
        s_result.settemp(i, arguments[i])
    s_result._stack = []
    s_result.invalidate_w_self()
    return w_result

# Use black magic to create w_nil without running the constructor,
# thus allowing it to be used even in the constructor of its own
# class.  Note that we patch its class in objtable.
w_nil = instantiate(W_PointersObject)
w_nil._vars = []
