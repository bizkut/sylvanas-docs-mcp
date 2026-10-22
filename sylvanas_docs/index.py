"""Build an atomic SQLite FTS5 snapshot of documentation and Lua sources."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import subprocess
import tempfile
from urllib.parse import urlsplit, urlunsplit

from bs4 import BeautifulSoup

SCHEMA_VERSION = 1
MAX_FILE_BYTES = 8 * 1024 * 1024
IDENTIFIER = r"[A-Za-z_][A-Za-z_0-9]*(?:[.:][A-Za-z_][A-Za-z_0-9]*)*"
SYMBOL_RE = re.compile(IDENTIFIER)


class RetrievalError(ValueError):
    """An actionable corpus or request error, safe to return to a client."""


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalized_symbol(value: str) -> str:
    return value.strip().strip("`").removesuffix("()").replace(":", ".").casefold()


def canonical_url(value: str) -> str:
    parsed = urlsplit(value)
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), "", ""))


def safe_read(root: Path, relative: str) -> bytes:
    """Read only regular files beneath root, without following any symlink component.

    Directory descriptors make this safe even if a component is swapped during
    traversal. The configured root is trusted; every child component is not.
    """
    parts = PurePosixPath(relative).parts
    if not relative or "\\" in relative or "\x00" in relative or relative.startswith("/") or any(p in {".", ".."} for p in relative.split("/")):
        raise RetrievalError("Path must be relative to the source root, without traversal or backslashes")
    if not parts:
        raise RetrievalError("A file path is required")
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        file_descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        with os.fdopen(file_descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
                raise RetrievalError("Source must be a regular file no larger than 8 MiB")
            data = stream.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES:
                raise RetrievalError("Source exceeds the 8 MiB read bound")
            return data
    except OSError as error:
        raise RetrievalError("File unavailable or symlink traversal rejected") from error
    finally:
        os.close(descriptor)


def markdown_body(text: str) -> str:
    if text.startswith("---\n"):
        _, separator, body = text[4:].partition("\n---\n")
        if separator:
            return body
    return text


def slug(value: str) -> str:
    value = value.strip().strip("`").casefold()
    return re.sub(r"\s+", "-", re.sub(r"[^\w\s-]", "", value))


def sections(text: str) -> list[dict]:
    """Return heading subtrees in original order; fenced headings are not headings."""
    lines = text.splitlines(keepends=True)
    headings = []
    stack = []
    seen = {}
    fence = None
    for index, line in enumerate(lines):
        marker = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if marker:
            run = marker.group(1)
            if fence is None:
                fence = run
            elif run[0] == fence[0] and len(run) >= len(fence) and not line[marker.end():].strip():
                fence = None
            continue
        match = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line) if fence is None else None
        if not match:
            continue
        level, title = len(match.group(1)), match.group(2)
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        base = slug(title)
        duplicate = seen.get(base, 0)
        seen[base] = duplicate + 1
        headings.append({"heading": title, "heading_path": [item[1] for item in stack], "slug": base + (f"-{duplicate}" if duplicate else ""), "level": level, "start": index})
    for position, heading in enumerate(headings):
        end = next((other["start"] for other in headings[position + 1:] if other["level"] <= heading["level"]), len(lines))
        heading["content"] = "".join(lines[heading["start"]:end])
    return headings


def heading_symbols(headings: list[str]) -> set[str]:
    found = set()
    for heading in headings:
        candidate = heading.strip()
        code_heading = re.fullmatch(r"`([^`]+)`", candidate)
        if code_heading:
            candidate = code_heading.group(1).strip()
        signature = re.fullmatch("(" + IDENTIFIER + r")\s*\([^()\n]*\)(?:\s*->\s*\S[^\n]*)?", candidate)
        if signature:
            found.add(signature.group(1))
        elif SYMBOL_RE.fullmatch(candidate) and (code_heading or "." in candidate or ":" in candidate):
            found.add(candidate)
    return found


def lua_declarations(lines: list[str]) -> list[tuple[int, str]]:
    found = []
    owner = None
    for index, line in enumerate(lines):
        class_match = re.match(r"\s*---@class\s+(" + IDENTIFIER + ")", line)
        field = re.match(r"\s*---@field\s+(?:(?:public|private|protected)\s+)?(" + IDENTIFIER + r")\??\s", line)
        function = re.match(r"\s*(?:local\s+)?function\s+(" + IDENTIFIER + r")\s*\(|\s*(" + IDENTIFIER + r")\s*=\s*function\s*\(", line)
        if class_match:
            owner = class_match.group(1)
            found.append((index, owner))
        elif field and owner:
            found.append((index, owner + "." + field.group(1)))
        elif function:
            found.append((index, function.group(1) or function.group(2)))
    return found


def family_for_page(url: str) -> str:
    path = urlsplit(url).path.lower()
    if "/izi" in path or "/examples/izi/" in path:
        return "izi"
    if path.startswith(("/dev/api/", "/dev/modules/", "/examples/legacy/")) or path.startswith(("/dev/libraries/modules/", "/dev/libraries/mini-libs/")):
        return "legacy"
    return "general"


SCHEMA = """
CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE pages (url TEXT PRIMARY KEY, payload TEXT NOT NULL, content TEXT NOT NULL);
CREATE TABLE sections (url TEXT NOT NULL, ordinal INTEGER NOT NULL, payload TEXT NOT NULL,
                       PRIMARY KEY(url, ordinal));
