"""Mirror Project Sylvanas developer docs into an LLM-friendly local corpus."""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from bs4 import BeautifulSoup, NavigableString, Tag

SITEMAP_URL = "https://docs.project-sylvanas.net/sitemap.xml"
SCOPE_ROOTS = ("/dev", "/examples", "/getting-started", "/guides", "/modules", "/plugins")
DOCS_HOST = "docs.project-sylvanas.net"
PROJECT_HOST_SUFFIX = "project-sylvanas.net"
USER_AGENT = "SylvanasDocsCorpus/1.0 (+local coding-assistant indexer)"
PAGE_LIMIT_BYTES = 16 * 1024 * 1024
ASSET_LIMIT_BYTES = 64 * 1024 * 1024
MAX_CHUNK_CHARS = 6_000
ASSET_EXTENSIONS = {
    ".avif",
    ".gif",
    ".jpeg",
    ".jpg",
    ".json",
    ".lua",
    ".pdf",
    ".png",
    ".svg",
    ".webp",
    ".zip",
}


@dataclass(frozen=True)
class Response:
    requested_url: str
    final_url: str
    content_type: str
    etag: str | None
    last_modified: str | None
    body: bytes


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch(url: str, byte_limit: int, attempts: int = 3) -> Response:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/*;q=0.8,*/*;q=0.5",
        "Accept-Encoding": "identity",
    }
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=45) as response:
                body = response.read(byte_limit + 1)
                if len(body) > byte_limit:
                    raise ValueError(f"response exceeds {byte_limit} bytes")
                return Response(
                    requested_url=url,
                    final_url=response.geturl(),
                    content_type=response.headers.get_content_type(),
                    etag=response.headers.get("ETag"),
                    last_modified=response.headers.get("Last-Modified"),
                    body=body,
                )
        except (OSError, ValueError, urllib.error.URLError) as error:
            last_error = error
            if attempt + 1 < attempts:
                time.sleep(0.5 * (2**attempt))
    assert last_error is not None
    raise RuntimeError(f"failed to fetch {url}: {last_error}") from last_error


def in_scope(url: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    if parsed.hostname != DOCS_HOST:
        return False
    path = parsed.path.rstrip("/")
    return any(path == root or path.startswith(root + "/") for root in SCOPE_ROOTS)


def normalized_url(url: str, base_url: str | None = None, keep_fragment: bool = True) -> str:
    absolute = urllib.parse.urljoin(base_url or "", url)
    parsed = urllib.parse.urlsplit(absolute)
    if parsed.scheme not in {"http", "https"}:
        return absolute
    path = parsed.path or "/"
    fragment = parsed.fragment if keep_fragment else ""
    return urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, parsed.query, fragment))


def load_scoped_urls() -> tuple[list[str], Response]:
    response = fetch(SITEMAP_URL, PAGE_LIMIT_BYTES)
    root = ET.fromstring(response.body)
    urls = {
        normalized_url(element.text.strip(), keep_fragment=False)
        for element in root.findall(".//{*}loc")
        if element.text and in_scope(element.text.strip())
    }
    return sorted(urls), response


def route_stem(url: str) -> Path:
    path = urllib.parse.unquote(urllib.parse.urlsplit(url).path)
    segments = [segment for segment in path.split("/") if segment]
    if not segments:
        segments = ["index"]
    if path.endswith("/") or segments[-1] in {"dev", "examples"}:
        segments.append("index")
    safe_segments = [re.sub(r"[^A-Za-z0-9._-]+", "-", segment).strip("-") or "index" for segment in segments]
    return Path(*safe_segments)


