"""Static analysis of the pinned sqlglot sources (used at build time and runtime)."""
import ast, io, tokenize


def statements(src):
    """Return a list of statement records for a Python source.

    Each record: [start, end, hdr_end, kind, scope_start, scope_end, owner, targets]
      kind: 'f' statement inside a function body, 'm' module level, 'c' class body,
            'd' def header (decorators+signature), 'k' class header
      scope_*: line range of the enclosing function (kind 'f'/'d') or class ('c'/'k')
      owner: qualified name of the enclosing class (or '')
      targets: names assigned by an import-time assignment
    """
    tree = ast.parse(src)
    out = []

    def hdr_end(node):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Match)) or (hasattr(ast, "TryStar") and isinstance(node, ast.TryStar)):
            return body[0].lineno - 1 if body else node.end_lineno
        return node.end_lineno

    def targets_of(node):
        names = []
        tg = []
        if isinstance(node, ast.Assign):
            tg = node.targets
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            tg = [node.target]
        for t in tg:
            for n in ast.walk(t):
                if isinstance(n, ast.Name):
                    names.append(n.id)
        return names

    def walk(nodes, ctx, scope, owner):
        for node in nodes:
            if not isinstance(node, ast.stmt):
                continue
            start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                he = node.body[0].lineno - 1
                # docstring-only first line: header ends before body
                out.append([start, node.end_lineno, max(he, node.lineno), "d", node.lineno, node.end_lineno, owner, [node.name]])
                walk(node.body, "f", (node.lineno, node.end_lineno), owner)
                continue
            if isinstance(node, ast.ClassDef):
                he = node.body[0].lineno - 1
                q = (owner + "." if owner else "") + node.name
                out.append([start, node.end_lineno, max(he, node.lineno), "k", node.lineno, node.end_lineno, q, [node.name]])
                walk(node.body, "c" if ctx != "f" else "f", (node.lineno, node.end_lineno) if ctx != "f" else scope, q if ctx != "f" else owner)
                continue
            he = hdr_end(node)
            tg = targets_of(node) if ctx in "mc" else []
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str) and nodes and node is nodes[0]:
                tg = ["<doc>"]
            out.append([start, node.end_lineno, he, ctx, scope[0], scope[1], owner, tg])
            for field in ("body", "orelse", "finalbody"):
                sub = getattr(node, field, None)
                if isinstance(sub, list):
                    walk(sub, ctx, scope, owner)
            for h in getattr(node, "handlers", []) or []:
                out.append([h.lineno, h.end_lineno, h.body[0].lineno - 1, ctx, scope[0], scope[1], owner, []])
                walk(h.body, ctx, scope, owner)
            for c in getattr(node, "cases", []) or []:
                walk(c.body, ctx, scope, owner)

    walk(tree.body, "m", (1, len(src.splitlines()) + 1), "")
    return out


def string_ranges(src):
    """Line ranges [s, e] covered by multi-line string tokens (incl. f-strings)."""
    out = []
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except Exception:
        return out
    fstart = []
    for t in toks:
        if t.type == tokenize.STRING and t.end[0] > t.start[0]:
            out.append([t.start[0], t.end[0]])
        elif t.type == getattr(tokenize, "FSTRING_START", -1):
            fstart.append(t.start[0])
        elif t.type == getattr(tokenize, "FSTRING_END", -1) and fstart:
            s = fstart.pop()
            if t.end[0] > s:
                out.append([s, t.end[0]])
    return out


def lambda_spans(src):
    """Spans (line, col, end_line, end_col) of every lambda body."""
    out = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Lambda):
            b = node.body
            out.append([b.lineno, b.col_offset, b.end_lineno, b.end_col_offset])
    return out
