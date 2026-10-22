# Sylvanas developer documentation MCP

Local, read-only retrieval over the downloaded Sylvanas documentation (`/dev`,
`/examples`, `/getting-started`, `/guides`, `/modules`, `/plugins`) and the Lua
source checkout. SQLite FTS5 supplies lexical/BM25
search; an exact-symbol table puts API definitions ahead of incidental mentions.
There are no embeddings, API keys, hosted databases, or game/runtime integration.

The downloaded corpus contains 98 documentation pages. The source checkout used
when adding this server is `60ef94704d0d787133918beb90af023656d4f724` of
[`bluesilvi/project-sylvanas`](https://github.com/bluesilvi/project-sylvanas).
These describe a snapshot, not a promise that documentation and stubs agree or
that any API has been exercised against a game.

## Install and build the index

Use Python 3.10+ on Linux/POSIX with SQLite FTS5. The server uses the maintained
[official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
(`mcp>=2.3,<3`), not an independently implemented protocol transport.
From this workspace:

```sh
uv venv .venv
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python -m sylvanas_docs build-index
```

If a virtual environment already exists, omit the first command. The equivalent
without `uv` is `python3 -m venv .venv` followed by
`.venv/bin/python -m pip install -r requirements.txt`.

`build-index` reads `corpus/manifest.json`, `pages.jsonl`, `chunks.jsonl`, the
normalized Markdown pages, and every `*.lua` under
`sources/project-sylvanas-api`. It checks recorded Markdown/chunk hashes, records
the source Git revision and individual file hashes, and writes
`.sylvanas/index.sqlite3` through an atomic temporary-file replacement. A failed
build leaves the previous index in place. The index intentionally lives outside
`corpus/` because the downloader replaces that directory on refresh.

Explicit paths are available:

```sh
.venv/bin/python -m sylvanas_docs build-index \
  --corpus corpus \
  --source-root sources/project-sylvanas-api \
  --database .sylvanas/index.sqlite3
```

For an exported source tree without `.git`, also provide
`--source-revision FULL_COMMIT_ID`. That is an operator-supplied provenance claim;
per-file SHA-256 values always identify the bytes actually indexed, including
local changes. The default Git revision is the checkout's HEAD, not an assertion
that its files are unmodified. Paths in source results are relative to the source
checkout, not the workspace.

## Local query CLI

The query CLI calls exactly the same retrieval implementation as MCP. It prints
JSON to stdout; failures go to stderr with a nonzero exit status.

```sh
.venv/bin/python -m sylvanas_docs query corpus_status
.venv/bin/python -m sylvanas_docs query lookup_symbol --args '{"name":"core.log"}'
.venv/bin/python -m sylvanas_docs query search_docs \
  --args '{"query":"izi.spell","api_family":"izi","document_type":"docs","limit":3}'
.venv/bin/python -m sylvanas_docs query search_examples \
  --args '{"query":"core.log","api_family":"legacy","limit":3}'
.venv/bin/python -m sylvanas_docs query get_section \
  --args '{"url":"https://docs.project-sylvanas.net/dev/api/core","heading":"core.register_on_update_callback"}'
.venv/bin/python -m sylvanas_docs query read_api_source \
  --args '{"path":"legacy/_api/core.lua","start_line":1,"end_line":40}'
```

`query` and `serve` accept `--database PATH` and an optional `--source-root PATH`
for a relocated checkout. Relocation does not bypass indexed-file hash checks.

## MCP client configuration

The server is **stdio only**. The client owns its subprocess lifecycle. Do not
pipe startup banners, query output, or shell wrappers into its stdout. Run it with:

```sh
.venv/bin/python -m sylvanas_docs serve
```

A client using an `mcpServers` configuration can use the following (adjust all
absolute paths if the workspace moves):

```json
{
  "mcpServers": {
    "sylvanas-docs": {
      "command": "/home/bizkut/workspace/project-sylvanas/.venv/bin/python",
      "args": [
        "-m", "sylvanas_docs", "serve",
        "--database", "/home/bizkut/workspace/project-sylvanas/.sylvanas/index.sqlite3"
      ],
      "env": {
        "PYTHONPATH": "/home/bizkut/workspace/project-sylvanas"
      }
    }
  }
}
```

`PYTHONPATH` makes the workspace package importable even when the client's working
directory is elsewhere. No editable install is required. Tools advertise
read-only, idempotent, closed-world annotations; enforcement is in the server,
not those client hints.

### Tools and retrieval semantics

| Tool | Arguments | Behavior |
| --- | --- | --- |
| `lookup_symbol` | `name`, `limit=10` | Exact qualified definitions first, then leaf-name aliases; separate lexical mentions. Dots/colons and optional trailing `()` normalize to the same lookup. |
| `search_docs` | `query`, `api_family?`, `document_type?`, `limit=5` | Search all indexed documentation, Lua stubs and Lua examples with exact-definition priority, then FTS5 BM25. |
| `get_section` | `url`, `heading?`, `start_line=1`, `end_line?` | Return a page or heading subtree in original Markdown order, including nested sections. No network fetch. |
| `search_examples` | `query`, `api_family?`, `limit=5` | Search example pages, Lua example files, and documentation chunks containing code. Excludes Lua stubs. |
| `read_api_source` | `path`, `start_line=1`, `end_line?` | Read only an indexed Lua source file under the configured checkout, checking its current hash. |
| `list_members` | `namespace`, `limit=100` | List every function, field and child namespace under a class or namespace (`core.graphics`, `game_object`, `izi`), each with stub/doc signatures and a one-line summary. Unknown namespaces return close matches. |
| `check_lua_api` | `code` | Statically check Lua code (up to 100000 characters) for API names that do not exist. Follows `require("common/...")` and `local g = core.graphics` aliases and the `core` global; ignores strings, comments, plugin-local modules and names the code defines. Method calls on unknown receivers are checked by method name. Errors carry close-match suggestions with signatures; names that are undocumented but used by official examples are warnings. |
| `corpus_status` | none | Snapshot counts, build time, input hashes, source revision, known asset-failure count, filter values and limits. |

- `api_family` is `izi`, `legacy`, or `general`; it is derived from the corpus URL
  or source directory, not a claim of API compatibility. Common legacy helper
  docs are classified `legacy`; unclassified guides are `general`.
- `document_type` is `docs`, `stub`, or `example`. `source_kind` separately
  distinguishes `documentation` from `lua_source`, so example docs and example
  code cannot be mistaken for stubs. Pages under `/examples` and `/dev/examples`
  are type `example`, including overview chunks without code. Code-bearing API
  docs stay type `docs` even when returned by `search_examples`.
- Search uses Porter stemming, so `debuffs`, `interrupting` and `logs` match
  `debuff`, `interrupt` and `log`.
- `check_lua_api` is a static, best-effort check. It catches misspelled or invented
  names, not wrong argument types, and it cannot follow types returned by calls
  (`me:get_buffs()` is checked as "some class has get_buffs"). Over the 33 bundled
  example plugins it reports no errors.
- Search terms are literal words/identifiers, not raw SQL or FTS expressions.
  Question filler ("how", "do", "I", "a", "the", ...) is dropped and the rest are
  combined with **AND**; when that yields fewer than `limit` results, records
  matching any of the words fill the remainder with `match_type: "partial"`.
  Punctuation splits lexical terms; exact lookup preserves identifier structure.
  Lexical search has no semantic/embedding fallback.
- Definitions are extracted from identifier/signature headings (including
  parameters and `->` return annotations) and Lua `function`, `---@class`, and
  `---@field` declarations. Prose headings mentioning an API are not definitions.
  This is static symbol discovery, not a Lua parser or evaluator; dynamically
  constructed symbols remain searchable text rather than invented definitions.
  Qualified definitions precede leaf aliases.
- Every content result carries its source URL/path, heading context, API family,
  document type, source hash and either fetch metadata or repository revision.
  `content_sha256` identifies the **complete indexed record/section**, including
  when a preview is clipped. Line reads additionally return
  `returned_content_sha256` for the exact returned text. Do not merge differing
  docs/stub signatures into an unsupported claim of runtime truth.
- Search/lookup limits are 1–20; each content preview is at most 6,000 characters
  and explicitly reports `content_truncated`. Lookup bounds definitions and
  lexical mentions separately; definition and suffix matches do not consume the
  mentions limit. Use `get_section`/`read_api_source` for the complete selected
  source, rather than treating a preview as complete evidence.
- Section/source reads default to 200 lines and allow at most 400 lines or
  32,000 characters per response. Line numbers are 1-based and inclusive,
  relative to the selected page/section/file. Follow `next_line` to continue;
  no partial line is silently returned. A single line over the character bound
  produces an explicit error (search still provides a bounded preview).
- `get_section` accepts an exact heading title, full `A > B > C` path, or `#slug`.
  URL fragments also select a slug. Original HTML heading IDs are preserved when
  raw pages are present, including trailing hyphens such as `#callback-functions-`.
  Repeated bare titles are errors; use the returned full path or numbered duplicate
  slug (`#shared-1`). Headings inside fenced code are ignored. Unknown URLs/headings
  return actionable errors.
- Source reads require an exact indexed relative Lua path. Absolute paths,
  `..`, backslashes, unindexed files, symlinks at any child component, and
  nonregular files are rejected. Descriptor-relative no-follow reads also
  prevent a symlink swap during traversal. The explicitly configured source root
  and the local database are trusted operator inputs, not remotely selectable
  roots. Source reads fail after file changes until the index is rebuilt.

## Refresh sequence

1. Stop/disconnect the MCP client so no server is using the old snapshot.
2. Refresh documentation with
   `.venv/bin/python scripts/fetch_sylvanas_docs.py --output corpus` if desired.
   This is the only network operation here. Existing protected asset failures
   are retained as provenance; they do not prevent text retrieval.
3. If updating the Lua checkout, explicitly choose and check out the desired
   upstream commit first. Keep the current pinned checkout for reproducibility;
   the server never pulls or updates it automatically.
4. Run `.venv/bin/python -m sylvanas_docs build-index` with the same paths.
5. Restart the MCP client/server. `corpus_status` describes the indexed snapshot,
   not a fresh crawl or a complete live filesystem integrity audit.

## Verification recipe

Run behavioral tests (temporary fixtures; no network or source changes):

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Build the real index and run the CLI examples above. Then exercise actual stdio
initialization, tool listing, all six tools, read-only hints and a rejected
traversal through the official client (from this workspace):

```sh
.venv/bin/python - <<'PY'
import anyio
from pathlib import Path
import sys
from mcp import Client, StdioServerParameters

root = Path.cwd()
params = StdioServerParameters(
    command=sys.executable,
    args=["-m", "sylvanas_docs", "serve"],
    env={"PYTHONPATH": str(root)},
)

async def main():
    async with Client(params) as client:
        print("Protocol:", client.protocol_version)
        tools = (await client.list_tools()).tools
        expected = {"lookup_symbol", "search_docs", "get_section",
                    "search_examples", "read_api_source", "corpus_status"}
        assert {tool.name for tool in tools} == expected
        assert all(tool.annotations.read_only_hint for tool in tools)
        calls = [
            ("corpus_status", {}),
            ("lookup_symbol", {"name": "core.log"}),
            ("search_docs", {"query": "izi.spell", "api_family": "izi", "limit": 3}),
            ("get_section", {"url": "https://docs.project-sylvanas.net/dev/api/core",
                             "heading": "core.register_on_update_callback"}),
            ("search_examples", {"query": "core.log", "api_family": "legacy", "limit": 3}),
            ("read_api_source", {"path": "legacy/_api/core.lua", "start_line": 1, "end_line": 20}),
        ]
        for name, arguments in calls:
            result = await client.call_tool(name, arguments)
            assert not result.is_error, (name, result.content)
            data = result.structured_content
            assert data is not None, name
            if name == "corpus_status":
                assert data["page_count"] == 98
                assert data["read_only"] and not data["embeddings"]
            elif name == "lookup_symbol":
                assert data["definitions"]
            elif name in {"search_docs", "search_examples"}:
                assert data["results"]
            else:
                assert data["content"]
            print(name, "OK")
        rejected = await client.call_tool("read_api_source", {"path": "../README.md"})
        assert rejected.is_error
        print("Traversal rejected")

anyio.run(main)
PY
```
