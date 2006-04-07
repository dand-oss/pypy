from pypy.objspace.flow.model import Block, Variable, Constant
from pypy.objspace.flow.model import traverse
from pypy.rpython.lltypesystem.lltype import Void
from pypy.translator import simplify
from pypy import conftest

def remove_unaryops(graph, opnames):
    """Removes unary low-level ops with a name appearing in the opnames list.
    """
    positions = []
    def visit(node): 
        if isinstance(node, Block): 
            for i, op in enumerate(node.operations):
                if op.opname in opnames:
                    positions.append((node, i))
    traverse(visit, graph)
    while positions:
        block, index = positions.pop()
        op_result = block.operations[index].result
        op_arg = block.operations[index].args[0]
        # replace the new variable (op_result) with the old variable
        # (from all subsequent positions)
        for op in block.operations[index:]:
            if op is not None:
                for i in range(len(op.args)):
                    if op.args[i] == op_result:
                        op.args[i] = op_arg
        for link in block.exits:
            for i in range(len(link.args)):
                if link.args[i] == op_result:
                    link.args[i] = op_arg
        if block.exitswitch == op_result:
            if isinstance(op_arg, Variable):
                block.exitswitch = op_arg
            else:
                assert isinstance(op_arg, Constant)
                newexits = [link for link in block.exits
                                 if link.exitcase == op_arg.value]
                assert len(newexits) == 1
                newexits[0].exitcase = None
                if hasattr(newexits[0], 'llexitcase'):
                    newexits[0].llexitcase = None
                block.exitswitch = None
                block.recloseblock(*newexits)
        block.operations[index] = None
       
    # remove all operations
    def visit(node): 
        if isinstance(node, Block) and node.operations:
            node.operations[:] = filter(None, node.operations)
    traverse(visit, graph)

def remove_same_as(graph):
    remove_unaryops(graph, ["same_as"])


def remove_void(translator):
    for graph in translator.graphs:
        args = [arg for arg in graph.startblock.inputargs
                    if arg.concretetype is not Void]
        graph.startblock.inputargs = args
        for block in graph.iterblocks():
            for op in block.operations:
                if op.opname in ('direct_call', 'indirect_call'):
                    args = [arg for arg in op.args
                                if arg.concretetype is not Void]
                    op.args = args

def remove_duplicate_casts(graph, translator):
    simplify.join_blocks(graph)
    num_removed = 0
    # remove chains of casts
    for block in graph.iterblocks():
        comes_from = {}
        for op in block.operations:
            if op.opname == "cast_pointer":
                if op.args[0] in comes_from:
                    from_var = comes_from[op.args[0]]
                    comes_from[op.result] = from_var
                    if from_var.concretetype == op.result.concretetype:
                        op.opname = "same_as"
                        op.args = [from_var]
                        num_removed += 1
                    else:
                        op.args = [from_var]
                else:
                    comes_from[op.result] = op.args[0]
    if num_removed:
        remove_same_as(graph)
    # remove duplicate casts
    for block in graph.iterblocks():
        available = {}
        for op in block.operations:
            if op.opname == "cast_pointer":
                key = (op.args[0], op.result.concretetype)
                if key in available:
                    op.opname = "same_as"
                    op.args = [available[key]]
                    num_removed += 1
                else:
                    available[key] = op.result
    if num_removed:
        remove_same_as(graph)
        # remove casts with unused results
        for block in graph.iterblocks():
            used = {}
            for link in block.exits:
                for arg in link.args:
                    used[arg] = True
            for i, op in list(enumerate(block.operations))[::-1]:
                if op.opname == "cast_pointer" and op.result not in used:
                    del block.operations[i]
                    num_removed += 1
                else:
                    for arg in op.args:
                        used[arg] = True
        print "removed %s cast_pointers in %s" % (num_removed, graph.name)
    return num_removed

def remove_superfluous_keep_alive(graph):
    for block in graph.iterblocks():
        used = {}
        for i, op in list(enumerate(block.operations))[::-1]:
            if op.opname == "keepalive":
                if op.args[0] in used:
                    del block.operations[i]
                else:
                    used[op.args[0]] = True
 
##def rename_extfunc_calls(translator):
##    from pypy.rpython.extfunctable import table as extfunctable
##    def visit(block): 
##        if isinstance(block, Block):
##            for op in block.operations:
##                if op.opname != 'direct_call':
##                    continue
##                functionref = op.args[0]
##                if not isinstance(functionref, Constant):
##                    continue
##                _callable = functionref.value._obj._callable
##                for func, extfuncinfo in extfunctable.iteritems():  # precompute a dict?
##                    if _callable is not extfuncinfo.ll_function or not extfuncinfo.backend_functiontemplate:
##                        continue
##                    language, functionname = extfuncinfo.backend_functiontemplate.split(':')
##                    if language is 'C':
##                        old_name = functionref.value._obj._name[:]
##                        functionref.value._obj._name = functionname
##                        #print 'rename_extfunc_calls: %s -> %s' % (old_name, functionref.value._obj._name)
##                        break
##    for func, graph in translator.flowgraphs.iteritems():
##        traverse(visit, graph)
