"""Multi-language AST parsing via tree-sitter with a declarative grammar registry."""

from __future__ import annotations

import fnmatch
import importlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence

from tree_sitter import Language, Node, Parser, Tree

from repo2neo4j.models.code import ClassModel, FileModel, FunctionModel, ImportModel

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Declarative grammar specs (extend this list to add languages without logic)
# ---------------------------------------------------------------------------
# Keys:
#   name: registry key / FileModel.language
#   extensions: file suffixes including dot
#   ts_module: tree-sitter PyPI module to import
#   ts_language_fn: attribute on module returning the language capsule
#   class_types / function_types / import_types / call_types: tree-sitter node `type` strings
#   class_name_field / function_name_field: field names on nodes (tree-sitter fields)
#   class_base_fields: optional field names whose subtrees contain base type names
#   extra_class_walk_types: optional node types that also produce ClassModel (e.g. Rust impl)
_DEFAULT_GRAMMAR_SPECS: list[dict[str, Any]] = [
    {
        "name": "python",
        "extensions": [".py"],
        "ts_module": "tree_sitter_python",
        "ts_language_fn": "language",
        "class_types": ["class_definition"],
        "function_types": ["function_definition"],
        "import_types": ["import_statement", "import_from_statement"],
        "call_types": ["call"],
        "class_name_field": "name",
        "function_name_field": "name",
        "class_base_fields": ["superclasses"],
    },
    {
        "name": "javascript",
        "extensions": [".js", ".mjs", ".cjs"],
        "ts_module": "tree_sitter_javascript",
        "ts_language_fn": "language",
        "class_types": ["class_declaration"],
        "function_types": ["function_declaration", "function_expression", "arrow_function"],
        "import_types": ["import_statement", "import_declaration"],
        "call_types": ["call_expression"],
        "class_name_field": "name",
        "function_name_field": "name",
        "class_base_fields": [],
        "class_base_child_types": ["class_heritage"],
    },
    {
        "name": "typescript",
        "extensions": [".ts"],
        "ts_module": "tree_sitter_typescript",
        "ts_language_fn": "language_typescript",
        "class_types": ["class_declaration", "interface_declaration"],
        "function_types": [
            "function_declaration",
            "function_signature",
            "method_definition",
            "arrow_function",
        ],
        "import_types": ["import_statement"],
        "call_types": ["call_expression"],
        "class_name_field": "name",
        "function_name_field": "name",
        "class_base_fields": [],
        "class_base_child_types": ["class_heritage", "extends_type_clause"],
    },
    {
        "name": "tsx",
        "extensions": [".tsx"],
        "ts_module": "tree_sitter_typescript",
        "ts_language_fn": "language_tsx",
        "class_types": ["class_declaration", "interface_declaration"],
        "function_types": [
            "function_declaration",
            "function_signature",
            "method_definition",
            "arrow_function",
        ],
        "import_types": ["import_statement"],
        "call_types": ["call_expression"],
        "class_name_field": "name",
        "function_name_field": "name",
        "class_base_fields": [],
        "class_base_child_types": ["class_heritage", "extends_type_clause"],
    },
    {
        "name": "java",
        "extensions": [".java"],
        "ts_module": "tree_sitter_java",
        "ts_language_fn": "language",
        "class_types": ["class_declaration", "interface_declaration", "enum_declaration"],
        "function_types": ["method_declaration", "constructor_declaration"],
        "import_types": ["import_declaration", "package_declaration"],
        "call_types": ["method_invocation"],
        "class_name_field": "name",
        "function_name_field": "name",
        "class_base_fields": ["superclass", "super_interfaces"],
    },
    {
        "name": "go",
        "extensions": [".go"],
        "ts_module": "tree_sitter_go",
        "ts_language_fn": "language",
        "class_types": ["type_declaration"],
        "function_types": ["function_declaration", "method_declaration", "func_literal"],
        "import_types": ["import_declaration", "package_clause"],
        "call_types": ["call_expression"],
        "class_name_field": "name",
        "function_name_field": "name",
        "class_base_fields": [],
        "go_struct_only": True,
    },
    {
        "name": "rust",
        "extensions": [".rs"],
        "ts_module": "tree_sitter_rust",
        "ts_language_fn": "language",
        "class_types": ["struct_item", "enum_item", "union_item", "trait_item"],
        "function_types": ["function_item", "closure_expression"],
        "import_types": ["use_declaration", "extern_crate_declaration"],
        "call_types": ["call_expression", "macro_invocation"],
        "class_name_field": "name",
        "function_name_field": "name",
        "class_base_fields": [],
        "rust_impl_blocks": True,
    },
    {
        "name": "c",
        "extensions": [".c", ".h"],
        "ts_module": "tree_sitter_c",
        "ts_language_fn": "language",
        "class_types": ["struct_specifier", "union_specifier", "enum_specifier"],
        "function_types": ["function_definition"],
        "import_types": ["preproc_include"],
        "call_types": ["call_expression"],
        "class_name_field": "name",
        "function_name_field": "declarator",
        "class_base_fields": [],
    },
    {
        "name": "cpp",
        "extensions": [".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx", ".h++", ".inl", ".ipp"],
        "ts_module": "tree_sitter_cpp",
        "ts_language_fn": "language",
        "class_types": ["class_specifier", "struct_specifier", "union_specifier", "enum_specifier"],
        "function_types": ["function_definition", "template_declaration"],
        "import_types": ["preproc_include"],
        "call_types": ["call_expression"],
        "class_name_field": "name",
        "function_name_field": "declarator",
        "class_base_fields": ["base_class_clause"],
    },
]

