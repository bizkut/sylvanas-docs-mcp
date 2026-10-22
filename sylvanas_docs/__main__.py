"""Command-line entry points for setup, indexing, local queries and stdio serving."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

from .index import RetrievalError, build_index
from .retrieval import Corpus

REPOSITORY_URL = "https://github.com/bluesilvi/project-sylvanas"
TOOL_NAMES = ("lookup_symbol", "search_docs", "get_section", "search_examples", "read_api_source", "list_members", "check_lua_api", "corpus_status")


def data_home() -> Path:
    """Where setup keeps the downloaded docs, SDK checkout and index."""
    if os.environ.get("SYLVANAS_DOCS_HOME"):
        return Path(os.environ["SYLVANAS_DOCS_HOME"]).expanduser()
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "sylvanas-docs"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "sylvanas-docs"


def update_sdk(checkout: Path, repository_url: str) -> str:
    """Clone the SDK repository, or fast-forward an existing clone; returns the HEAD commit."""
    if shutil.which("git") is None:
        raise RetrievalError("git is required to download the Sylvanas SDK; install git and rerun setup")
    if (checkout / ".git").exists():
        subprocess.run(["git", "-C", str(checkout), "pull", "--ff-only", "--quiet"], check=True)
    else:
        checkout.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--depth", "1", "--quiet", repository_url + ".git", str(checkout)], check=True)
    return subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()


def setup(home: Path, workers: int, assets: bool, repository_url: str) -> dict:
    from .fetch import fetch_corpus

    home.mkdir(parents=True, exist_ok=True)
    print(f"Downloading documentation into {home / 'corpus'} ...", file=sys.stderr)
    fetched = fetch_corpus(home / "corpus", workers=workers, assets=assets)
    print(f"Fetched {fetched['pages']} pages. Updating the SDK checkout ...", file=sys.stderr)
    update_sdk(home / "sources" / "project-sylvanas-api", repository_url)
    print("Building the index ...", file=sys.stderr)
    summary = build_index(home / "corpus", home / "sources" / "project-sylvanas-api", home / "index.sqlite3", None, repository_url)
    return {"home": str(home), "database": str(home / "index.sqlite3"), **{key: summary[key] for key in ("page_count", "chunk_count", "source_file_count", "member_count", "repository_revision")}}


def open_corpus(database: Path, source_root: Path | None) -> Corpus:
    if not database.exists():
        raise RetrievalError(f"No index at {database}; run `sylvanas-docs setup` first")
    return Corpus(database, source_root)


def main() -> int:
    home = data_home()
    parser = argparse.ArgumentParser(prog="sylvanas-docs", description="Local Sylvanas documentation search and read-only MCP server")
    commands = parser.add_subparsers(dest="command", required=True)
    initial = commands.add_parser("setup", help="Download the docs and SDK, then build the index (refreshes on rerun)")
    initial.add_argument("--home", type=Path, default=home, help=f"data directory (default {home}; or set SYLVANAS_DOCS_HOME)")
    initial.add_argument("--workers", type=int, default=8, help="parallel HTTP requests (1-32)")
    initial.add_argument("--with-assets", action="store_true", help="also download images and other linked files (not needed for search)")
    initial.add_argument("--repository-url", default=REPOSITORY_URL)
    fetch = commands.add_parser("fetch", help="Only download the documentation corpus")
    fetch.add_argument("--output", type=Path, default=home / "corpus")
    fetch.add_argument("--workers", type=int, default=8)
    fetch.add_argument("--with-assets", action="store_true")
    build = commands.add_parser("build-index", help="Atomically rebuild the index from an existing corpus and checkout (no network access)")
    build.add_argument("--corpus", type=Path, default=home / "corpus")
    build.add_argument("--source-root", type=Path, default=home / "sources" / "project-sylvanas-api")
    build.add_argument("--database", type=Path, default=home / "index.sqlite3")
    build.add_argument("--source-revision", help="Full Git commit ID, required only for a non-Git source export")
    build.add_argument("--repository-url", default=REPOSITORY_URL)
    for command in ("serve", "query"):
        subparser = commands.add_parser(command, help="Serve stdio MCP" if command == "serve" else "Call one retrieval tool and print JSON")
        subparser.add_argument("--database", type=Path, default=home / "index.sqlite3")
        subparser.add_argument("--source-root", type=Path, help="Relocated source checkout; indexed hashes must still match")
        if command == "query":
            subparser.add_argument("tool", choices=TOOL_NAMES)
            subparser.add_argument("--args", default="{}", help="Tool arguments as a JSON object")
    args = parser.parse_args()
    try:
        if args.command == "setup":
            result = setup(args.home, args.workers, args.with_assets, args.repository_url)
        elif args.command == "fetch":
            from .fetch import fetch_corpus

            result = fetch_corpus(args.output, workers=args.workers, assets=args.with_assets)
        elif args.command == "build-index":
            result = build_index(args.corpus, args.source_root, args.database, args.source_revision, args.repository_url)
        else:
            corpus = open_corpus(args.database, args.source_root)
            if args.command == "serve":
                from .server import create_server

                create_server(corpus).run(transport="stdio")
                return 0
            parameters = json.loads(args.args)
            if not isinstance(parameters, dict):
                raise RetrievalError("--args must be a JSON object")
            result = getattr(corpus, args.tool)(**parameters)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (RetrievalError, OSError, sqlite3.Error, json.JSONDecodeError, TypeError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"sylvanas-docs: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
