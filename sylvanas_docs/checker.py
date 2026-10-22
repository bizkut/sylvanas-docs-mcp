"""Static scan of Lua plugin code: API references, require aliases and locally defined names."""

from __future__ import annotations

from dataclasses import dataclass, field
import re

from .index import RetrievalError

MAX_CODE_CHARS = 100_000
NAME = r"[A-Za-z_][A-Za-z_0-9]*"
# require() paths whose returned table is a class named differently from the file.
REQUIRE_OWNERS = {"common/izi_sdk": "izi", "common/geometry/vector_2": "vec2", "common/geometry/vector_3": "vec3"}
LUA_BUILTINS = {"_G", "_ENV", "bit", "bit32", "coroutine", "debug", "io", "math", "os", "package", "self", "string", "table", "utf8"}
LONG_BRACKET = re.compile(r"\[(=*)\[")
CHAIN = re.compile(r"(?<![\w.:\])])(" + NAME + r")((?:[ \t]*[.:][ \t]*" + NAME + r")+)")
REQUIRE = re.compile(r"\blocal\s+(" + NAME + r")\s*=\s*require\s*\(?\s*[\"']([^\"'\n]+)[\"']")
ALIAS = re.compile(r"(?m)^[ \t]*local[ \t]+(" + NAME + r")[ \t]*=[ \t]*(" + NAME + r"(?:[ \t]*\.[ \t]*" + NAME + r")+)[ \t;]*$")
LOCALS = re.compile(r"\blocal\s+(?:function\s+(" + NAME + r")|(" + NAME + r"(?:\s*,\s*" + NAME + r")*))")
PARAMETERS = re.compile(r"\bfunction\b[^(\n]*\(([^)]*)\)")
FOR_NAMES = re.compile(r"\bfor\s+(" + NAME + r"(?:\s*,\s*" + NAME + r")*)\s*(?:=|\bin\b)")
DEFINED = re.compile(r"\bfunction\s+" + NAME + r"(?:\s*[.:]\s*" + NAME + r")*\s*[.:]\s*(" + NAME + r")\s*\(|(?<![\w.])(" + NAME + r")\s*=\s*function\b")


@dataclass
class Reference:
    line: int
    expression: str
    parts: list[str]
    method_at: int | None  # index of the first part reached through ':'
    is_call: bool


@dataclass
class Scan:
    requires: dict[str, str] = field(default_factory=dict)
    aliases: dict[str, list[str]] = field(default_factory=dict)
    locals: set[str] = field(default_factory=set)
    defined: set[str] = field(default_factory=set)
    references: list[Reference] = field(default_factory=list)


def blank(text: str) -> str:
    return re.sub(r"[^\n]", " ", text)


def strip_lua(code: str) -> str:
    """Replace comments and string literals with spaces, keeping line numbers intact."""
    out, i, n = [], 0, len(code)
    while i < n:
        if code.startswith("--", i):
            bracket = LONG_BRACKET.match(code, i + 2)
            if bracket:
                end = code.find("]" + bracket.group(1) + "]", bracket.end())
                end = n if end < 0 else end + len(bracket.group(1)) + 2
            else:
                end = code.find("\n", i)
                end = n if end < 0 else end
            out.append(blank(code[i:end]))
            i = end
            continue
        bracket = LONG_BRACKET.match(code, i)
        if bracket:
            end = code.find("]" + bracket.group(1) + "]", bracket.end())
            end = n if end < 0 else end + len(bracket.group(1)) + 2
            out.append(blank(code[i:end]))
            i = end
            continue
        if code[i] in "\"'":
            quote, end = code[i], i + 1
            while end < n and code[end] not in (quote, "\n"):
                end += 2 if code[end] == "\\" else 1
            end = min(end + 1, n)
            out.append(blank(code[i:end]))
            i = end
            continue
        out.append(code[i])
        i += 1
    return "".join(out)


def names(group: str | None) -> list[str]:
    return [item.strip() for item in (group or "").split(",") if item.strip() and item.strip() != "..."]


def scan(code: str) -> Scan:
    if not isinstance(code, str) or not code.strip() or len(code) > MAX_CODE_CHARS:
        raise RetrievalError("code must contain 1 to 100000 characters of Lua")
    result = Scan()
    for match in REQUIRE.finditer(code):
        result.requires[match.group(1)] = match.group(2).strip()
    stripped = strip_lua(code)
    for match in ALIAS.finditer(stripped):
        result.aliases[match.group(1)] = re.split(r"\s*\.\s*", match.group(2).strip())
    for match in LOCALS.finditer(stripped):
        result.locals.update(names(match.group(1)) + names(match.group(2)))
    for pattern in (PARAMETERS, FOR_NAMES):
        for match in pattern.finditer(stripped):
            result.locals.update(names(match.group(1)))
    for match in DEFINED.finditer(stripped):
        result.defined.add((match.group(1) or match.group(2)).casefold())
    for match in CHAIN.finditer(stripped):
        before = stripped[max(0, match.start() - 12):match.start()]
        after = stripped[match.end():match.end() + 3]
        # Declarations (`function a.b(`) and assignment targets (`a.b = `) define names, not use them.
        if re.search(r"\bfunction\s*$", before) or re.match(r"\s*=(?!=)", after):
            continue
        tokens = re.findall(r"[.:]|" + NAME, match.group(0))
        parts = [token for token in tokens if token not in ".:"]
        separators = [token for token in tokens if token in ".:"]
        method_at = next((index + 1 for index, separator in enumerate(separators) if separator == ":"), None)
        result.references.append(Reference(
            line=stripped.count("\n", 0, match.start()) + 1,
            expression=re.sub(r"\s+", "", match.group(0)),
            parts=parts,
            method_at=method_at,
            is_call=bool(re.match(r"\s*[({]", stripped[match.end():match.end() + 64])),
        ))
    return result


def is_sdk_module(path: str) -> bool:
    """SDK modules live under common/; anything else is one of the plugin's own files."""
    return path.startswith("common/")


def require_owner(path: str) -> str:
    return REQUIRE_OWNERS.get(path, path.rsplit("/", 1)[-1]).casefold()


def used_names(code: str, globals_: set[str]) -> set[tuple[str, str]]:
    """API names an example uses: ("member", qualified name) or ("method", leaf) for
    method calls on receivers whose type is unknown."""
    try:
        scanned = scan(code)
    except RetrievalError:
        return set()
    found = set()
    for reference in scanned.references:
        parts = [part.casefold() for part in reference.parts]
        root = reference.parts[0]
        if root in scanned.requires:
            if not is_sdk_module(scanned.requires[root]):
                continue
            qualified = [require_owner(scanned.requires[root]), *parts[1:]]
        elif root in scanned.aliases:
            qualified = [part.casefold() for part in scanned.aliases[root]] + parts[1:]
        elif root in globals_ and root not in scanned.locals:
            qualified = parts
        else:
            if reference.method_at is not None:
                found.add(("method", parts[reference.method_at]))
            continue
        # Every prefix counts: a.b.c also shows that a.b exists.
        found.update(("member", ".".join(qualified[:length])) for length in range(2, len(qualified) + 1))
    return found
