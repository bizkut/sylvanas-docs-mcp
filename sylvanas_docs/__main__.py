"""Command-line entry points for indexing, local queries and stdio serving."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

from .index import RetrievalError, build_index
from .retrieval import Corpus

WORKSPACE = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE = WORKSPACE / ".sylvanas" / "index.sqlite3"
TOOL_NAMES = ("lookup_symbol", "search_docs", "get_section", "search_examples", "read_api_source", "list_members", "check_lua_api", "corpus_status")


def main() -> int:
    parser = argparse.ArgumentParser(description="Local Sylvanas SQLite FTS5 retrieval and read-only MCP")
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build-index", help="Atomically rebuild the local index (no network access)")
    build.add_argument("--corpus", type=Path, default=WORKSPACE / "corpus")
    build.add_argument("--source-root", type=Path, default=WORKSPACE / "sources/project-sylvanas-api")
    build.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    build.add_argument("--source-revision", help="Full Git commit ID, required only for a non-Git source export")
    build.add_argument("--repository-url", default="https://github.com/bluesilvi/project-sylvanas")
    for command in ("serve", "query"):
        subparser = commands.add_parser(command, help="Serve stdio MCP" if command == "serve" else "Call one retrieval tool and print JSON")
        subparser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
        subparser.add_argument("--source-root", type=Path, help="Relocated source checkout; indexed hashes must still match")
        if command == "query":
            subparser.add_argument("tool", choices=TOOL_NAMES)
            subparser.add_argument("--args", default="{}", help="Tool arguments as a JSON object")
    args = parser.parse_args()
    try:
        if args.command == "build-index":
            result = build_index(args.corpus, args.source_root, args.database, args.source_revision, args.repository_url)
        else:
            corpus = Corpus(args.database, args.source_root)
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
    except (RetrievalError, OSError, sqlite3.Error, json.JSONDecodeError, TypeError) as error:
        print(f"sylvanas-docs: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
