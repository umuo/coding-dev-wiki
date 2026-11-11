"""通用文件快照及 Tree-sitter / Python AST 符号定位。"""
import ast
import hashlib
import importlib
import re
from pathlib import Path
from tree_sitter import Language, Parser
from .. import config
from ..importing.sources import SKIP_DIRS

LANGUAGES = {".py": "python", ".js": "javascript", ".jsx": "javascript", ".ts": "typescript",
             ".tsx": "tsx", ".java": "java", ".json": "json", ".md": "markdown", ".yml": "yaml",
             ".yaml": "yaml", ".toml": "toml", ".xml": "xml", ".html": "html", ".css": "css",
             ".go": "go", ".rs": "rust", ".sh": "shell", ".sql": "sql", ".txt": "text", ".kt": "kotlin"}
TEXT_NAMES = {"readme": "markdown", "license": "text", "dockerfile": "dockerfile", "makefile": "makefile", ".gitignore": "text", ".dockerignore": "text"}
SECRET = re.compile(r"(^\.env($|\.)|\.(pem|key|p12|pfx)$|^(credentials|secrets)(\.|$))", re.I)


def extract_symbols(content, lang):
    if lang == "python":
        try:
            root = ast.parse(content)
        except SyntaxError:
            return []
        output = []
        def walk(node, prefix=""):
            for item in ast.iter_child_nodes(node):
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    name = prefix + item.name
                    output.append({"name": name, "short": item.name,
                                   "kind": "class" if isinstance(item, ast.ClassDef) else "function",
                                   "line": item.lineno, "end_line": item.end_lineno,
                                   "doc": (ast.get_docstring(item) or "")[:400]})
                    walk(item, name + ".")
                else:
                    walk(item, prefix)
        walk(root)
        return output
    if lang not in {"javascript", "typescript", "tsx", "java"}:
        return []
    if lang in {"typescript", "tsx"}:
        module = importlib.import_module("tree_sitter_typescript")
        language = module.language_tsx() if lang == "tsx" else module.language_typescript()
    else:
        module = importlib.import_module("tree_sitter_" + lang)
        language = module.language()
    parser = Parser(Language(language))
    source = content.encode()
    root = parser.parse(source).root_node
    kinds = {"function_declaration", "method_definition", "method_declaration", "class_declaration",
             "interface_declaration", "enum_declaration", "record_declaration", "constructor_declaration",
             "variable_declarator"}
    output = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type in kinds:
            name = node.child_by_field_name("name")
            value = node.child_by_field_name("value")
            if node.type != "variable_declarator" or (value and value.type in {"arrow_function", "function_expression"}):
                if name:
                    label = source[name.start_byte:name.end_byte].decode()
                    output.append({"name": label, "short": label, "kind": node.type.replace("_declaration", ""),
                                   "line": node.start_point.row + 1, "end_line": node.end_point.row + 1, "doc": ""})
        stack.extend(reversed(node.children))
    return output


def checked_paths(root, check):
    for path in root.rglob("*"):
        check()
        yield path


def build_index(root, log, check=lambda: None):
    files = {}
    skipped = 0
    digest = hashlib.sha256()
    total = 0
    for path in sorted(checked_paths(root, check)):
        check()
        rel = path.relative_to(root)
        if path.is_symlink() or not path.is_file() or any(part in SKIP_DIRS for part in rel.parts):
            continue
        if (SECRET.search(path.name) or (path.suffix.lower() not in LANGUAGES and path.name.lower() not in TEXT_NAMES)
                or path.name.endswith("lock.json")):
            skipped += 1
            continue
        if path.stat().st_size > config.MAX_FILE_BYTES:
            skipped += 1
            continue
        data = path.read_bytes()
        if b"\x00" in data:
            skipped += 1
            continue
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError:
            skipped += 1
            continue
        if not content.strip():
            continue
        if len(files) >= config.MAX_FILES or total + len(data) > 40 * 1024 * 1024:
            raise ValueError("可分析源码超过 2000 文件或 40 MiB，请缩小项目范围")
        key = rel.as_posix()
        lang = LANGUAGES.get(path.suffix.lower(), TEXT_NAMES.get(path.name.lower(), "text"))
        symbols = extract_symbols(content, lang)
        files[key] = {"path": key, "name": path.name, "lang": lang, "content": content,
                      "lines": len(content.splitlines()), "size": len(data), "symbols": symbols}
        digest.update(key.encode() + b"\0" + data)
        total += len(data)
    if not files:
        raise ValueError("未发现可分析的 UTF-8 源码或文档文件")
    log(f"已索引 {len(files)} 个文件；跳过 {skipped} 个不支持、敏感、二进制或超大文件")
    return files, digest.hexdigest(), skipped
