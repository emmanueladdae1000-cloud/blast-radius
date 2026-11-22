"""Locate the first possible runtime effect of supported comparison edits.

Only source positions and existing baseline coverage are used. Unreached nested
comparisons do not inherit the coverage of an enclosing short-circuit header.
"""
import ast
import dis
from functools import lru_cache
import inspect
import types

from cmpprune import compare_operand_changes


def _position(node):
    return (node.lineno, node.end_lineno, node.col_offset, node.end_col_offset)


def _runtime_comparisons(tree):
    allowed = set()

    def visit(node, runtime=False, forbidden=False):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for name, value in ast.iter_fields(node):
                values = value if isinstance(value, list) else [value]
                for child in values:
                    if isinstance(child, ast.AST):
                        visit(child, name == 'body', forbidden or name != 'body')
            return
        if isinstance(node, ast.ClassDef):
            for name, value in ast.iter_fields(node):
                values = value if isinstance(value, list) else [value]
                for child in values:
                    if isinstance(child, ast.AST):
                        visit(child, False, forbidden or name != 'body')
            return
        if isinstance(node, ast.Lambda):
            visit(node.args, False, True)
            visit(node.body, True, forbidden)
            return
        if isinstance(node, ast.Compare) and runtime and not forbidden:
            allowed.add(_position(node))
        for child in ast.iter_child_nodes(node):
            visit(child, runtime, forbidden)

    visit(tree)
    return allowed


def _comparison_instruction(instruction):
    return (instruction.opname in ('COMPARE_OP', 'IS_OP') or
            instruction.opname in ('POP_JUMP_IF_NONE', 'POP_JUMP_IF_NOT_NONE'))


@lru_cache(maxsize=64)
def _baseline_anchors(source, rel):
    tree = ast.parse(source.encode('utf8'), filename=rel)
    allowed = _runtime_comparisons(tree)
    nodes = {(n.lineno, n.col_offset): _position(n)
             for n in ast.walk(tree) if isinstance(n, ast.Compare)}
    anchors = {}

    def scan(code):
        if code.co_flags & inspect.CO_NEWLOCALS:
            lines = tuple(code.co_lines())
            for instruction in dis.get_instructions(code):
                if not _comparison_instruction(instruction):
                    continue
                p = instruction.positions
                position = (p.lineno, p.end_lineno, p.col_offset, p.end_col_offset)
                if position not in allowed:
                    continue
                event_lines = {line for start, end, line in lines
                               if start <= instruction.offset < end}
                # Require the VM's actual line association, not an AST guess.
                if len(event_lines) != 1:
                    continue
                line = next(iter(event_lines))
                if type(line) is not int or not position[0] <= line <= position[1]:
                    continue
                anchors.setdefault(position, set()).add(line)
        for value in code.co_consts:
            if isinstance(value, types.CodeType):
                scan(value)

    scan(compile(source.encode('utf8'), rel, 'exec', dont_inherit=True))
    return nodes, {position: frozenset(lines) for position, lines in anchors.items()}


def affected_tests(rel, old_source, new_source, maps):
    """Return candidate map indices, or None when proof is unsupported.

    Every edit must be one recognized comparison change. Each old comparison
    needs a compiled comparison at its exact source span in a function body.
    Import execution, missing coverage metadata and unknown positions fall back.
    An absent line in a valid coverage map means that no baseline test reached it.
    The caller must require a whole patch that changes only this one file, so
    another edit cannot make an old, unexecuted comparison newly reachable.
    """
    if not rel.startswith('sqlglot/') or not rel.endswith('.py'):
        return None
    if rel.startswith('sqlglot/typing/'):
        # TestTyping compares annotator co_code/co_consts without calling the
        # functions. A comparison opcode is not their first observable effect.
        return None
    changes = compare_operand_changes(old_source, new_source)
    if not changes:
        return None
    try:
        data = maps.src(rel)
        if not isinstance(data, dict) or not all(k in data for k in ('cov', 'sets', 'imp')):
            return None
        if not isinstance(data['cov'], dict) or not isinstance(data['sets'], list):
            return None
        nodes, anchors = _baseline_anchors(old_source, rel)
        imported = set(data['imp'])
        out = set()
        for line, col, *_ in changes:
            position = nodes.get((line, col))
            required = anchors.get(position)
            if not required or required & imported:
                return None
            for anchor in required:
                sid = data['cov'].get(anchor)
                if sid is None:
                    continue
                if type(sid) is not int or not 0 <= sid < len(data['sets']):
                    return None
                tests = data['sets'][sid]
                if not isinstance(tests, (list, tuple)) or any(type(t) is not int for t in tests):
                    return None
                out.update(tests)
        return out
    except (OSError, SyntaxError, ValueError, TypeError, KeyError, AttributeError, IndexError):
        return None
