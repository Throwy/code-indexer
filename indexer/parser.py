"""
Multi-language symbol extractor using tree-sitter.
Extracts functions, classes, methods, interfaces, and structs
with their signatures, docstrings, and bodies.
"""

from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
import re

try:
    from tree_sitter import Language, Parser, Node
    import tree_sitter_python as ts_python
    import tree_sitter_javascript as ts_javascript
    import tree_sitter_typescript as ts_typescript
    import tree_sitter_c_sharp as ts_csharp
    import tree_sitter_go as ts_go
    import tree_sitter_java as ts_java
    import tree_sitter_rust as ts_rust
    import tree_sitter_c as ts_c
    import tree_sitter_cpp as ts_cpp
    TREE_SITTER_AVAILABLE = True
except ImportError:
    TREE_SITTER_AVAILABLE = False


EXTENSION_TO_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".cs": "c_sharp",
    ".go": "go",
    ".java": "java",
    ".rs": "rust",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
}

IGNORED_DIRS = {
    # VCS
    ".git", ".hg", ".svn",
    # JS/TS package dirs & build output
    "node_modules", "bower_components", "dist", ".next", ".nuxt",
    ".parcel-cache", ".turbo", ".nx",
    # Generic build output
    "build", "out", "bin", "obj", "target",
    # Python
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "venv", ".venv", "env",
    # Go / PHP / Ruby vendoring
    "vendor",
    # Java / Kotlin
    ".gradle",
    # Test coverage reports
    "coverage",
    # Caches & temp
    ".cache", "tmp", "temp",
    # IDE
    ".idea", ".vscode",
}


@dataclass
class Symbol:
    name: str
    kind: str        # 'function' | 'class' | 'method' | 'interface' | 'struct'
    signature: Optional[str]
    docstring: Optional[str]
    body: Optional[str]
    start_line: int
    end_line: int


def _get_languages() -> dict[str, Language]:
    if not TREE_SITTER_AVAILABLE:
        return {}
    langs = {}
    try:
        langs["python"] = Language(ts_python.language())
        langs["javascript"] = Language(ts_javascript.language())
        langs["typescript"] = Language(ts_typescript.language_typescript())
        langs["tsx"] = Language(ts_typescript.language_tsx())
        langs["c_sharp"] = Language(ts_csharp.language())
        langs["go"] = Language(ts_go.language())
        langs["java"] = Language(ts_java.language())
        langs["rust"] = Language(ts_rust.language())
        langs["c"] = Language(ts_c.language())
        langs["cpp"] = Language(ts_cpp.language())
    except Exception as e:
        print(f"Warning: some tree-sitter languages failed to load: {e}")
    return langs


_LANGUAGES: dict[str, Language] = {}


def _langs() -> dict[str, Language]:
    global _LANGUAGES
    if not _LANGUAGES:
        _LANGUAGES = _get_languages()
    return _LANGUAGES