CREATE TABLE sources (path TEXT PRIMARY KEY, sha256 TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE records (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, family TEXT NOT NULL,
                      example INTEGER NOT NULL, title TEXT NOT NULL, context TEXT NOT NULL,
                      content TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE symbols (name TEXT NOT NULL, record_id INTEGER NOT NULL, quality INTEGER NOT NULL,
                      PRIMARY KEY(name, record_id));
CREATE INDEX symbol_lookup ON symbols(name, quality);
CREATE INDEX record_filters ON records(family, kind, example);
CREATE VIRTUAL TABLE search USING fts5(title, context, content, tokenize='unicode61');
"""


def add_record(db: sqlite3.Connection, payload: dict, symbols: set[str]) -> None:
    cursor = db.execute("INSERT INTO records(kind,family,example,title,context,content,payload) VALUES(?,?,?,?,?,?,?)", (payload["document_type"], payload["api_family"], int(payload["is_example"]), payload["title"], payload["context"], payload["content"], json.dumps(payload, ensure_ascii=False)))
    rowid = cursor.lastrowid
    db.execute("INSERT INTO search(rowid,title,context,content) VALUES(?,?,?,?)", (rowid, payload["title"], payload["context"], payload["content"]))
    aliases = {}
    for symbol in symbols:
        canonical = normalized_symbol(symbol)
        aliases[canonical] = 0
        leaf = canonical.rsplit(".", 1)[-1]
        aliases.setdefault(leaf, 1)
    db.executemany("INSERT INTO symbols VALUES(?,?,?)", [(name, rowid, quality) for name, quality in aliases.items()])


def load_jsonl(path: Path):
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def source_revision(root: Path) -> str:
    try:
        top = subprocess.check_output(["git", "-C", str(root), "rev-parse", "--show-toplevel"], stderr=subprocess.DEVNULL, text=True).strip()
        if Path(top).resolve() != root:
            raise RetrievalError("Source directory must be its own Git checkout")
        return subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True).strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise RetrievalError("Cannot read source Git revision; supply --source-revision for an exported snapshot") from error


def build_index(corpus: Path, source_root: Path, destination: Path, revision: str | None = None,
                repository_url: str = "https://github.com/bluesilvi/project-sylvanas") -> dict:
    corpus, source_root = corpus.resolve(strict=True), source_root.resolve(strict=True)
    destination = destination.absolute()
    if destination.is_symlink() or destination.resolve().is_relative_to(corpus) or destination.resolve().is_relative_to(source_root):
        raise RetrievalError("Keep the index outside the generated corpus and source checkout, and do not use a symlink")
    revision = revision or source_revision(source_root)
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", revision):
        raise RetrievalError("Source revision must be a full hexadecimal Git commit ID")
    manifest_bytes = (corpus / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix="." + destination.name + ".", dir=destination.parent)
    os.close(descriptor)
    db = None
    try:
        db = sqlite3.connect(temporary)
        db.executescript(SCHEMA)
        pages = {}
        for page in load_jsonl(corpus / "pages.jsonl"):
            url = canonical_url(page["source_url"])
            markdown = safe_read(corpus, page["markdown_path"])
            if digest(markdown) != page["markdown_sha256"]:
                raise RetrievalError(f"Markdown hash mismatch: {page['markdown_path']}")
            content = markdown_body(markdown.decode("utf-8"))
            route = urlsplit(url).path
            kind = "example" if route in {"/examples", "/dev/examples"} or route.startswith(("/examples/", "/dev/examples/")) else "docs"
            metadata = {"url": url, "source_url": page["source_url"], "canonical_url": page["canonical_url"], "path": page["markdown_path"], "title": page["title"], "api_family": family_for_page(url), "document_type": kind, "source_kind": "documentation", "source_sha256": page["source_sha256"], "markdown_sha256": page["markdown_sha256"], "fetched_at": page["fetched_at"], "repository_revision": None}
            pages[url] = metadata
            db.execute("INSERT INTO pages VALUES(?,?,?)", (url, json.dumps(metadata), content))
            page_sections = sections(content)
            if page.get("raw_path"):
                raw = safe_read(corpus, page["raw_path"])
                if digest(raw) != page["source_sha256"]:
                    raise RetrievalError(f"Source HTML hash mismatch: {page['raw_path']}")
                article = BeautifulSoup(raw, "html.parser").select_one("article .theme-doc-markdown")
                source_headings = article.find_all(re.compile(r"^h[1-6]$")) if article else []
                source_headings = [
                    heading for heading in source_headings
                    if heading.get_text().replace("\u200b", "").strip()
                ]
                if len(source_headings) != len(page_sections) or any(int(source.name[1]) != section["level"] for source, section in zip(source_headings, page_sections)):
                    raise RetrievalError(f"Source HTML headings do not match Markdown: {page['markdown_path']}")
                # The source owns fragment spelling, including emoji punctuation,
                # explicit IDs, and duplicate suffixes; do not reconstruct those.
                for source, section in zip(source_headings, page_sections):
                    if source.get("id"):
                        section["slug"] = source["id"]
            for ordinal, section in enumerate(page_sections):
                section["content_sha256"] = digest(section["content"].encode())
                db.execute("INSERT INTO sections VALUES(?,?,?)", (url, ordinal, json.dumps(section, ensure_ascii=False)))
        chunk_count = 0
        for chunk in load_jsonl(corpus / "chunks.jsonl"):
            url = canonical_url(chunk["url"])
            if url not in pages or digest(chunk["content"].encode()) != chunk["content_sha256"]:
                raise RetrievalError(f"Invalid chunk reference or content hash: {chunk['id']}")
            metadata = pages[url]
            payload = {**metadata, "id": chunk["id"], "heading_path": chunk["heading_path"], "context": chunk["context"], "content": chunk["content"], "content_sha256": chunk["content_sha256"], "part": chunk["part"], "is_example": metadata["document_type"] == "example" or bool(re.search(r"(?m)^\s*(?:>\s*)?`{3,}(?:lua|luau)?\s*$", chunk["content"]))}
            add_record(db, payload, heading_symbols(chunk["heading_path"][-1:]))
            chunk_count += 1
        source_count = 0
        for path in sorted(source_root.rglob("*.lua")):
            relative = path.relative_to(source_root).as_posix()
            data = safe_read(source_root, relative)
            lines = data.decode("utf-8-sig").splitlines(keepends=True)
            kind = "stub" if "_api" in path.relative_to(source_root).parts else "example"
            family = "izi" if relative.startswith("izi/") else "legacy" if relative.startswith("legacy/") else "general"
            metadata = {"path": relative, "source_url": f"{repository_url.rstrip('/')}/blob/{revision}/{relative}", "url": f"{repository_url.rstrip('/')}/blob/{revision}/{relative}", "source_sha256": digest(data), "repository_revision": revision, "document_type": kind, "source_kind": "lua_source", "api_family": family, "title": relative, "total_lines": len(lines)}
            db.execute("INSERT INTO sources VALUES(?,?,?)", (relative, digest(data), json.dumps(metadata)))
            declarations = lua_declarations(lines)
            # Fixed source windows bound whole examples; declaration windows preserve
            # annotations and signatures independently of those chunk boundaries.
            windows = [(start, min(start + 100, len(lines)), set()) for start in range(0, len(lines), 100)]
            for index, name in declarations:
                start = index
                while start > 0 and index - start < 30 and lines[start - 1].lstrip().startswith("---"):
                    start -= 1
                windows.append((start, min(index + 16, len(lines)), {name}))
            for ordinal, (start, end, symbols) in enumerate(windows):
                content = "".join(lines[start:end])
                context = f"{relative} > {next(iter(symbols)) if symbols else 'lines ' + str(start + 1) + '-' + str(end)}"
                payload = {**metadata, "id": f"source:{relative}:{ordinal}", "heading_path": [relative, *sorted(symbols)], "context": context, "content": content, "content_sha256": digest(content.encode()), "start_line": start + 1, "end_line": end, "is_example": kind == "example"}
                add_record(db, payload, symbols)
            source_count += 1
        if not pages or not chunk_count or not source_count:
            raise RetrievalError("Corpus pages, chunks and Lua sources must all be nonempty")
        summary = {"schema_version": SCHEMA_VERSION, "built_at": dt.datetime.now(dt.timezone.utc).isoformat(), "page_count": len(pages), "chunk_count": chunk_count, "source_file_count": source_count, "record_count": db.execute("SELECT count(*) FROM records").fetchone()[0], "source_root": str(source_root), "corpus_root": str(corpus), "repository_url": repository_url, "repository_revision": revision, "manifest_sha256": digest(manifest_bytes), "pages_jsonl_sha256": digest((corpus / "pages.jsonl").read_bytes()), "chunks_jsonl_sha256": digest((corpus / "chunks.jsonl").read_bytes()), "asset_failure_count": manifest.get("asset_failure_count", 0), "scope": manifest.get("scope", []), "embeddings": False}
        db.execute("INSERT INTO metadata VALUES('summary',?)", (json.dumps(summary),))
        db.commit()
        db.close()
        db = None
        os.replace(temporary, destination)
        return summary
    finally:
        if db is not None:
            db.close()
        if os.path.exists(temporary):
            os.unlink(temporary)
