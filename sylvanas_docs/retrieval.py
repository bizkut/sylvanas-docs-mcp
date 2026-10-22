"""Bounded retrieval against a read-only SQLite snapshot; no network access."""

from __future__ import annotations

from contextlib import closing
import difflib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any
from urllib.parse import unquote, urlsplit

from .checker import is_sdk_module, require_owner, scan
from .index import SCHEMA_VERSION, RetrievalError, canonical_url, digest, normalized_symbol, safe_read

MAX_RESULTS = 20
MAX_RESULT_CHARS = 6_000
MAX_READ_CHARS = 32_000
MAX_READ_LINES = 400
FAMILIES = {"izi", "legacy", "general"}
DOCUMENT_TYPES = {"docs", "stub", "example"}
MAX_MEMBERS = 300
MAX_ISSUES = 50
# Question filler dropped from queries so that natural-language questions still match.
STOPWORDS = {"a", "an", "and", "are", "at", "be", "by", "can", "do", "does", "for", "from", "how", "i",
             "in", "is", "it", "me", "my", "of", "on", "or", "should", "the", "to", "what", "when",
             "where", "which", "with"}


def validate_limit(limit: int) -> None:
    if type(limit) is not int or not 1 <= limit <= MAX_RESULTS:
        raise RetrievalError("limit must be an integer between 1 and 20")


def query_expression(query: str, operator: str = "AND") -> str:
    if not isinstance(query, str) or not query.strip() or len(query) > 512:
        raise RetrievalError("query must contain 1 to 512 characters")
    tokens = re.findall(r"\w+", query, flags=re.UNICODE)
    if not tokens or len(tokens) > 32:
        raise RetrievalError("query must contain 1 to 32 words or identifier components")
    tokens = [token for token in tokens if token.casefold() not in STOPWORDS] or tokens
    # Treat input as literal words, never as user-controlled FTS or SQL syntax.
    return f" {operator} ".join('"' + token.replace('"', '""') + '"' for token in tokens)


def bounded_content(payload: dict) -> dict:
    payload = dict(payload)
    content = payload["content"]
    payload["content_char_count"] = len(content)
    payload["content_truncated"] = len(content) > MAX_RESULT_CHARS
    payload["content"] = content[:MAX_RESULT_CHARS]
    # content_sha256 always identifies the complete indexed record, not the preview.
    return payload


def line_window(content: str, start_line: int, end_line: int | None) -> dict:
    lines = content.splitlines(keepends=True)
    if type(start_line) is not int or start_line < 1 or start_line > max(1, len(lines)):
        raise RetrievalError("start_line must address an existing line (1-based)")
    if end_line is not None and (type(end_line) is not int or end_line < start_line or end_line - start_line + 1 > MAX_READ_LINES):
        raise RetrievalError("end_line must be at or after start_line, spanning at most 400 lines")
    stop = min(end_line if end_line is not None else start_line + 199, len(lines))
    selected = []
    characters = 0
    for line in lines[start_line - 1:stop]:
        if characters + len(line) > MAX_READ_CHARS:
            if not selected:
                raise RetrievalError("One line exceeds the 32000-character response bound; use search_docs for an indexed preview")
            break
        selected.append(line)
        characters += len(line)
    last = start_line + len(selected) - 1
    text = "".join(selected)
    return {"content": text, "returned_content_sha256": digest(text.encode()), "start_line": start_line, "end_line": last, "total_lines": len(lines), "content_truncated": last < len(lines), "next_line": last + 1 if last < len(lines) else None}


