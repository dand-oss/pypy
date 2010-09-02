from pypy.rlib import rstring

def compound(name):
    name = "".join(rstring.split(name, "const")) # poor man's replace
    i = _find_qualifier_index(name)
    if name[-1] == "]":                          # array type
        for i in range(len(name) - 1, -1, -1):
            c = name[i]
            if c == "[":
                return "[]", int(name[i+1:-1])
    return "".join(name[i:].split(" ")), None

def _find_qualifier_index(name):
    i = len(name)
    for i in range(len(name) - 1, -1, -1):
        c = name[i]
        if c.isalnum() or c == ">" or c == "]":
            break
    return i + 1

def clean_type(name):
    assert name.find("const") == -1
    i = _find_qualifier_index(name)
    name = name[:i].strip(' ')
    if name[-1] == "]":
        for i in range(len(name) - 1, -1, -1):
            c = name[i]
            if c == "[":
                return name[:i]
    return name