def json_dump(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def jsonl_dump(path: Path, rows: Iterable[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as output:
        for row in rows:
            output.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def meta_content(soup: BeautifulSoup, name: str) -> str | None:
    element = soup.find("meta", attrs={"name": name})
    if isinstance(element, Tag):
        value = element.get("content")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def canonical_url(soup: BeautifulSoup, fallback: str) -> str:
    element = soup.find("link", attrs={"rel": lambda value: value and "canonical" in value})
    if isinstance(element, Tag):
        href = element.get("href")
        if isinstance(href, str) and href.strip():
            return normalized_url(href.strip(), fallback, keep_fragment=False)
    return normalized_url(fallback, keep_fragment=False)


def classify_link(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    if in_scope(url):
        return "corpus-page"
    if host == DOCS_HOST:
        return "same-site"
    suffix = Path(parsed.path).suffix.lower()
    if host.endswith(PROJECT_HOST_SUFFIX) and suffix in ASSET_EXTENSIONS:
        return "project-asset"
    return "external"


def collect_links(article: Tag, page_url: str) -> tuple[list[dict[str, str]], set[str]]:
    rows: list[dict[str, str]] = []
    assets: set[str] = set()
    seen: set[tuple[str, str, str]] = set()

    for element in article.select("a[href], img[src]"):
        is_image = element.name == "img"
        attribute = "src" if is_image else "href"
        raw_target = element.get(attribute)
        if not isinstance(raw_target, str) or not raw_target.strip():
            continue
        target = normalized_url(raw_target.strip(), page_url)
        parsed = urllib.parse.urlsplit(target)
        if parsed.scheme not in {"http", "https"}:
            continue
        text = (element.get("alt") if is_image else element.get_text(" ", strip=True)) or ""
        relation = "image" if is_image else "link"
        key = (target, relation, text)
        if key in seen:
            continue
        seen.add(key)
        kind = classify_link(target)
        rows.append({"url": target, "relation": relation, "kind": kind, "text": text})

        suffix = Path(parsed.path).suffix.lower()
        if is_image or kind == "project-asset" or suffix in ASSET_EXTENSIONS and (parsed.hostname or "").endswith(PROJECT_HOST_SUFFIX):
            assets.add(normalized_url(target, keep_fragment=False))

    rows.sort(key=lambda row: (row["url"], row["relation"], row["text"]))
    return rows, assets


def code_text(node: Tag) -> str:
    def visit(child: object) -> str:
        if isinstance(child, NavigableString):
            return str(child)
        if not isinstance(child, Tag):
            return ""
        if child.name == "br":
            return "\n"
        rendered = "".join(visit(grandchild) for grandchild in child.children)
        classes = child.get("class", [])
        if child.name == "div" and isinstance(classes, list) and "token-line" in classes and not rendered.endswith("\n"):
            rendered += "\n"
        return rendered

    return visit(node).replace("\r\n", "\n").replace("\r", "\n").replace("\u200b", "")


def fence_for(code: str) -> str:
    longest = max((len(match.group(0)) for match in re.finditer(r"`+", code)), default=0)
    return "`" * max(3, longest + 1)


class MarkdownRenderer:
    def __init__(self, page_url: str, page_path: Path, asset_paths: dict[str, str]) -> None:
        self.page_url = page_url
        self.page_path = page_path
        self.asset_paths = asset_paths

    def link_target(self, raw_url: str) -> str:
        target = normalized_url(raw_url, self.page_url)
        asset_key = normalized_url(target, keep_fragment=False)
        asset_path = self.asset_paths.get(asset_key)
        if asset_path:
            relative = os.path.relpath(asset_path, start=self.page_path.parent.as_posix())
            return relative.replace(os.sep, "/")
        return target

    def inline(self, node: object) -> str:
        if isinstance(node, NavigableString):
            return re.sub(r"\s+", " ", str(node))
        if not isinstance(node, Tag):
            return ""
        if node.name in {"button", "script", "style", "svg"}:
            return ""
        classes = node.get("class", [])
        if node.name == "a" and isinstance(classes, list) and "hash-link" in classes:
            return ""
        if node.name == "br":
            return "\n"
        if node.name == "img":
            source = node.get("src")
            if not isinstance(source, str):
                return ""
            alt = str(node.get("alt") or "").replace("]", "\\]")
            return f"![{alt}]({self.link_target(source)})"

        content = "".join(self.inline(child) for child in node.children)
        if node.name == "a":
            href = node.get("href")
            if not isinstance(href, str) or not href.strip():
                return content
            label = content.strip() or self.link_target(href)
            return f"[{label}]({self.link_target(href)})"
        if node.name in {"strong", "b"}:
            return f"**{content.strip()}**"
        if node.name in {"em", "i"}:
            return f"*{content.strip()}*"
        if node.name in {"s", "del"}:
            return f"~~{content.strip()}~~"
        if node.name == "code" and node.parent and node.parent.name != "pre":
            value = content.strip()
            delimiter = "``" if "`" in value else "`"
            return f"{delimiter}{value}{delimiter}"
        return content

    @staticmethod
    def clean_inline(value: str) -> str:
        value = re.sub(r"[ \t]+", " ", value)
        value = re.sub(r" *\n *", "\n", value)
        return value.strip()

    def render_table(self, table: Tag) -> str:
        rows: list[list[str]] = []
        header_row = -1
        for row_index, row in enumerate(table.find_all("tr")):
            cells: list[str] = []
            has_header = False
            for cell in row.find_all(["th", "td"], recursive=False):
                if cell.name == "th":
                    has_header = True
                value = self.clean_inline(self.inline(cell)).replace("\n", "<br>")
                cells.append(value.replace("|", "\\|"))
            if cells:
                if has_header and header_row < 0:
                    header_row = row_index
                rows.append(cells)
        if not rows:
            return ""
        width = max(len(row) for row in rows)
        rows = [row + [""] * (width - len(row)) for row in rows]
        if header_row != 0:
            rows.insert(0, [""] * width)
        output = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
        output.extend("| " + " | ".join(row) + " |" for row in rows[1:])
        return "\n".join(output) + "\n\n"

    def render_list(self, list_element: Tag, depth: int = 0) -> str:
        lines: list[str] = []
        ordered = list_element.name == "ol"
        index = 1
        for item in list_element.find_all("li", recursive=False):
            nested = [child for child in item.children if isinstance(child, Tag) and child.name in {"ul", "ol"}]
            body_parts = [self.inline(child) for child in item.children if child not in nested]
            body = self.clean_inline("".join(body_parts))
            marker = f"{index}." if ordered else "-"
            indent = "  " * depth
            body_lines = body.splitlines() or [""]
            lines.append(f"{indent}{marker} {body_lines[0]}")
            continuation = " " * (len(indent) + len(marker) + 1)
            lines.extend(f"{continuation}{line}" for line in body_lines[1:])
            for child in nested:
                lines.extend(self.render_list(child, depth + 1).rstrip().splitlines())
            index += 1
        return "\n".join(lines) + "\n\n"

    def render_admonition(self, element: Tag) -> str:
        heading = element.find(class_=re.compile(r"admonitionHeading"))
        content = element.find(class_=re.compile(r"admonitionContent"))
        label = self.clean_inline(self.inline(heading)).title() if isinstance(heading, Tag) else "Note"
        body = self.render_blocks(content) if isinstance(content, Tag) else self.render_blocks(element)
        body_lines = body.strip().splitlines()
        lines = [f"> **{label}**", ">"]
        lines.extend(">" if not line else f"> {line}" for line in body_lines)
        return "\n".join(lines) + "\n\n"

    def render_blocks(self, parent: Tag) -> str:
        output: list[str] = []
        for child in parent.children:
            if isinstance(child, NavigableString):
                text = self.clean_inline(str(child))
                if text:
                    output.append(text + "\n\n")
                continue
            if not isinstance(child, Tag) or child.name in {"button", "script", "style", "svg"}:
                continue
            classes = child.get("class", [])
            if isinstance(classes, list) and "hash-link" in classes:
                continue
            if child.name and re.fullmatch(r"h[1-6]", child.name):
                level = int(child.name[1])
                title = self.clean_inline(self.inline(child))
                if title:
                    output.append(f"{'#' * level} {title}\n\n")
            elif child.name == "p":
                text = self.clean_inline(self.inline(child))
                if text:
                    output.append(text + "\n\n")
            elif child.name == "pre":
                code = code_text(child).strip("\n")
                code_element = child.find("code")
                classes_to_check: list[str] = []
                for element in (child, code_element):
                    if isinstance(element, Tag):
                        value = element.get("class", [])
                        if isinstance(value, list):
                            classes_to_check.extend(str(item) for item in value)
                language = ""
                for class_name in classes_to_check:
                    match = re.fullmatch(r"language-([A-Za-z0-9_+-]+)", class_name)
                    if match:
                        language = match.group(1)
                        break
                fence = fence_for(code)
                output.append(f"{fence}{language}\n{code}\n{fence}\n\n")
            elif child.name in {"ul", "ol"}:
                output.append(self.render_list(child))
            elif child.name == "table":
                output.append(self.render_table(child))
            elif child.name == "blockquote":
                body = self.render_blocks(child).strip().splitlines()
                output.append("\n".join(">" if not line else f"> {line}" for line in body) + "\n\n")
            elif child.name == "hr":
                output.append("---\n\n")
            elif isinstance(classes, list) and "theme-admonition" in classes:
                output.append(self.render_admonition(child))
            elif child.name in {"div", "section", "header", "main", "article", "details"}:
                output.append(self.render_blocks(child))
            else:
                text = self.clean_inline(self.inline(child))
                if text:
                    output.append(text + "\n\n")
        return "".join(output)

    def render(self, article: Tag) -> str:
        markdown = self.render_blocks(article).replace("\u200b", "")
        lines = [line.rstrip() for line in markdown.splitlines()]
        return "\n".join(lines).strip() + "\n"


def yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def markdown_document(metadata: dict[str, str], body: str) -> str:
    frontmatter = ["---"]
    for key in ("title", "description", "source_url", "canonical_url", "fetched_at", "source_sha256"):
        frontmatter.append(f"{key}: {yaml_string(metadata.get(key, ''))}")
    frontmatter.extend(["---", ""])
    return "\n".join(frontmatter) + body


def split_markdown_blocks(lines: list[str]) -> list[str]:
    blocks: list[str] = []
    current: list[str] = []
    fence: str | None = None
    for line in lines:
        fence_match = re.match(r"^(`{3,}|~{3,})", line)
        if fence_match:
            marker = fence_match.group(1)[0]
            if fence is None:
                fence = marker
            elif fence == marker:
                fence = None
        if not line.strip() and fence is None:
            if current:
                blocks.append("\n".join(current).strip())
                current = []
        else:
            current.append(line)
    if current:
        blocks.append("\n".join(current).strip())
    return [block for block in blocks if block]


def chunk_markdown(url: str, page_title: str, body: str) -> list[dict[str, object]]:
    sections: list[tuple[list[str], list[str]]] = []
    headings: list[str] = []
    current_lines: list[str] = []
    current_path: list[str] = []
    fence: str | None = None

    for line in body.splitlines():
        fence_match = re.match(r"^(`{3,}|~{3,})", line)
        if fence_match:
            marker = fence_match.group(1)[0]
            if fence is None:
                fence = marker
            elif fence == marker:
                fence = None
        heading_match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line) if fence is None else None
        if heading_match:
            if current_lines:
                sections.append((current_path, current_lines))
            level = len(heading_match.group(1))
            title = heading_match.group(2).strip()
            headings = headings[: level - 1]
            while len(headings) < level - 1:
                headings.append("")
            headings.append(title)
            current_path = [heading for heading in headings if heading]
            current_lines = [line]
        else:
            current_lines.append(line)
    if current_lines:
        sections.append((current_path, current_lines))

    chunks: list[dict[str, object]] = []
    for heading_path, lines in sections:
        blocks = split_markdown_blocks(lines)
        if not blocks or (len(blocks) == 1 and re.fullmatch(r"#{1,6}\s+.+", blocks[0])):
            continue
        groups: list[list[str]] = []
        group: list[str] = []
        group_size = 0
        for block in blocks:
            block_size = len(block) + 2
            if group and group_size + block_size > MAX_CHUNK_CHARS:
                groups.append(group)
                group = []
                group_size = 0
            group.append(block)
            group_size += block_size
        if group:
            groups.append(group)

        section_heading = lines[0] if lines and re.match(r"^#{1,6}\s+", lines[0]) else None
        if (
            len(groups) > 1
            and section_heading
            and len(groups[0]) == 1
            and groups[0][0] == section_heading
        ):
            groups[1].insert(0, section_heading)
            groups.pop(0)
        for part_index, group_blocks in enumerate(groups):
            content_blocks = list(group_blocks)
            if part_index > 0 and section_heading and not content_blocks[0].startswith("#"):
                content_blocks.insert(0, section_heading)
            content = "\n\n".join(content_blocks).strip() + "\n"
            context_parts = [page_title]
            for heading in heading_path:
                if heading != context_parts[-1]:
                    context_parts.append(heading)
            context = " > ".join(context_parts)
            embedding_text = f"{context}\n\n{content}"
            identity = f"{url}\n{' > '.join(heading_path)}\n{part_index}\n{sha256(content.encode('utf-8'))}"
            chunks.append(
                {
                    "id": hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24],
                    "url": url,
                    "page_title": page_title,
                    "heading_path": heading_path,
                    "context": context,
                    "part": part_index,
                    "content": content,
                    "embedding_text": embedding_text,
                    "content_sha256": sha256(content.encode("utf-8")),
                    "estimated_tokens": max(1, round(len(embedding_text) / 4)),
                }
            )
    return chunks


def asset_output_path(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    basename = Path(urllib.parse.unquote(parsed.path)).name or "asset"
    basename = re.sub(r"[^A-Za-z0-9._-]+", "-", basename).strip("-") or "asset"
    return f"assets/{hashlib.sha256(url.encode('utf-8')).hexdigest()[:12]}-{basename}"


def prepare_build_dir(output: Path) -> Path:
    build = output.parent / f".{output.name}.build"
    if build.exists():
        shutil.rmtree(build)
    build.mkdir(parents=True)
    return build


def replace_output(build: Path, output: Path) -> None:
    backup = output.parent / f".{output.name}.previous"
    if backup.exists():
        shutil.rmtree(backup)
    if output.exists():
        output.replace(backup)
    build.replace(output)
    if backup.exists():
        shutil.rmtree(backup)


def fetch_corpus(output: Path, workers: int = 8, assets: bool = True) -> dict[str, object]:
    """Download the docs into output, replacing it atomically. Images and other linked
    assets are optional: retrieval never reads them, and skipped links stay remote."""
    if workers < 1 or workers > 32:
        raise ValueError("workers must be between 1 and 32")
    output = output.resolve()
    build = prepare_build_dir(output)
    fetched_at = utc_now()
    urls, sitemap_response = load_scoped_urls()
    if not urls:
        raise RuntimeError("sitemap contained no scoped developer documentation URLs")

    page_responses: dict[str, Response] = {}
    failures: list[dict[str, str]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(fetch, url, PAGE_LIMIT_BYTES): url for url in urls}
        for future in concurrent.futures.as_completed(futures):
            url = futures[future]
            try:
                page_responses[url] = future.result()
            except Exception as error:  # report every failed source together
                failures.append({"url": url, "error": str(error)})

    if failures:
        json_dump(build / "failures.json", failures)
        raise RuntimeError(f"failed to fetch {len(failures)} of {len(urls)} pages")

    parsed_pages: dict[str, dict[str, object]] = {}
    asset_urls: set[str] = set()
    link_rows: list[dict[str, str]] = []
    for url in urls:
        response = page_responses[url]
        soup = BeautifulSoup(response.body, "html.parser")
        article = soup.select_one("article .theme-doc-markdown")
        if not isinstance(article, Tag):
            failures.append({"url": url, "error": "missing article .theme-doc-markdown"})
            continue
        title = (soup.title.get_text(" ", strip=True) if soup.title else "").removesuffix(" | Project Sylvanas").strip()
        description = meta_content(soup, "description") or ""
        links, page_assets = collect_links(article, url)
        asset_urls.update(page_assets)
        link_rows.extend({"source_url": url, **link} for link in links)
        parsed_pages[url] = {
            "response": response,
            "soup": soup,
            "article": article,
            "title": title,
            "description": description,
            "canonical_url": canonical_url(soup, url),
            "links": links,
        }

    if failures:
        json_dump(build / "failures.json", failures)
        raise RuntimeError(f"failed to parse {len(failures)} of {len(urls)} pages")

    asset_records: list[dict[str, object]] = []
    asset_paths: dict[str, str] = {}
    asset_failures: list[dict[str, str]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(fetch, url, ASSET_LIMIT_BYTES): url for url in sorted(asset_urls if assets else ())}
        for future in concurrent.futures.as_completed(futures):
            url = futures[future]
            try:
                response = future.result()
                relative_path = asset_output_path(url)
                asset_paths[url] = relative_path
                path = build / relative_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(response.body)
                asset_records.append(
                    {
                        "url": url,
                        "downloaded": True,
                        "final_url": response.final_url,
                        "path": relative_path,
                        "content_type": response.content_type,
                        "byte_count": len(response.body),
                        "sha256": sha256(response.body),
                        "etag": response.etag,
                        "last_modified": response.last_modified,
                    }
                )
            except Exception as error:
                failure = {"url": url, "error": str(error)}
                asset_failures.append(failure)
                asset_records.append({"url": url, "downloaded": False, "error": str(error)})

    page_records: list[dict[str, object]] = []
    chunks: list[dict[str, object]] = []
    for url in urls:
        page = parsed_pages[url]
        response = page["response"]
        article = page["article"]
        assert isinstance(response, Response)
        assert isinstance(article, Tag)
        stem = route_stem(url)
        raw_relative = Path("raw") / stem.with_suffix(".html")
        page_relative = Path("pages") / stem.with_suffix(".md")
        raw_path = build / raw_relative
        page_path = build / page_relative
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        page_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_bytes(response.body)

        renderer = MarkdownRenderer(url, page_relative, asset_paths)
        body = renderer.render(article)
        metadata = {
            "title": str(page["title"]),
            "description": str(page["description"]),
            "source_url": url,
            "canonical_url": str(page["canonical_url"]),
            "fetched_at": fetched_at,
            "source_sha256": sha256(response.body),
        }
        document = markdown_document(metadata, body)
        page_path.write_text(document, encoding="utf-8", newline="\n")
        document_bytes = document.encode("utf-8")
        page_chunks = chunk_markdown(url, str(page["title"]), body)
        chunks.extend(page_chunks)
        page_records.append(
            {
                **metadata,
                "final_url": response.final_url,
                "content_type": response.content_type,
                "etag": response.etag,
                "last_modified": response.last_modified,
                "raw_path": raw_relative.as_posix(),
                "raw_byte_count": len(response.body),
                "markdown_path": page_relative.as_posix(),
                "markdown_byte_count": len(document_bytes),
                "markdown_sha256": sha256(document_bytes),
                "link_count": len(page["links"]),
                "chunk_count": len(page_chunks),
            }
        )

    asset_records.sort(key=lambda row: str(row["url"]))
    link_rows.sort(key=lambda row: (row["source_url"], row["url"], row["relation"], row["text"]))
    chunks.sort(key=lambda row: (row["url"], tuple(row["heading_path"]), row["part"]))
    page_records.sort(key=lambda row: str(row["source_url"]))

    jsonl_dump(build / "pages.jsonl", page_records)
    jsonl_dump(build / "links.jsonl", link_rows)
    jsonl_dump(build / "chunks.jsonl", chunks)
    jsonl_dump(build / "assets.jsonl", asset_records)
    manifest = {
        "schema_version": 1,
        "generated_at": fetched_at,
        "sitemap_url": SITEMAP_URL,
        "sitemap_sha256": sha256(sitemap_response.body),
        "scope": [pattern for root in SCOPE_ROOTS for pattern in (root, root + "/**")],
        "page_count": len(page_records),
        "chunk_count": len(chunks),
        "link_count": len(link_rows),
        "asset_count": len(asset_records),
        "asset_downloaded_count": len(asset_paths),
        "asset_failure_count": len(asset_failures),
        "failures": [],
        "asset_failures": asset_failures,
        "pages": page_records,
        "assets": asset_records,
    }
    json_dump(build / "manifest.json", manifest)
    replace_output(build, output)
    return {
        "output": str(output),
        "pages": len(page_records),
        "chunks": len(chunks),
        "links": len(link_rows),
        "assets": len(asset_records),
        "assets_downloaded": len(asset_paths),
        "asset_failures": len(asset_failures),
    }