class Corpus:
    def __init__(self, database: Path, source_root: Path | None = None):
        self.database = database.resolve(strict=True)
        with closing(self.connect()) as db:
            row = db.execute("SELECT value FROM metadata WHERE key='summary'").fetchone()
            if row is None:
                raise RetrievalError("Missing index metadata; run build-index")
            self.summary = json.loads(row[0])
        if self.summary.get("schema_version") != SCHEMA_VERSION:
            raise RetrievalError("Unsupported index schema; rebuild the index")
        self.source_root = (source_root or Path(self.summary["source_root"])).resolve()

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        return db

    def _filters(self, api_family: str | None, document_type: str | None, examples: bool) -> tuple[str, list]:
        if api_family is not None and api_family not in FAMILIES:
            raise RetrievalError("api_family must be izi, legacy, or general")
        if document_type is not None and document_type not in DOCUMENT_TYPES:
            raise RetrievalError("document_type must be docs, stub, or example")
        clauses, parameters = [], []
        if api_family is not None:
            clauses.append("r.family=?")
            parameters.append(api_family)
        if document_type is not None:
            clauses.append("r.kind=?")
            parameters.append(document_type)
        if examples:
            clauses.append("r.example=1")
        return "".join(" AND " + item for item in clauses), parameters

    def search_docs(self, query: str, api_family: str | None = None,
                    document_type: str | None = None, limit: int = 5) -> dict[str, Any]:
        """Search docs, Lua stubs and examples; exact identifier definitions rank first.

        Family: izi/legacy/general. Type: docs/stub/example. Literal words (question
        filler dropped) are combined with AND; if that yields fewer than limit results,
        records matching any word fill the rest as match_type "partial". No FTS operators. Results include capped content and provenance.
        """
        return self._search(query, api_family, document_type, limit, False)

    def _search(self, query: str, api_family: str | None, document_type: str | None,
                limit: int, examples: bool, lexical_only: bool = False) -> dict:
        validate_limit(limit)
        expression = query_expression(query)
        filters, parameters = self._filters(api_family, document_type, examples)
        if lexical_only:
            filters += " AND NOT EXISTS (SELECT 1 FROM symbols s WHERE s.record_id=r.id AND s.name=?)"
            parameters.append(normalized_symbol(query))
        sql = """SELECT r.id, r.payload, bm25(search, 3.0, 5.0, 1.0) AS score,
                 (SELECT min(quality) FROM symbols s WHERE s.record_id=r.id AND s.name=?) AS exact_quality
                 FROM search JOIN records r ON r.id=search.rowid
                 WHERE search MATCH ?""" + filters + " ORDER BY coalesce(exact_quality,2), score, r.id LIMIT ?"
        with closing(self.connect()) as db:
            rows = db.execute(sql, [normalized_symbol(query), expression, *parameters, limit + 1]).fetchall()
            matched = {row["id"] for row in rows}
            partial = []
            if len(rows) <= limit:
                # Too few records contain every word: top up with records matching any of them.
                # bm25 favours records that match more of the words.
                any_expression = query_expression(query, "OR")
                if any_expression != expression:
                    partial = [row for row in db.execute(sql, [normalized_symbol(query), any_expression, *parameters, limit + 1]).fetchall() if row["id"] not in matched]
        rows = [(row, False) for row in rows] + [(row, True) for row in partial]
        results = []
        for row, is_partial in rows[:limit]:
            result = bounded_content(json.loads(row["payload"]))
            result["match_type"] = "exact_symbol" if row["exact_quality"] == 0 else "symbol_suffix" if row["exact_quality"] == 1 else "partial" if is_partial else "lexical"
            result["bm25"] = row["score"]
            results.append(result)
        return {"query": query, "api_family": api_family, "document_type": document_type, "results": results, "has_more": len(rows) > limit, "limit": limit}

    def lookup_symbol(self, name: str, limit: int = 10) -> dict[str, Any]:
        """Find exact Lua/API definitions (dot and colon equivalent), then lexical mentions.

        Qualified definitions outrank leaf-name aliases. Docs and source stubs
        remain separate evidence, not merged signatures. limit: 1..20.
        """
        validate_limit(limit)
        if not isinstance(name, str) or len(name) > 256 or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*(?:[.:][A-Za-z_][A-Za-z_0-9]*)*(?:\(\))?", name.strip().strip("`")):
            raise RetrievalError("name must be a Lua identifier, optionally qualified with dots or colons")
        normalized = normalized_symbol(name)
        with closing(self.connect()) as db:
            rows = db.execute("SELECT r.payload,s.quality FROM symbols s JOIN records r ON r.id=s.record_id WHERE s.name=? ORDER BY s.quality, CASE r.kind WHEN 'docs' THEN 0 WHEN 'stub' THEN 1 ELSE 2 END, r.id LIMIT ?", (normalized, limit + 1)).fetchall()
        results = []
        for row in rows[:limit]:
            result = bounded_content(json.loads(row["payload"]))
            result["match_type"] = "exact_symbol" if row["quality"] == 0 else "symbol_suffix"
            results.append(result)
        mentions = self._search(name, None, None, limit, False, lexical_only=True)["results"]
        return {"name": name, "normalized_name": normalized, "definitions": results, "mentions": mentions, "has_more_definitions": len(rows) > limit, "limit": limit, "note": "Documentation and Lua stubs are independently attributed; differences are not resolved as runtime truth."}

    def search_examples(self, query: str, api_family: str | None = None, limit: int = 5) -> dict[str, Any]:
        """Search example pages, documentation code blocks and Lua example projects.

        Excludes source stubs. api_family: izi/legacy/general; limit: 1..20.
        """
        return self._search(query, api_family, None, limit, True)

    def get_section(self, url: str, heading: str | None = None,
                    start_line: int = 1, end_line: int | None = None) -> dict[str, Any]:
        """Read an indexed page or heading subtree in original Markdown order.

        heading accepts an exact title, a full ' > ' path, or an anchor slug.
        URL fragments select headings when heading is omitted. Reads default to
        200 lines (maximum 400/32000 characters); follow next_line to continue.
        """
        if not isinstance(url, str) or len(url) > 2048:
            raise RetrievalError("url must be an indexed documentation URL of at most 2048 characters")
        parsed = urlsplit(url)
        selector = heading if heading is not None else unquote(parsed.fragment) or None
        with closing(self.connect()) as db:
            page = db.execute("SELECT payload,content FROM pages WHERE url=?", (canonical_url(url),)).fetchone()
            if page is None:
                raise RetrievalError("URL is not in the documentation index; use search_docs to find an indexed URL")
            available = [json.loads(row[0]) for row in db.execute("SELECT payload FROM sections WHERE url=? ORDER BY ordinal", (canonical_url(url),))]
        metadata = json.loads(page["payload"])
        content = page["content"]
        chosen_path = [metadata["title"]]
        chosen_slug = None
        if selector is not None:
            if not isinstance(selector, str) or not selector.strip() or len(selector) > 512:
                raise RetrievalError("heading must contain 1 to 512 characters")
            key = selector.strip().lstrip("#").strip().strip("`").casefold()
            # A #slug/URL fragment is explicit. Repeated bare titles must not
            # silently pick a different section from the caller's intended one.
            explicit_slug = selector.strip().startswith("#") or (heading is None and bool(parsed.fragment))
            matches = [] if explicit_slug else [item for item in available if item["heading"].strip("`").casefold() == key or " > ".join(item["heading_path"]).casefold() == key]
            if not matches:
                matches = [item for item in available if item["slug"] == key]
            if len(matches) != 1:
                candidates = [" > ".join(item["heading_path"]) + " (#" + item["slug"] + ")" for item in (matches or available)][:20]
                raise RetrievalError(("Ambiguous heading" if matches else "Heading not found") + "; available selectors: " + "; ".join(candidates))
            content = matches[0]["content"]
            chosen_path, chosen_slug = matches[0]["heading_path"], matches[0]["slug"]
        return {**metadata, "heading_path": chosen_path, "heading_slug": chosen_slug, "context": " > ".join(chosen_path), "content_sha256": digest(content.encode()), **line_window(content, start_line, end_line)}

    def read_api_source(self, path: str, start_line: int = 1, end_line: int | None = None) -> dict[str, Any]:
        """Read an indexed Lua file relative to the downloaded source checkout only.

        Absolute paths, traversal and symlinks are rejected. Indexed file hashes
        must still match. Default 200 lines, maximum 400 lines/32000 characters.
        """
        if not isinstance(path, str) or not path or len(path) > 1024 or "\\" in path or "\x00" in path or path.startswith("/") or any(part in {"", ".", ".."} for part in path.split("/")):
            raise RetrievalError("path must be a relative indexed Lua path without traversal")
        with closing(self.connect()) as db:
            row = db.execute("SELECT sha256,payload FROM sources WHERE path=?", (path,)).fetchone()
        if row is None:
            raise RetrievalError("Path is not an indexed Lua source; use lookup_symbol to discover source paths")
        data = safe_read(self.source_root, path)
        if digest(data) != row["sha256"]:
            raise RetrievalError("Source changed since indexing; rebuild the index before reading this file")
        metadata = json.loads(row["payload"])
        return {**metadata, "heading_path": [path], "context": path, **line_window(data.decode("utf-8-sig"), start_line, end_line)}

    def _api(self) -> dict:
        if not hasattr(self, "_api_cache"):
            with closing(self.connect()) as db:
                rows = db.execute("SELECT name, display, owner, leaf, kind, origin FROM members").fetchall()
                used = db.execute("SELECT kind, name FROM example_uses").fetchall()
            api = {"names": {}, "owners": {}, "leaves": {}, "module_classes": {}, "used": {(row["kind"], row["name"]) for row in used}}
            for row in rows:
                api["names"].setdefault(row["name"], row["display"])
                if row["owner"]:
                    api["owners"].setdefault(row["owner"], set()).add(row["leaf"])
                if row["kind"] != "class":
                    api["leaves"].setdefault(row["leaf"], set()).add(row["owner"])
                path = json.loads(row["origin"]).get("path")
                if row["kind"] == "class" and path:
                    api["module_classes"].setdefault(path.removesuffix(".lua"), set()).add(row["name"])
            self._api_cache = api
        return self._api_cache

    def _member_details(self, names: list[str]) -> list[dict]:
        if not names:
            return []
        with closing(self.connect()) as db:
            rows = db.execute(f"SELECT name, display, kind, signature, summary, origin FROM members WHERE name IN ({','.join('?' * len(names))}) ORDER BY CASE kind WHEN 'documented' THEN 1 ELSE 0 END", names).fetchall()
        grouped: dict[str, dict] = {}
        for row in rows:
            item = grouped.setdefault(row["name"], {"name": row["display"], "kind": row["kind"], "summary": "", "signatures": []})
            item["summary"] = item["summary"] or row["summary"]
            item["signatures"].append({"signature": row["signature"], "source": row["kind"], **json.loads(row["origin"])})
        return [grouped[name] for name in names if name in grouped]

    def list_members(self, namespace: str, limit: int = 100) -> dict[str, Any]:
        """List the functions, fields and nested namespaces under a class or namespace.

        Examples: core.graphics, game_object, izi, buff_manager, vec3. Each member has
        its stub/doc signatures and a one-line summary. limit: 1..300 members.
        """
        if type(limit) is not int or not 1 <= limit <= MAX_MEMBERS:
            raise RetrievalError("limit must be an integer between 1 and 300")
        if not isinstance(namespace, str) or len(namespace) > 256 or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*(?:[.:][A-Za-z_][A-Za-z_0-9]*)*", namespace.strip()):
            raise RetrievalError("namespace must be a Lua identifier, optionally qualified with dots or colons")
        api = self._api()
        owner = normalized_symbol(namespace)
        leaves = sorted(api["owners"].get(owner, ()))
        children = sorted({name[len(owner) + 1:].split(".", 1)[0] for name in api["owners"] if name.startswith(owner + ".")})
        if not leaves and not children:
            close = difflib.get_close_matches(owner, list(api["owners"]), n=8, cutoff=0.6)
            raise RetrievalError("Unknown namespace; similar namespaces: " + (", ".join(close) or "none") + ". Use lookup_symbol for a single name.")
        members = self._member_details([owner + "." + leaf for leaf in leaves[:limit]])
        return {"namespace": namespace, "member_count": len(leaves), "members": members, "child_namespaces": [owner + "." + child for child in children], "has_more": len(leaves) > limit, "limit": limit}

    def _resolve(self, parts: list[str], scanned) -> tuple[str, list[str] | None, dict]:
        """Map a reference to API name candidates: (status, candidates, context)."""
        api = self._api()
        root = parts[0]
        if root in scanned.requires:
            path = scanned.requires[root]
            if not is_sdk_module(path):
                return "local_module", None, {"require": path}
            owner = require_owner(path)
            if owner in api["owners"]:
                return "resolved", [owner, *parts[1:]], {"require": path}
            classes = api["module_classes"].get("legacy/_api/" + path, set())
            if classes:
                # Modules such as common/enums return a table of top-level classes.
                return "module", parts[1:], {"require": path, "classes": classes}
            return "unresolved", None, {"require": path}
        if root in scanned.aliases:
            return self._resolve([*scanned.aliases[root], *parts[1:]], scanned)
        if root in self.summary.get("globals", []) and root not in scanned.locals:
            return "resolved", [part.casefold() for part in parts], {}
        return "unresolved", None, {}

    def check_lua_api(self, code: str) -> dict[str, Any]:
        """Check Lua code for Sylvanas API names that do not exist; run before finalizing code.

        Follows require() aliases, `local x = core.graphics` aliases and the core global.
        Method calls on unknown receivers (`me:get_buffs()`) are checked by method name
        against every class. Issues include close-match suggestions with signatures.
        Static and best-effort: it cannot infer types returned from calls.
        """
        scanned = scan(code)
        api = self._api()
        issues, verified, unverifiable = [], {}, 0
        seen_issues = set()
        for reference in scanned.references:
            if reference.parts[0] in {"_G", "self"} or (reference.parts[0] in scanned.locals and reference.parts[0] not in scanned.requires and reference.parts[0] not in scanned.aliases and reference.method_at is None):
                unverifiable += 1
                continue
            status, candidate, context = self._resolve(reference.parts, scanned)
            issue = None
            if status == "local_module":
                unverifiable += 1
                continue
            if status in {"resolved", "module"}:
                lowered = [part.casefold() for part in candidate]
                known = 0
                for length in range(len(lowered), 0, -1):
                    prefix = ".".join(lowered[:length])
                    if prefix in api["names"] or prefix in api["owners"]:
                        known = length
                        break
                if status == "module" and lowered[0] not in context["classes"]:
                    known = 0  # only the module's own top-level classes count
                if known == len(lowered) or (known and ".".join(lowered[:known]) not in api["owners"]):
                    # A known function/field ends what can be checked statically.
                    name = ".".join(lowered[:known])
                    verified[name] = verified.get(name, 0) + 1
                    continue
                namespace = ".".join(lowered[:known])
                missing = lowered[known] if known < len(lowered) else lowered[-1]
                pool = sorted(api["owners"].get(namespace, ())) if known else sorted(context.get("classes", ()))
                close = difflib.get_close_matches(missing, pool, n=5, cutoff=0.5)
                suggestions = [(namespace + "." + item) if namespace else item for item in close]
                where = f"`{namespace}`" if namespace else f"module {context.get('require')!r}"
                if ("member", ".".join(lowered[:known + 1])) in api["used"]:
                    issue = {"kind": "undocumented", "severity": "warning", "message": f"`{missing}` is not documented under {where}, but official examples use it", "suggestions": suggestions}
                else:
                    issue = {"kind": "unknown_member", "severity": "error", "message": f"`{missing}` is not a member of {where}", "suggestions": suggestions}
            elif reference.method_at is not None:
                method = reference.parts[reference.method_at].casefold()
                if method in api["leaves"] or method in scanned.defined:
                    unverifiable += 1
                    continue
                close = difflib.get_close_matches(method, list(api["leaves"]), n=5, cutoff=0.6)
                suggestions = [sorted(owner + "." + item for owner in api["leaves"][item] if owner)[:1] or [item] for item in close]
                if ("method", method) in api["used"]:
                    issue = {"kind": "undocumented", "severity": "warning", "message": f"No documented class has a method `{method}`, but official examples call it", "suggestions": [item[0] for item in suggestions]}
                else:
                    issue = {"kind": "unknown_method", "severity": "error", "message": f"No Sylvanas class or documented API has a method `{method}`, and the code does not define it", "suggestions": [item[0] for item in suggestions]}
            else:
                unverifiable += 1
                continue
            key = (reference.expression, issue["message"])
            if key in seen_issues:
                continue
            seen_issues.add(key)
            if len(issues) < MAX_ISSUES:
                issues.append({"line": reference.line, "expression": reference.expression, **issue, "suggestion_details": self._member_details(issue["suggestions"][:3])})
        return {
            "ok": not any(item["severity"] == "error" for item in issues),
            "error_count": sum(item["severity"] == "error" for item in issues),
            "warning_count": sum(item["severity"] == "warning" for item in issues),
            "issues": issues,
            "issue_count": len(seen_issues),
            "verified": [{"name": api["names"].get(name, name), "uses": count} for name, count in sorted(verified.items())],
            "unverifiable_reference_count": unverifiable,
            "requires": scanned.requires,
            "note": "Static check against indexed stubs and docs. It catches misspelled or invented names, not wrong argument types or values returned at runtime.",
        }

    def corpus_status(self) -> dict[str, Any]:
        """Return snapshot provenance, counts, filter values and response limits.

        Counts describe the indexed snapshot, not an automatic live revalidation.
        """
        return {**self.summary, "read_only": True, "transport": "stdio", "api_families": sorted(FAMILIES), "document_types": sorted(DOCUMENT_TYPES), "limits": {"max_results": MAX_RESULTS, "result_content_characters": MAX_RESULT_CHARS, "read_lines": MAX_READ_LINES, "read_characters": MAX_READ_CHARS}, "freshness": "Indexed snapshot; rebuild after refreshing docs or source. Source reads check current file hashes.", "revision_semantics": "Git HEAD identifies the checkout; per-file SHA-256 identifies indexed bytes, including any local edits."}