# Optional hook: append dict specs here, or call register_grammar_spec() at runtime.
EXTRA_GRAMMAR_SPECS: list[dict[str, Any]] = []


def _load_tree_sitter_language(module_name: str, function_name: str) -> Language | None:
    try:
        mod = importlib.import_module(module_name)
    except ImportError:
        logger.warning(
            "Tree-sitter grammar module %r is not installed; skipping language loader %s.%s",
            module_name,
            module_name,
            function_name,
        )
        return None
    try:
        lang_fn = getattr(mod, function_name)
    except AttributeError:
        logger.warning(
            "Tree-sitter grammar module %r has no attribute %r; skipping.",
            module_name,
            function_name,
        )
        return None
    try:
        return Language(lang_fn())
    except Exception as exc:  # noqa: BLE001 - surface binding errors as skip
        logger.warning(
            "Failed to construct tree-sitter Language from %s.%s: %s",
            module_name,
            function_name,
            exc,
        )
        return None


@dataclass(frozen=True)
class LanguageConfig:
    """Runtime configuration for one tree-sitter grammar."""

    name: str
    extensions: tuple[str, ...]
    language: Language
    class_node_types: frozenset[str]
    function_node_types: frozenset[str]
    import_node_types: frozenset[str]
    call_node_types: frozenset[str]
    class_name_field: str
    function_name_field: str
    class_base_field_names: tuple[str, ...] = ()
    class_base_child_types: tuple[str, ...] = ()
    raw_spec: Mapping[str, Any] = field(default_factory=dict)

    @staticmethod
    def from_spec(language: Language, spec: Mapping[str, Any]) -> LanguageConfig:
        exts = tuple(str(e) for e in spec["extensions"])
        return LanguageConfig(
            name=str(spec["name"]),
            extensions=exts,
            language=language,
            class_node_types=frozenset(str(x) for x in spec["class_types"]),
            function_node_types=frozenset(str(x) for x in spec["function_types"]),
            import_node_types=frozenset(str(x) for x in spec["import_types"]),
            call_node_types=frozenset(str(x) for x in spec["call_types"]),
            class_name_field=str(spec.get("class_name_field", "name")),
            function_name_field=str(spec.get("function_name_field", "name")),
            class_base_field_names=tuple(str(x) for x in spec.get("class_base_fields", ())),
            class_base_child_types=tuple(str(x) for x in spec.get("class_base_child_types", ())),
            raw_spec=dict(spec),
        )


def _language_config_from_spec(spec: dict[str, Any]) -> LanguageConfig | None:
    module_name = str(spec["ts_module"])
    fn_name = str(spec.get("ts_language_fn", "language"))
    language = _load_tree_sitter_language(module_name, fn_name)
    if language is None:
        return None
    return LanguageConfig.from_spec(language, spec)


def build_language_registry(
    extra_specs: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, LanguageConfig]:
    """Build a language registry from default + optional extra grammar specs."""
    registry: dict[str, LanguageConfig] = {}
    extra_list: list[dict[str, Any]] = (
        [dict(s) for s in extra_specs] if extra_specs is not None else []
    )
    all_specs: list[dict[str, Any]] = [
        *_DEFAULT_GRAMMAR_SPECS,
        *EXTRA_GRAMMAR_SPECS,
        *extra_list,
    ]
    for spec in all_specs:
        cfg = _language_config_from_spec(dict(spec))
        if cfg is not None:
            if cfg.name in registry:
                logger.warning("Duplicate language registry key %r; later spec wins.", cfg.name)
            registry[cfg.name] = cfg
    return registry


LANGUAGE_REGISTRY: dict[str, LanguageConfig] = build_language_registry()


def register_grammar_spec(spec: dict[str, Any]) -> None:
    """Register an additional language from a grammar spec dict (runtime hook)."""
    built = _language_config_from_spec(spec)
    if built is not None:
        LANGUAGE_REGISTRY[built.name] = built