def _node_text(node: "Node", src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _first_child_of_type(node: "Node", *types: str) -> Optional["Node"]:
    for child in node.children:
        if child.type in types:
            return child
    return None


def _children_of_type(node: "Node", *types: str) -> list["Node"]:
    return [c for c in node.children if c.type in types]


# ── Language-specific extractors ─────────────────────────────────────────────

def _extract_python_docstring(body_node: "Node", src: bytes) -> Optional[str]:
    """Pull the first string literal from a function/class body as docstring."""
    for child in body_node.children:
        if child.type == "expression_statement":
            inner = _first_child_of_type(child, "string")
            if inner:
                raw = _node_text(inner, src)
                return raw.strip("\"' \n").strip('"""').strip("'''").strip()
    return None


def _extract_python_symbols(tree, src: bytes) -> list[Symbol]:
    symbols = []

    def walk(node: "Node", class_name: Optional[str] = None):
        if node.type in ("function_definition", "async_function_definition"):
            name_node = _first_child_of_type(node, "identifier")
            name = _node_text(name_node, src) if name_node else "<anon>"
            params_node = _first_child_of_type(node, "parameters")
            sig = f"def {name}{_node_text(params_node, src) if params_node else '()'}"
            body_node = _first_child_of_type(node, "block")
            docstring = _extract_python_docstring(body_node, src) if body_node else None
            body_text = _node_text(node, src)[:2000]
            kind = "method" if class_name else "function"
            symbols.append(Symbol(
                name=name, kind=kind, signature=sig,
                docstring=docstring, body=body_text,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
            ))
            # Don't recurse into nested functions for now
            return

        if node.type == "class_definition":
            name_node = _first_child_of_type(node, "identifier")
            name = _node_text(name_node, src) if name_node else "<anon>"
            body_node = _first_child_of_type(node, "block")
            docstring = _extract_python_docstring(body_node, src) if body_node else None
            sig = _node_text(node, src).split(":")[0].strip()
            symbols.append(Symbol(
                name=name, kind="class", signature=sig,
                docstring=docstring, body=None,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
            ))
            for child in node.children:
                walk(child, class_name=name)
            return

        for child in node.children:
            walk(child, class_name=class_name)

    walk(tree.root_node)
    return symbols


def _extract_js_ts_symbols(tree, src: bytes) -> list[Symbol]:
    symbols = []

    def get_name(node: "Node") -> Optional[str]:
        for child in node.children:
            if child.type == "identifier":
                return _node_text(child, src)
            if child.type == "property_identifier":
                return _node_text(child, src)
        return None

    def get_params(node: "Node") -> Optional[str]:
        for child in node.children:
            if child.type in ("formal_parameters", "parameters"):
                return _node_text(child, src)
        return None

    def walk(node: "Node", class_name: Optional[str] = None):
        t = node.type

        if t in ("function_declaration", "function_expression", "generator_function_declaration",
                  "generator_function", "arrow_function"):
            name = get_name(node) or "<anon>"
            params = get_params(node) or "()"
            sig = f"function {name}{params}"
            body_text = _node_text(node, src)[:2000]
            kind = "method" if class_name else "function"
            symbols.append(Symbol(
                name=name, kind=kind, signature=sig,
                docstring=None, body=body_text,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
            ))

        elif t == "method_definition":
            name = get_name(node) or "<anon>"
            params = get_params(node) or "()"
            sig = f"{name}{params}"
            body_text = _node_text(node, src)[:2000]
            symbols.append(Symbol(
                name=name, kind="method", signature=sig,
                docstring=None, body=body_text,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
            ))

        elif t == "class_declaration":
            name = get_name(node) or "<anon>"
            symbols.append(Symbol(
                name=name, kind="class", signature=f"class {name}",
                docstring=None, body=None,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
            ))
            for child in node.children:
                walk(child, class_name=name)
            return

        elif t == "interface_declaration":
            name = get_name(node) or "<anon>"
            symbols.append(Symbol(
                name=name, kind="interface", signature=f"interface {name}",
                docstring=None, body=_node_text(node, src)[:2000],
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
            ))

        elif t == "lexical_declaration":
            # const foo = () => ...  or  const foo = function ...
            for child in node.children:
                if child.type == "variable_declarator":
                    val = _first_child_of_type(child, "arrow_function", "function_expression")
                    if val:
                        name_node = _first_child_of_type(child, "identifier")
                        name = _node_text(name_node, src) if name_node else "<anon>"
                        params = get_params(val) or "()"
                        sig = f"const {name} = {val.type.replace('_', ' ')}{params}"
                        body_text = _node_text(val, src)[:2000]
                        kind = "method" if class_name else "function"
                        symbols.append(Symbol(
                            name=name, kind=kind, signature=sig,
                            docstring=None, body=body_text,
                            start_line=val.start_point[0] + 1,
                            end_line=val.end_point[0] + 1,
                        ))

        for child in node.children:
            if t not in ("class_declaration",):
                walk(child, class_name=class_name)

    walk(tree.root_node)
    return symbols


def _extract_csharp_symbols(tree, src: bytes) -> list[Symbol]:
    symbols = []

    def walk(node: "Node", class_name: Optional[str] = None):
        t = node.type

        if t in ("class_declaration", "interface_declaration", "struct_declaration", "record_declaration"):
            name_node = _first_child_of_type(node, "identifier")
            name = _node_text(name_node, src) if name_node else "<anon>"
            kind = {"class_declaration": "class", "interface_declaration": "interface",
                    "struct_declaration": "struct", "record_declaration": "class"}.get(t, "class")
            symbols.append(Symbol(
                name=name, kind=kind, signature=f"{kind} {name}",
                docstring=None, body=None,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
            ))
            for child in node.children:
                walk(child, class_name=name)
            return

        if t == "method_declaration":
            name_node = _first_child_of_type(node, "identifier")
            name = _node_text(name_node, src) if name_node else "<anon>"
            sig = _node_text(node, src).split("{")[0].strip()
            body_text = _node_text(node, src)[:2000]
            symbols.append(Symbol(
                name=name, kind="method", signature=sig,
                docstring=None, body=body_text,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
            ))

        for child in node.children:
            walk(child, class_name=class_name)

    walk(tree.root_node)
    return symbols


def _extract_go_symbols(tree, src: bytes) -> list[Symbol]:
    symbols = []

    def walk(node: "Node"):
        t = node.type

        if t == "function_declaration":
            name_node = _first_child_of_type(node, "identifier")
            name = _node_text(name_node, src) if name_node else "<anon>"
            sig = _node_text(node, src).split("{")[0].strip()
            body_text = _node_text(node, src)[:2000]
            symbols.append(Symbol(
                name=name, kind="function", signature=sig,
                docstring=None, body=body_text,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
            ))

        elif t == "method_declaration":
            name_node = _first_child_of_type(node, "field_identifier")
            name = _node_text(name_node, src) if name_node else "<anon>"
            sig = _node_text(node, src).split("{")[0].strip()
            body_text = _node_text(node, src)[:2000]
            symbols.append(Symbol(
                name=name, kind="method", signature=sig,
                docstring=None, body=body_text,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
            ))

        elif t == "type_declaration":
            for spec in _children_of_type(node, "type_spec"):
                name_node = _first_child_of_type(spec, "type_identifier")
                name = _node_text(name_node, src) if name_node else "<anon>"
                body_type = _first_child_of_type(spec, "struct_type", "interface_type")
                kind = "struct" if body_type and body_type.type == "struct_type" else "interface"
                symbols.append(Symbol(
                    name=name, kind=kind, signature=f"type {name} {kind}",
                    docstring=None, body=None,
                    start_line=spec.start_point[0] + 1,
                    end_line=spec.end_point[0] + 1,
                ))

        for child in node.children:
            walk(child)

    walk(tree.root_node)
    return symbols


def _extract_generic_symbols(tree, src: bytes) -> list[Symbol]:
    """Fallback for Java, Rust, C, C++ — captures functions and classes by common node names."""
    symbols = []
    FUNC_NODES = {
        "function_definition", "function_declaration", "function_item",
        "method_declaration", "method_definition",
    }
    CLASS_NODES = {
        "class_declaration", "struct_item", "impl_item", "interface_declaration",
        "struct_specifier",
    }

    def walk(node: "Node"):
        if node.type in FUNC_NODES:
            name_node = _first_child_of_type(node, "identifier", "field_identifier", "name")
            name = _node_text(name_node, src) if name_node else "<anon>"
            sig = _node_text(node, src).split("{")[0].strip()[:300]
            body_text = _node_text(node, src)[:2000]
            symbols.append(Symbol(
                name=name, kind="function", signature=sig,
                docstring=None, body=body_text,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
            ))
        elif node.type in CLASS_NODES:
            name_node = _first_child_of_type(node, "identifier", "type_identifier", "name")
            name = _node_text(name_node, src) if name_node else "<anon>"
            symbols.append(Symbol(
                name=name, kind="class", signature=f"{node.type.replace('_', ' ')} {name}",
                docstring=None, body=None,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
            ))
        for child in node.children:
            walk(child)

    walk(tree.root_node)
    return symbols


_EXTRACTORS = {
    "python": _extract_python_symbols,
    "javascript": _extract_js_ts_symbols,
    "typescript": _extract_js_ts_symbols,
    "tsx": _extract_js_ts_symbols,
    "c_sharp": _extract_csharp_symbols,
    "go": _extract_go_symbols,
    "java": _extract_generic_symbols,
    "rust": _extract_generic_symbols,
    "c": _extract_generic_symbols,
    "cpp": _extract_generic_symbols,
}


def language_for_path(path: Path) -> Optional[str]:
    return EXTENSION_TO_LANGUAGE.get(path.suffix.lower())


def parse_file(path: Path) -> list[Symbol]:
    lang_name = language_for_path(path)
    if not lang_name:
        return []

    langs = _langs()
    if lang_name not in langs:
        return []

    try:
        src = path.read_bytes()
    except (OSError, PermissionError):
        return []

    parser = Parser(langs[lang_name])
    tree = parser.parse(src)
    extractor = _EXTRACTORS.get(lang_name, _extract_generic_symbols)
    return extractor(tree, src)


def iter_source_files(root: Path) -> list[Path]:
    """Walk a directory tree, skipping common noise dirs, returning indexable files."""
    results = []
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in EXTENSION_TO_LANGUAGE:
            if not any(part in IGNORED_DIRS for part in p.parts):
                results.append(p)
    return results
