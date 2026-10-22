# sylvanas-docs-mcp

A local, read-only [MCP](https://modelcontextprotocol.io) server that gives coding
assistants (Claude Code, Claude Desktop, Cursor, and other MCP clients) accurate
knowledge of the [Project Sylvanas](https://docs.project-sylvanas.net/dev) Lua API:

- **Search** the developer docs, the SDK's typed Lua stubs and the example plugins.
- **Look up** exact API symbols (`core.graphics.circle_3d`, `game_object:get_buffs`).
- **Browse** a namespace's functions with signatures (`list_members("core.graphics")`).
- **Check** Lua code for API calls that do not exist, with suggested corrections:

```
L15 error   gfx.draw_circle   `draw_circle` is not a member of `core.graphics`
            → core.graphics.draw_image, core.graphics.circle_3d, core.graphics.circle_2d
L22 error   izi.get_playr     → izi.get_player
L12 warning izi.me            not documented, but official examples use it
```

Everything runs on your machine with SQLite full-text search: no embeddings, API
keys or hosted services.

> **Unofficial.** This project is not affiliated with or endorsed by Project
> Sylvanas. The repository contains none of their documentation or code: `setup`
> downloads the public docs site and clones the public
> [SDK repository](https://github.com/bluesilvi/project-sylvanas) onto your own
> machine. That content remains theirs.

## Requirements

- Python 3.10 or newer (SQLite with FTS5, which standard Python builds include)
- `git` (setup clones the SDK)
- Internet access during setup only. The server itself never uses the network.
- [`uv`](https://docs.astral.sh/uv/) is recommended. `pipx` also works.

## Install

Download the docs and SDK and build the index (about a minute; rerun it any time
to refresh):

```sh
uvx --from git+https://github.com/bizkut/sylvanas-docs-mcp sylvanas-docs setup
```

Or install the command permanently:

```sh
pipx install git+https://github.com/bizkut/sylvanas-docs-mcp   # or: uv tool install ...
sylvanas-docs setup
```

Setup stores its data here (set `SYLVANAS_DOCS_HOME` to use another directory):

| OS | Location |
| --- | --- |
| Windows | `%LOCALAPPDATA%\sylvanas-docs` |
| Linux / macOS | `$XDG_DATA_HOME/sylvanas-docs`, default `~/.local/share/sylvanas-docs` |

Images from the docs are skipped because search never reads them. Pass
`--with-assets` to download them as well.

## Connect your MCP client

**Claude Code:**

```sh
claude mcp add --scope user sylvanas-docs -- uvx --from git+https://github.com/bizkut/sylvanas-docs-mcp sylvanas-docs serve
```

If you installed with `pipx` or `uv tool`, the command is just `sylvanas-docs serve`:

```sh
claude mcp add --scope user sylvanas-docs -- sylvanas-docs serve
```

**Claude Desktop, Cursor and other clients** that use an `mcpServers` file:

```json
{
  "mcpServers": {
    "sylvanas-docs": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/bizkut/sylvanas-docs-mcp", "sylvanas-docs", "serve"]
    }
  }
}
```

GUI apps may not see your shell's `PATH`. If the server fails to start, use the
full path to `uvx` or `sylvanas-docs` (`where uvx` on Windows, `which uvx`
elsewhere). If you set `SYLVANAS_DOCS_HOME` for setup, set it in the client
configuration too (an `"env"` entry), or pass `--database PATH` to `serve`.

### Tell your assistant how to use it

The server already advertises these instructions, but an explicit rule in your
plugin project (`CLAUDE.md`, `AGENTS.md`, `.cursor/rules`, ...) makes them stick:

```markdown
When writing Project Sylvanas Lua code, use the sylvanas-docs MCP tools:
- Before calling an API you are not sure of, use lookup_symbol or list_members
  to confirm the exact name and signature. Do not guess.
- Use search_docs / search_examples for how-to questions and working examples.
- Run check_lua_api on every Lua file you write or change, and fix all errors
  before presenting the code.
```

## Tools

| Tool | Arguments | Behavior |
| --- | --- | --- |
| `lookup_symbol` | `name`, `limit=10` | Exact qualified definitions first, then leaf-name aliases; separate lexical mentions. Dots/colons and optional trailing `()` normalize to the same lookup. |
| `list_members` | `namespace`, `limit=100` | Every function, field and child namespace under a class or namespace (`core.graphics`, `game_object`, `izi`), each with stub/doc signatures and a one-line summary. Unknown namespaces return close matches. |
| `check_lua_api` | `code` | Statically checks Lua code (up to 100,000 characters) for API names that do not exist. Follows `require("common/...")` and `local g = core.graphics` aliases and the `core` global; ignores strings, comments, plugin-local modules and names the code defines. Method calls on unknown receivers are checked by method name. Errors carry close-match suggestions with signatures; names that are undocumented but used by official examples are warnings. |
| `search_docs` | `query`, `api_family?`, `document_type?`, `limit=5` | Searches docs, Lua stubs and Lua examples, with exact definitions ranked first, then BM25. |
| `search_examples` | `query`, `api_family?`, `limit=5` | Searches example pages, Lua example files and documentation chunks containing code. Excludes stubs. |
| `get_section` | `url`, `heading?`, `start_line=1`, `end_line?` | Returns a page or heading subtree in original Markdown order. |
| `read_api_source` | `path`, `start_line=1`, `end_line?` | Reads an indexed Lua file from the SDK checkout, checking its hash. |
| `corpus_status` | none | Snapshot counts, build time, input hashes, SDK revision, filter values and limits. |

## Refreshing

Rerun `sylvanas-docs setup`, then restart your MCP client. Setup re-downloads the
docs, fast-forwards the SDK checkout and rebuilds the index. A failed download or
build leaves the previous index in place.

## Command line

`query` calls the same code as the MCP tools and prints JSON:

```sh
sylvanas-docs query corpus_status
sylvanas-docs query lookup_symbol --args '{"name":"core.log"}'
sylvanas-docs query list_members --args '{"namespace":"core.graphics"}'
sylvanas-docs query search_docs --args '{"query":"how do I check if target has a debuff"}'
sylvanas-docs query check_lua_api --args '{"code":"core.graphics.draw_circle(p, 5)"}'
```

Other commands: `fetch` (download the docs only), `build-index` (rebuild offline
from an existing download), `serve`. Each takes `--help`.

## How search and checking work

- Search terms are literal words and identifiers, never raw SQL or FTS syntax.
  Question filler ("how", "do", "I", "the", ...) is dropped and the remaining words
  must all match. When that yields fewer than `limit` results, records matching
  any of the words fill the rest as `match_type: "partial"`. Porter stemming makes
  `debuffs`, `interrupting` and `logs` match `debuff`, `interrupt` and `log`.
  There is no semantic (embedding) search, so "draw circle" will not find a page
  that only says "render ring".
- Definitions come from identifier/signature headings in the docs and from Lua
  `function`, `---@class` and `---@field` declarations in the stubs. Prose headings
  that mention an API are not definitions. Docs and stub signatures are reported
  side by side, never merged into a claim about runtime behavior.
- `check_lua_api` is static and best-effort. It catches misspelled or invented
  names, not wrong argument types, and it cannot follow types returned by calls:
  `me:get_buffs()` is checked as "some class has a method get_buffs". Over the
  SDK's example plugins it reports no errors.
- `api_family` (`izi`, `legacy`, `general`) is derived from the URL or SDK
  directory. `document_type` (`docs`, `stub`, `example`) is separate from
  `source_kind` (`documentation` or `lua_source`).
- Every result carries its source URL or path, heading context, hashes and fetch
  time or SDK revision. `content_sha256` identifies the complete indexed record,
  even when the returned preview is clipped.
- Search and lookup return 1–20 results with previews of at most 6,000 characters
  (`content_truncated` says when one is clipped). Section and source reads default
  to 200 lines, allow at most 400 lines or 32,000 characters, and return
  `next_line` to continue.
- `get_section` accepts an exact heading, a full `A > B > C` path, or a `#slug`
  (URL fragments work too). Ambiguous headings are errors that list the choices.
- `read_api_source` only reads files that were indexed, by relative path.
  Absolute paths, `..`, backslashes, symlinks and non-regular files are rejected,
  and a file that changed since indexing is refused until the index is rebuilt.
  On Linux and macOS, reads are descriptor-relative and never follow symlinks. On
  Windows, which lacks those primitives, every path component is checked for
  symlinks and the resolved path must stay inside the checkout.

## Development

```sh
git clone https://github.com/bizkut/sylvanas-docs-mcp
cd sylvanas-docs-mcp
uv venv && uv pip install -e .
.venv/bin/python -m unittest discover -s tests -v    # offline, temporary fixtures
```

To work against a data directory inside the checkout (ignored by git):

```sh
export SYLVANAS_DOCS_HOME="$PWD/.sylvanas"
.venv/bin/sylvanas-docs setup
```

End-to-end MCP check through the official client, covering every tool and a
rejected path traversal:

```sh
.venv/bin/python - <<'PY'
import anyio, sys
from mcp import Client, StdioServerParameters
from sylvanas_docs.__main__ import data_home

params = StdioServerParameters(
    command=sys.executable,
    args=["-m", "sylvanas_docs", "serve", "--database", str(data_home() / "index.sqlite3")],
)

async def main():
    async with Client(params) as client:
        tools = (await client.list_tools()).tools
        assert {tool.name for tool in tools} == {
            "lookup_symbol", "search_docs", "get_section", "search_examples",
            "read_api_source", "list_members", "check_lua_api", "corpus_status"}
        assert all(tool.annotations.read_only_hint for tool in tools)
        calls = [
            ("corpus_status", {}),
            ("lookup_symbol", {"name": "core.log"}),
            ("search_docs", {"query": "izi.spell", "api_family": "izi", "limit": 3}),
            ("get_section", {"url": "https://docs.project-sylvanas.net/dev/api/core",
                             "heading": "core.register_on_update_callback"}),
            ("search_examples", {"query": "core.log", "api_family": "legacy", "limit": 3}),
            ("read_api_source", {"path": "legacy/_api/core.lua", "start_line": 1, "end_line": 20}),
            ("list_members", {"namespace": "core.graphics", "limit": 5}),
            ("check_lua_api", {"code": "core.graphics.draw_circle(p, 5)\ncore.log('x')"}),
        ]
        for name, arguments in calls:
            result = await client.call_tool(name, arguments)
            assert not result.is_error, (name, result.content)
            data = result.structured_content
            if name == "corpus_status":
                assert data["page_count"] > 100 and data["read_only"]
            elif name == "lookup_symbol":
                assert data["definitions"]
            elif name == "list_members":
                assert data["members"]
            elif name == "check_lua_api":
                assert [issue["expression"] for issue in data["issues"]] == ["core.graphics.draw_circle"]
            elif name in {"search_docs", "search_examples"}:
                assert data["results"]
            else:
                assert data["content"]
            print(name, "OK")
        assert (await client.call_tool("read_api_source", {"path": "../README.md"})).is_error
        print("Traversal rejected")

anyio.run(main)
PY
```

## License

The code in this repository is MIT-licensed; see [LICENSE](LICENSE). The Project
Sylvanas documentation and SDK that `setup` downloads belong to their owners and
are not covered by this license.