def _posix_relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _matches_ignore(rel_posix_path: str, patterns: Sequence[str]) -> bool:
    if not patterns:
        return False
    normalized = rel_posix_path.replace("\\", "/")
    base = Path(normalized).name
    for pattern in patterns:
        if fnmatch.fnmatch(normalized, pattern) or fnmatch.fnmatch(base, pattern):
            return True
    return False


def _get_node_text(node: Node | None, source: bytes) -> str:
    if node is None:
        return ""
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _node_line_1based(node: Node) -> int:
    return int(node.start_point[0]) + 1


def _node_end_line_1based(node: Node) -> int:
    return int(node.end_point[0]) + 1


_DECL_WRAPPERS: frozenset[str] = frozenset(
    {"pointer_declarator", "function_declarator", "array_declarator", "parenthesized_declarator"}
)
_BASE_TYPE_IDENTIFIER_TYPES: frozenset[str] = frozenset(
    {"identifier", "type_identifier", "scoped_type_identifier", "property_identifier"}
)
_C_AGGREGATE_TYPES: frozenset[str] = frozenset(
    {"struct_specifier", "class_specifier", "union_specifier", "enum_specifier"}
)
_RUST_TYPE_DECL_NODES: frozenset[str] = frozenset(
    {"struct_item", "enum_item", "union_item", "trait_item"}
)


def _identifier_from_declarator(node: Node | None, source: bytes) -> str:
    """Best-effort name for C/C++ declarator / pointer / function_declarator."""
    if node is None:
        return ""
    if node.type == "identifier":
        return _get_node_text(node, source).strip()
    for child in node.named_children:
        t = child.type
        if t == "identifier":
            return _get_node_text(child, source).strip()
        if t in _DECL_WRAPPERS:
            return _identifier_from_declarator(child, source)
    return _get_node_text(node, source).strip()


def _extract_type_identifiers_from_bases(node: Node | None, source: bytes) -> list[str]:
    if node is None:
        return []
    names: list[str] = []

    def visit(n: Node) -> None:
        if n.type in _BASE_TYPE_IDENTIFIER_TYPES:
            text = _get_node_text(n, source).strip()
            if text and text not in names:
                names.append(text)
        for ch in n.named_children:
            visit(ch)

    visit(node)
    return names


def _callee_to_string(node: Node | None, source: bytes) -> str:
    if node is None:
        return ""
    if node.type == "identifier":
        return _get_node_text(node, source).strip()
    if node.type == "attribute":
        obj = node.child_by_field_name("object")
        attr = node.child_by_field_name("attribute")
        left = _callee_to_string(obj, source)
        right = _callee_to_string(attr, source)
        if left and right:
            return f"{left}.{right}"
        return left or right
    if node.type in {"field_expression", "member_expression", "field_identifier"}:
        parts: list[str] = []
        for ch in node.named_children:
            s = _callee_to_string(ch, source)
            if s:
                parts.append(s)
        return ".".join(parts) if parts else _get_node_text(node, source).strip()
    if node.type == "scoped_identifier":
        return _get_node_text(node, source).strip()
    if node.type == "selector_expression":
        operand = node.child_by_field_name("operand")
        field = node.child_by_field_name("field")
        left = _callee_to_string(operand, source)
        right = _callee_to_string(field, source)
        if left and right:
            return f"{left}.{right}"
        return left or right or _get_node_text(node, source).strip()
    if node.type == "subscript_expression":
        obj = node.child_by_field_name("object")
        return _callee_to_string(obj, source)
    if node.type == "call":
        fn = node.child_by_field_name("function")
        return _callee_to_string(fn, source)
    if node.type == "call_expression":
        fn = node.child_by_field_name("function")
        return _callee_to_string(fn, source)
    if node.type == "method_invocation":
        name = node.child_by_field_name("name")
        obj = node.child_by_field_name("object")
        if obj is not None:
            return f"{_callee_to_string(obj, source)}.{_callee_to_string(name, source)}".strip(".")
        return _callee_to_string(name, source)
    if node.type == "macro_invocation":
        macro = node.child_by_field_name("macro")
        return _callee_to_string(macro, source)
    return _get_node_text(node, source).strip()


def _collect_calls_in(node: Node, lang: LanguageConfig, source: bytes) -> list[str]:
    calls: list[str] = []

    def walk(n: Node) -> None:
        if n.type in lang.call_node_types:
            callee = ""
            if n.type in {"call", "call_expression"}:
                callee = _callee_to_string(n.child_by_field_name("function"), source)
            elif n.type == "method_invocation":
                callee = _callee_to_string(n, source)
            elif n.type == "macro_invocation":
                callee = _callee_to_string(n.child_by_field_name("macro"), source)
            else:
                first = n.named_children[0] if n.named_children else None
                callee = _callee_to_string(first, source)
            if callee and callee not in calls:
                calls.append(callee)
        for ch in n.children:
            walk(ch)

    walk(node)
    return calls


