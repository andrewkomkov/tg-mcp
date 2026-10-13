"""The TDLib API schema for the installed TDLib build: lookup and validation.

TDLib ignores unknown request fields silently. Checking requests against the
schema of the exact commit in use turns API drift into a clear error.
"""

from __future__ import annotations

import ast
import difflib
import re
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .tdlib import TgError

_URL = "https://raw.githubusercontent.com/tdlib/td/{commit}/td/generate/scheme/td_api.tl"
_PRIMITIVES = {"int32", "int53", "int64", "string", "bytes", "Bool", "double"}
_META = {"@type", "@extra", "@client_id"}


@dataclass
class Def:
    name: str
    params: dict[str, str]
    result: str
    is_function: bool
    doc: str = ""
    param_docs: dict[str, str] = field(default_factory=dict)

    @property
    def signature(self) -> str:
        args = ", ".join(f"{k}: {v}" for k, v in self.params.items())
        return f"{self.name}({args}) -> {self.result}"


class Schema:
    def __init__(self, text: str):
        self.defs: dict[str, Def] = {}
        self.classes: dict[str, list[str]] = {}
        self.class_docs: dict[str, str] = {}
        self._parse(text)

    def _parse(self, text: str) -> None:
        is_function = False
        doc: list[str] = []
        for raw in text.splitlines():
            line = raw.strip()
            if line == "---functions---":
                is_function = True
            elif line.startswith("//@class"):
                if m := re.match(r"//@class (\w+) @description (.*)", line):
                    self.class_docs[m.group(1)] = m.group(2)
            elif line.startswith("//"):
                doc.append(line[2:].lstrip("-").strip())
            elif not line:
                doc = []
            elif m := re.fullmatch(r"(\w+)((?: \w+:[\w<>]+)*) = (\w+);", line):
                name, params, result = m.groups()
                d = Def(name, dict(p.split(":") for p in params.split()), result, is_function)
                self._attach_doc(d, " ".join(doc))
                doc = []
                # The first few definitions redeclare builtins (double, string, ...).
                if name in _PRIMITIVES or name.startswith("vector"):
                    continue
                self.defs[name] = d
                if not is_function:
                    self.classes.setdefault(result, []).append(name)

    @staticmethod
    def _attach_doc(d: Def, doc: str) -> None:
        for tag, body in re.findall(r"@(\w+) ((?:[^@]|@(?!\w+ ))*)", doc):
            if tag == "description":
                d.doc = body.strip()
            else:
                d.param_docs[tag.removeprefix("param_")] = body.strip()

    # -- lookup ------------------------------------------------------------

    def search(self, query: str, limit: int = 15, types: bool = False) -> list[dict]:
        words = [w.lower() for w in re.findall(r"\w+", query)]
        scored = []
        for d in self.defs.values():
            if d.is_function is types:
                continue
            name, doc = d.name.lower(), d.doc.lower()
            score = sum(3 * (w in name) + (w in doc) for w in words)
            if words and all(w in name or w in doc for w in words):
                scored.append((-score, len(d.name), d))
        scored.sort(key=lambda t: t[:2])
        return [{"name": d.name, "signature": d.signature, "description": d.doc}
                for _, _, d in scored[:limit]]

    def describe(self, name: str) -> dict:
        if name in self.classes and name not in self.defs:
            return {"name": name, "kind": "class",
                    "description": self.class_docs.get(name, ""),
                    "constructors": [self.defs[c].signature for c in self.classes[name]]}
        d = self.defs.get(name)
        if d is None:
            raise TgError(404, f"no such TDLib method or type: {name}{self._hint(name)}")
        return {"name": d.name, "kind": "method" if d.is_function else "type",
                "description": d.doc, "returns": d.result,
                "params": [{"name": k, "type": v, "description": d.param_docs.get(k, "")}
                           for k, v in d.params.items()]}

    def _hint(self, name: str, among: Any = None) -> str:
        close = difflib.get_close_matches(name, list(among or self.defs), n=4, cutoff=0.6)
        return f"; did you mean {', '.join(close)}?" if close else ""

    # -- validation --------------------------------------------------------

    def validate_call(self, method: str, params: dict) -> None:
        d = self.defs.get(method)
        if d is None or not d.is_function:
            raise TgError(400, f"no such TDLib method: {method}{self._hint(method)}")
        self._check_fields(d, params, method)

    def _check_fields(self, d: Def, obj: dict, path: str) -> None:
        for key, value in obj.items():
            if key in _META:
                continue
            if key not in d.params:
                raise TgError(400, f"{path}: {d.name} has no field '{key}'"
                                   f"{self._hint(key, d.params)}; fields: "
                                   f"{', '.join(d.params) or 'none'}")
            self._check_value(d.params[key], value, f"{path}.{key}")

    def _check_value(self, expected: str, value: Any, path: str) -> None:
        if m := re.fullmatch(r"vector<(.+)>", expected):
            if isinstance(value, list):
                for i, item in enumerate(value):
                    self._check_value(m.group(1), item, f"{path}[{i}]")
            return
        if expected in _PRIMITIVES or not isinstance(value, dict):
            return
        concrete = expected[0].islower()
        kind = value.get("@type", expected if concrete else None)
        options = [expected] if concrete else self.classes.get(expected, [])
        if kind is None:
            raise TgError(400, f"{path}: needs \"@type\", one of: {', '.join(options)}")
        if kind not in options:
            raise TgError(400, f"{path}: '{kind}' is not a {expected}"
                               f"{self._hint(kind, options)}; allowed: {', '.join(options)}")
        self._check_fields(self.defs[kind], value, path)

    def lint(self, source: str) -> list[str]:
        """Check the request literals in Python source against this schema."""
        problems = []
        for node in ast.walk(ast.parse(source)):
            name, keys = None, []
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "_req" and node.args
                    and isinstance(node.args[0], ast.Constant)):
                name = node.args[0].value
                keys = [k.arg for k in node.keywords if k.arg and k.arg != "timeout"]
                if name not in self.defs or not self.defs[name].is_function:
                    problems.append(f"line {node.lineno}: unknown method {name}")
                    continue
            elif isinstance(node, ast.Dict):
                pairs = {k.value: v for k, v in zip(node.keys, node.values)
                         if isinstance(k, ast.Constant)}
                kind = pairs.get("@type")
                if not isinstance(kind, ast.Constant):
                    continue
                name, keys = kind.value, [k for k in pairs if k != "@type"]
                if name not in self.defs:
                    problems.append(f"line {node.lineno}: unknown type {name}")
                    continue
            if name:
                problems += [f"line {node.lineno}: {name} has no field '{k}'"
                             for k in keys if k not in self.defs[name].params]
        return problems


def load(commit: str, cache_dir: Path) -> Schema | None:
    """Schema for a TDLib commit, from the cache or GitHub; None when unreachable."""
    if not re.fullmatch(r"[0-9a-f]{40}", commit or ""):
        return None
    cached = cache_dir / f"td_api-{commit}.tl"
    try:
        text = cached.read_text()
    except OSError:
        try:
            with urllib.request.urlopen(_URL.format(commit=commit), timeout=15) as r:
                text = r.read().decode()
        except OSError:
            return None
        cache_dir.mkdir(parents=True, exist_ok=True)
        cached.write_text(text)
    return Schema(text)
