from pypy.rlib import rope

class RopeBaseString(object):
    def __init__(self):
        self._node = None
    
    def __len__(self):
        return self._node.length()
    
    def __rmul__(self, n):
        return self * n
    
    def __hash__(self):
        return rope.hash_rope(self._node)
    
    def __eq__(self):
        pass
    
    def __ne__(self, other):
        return not self == other
    
    def __mul__(self, n):
        return self.__class__(rope.multiply(self._node, n))
    
    def __add__(self, other):
        if isinstance(self, RopeUnicode) or isinstance(other, RopeUnicode):
            return RopeUnicode(self._node + other._node)
        else:
            return self.__class__(self._node + other._node)
    
    def __getitem__(self, index):
        if isinstance(index, int):
            return self.getchar(index)
        if isinstance(index, slice):
            start, stop, step = index.indices(self._node.length())
            return self.__class__(rope.getslice(self._node, start, stop, step))
        raise NotImplementedError("Index type not known.")
    
        
class RopeStringIterator(object):
    def __init__(self, node):
        self._iter = rope.ItemIterator(node)
    
    def next(self):
        return self._iter.nextchar()
    
    def __iter__():
        return self

class RopeString(RopeBaseString):
    def __init__(self, s):
        if isinstance(s, str):
            self._node = rope.LiteralStringNode(s)
	elif isinstance(s, rope.StringNode):
            self._node = s
    
    def __eq__(self, other):
        if isinstance(other, RopeBaseString):
            return (rope.eq(self._node, other._node))
        else:
            return rope.eq(self._node, rope.LiteralStringNode(other))    
    
    def __iter__(self):
        return RopeStringIterator(self._node)
    
    def decode(self, codepage, errors='strict'):
        s = self._node.flatten_string()
        result = s.decode(codepage, errors)
        return RopeUnicode(result)

    def getchar(self,index):
        return self._node.getchar(index)

class RopeUnicodeIterator(object):
    def __init__(self, node):
        self._iter = rope.ItemIterator(node)
    
    def next(self):
        return self._iter.nextunichar()
    
    def __iter__():
        return self

class RopeUnicode(RopeBaseString):
    def __init__(self, s):
        if isinstance(s, str):
            self._node = rope.LiteralUnicodeNode(unicode(s))
        if isinstance(s, unicode):
            self._node = rope.LiteralUnicodeNode(s)
        if isinstance(s, rope.StringNode):
            self._node = s
    
    def __eq__(self, other):
        if isinstance (other, RopeBaseString):
            return rope.eq(self._node, other._node)
        else:
            return rope.eq(self._node, rope.LiteralUnicodeNode(other))
   
    def __iter__(self):
        return RopeUnicodeIterator(self._node)
    
    def encode(self, encoding, errors = 'strict'):
        s = self._node.flatten_unicode()
        result = s.encode(encoding, errors)
        return RopeString(result)
    
    def getchar(self,index):
        return self._node.getunichar(index)