def _extract_parameters(node: Node | None, source: bytes) -> list[str]:
    if node is None:
        return []
    params: list[str] = []
    for p in node.named_children:
        if p.type in {
            "identifier",
            "typed_parameter",
            "typed_default_parameter",
            "default_parameter",
            "keyword_separator",
            "list_splat_pattern",
            "dictionary_splat_pattern",
        }:
            if p.type == "identifier":
                params.append(_get_node_text(p, source).strip())
            else:
                for ch in p.named_children:
                    if ch.type == "identifier":
                        params.append(_get_node_text(ch, source).strip())
                        break
        elif p.type in {"required_parameter", "optional_parameter", "formal_parameter"}:
            for ch in p.named_children:
                if ch.type == "identifier":
                    params.append(_get_node_text(ch, source).strip())
                    break
        elif p.type in {"parameter", "spread_parameter", "rest_pattern"}:
            for ch in p.named_children:
                if ch.type in {"identifier", "pattern", "assignment_pattern"}:
                    params.append(_get_node_text(ch, source).strip())
                    break
        elif p.type in {"receiver", "variadic_parameter_declaration", "ERROR"}:
            continue
    return [p for p in params if p]


def _return_type_text(node: Node, field: str, source: bytes) -> str | None:
    rt = node.child_by_field_name(field)
    if rt is None:
        return None
    text = _get_node_text(rt, source).strip()
    return text or None


def _class_bases(node: Node, lang: LanguageConfig, source: bytes) -> list[str]:
    bases: list[str] = []
    for fname in lang.class_base_field_names:
        child = node.child_by_field_name(fname)
        bases.extend(_extract_type_identifiers_from_bases(child, source))
    for ctype in lang.class_base_child_types:
        for ch in node.named_children:
            if ch.type == ctype:
                bases.extend(_extract_type_identifiers_from_bases(ch, source))
    # Java: superclass is type_identifier directly
    if lang.name == "java":
        sup = node.child_by_field_name("superclass")
        if sup is not None:
            tid = sup
            for c in sup.named_children:
                if c.type == "type_identifier":
                    tid = c
                    break
            t = _get_node_text(tid, source).strip()
            if t:
                bases.append(t)
        si = node.child_by_field_name("super_interfaces")
        if si is not None:
            bases.extend(_extract_type_identifiers_from_bases(si, source))
    # Dedupe preserving order
    seen: set[str] = set()
    out: list[str] = []
    for b in bases:
        if b not in seen:
            seen.add(b)
            out.append(b)
    return out


def _is_descendant_of(node: Node, ancestor_types: frozenset[str]) -> bool:
    current = node.parent
    while current is not None:
        if current.type in ancestor_types:
            return True
        current = current.parent
    return False


def _omit_file_level_function(node: Node, lang: LanguageConfig) -> bool:
    """True if this function node should not appear on FileModel.functions."""
    if lang.name == "go" and node.type == "method_declaration":
        return True
    if (
        lang.name == "rust"
        and node.type == "function_item"
        and node.parent is not None
        and node.parent.type == "impl_item"
    ):
        return True
    return _is_descendant_of(node, lang.class_node_types)


def _go_type_spec_struct_name(type_decl: Node, source: bytes) -> str | None:
    for ch in type_decl.named_children:
        if ch.type != "type_spec":
            continue
        name = ch.child_by_field_name("name")
        st = ch.child_by_field_name("type")
        if name is None or st is None or st.type != "struct_type":
            continue
        return _get_node_text(name, source).strip()
    return None


def _rust_impl_type_name(impl: Node, source: bytes) -> str | None:
    for ch in impl.named_children:
        if ch.type in {"type_identifier", "scoped_type_identifier", "generic_type"}:
            return _get_node_text(ch, source).strip()
    return None


class CodeParser:
    """Lazily parses individual source files using registered tree-sitter grammars."""

    def __init__(self, languages: list[str], ignore_patterns: list[str]) -> None:
        requested = {lang.strip().lower() for lang in languages if lang.strip()}
        if "typescript" in requested:
            requested.add("tsx")
        self._languages = frozenset(requested)
        self._ignore_patterns = list(ignore_patterns)
        self._parsers: dict[str, Parser] = {}
        self._extension_map: dict[str, str] = {}
        for name in self._languages:
            cfg = LANGUAGE_REGISTRY.get(name)
            if cfg is None:
                logger.warning("Requested language %r is not available in LANGUAGE_REGISTRY.", name)
                continue
            try:
                self._parsers[name] = Parser(cfg.language)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to create Parser for %r: %s", name, exc)
                continue
            for ext in cfg.extensions:
                self._extension_map.setdefault(ext.lower(), name)

    def _parser_for(self, lang_name: str) -> Parser | None:
        return self._parsers.get(lang_name)

    def _detect_language(self, file_path: Path) -> str | None:
        ext = file_path.suffix.lower()
        return self._extension_map.get(ext)

    def parse_file(self, file_path: str | Path, repo_root: str | Path) -> FileModel | None:
        path = Path(file_path)
        root = Path(repo_root)
        if not path.is_file():
            logger.debug("parse_file: not a file: %s", path)
            return None
        rel = _posix_relative(path, root)
        if _matches_ignore(rel, self._ignore_patterns):
            return None
        lang_name = self._detect_language(path)
        if lang_name is None:
            return None
        cfg = LANGUAGE_REGISTRY.get(lang_name)
        if cfg is None:
            return None
        parser = self._parser_for(lang_name)
        if parser is None:
            return None
        try:
            source = path.read_bytes()
        except OSError as exc:
            logger.warning("Could not read %s: %s", path, exc)
            return None
        try:
            tree = parser.parse(source)
        except Exception as exc:  # noqa: BLE001
            logger.warning("tree-sitter failed to parse %s: %s", path, exc)
            return None
        if tree.root_node.has_error:
            logger.debug("Parse tree for %s contains errors; extracting best-effort.", path)
        try:
            classes = self._extract_classes(tree, source, rel, cfg)
            functions = self._extract_functions(tree, source, rel, cfg)
            imports = self._extract_imports(tree, source, rel, cfg)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Extraction failed for %s: %s", path, exc)
            return None
        return FileModel(
            path=rel,
            language=cfg.name,
            size=len(source),
            classes=classes,
            functions=functions,
            imports=imports,
        )

    def parse_file_content(self, rel_path: str, content: str) -> FileModel | None:
        """Parse a file from its content string (no disk access). For remote/API mode."""
        if _matches_ignore(rel_path, self._ignore_patterns):
            return None
        lang_name = self._detect_language(Path(rel_path))
        if lang_name is None:
            return None
        cfg = LANGUAGE_REGISTRY.get(lang_name)
        if cfg is None:
            return None
        parser = self._parser_for(lang_name)
        if parser is None:
            return None
        source = content.encode("utf-8")
        try:
            tree = parser.parse(source)
        except Exception as exc:  # noqa: BLE001
            logger.warning("tree-sitter failed to parse %s: %s", rel_path, exc)
            return None
        if tree.root_node.has_error:
            logger.debug("Parse tree for %s contains errors; extracting best-effort.", rel_path)
        try:
            classes = self._extract_classes(tree, source, rel_path, cfg)
            functions = self._extract_functions(tree, source, rel_path, cfg)
            imports = self._extract_imports(tree, source, rel_path, cfg)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Extraction failed for %s: %s", rel_path, exc)
            return None
        return FileModel(
            path=rel_path,
            language=cfg.name,
            size=len(source),
            classes=classes,
            functions=functions,
            imports=imports,
        )

    def iter_parse_directory(
        self,
        directory: str | Path,
        repo_root: str | Path | None = None,
    ) -> Iterator[FileModel]:
        root_dir = Path(directory)
        root = Path(repo_root) if repo_root is not None else root_dir
        if not root_dir.is_dir():
            logger.warning("iter_parse_directory: not a directory: %s", root_dir)
            return
        for path in sorted(root_dir.rglob("*")):
            if not path.is_file():
                continue
            model = self.parse_file(path, root)
            if model is not None:
                yield model

    def _extract_classes(
        self,
        tree: Tree,
        source: bytes,
        file_path: str,
        lang: LanguageConfig,
    ) -> list[ClassModel]:
        classes: list[ClassModel] = []
        by_name: dict[str, ClassModel] = {}

        def add_class(cm: ClassModel) -> None:
            classes.append(cm)
            if cm.name not in by_name:
                by_name[cm.name] = cm

        root = tree.root_node

        def extract_class_like(node: Node) -> None:
            if node.type not in lang.class_node_types:
                return
            if lang.name == "go" and lang.raw_spec.get("go_struct_only"):
                if node.type == "type_declaration":
                    sname = _go_type_spec_struct_name(node, source)
                    if not sname:
                        return
                    bases: list[str] = []
                    start = _node_line_1based(node)
                    end = _node_end_line_1based(node)
                    methods = self._methods_in_go_type(sname, root, source, lang, file_path)
                    add_class(
                        ClassModel(
                            name=sname,
                            qualified_name=sname,
                            file_path=file_path,
                            start_line=start,
                            end_line=end,
                            bases=bases,
                            methods=methods,
                        )
                    )
                return
            if lang.name in {"c", "cpp"} and node.type in _C_AGGREGATE_TYPES:
                name_node = node.child_by_field_name(lang.class_name_field)
                if name_node is None:
                    return
                cname = _get_node_text(name_node, source).strip()
                if not cname:
                    return
                bases = _class_bases(node, lang, source)
                body = node.child_by_field_name("body")
                methods = self._extract_methods_from_body(body, source, lang, file_path, cname)
                add_class(
                    ClassModel(
                        name=cname,
                        qualified_name=cname,
                        file_path=file_path,
                        start_line=_node_line_1based(node),
                        end_line=_node_end_line_1based(node),
                        bases=bases,
                        methods=methods,
                    )
                )
                return
            name_node = node.child_by_field_name(lang.class_name_field)
            if name_node is None and lang.name in {"c", "cpp"}:
                name_node = node.child_by_field_name("name")
            if name_node is None:
                return
            if lang.name in {"c", "cpp"} and lang.class_name_field == "declarator":
                cname = _identifier_from_declarator(name_node, source)
            else:
                cname = _get_node_text(name_node, source).strip()
            if not cname:
                return
            bases = _class_bases(node, lang, source)
            body = None
            if lang.name in {"python", "javascript", "typescript", "tsx", "java"} or (
                lang.name == "rust" and node.type in _RUST_TYPE_DECL_NODES
            ):
                body = node.child_by_field_name("body")
            methods = self._extract_methods_from_body(body, source, lang, file_path, cname)
            add_class(
                ClassModel(
                    name=cname,
                    qualified_name=cname,
                    file_path=file_path,
                    start_line=_node_line_1based(node),
                    end_line=_node_end_line_1based(node),
                    bases=bases,
                    methods=methods,
                )
            )

        def walk(n: Node) -> None:
            extract_class_like(n)
            for ch in n.children:
                walk(ch)

        walk(root)

        if lang.name == "rust" and lang.raw_spec.get("rust_impl_blocks"):
            for child in root.named_children:
                if child.type != "impl_item":
                    continue
                type_name = _rust_impl_type_name(child, source)
                if not type_name:
                    continue
                body = child.child_by_field_name("body")
                methods = self._extract_methods_from_body(body, source, lang, file_path, type_name)
                if not methods:
                    continue
                target = by_name.get(type_name)
                if target is not None:
                    merged = list(target.methods)
                    merged.extend(methods)
                    new_cm = ClassModel(
                        name=target.name,
                        qualified_name=target.qualified_name,
                        file_path=target.file_path,
                        start_line=target.start_line,
                        end_line=target.end_line,
                        bases=list(target.bases),
                        methods=merged,
                    )
                    idx = classes.index(target)
                    classes[idx] = new_cm
                    by_name[type_name] = new_cm
                else:
                    add_class(
                        ClassModel(
                            name=type_name,
                            qualified_name=type_name,
                            file_path=file_path,
                            start_line=_node_line_1based(child),
                            end_line=_node_end_line_1based(child),
                            bases=[],
                            methods=methods,
                        )
                    )

        return classes

    def _extract_methods_from_body(
        self,
        body: Node | None,
        source: bytes,
        lang: LanguageConfig,
        file_path: str,
        class_name: str,
    ) -> list[FunctionModel]:
        if body is None:
            return []
        methods: list[FunctionModel] = []
        for fn in body.named_children:
            if fn.type not in lang.function_node_types:
                continue
            m = self._function_model_from_node(
                fn,
                source,
                lang,
                file_path,
                class_name=class_name,
                is_method=True,
            )
            if m is not None:
                methods.append(m)
        return methods

    def _methods_in_go_type(
        self,
        type_name: str,
        root: Node,
        source: bytes,
        lang: LanguageConfig,
        file_path: str,
    ) -> list[FunctionModel]:
        methods: list[FunctionModel] = []

        def walk(n: Node) -> None:
            if n.type == "method_declaration":
                recv = n.child_by_field_name("receiver")
                receiver_type = None
                if recv is not None:
                    for p in recv.named_children:
                        if p.type == "parameter_declaration":
                            for ch in p.named_children:
                                if ch.type in {"pointer_type", "type_identifier"}:
                                    receiver_type = _get_node_text(ch, source)
                                    break
                if receiver_type and type_name in receiver_type.replace("*", "").strip():
                    m = self._function_model_from_node(
                        n,
                        source,
                        lang,
                        file_path,
                        class_name=type_name,
                        is_method=True,
                    )
                    if m is not None:
                        methods.append(m)
            for ch in n.children:
                walk(ch)

        walk(root)
        return methods

    def _function_model_from_node(
        self,
        node: Node,
        source: bytes,
        lang: LanguageConfig,
        file_path: str,
        *,
        class_name: str | None,
        is_method: bool,
    ) -> FunctionModel | None:
        if lang.name in {"c", "cpp"} and node.type == "template_declaration":
            for ch in node.named_children:
                if ch.type == "function_definition":
                    return self._function_model_from_node(
                        ch, source, lang, file_path, class_name=class_name, is_method=is_method
                    )
            return None
        name_node = node.child_by_field_name(lang.function_name_field)
        fname = ""
        if name_node is not None:
            if lang.name in {"c", "cpp"} and lang.function_name_field == "declarator":
                fname = _identifier_from_declarator(name_node, source)
            else:
                fname = _get_node_text(name_node, source).strip()
        if not fname and lang.name == "rust" and node.type == "closure_expression":
            fname = "<closure>"
        if not fname:
            return None
        params_node = node.child_by_field_name("parameters")
        if params_node is None:
            params_node = node.child_by_field_name("formal_parameters")
        parameters = _extract_parameters(params_node, source)
        ret: str | None = None
        if lang.name == "python":
            ret = _return_type_text(node, "return_type", source)
        elif lang.name in {"javascript", "typescript", "tsx"}:
            ret = _return_type_text(node, "return_type", source)
            if ret is None:
                ret = _return_type_text(node, "type", source)
        elif lang.name == "java":
            rt = node.child_by_field_name("type")
            ret = _get_node_text(rt, source).strip() if rt else None
        elif lang.name == "go":
            res = node.child_by_field_name("result")
            ret = _get_node_text(res, source).strip() if res else None
        elif lang.name == "rust":
            ret_node = node.child_by_field_name("return_type")
            ret = _get_node_text(ret_node, source).strip() if ret_node else None
        elif lang.name in {"c", "cpp"}:
            rt = node.child_by_field_name("type")
            ret = _get_node_text(rt, source).strip() if rt else None
        calls = _collect_calls_in(node, lang, source)
        qual = f"{class_name}.{fname}" if class_name else fname
        return FunctionModel(
            name=fname,
            qualified_name=qual,
            file_path=file_path,
            start_line=_node_line_1based(node),
            end_line=_node_end_line_1based(node),
            parameters=parameters,
            return_type=ret,
            is_method=is_method,
            class_name=class_name,
            calls=calls,
        )

    def _extract_functions(
        self,
        tree: Tree,
        source: bytes,
        file_path: str,
        lang: LanguageConfig,
    ) -> list[FunctionModel]:
        out: list[FunctionModel] = []

        def walk(n: Node) -> None:
            if n.type in lang.function_node_types and not _omit_file_level_function(n, lang):
                fm = self._function_model_from_node(
                    n, source, lang, file_path, class_name=None, is_method=False
                )
                if fm is not None:
                    out.append(fm)
            for ch in n.children:
                walk(ch)

        walk(tree.root_node)
        return out

    def _extract_imports(
        self,
        tree: Tree,
        source: bytes,
        file_path: str,
        lang: LanguageConfig,
    ) -> list[ImportModel]:
        ims: list[ImportModel] = []

        def add(im: ImportModel) -> None:
            ims.append(im)

        def walk(n: Node) -> None:
            if n.type not in lang.import_node_types:
                for ch in n.children:
                    walk(ch)
                return
            if lang.name == "python":
                self._imports_python(n, source, file_path, add)
            elif lang.name in {"javascript", "typescript", "tsx"}:
                self._imports_js_ts(n, source, file_path, add)
            elif lang.name == "java":
                self._imports_java(n, source, file_path, add)
            elif lang.name == "go":
                self._imports_go(n, source, file_path, add)
            elif lang.name == "rust":
                self._imports_rust(n, source, file_path, add)
            elif lang.name in {"c", "cpp"}:
                self._imports_c_cpp(n, source, file_path, add)
            for ch in n.children:
                walk(ch)

        walk(tree.root_node)
        return ims

    def _imports_python(
        self,
        n: Node,
        source: bytes,
        file_path: str,
        add: Callable[[ImportModel], None],
    ) -> None:
        if n.type == "import_from_statement":
            mod = n.child_by_field_name("module_name")
            module_path = _get_node_text(mod, source).strip() if mod else None
            for ch in n.named_children:
                if ch.type == "dotted_name" and mod is None:
                    module_path = _get_node_text(ch, source).strip()
                if ch.type == "wildcard_import":
                    add(
                        ImportModel(
                            source_file=file_path,
                            imported_name="*",
                            module_path=module_path,
                        )
                    )
                elif ch.type == "aliased_import":
                    name = ch.child_by_field_name("name")
                    alias = ch.child_by_field_name("alias")
                    add(
                        ImportModel(
                            source_file=file_path,
                            imported_name=_get_node_text(name, source).strip(),
                            module_path=module_path,
                            alias=_get_node_text(alias, source).strip() if alias else None,
                        )
                    )
        elif n.type == "import_statement":
            for ch in n.named_children:
                if ch.type == "dotted_name":
                    full = _get_node_text(ch, source).strip()
                    add(
                        ImportModel(
                            source_file=file_path,
                            imported_name=full.split(".")[-1],
                            module_path=full,
                        )
                    )
                elif ch.type == "dotted_as_names":
                    for imp in ch.named_children:
                        if imp.type != "aliased_import":
                            continue
                        name = imp.child_by_field_name("name")
                        alias = imp.child_by_field_name("alias")
                        full = _get_node_text(name, source).strip()
                        add(
                            ImportModel(
                                source_file=file_path,
                                imported_name=full.split(".")[-1],
                                module_path=full,
                                alias=_get_node_text(alias, source).strip() if alias else None,
                            )
                        )

    def _imports_js_ts(
        self,
        n: Node,
        source: bytes,
        file_path: str,
        add: Callable[[ImportModel], None],
    ) -> None:
        if n.type != "import_statement":
            return
        src = n.child_by_field_name("source")
        module_path = None
        if src is not None:
            module_path = _get_node_text(src, source).strip().strip("'\"")
        for ch in n.named_children:
            if ch.type == "import_clause":
                for sub in ch.named_children:
                    if sub.type == "identifier":
                        add(
                            ImportModel(
                                source_file=file_path,
                                imported_name=_get_node_text(sub, source).strip(),
                                module_path=module_path,
                            )
                        )
                    elif sub.type == "named_imports":
                        for spec in sub.named_children:
                            if spec.type != "import_specifier":
                                continue
                            name = spec.child_by_field_name("name")
                            alias = spec.child_by_field_name("alias")
                            add(
                                ImportModel(
                                    source_file=file_path,
                                    imported_name=_get_node_text(name, source).strip(),
                                    module_path=module_path,
                                    alias=_get_node_text(alias, source).strip() if alias else None,
                                )
                            )

    def _imports_java(
        self,
        n: Node,
        source: bytes,
        file_path: str,
        add: Callable[[ImportModel], None],
    ) -> None:
        if n.type == "package_declaration":
            scoped = n.child_by_field_name("name")
            if scoped is not None:
                add(
                    ImportModel(
                        source_file=file_path,
                        imported_name="<package>",
                        module_path=_get_node_text(scoped, source).strip(),
                    )
                )
        elif n.type == "import_declaration":
            decl = n.child_by_field_name("name")
            if decl is None:
                return
            text = _get_node_text(decl, source).strip()
            if text.endswith(".*"):
                add(
                    ImportModel(
                        source_file=file_path,
                        imported_name="*",
                        module_path=text[:-2],
                    )
                )
            else:
                add(
                    ImportModel(
                        source_file=file_path,
                        imported_name=text.split(".")[-1],
                        module_path=text,
                    )
                )

    def _imports_go(
        self,
        n: Node,
        source: bytes,
        file_path: str,
        add: Callable[[ImportModel], None],
    ) -> None:
        if n.type == "package_clause":
            pkg = n.child_by_field_name("name")
            if pkg is not None:
                add(
                    ImportModel(
                        source_file=file_path,
                        imported_name="<package>",
                        module_path=_get_node_text(pkg, source).strip(),
                    )
                )
        elif n.type == "import_declaration":
            for spec in n.named_children:
                if spec.type != "import_spec":
                    continue
                path = spec.child_by_field_name("path")
                alias = spec.child_by_field_name("name")
                mod = _get_node_text(path, source).strip().strip('"')
                if mod:
                    add(
                        ImportModel(
                            source_file=file_path,
                            imported_name=mod.split("/")[-1],
                            module_path=mod,
                            alias=_get_node_text(alias, source).strip() if alias else None,
                        )
                    )

    def _imports_rust(
        self,
        n: Node,
        source: bytes,
        file_path: str,
        add: Callable[[ImportModel], None],
    ) -> None:
        if n.type == "use_declaration":
            arg = n.child_by_field_name("argument")
            if arg is None:
                return
            add(
                ImportModel(
                    source_file=file_path,
                    imported_name=_get_node_text(arg, source).strip(),
                    module_path=None,
                )
            )
        elif n.type == "extern_crate_declaration":
            name = n.child_by_field_name("crate")
            alias = n.child_by_field_name("alias")
            if name is not None:
                add(
                    ImportModel(
                        source_file=file_path,
                        imported_name=_get_node_text(name, source).strip(),
                        module_path=None,
                        alias=_get_node_text(alias, source).strip() if alias else None,
                    )
                )

    def _imports_c_cpp(
        self,
        n: Node,
        source: bytes,
        file_path: str,
        add: Callable[[ImportModel], None],
    ) -> None:
        if n.type == "preproc_include":
            path_child = n.child_by_field_name("path")
            if path_child is None:
                return
            raw = _get_node_text(path_child, source).strip().strip("<>\"")
            if raw:
                add(ImportModel(source_file=file_path, imported_name=raw, module_path=raw))
