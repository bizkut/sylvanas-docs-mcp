"""Behavioral contracts for lexical retrieval, attribution and filesystem bounds."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import unittest.mock

from sylvanas_docs.index import RetrievalError, build_index, digest
from sylvanas_docs.retrieval import Corpus

REVISION = "a" * 40
BASE = "https://docs.project-sylvanas.net"


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.corpus = self.root / "corpus"
        self.sources = self.root / "sources"
        self.corpus.mkdir()
        self.sources.mkdir()
        self.source = self.sources / "legacy/_api/core.lua"
        self.source.parent.mkdir(parents=True)
        self.source.write_text("---@class core\n--- Log a message.\nfunction core.log(message) end\n---@field public count number\n", encoding="utf-8", newline="\n")
        example = self.sources / "izi/demo/main.lua"
        example.parent.mkdir(parents=True)
        example.write_text("local spell = izi.spell(123)\ncore.log('example')\n", encoding="utf-8", newline="\n")
        self.pages, self.chunks = [], []
        self.add_page("/dev/api/core", "Core", "# Core\n\n## `core.log`\n\nA logging definition.\n\n```lua\ncore.log('hello')\n```\n\n### Notes\n\nChild explanation.\n\n## Other\n\nNot in the log section.\n", [(["Core", "core.log"], "## `core.log`\n\nA logging definition.\n```lua\ncore.log('hello')\n```\n")])
        self.add_page("/dev/api/guide", "Guide", "# Guide\n\n## Tips\n\ncore.log core.log core.log mentions, not definitions.\n", [(["Guide", "Tips"], "core.log core.log core.log mentions, not definitions.")])
        self.add_page("/dev/libraries/izi/izi-spells", "IZI", "# IZI\n\n## `izi.spell`\n\nCreate a spell.\n", [(["IZI", "izi.spell"], "## `izi.spell`\n\nCreate a spell.\n")])
        self.add_page("/examples/izi/demo", "Demo", "# Demo\n\n## Sample\n\n```lua\ncore.log(izi.spell(123))\n```\n", [(["Demo", "Sample"], "```lua\ncore.log(izi.spell(123))\n```\n")])
        self.add_page("/dev/sections", "Sections", "# Sections\n\n## First\n\n### Shared\n\nfirst content\n\n```lua\n## Not a heading\n```\n\n## Second\n\n### Shared\n\nsecond content\n", [(["Sections", "First"], "first content")])
        (self.corpus / "manifest.json").write_text(json.dumps({"scope": ["/dev/**", "/examples/**"], "asset_failure_count": 7}), newline="\n")
        self.database = self.root / "index.sqlite3"
        self.rebuild()

    def add_page(self, route, title, content, chunks):
        path = "pages/" + route.strip("/") + ".md"
        output = self.corpus / path
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(content, encoding="utf-8", newline="\n")
        self.pages.append({"source_url": BASE + route, "canonical_url": BASE + route, "markdown_path": path, "markdown_sha256": digest(content.encode()), "source_sha256": digest(("html:" + content).encode()), "title": title, "fetched_at": "2026-10-22T00:00:00Z"})
        for headings, text in chunks:
            self.chunks.append({"id": str(len(self.chunks)), "url": BASE + route, "page_title": title, "heading_path": headings, "context": " > ".join(headings), "content": text, "content_sha256": digest(text.encode()), "part": 0})

    def write_records(self):
        for filename, records in (("pages.jsonl", self.pages), ("chunks.jsonl", self.chunks)):
            (self.corpus / filename).write_text("".join(json.dumps(row) + "\n" for row in records), encoding="utf-8", newline="\n")

    def rebuild(self):
        self.write_records()
        build_index(self.corpus, self.sources, self.database, REVISION)
        self.service = Corpus(self.database)

    def test_exact_definition_beats_repeated_mentions(self):
        result = self.service.search_docs("core.log", document_type="docs")
        self.assertEqual(result["results"][0]["heading_path"], ["Core", "core.log"])
        self.assertEqual(result["results"][0]["match_type"], "exact_symbol")
        lookup = self.service.lookup_symbol("core:log()")
        self.assertEqual({row["document_type"] for row in lookup["definitions"]}, {"docs", "stub"})
        self.assertTrue(all(row["match_type"] == "exact_symbol" for row in lookup["definitions"]))
        self.assertTrue(any("mentions" in row["content"] for row in lookup["mentions"]))
        suffix = self.service.lookup_symbol("log")
        self.assertTrue(all(row["match_type"] == "symbol_suffix" for row in suffix["definitions"]))

    def test_questions_drop_filler_and_fall_back_to_partial_matches(self):
        results = self.service.search_docs("how do I create a spell", limit=5)["results"]
        self.assertEqual(results[0]["context"], "IZI > izi.spell")
        self.assertEqual(results[0]["match_type"], "lexical")
        partial = self.service.search_docs("logging create", limit=5)["results"]
        self.assertEqual({item["match_type"] for item in partial}, {"partial"})
        self.assertIn("Core > core.log", [item["context"] for item in partial])

    def add_api_fixtures(self):
        (self.sources / "legacy/_api/globals.lua").write_text("_G.core = {}\n", encoding="utf-8", newline="\n")
        module = self.sources / "legacy/_api/common/modules/demo_mod.lua"
        module.parent.mkdir(parents=True)
        module.write_text("---@class demo_mod\n---Runs the demo.\n---@field public run fun(self: demo_mod): boolean\n", encoding="utf-8", newline="\n")
        (self.sources / "izi/demo/extra.lua").write_text("core.secret_helper()\nlocal x = get()\nx:hidden_method()\n", encoding="utf-8", newline="\n")
        self.rebuild()

    def test_check_lua_api_flags_invented_names_with_suggestions(self):
        self.add_api_fixtures()
        code = """local demo = require("common/modules/demo_mod")
local own = require("helpers")
local log = core.log
local function f(unit)
  core.log("x -- core.fake()") -- core.fake_comment()
  core.logg("typo")
  demo:run()
  demo:runn()
  own:anything()
  unit:totally_made_up()
  log("aliased")
end
"""
        result = self.service.check_lua_api(code)
        errors = {item["expression"]: item for item in result["issues"] if item["severity"] == "error"}
        self.assertEqual(set(errors), {"core.logg", "demo:runn", "unit:totally_made_up"})
        self.assertEqual(errors["core.logg"]["suggestions"][0], "core.log")
        self.assertEqual(errors["demo:runn"]["suggestions"], ["demo_mod.run"])
        self.assertEqual(errors["unit:totally_made_up"]["kind"], "unknown_method")
        self.assertIn("core.log", [item["name"] for item in result["verified"]])
        self.assertIn("demo_mod.run", [item["name"] for item in result["verified"]])
        self.assertFalse(result["ok"])
        self.assertTrue(self.service.check_lua_api("core.log('fine')")["ok"])

    def test_check_lua_api_downgrades_names_used_by_official_examples(self):
        self.add_api_fixtures()
        result = self.service.check_lua_api("core.secret_helper()\nlocal me = get()\nme:hidden_method()\n")
        self.assertEqual([item["severity"] for item in result["issues"]], ["warning", "warning"])
        self.assertTrue(result["ok"])
        with self.assertRaises(RetrievalError):
            self.service.check_lua_api("x" * 100_001)

    def test_list_members_returns_signatures_and_rejects_unknown_namespaces(self):
        self.add_api_fixtures()
        members = self.service.list_members("demo_mod")["members"]
        self.assertEqual([item["name"] for item in members], ["demo_mod.run"])
        self.assertEqual(members[0]["summary"], "Runs the demo.")
        self.assertEqual(members[0]["signatures"][0]["signature"], "run fun(self: demo_mod): boolean")
        core = self.service.list_members("core")
        self.assertIn("core.log", [item["name"] for item in core["members"]])
        with self.assertRaises(RetrievalError):
            self.service.list_members("demo_modd")

    def test_search_matches_word_stems(self):
        self.assertIn("Core > core.log", [item["context"] for item in self.service.search_docs("logs definitions")["results"]])

    def test_signature_headings_define_only_the_declared_identifier(self):
        headings = [
            "`core.object_manager.get_local_player() -> game_object`",
            "`get_spell_cooldown(spell_id)`",
            "core.spellbook:get_spell_cooldown(spell_id) -> number",
            "Using `core.log` safely",
            "Call get_spell_cooldown(spell_id) for details",
            "Overview",
        ]
        self.add_page("/dev/api/signatures", "Signatures",
                      "# Signatures\n\n" + "".join("## " + heading + "\n\nSignature details.\n\n" for heading in headings),
                      [(["Signatures", heading], "## " + heading + "\n\nSignature details.") for heading in headings])
        self.rebuild()
        for name, heading in zip(("core.object_manager.get_local_player", "get_spell_cooldown", "core.spellbook.get_spell_cooldown"), headings):
            with self.subTest(name=name):
                definitions = self.service.lookup_symbol(name)["definitions"]
                docs = [row for row in definitions if row["source_kind"] == "documentation" and row["match_type"] == "exact_symbol"]
                self.assertEqual(len(docs), 1)
                self.assertEqual(docs[0]["url"], BASE + "/dev/api/signatures")
                self.assertEqual(docs[0]["heading_path"][-1], heading)
                self.assertEqual(docs[0]["match_type"], "exact_symbol")
                self.assertEqual(self.service.search_docs(name)["results"][0]["match_type"], "exact_symbol")
        self.assertFalse(self.service.lookup_symbol("game_object")["definitions"])
        self.assertFalse(self.service.lookup_symbol("spell_id")["definitions"])
        self.assertFalse(self.service.lookup_symbol("Overview")["definitions"])
        self.assertFalse(any(row["url"] == BASE + "/dev/api/signatures" for row in self.service.lookup_symbol("core.log")["definitions"]))

    def test_lookup_mentions_have_an_independent_limit(self):
        for name, definition_match in (("core.log", "exact_symbol"), ("core:log()", "exact_symbol"), ("log", "symbol_suffix")):
            with self.subTest(name=name):
                lookup = self.service.lookup_symbol(name, limit=1)
                self.assertEqual(len(lookup["definitions"]), 1)
                self.assertEqual(lookup["definitions"][0]["match_type"], definition_match)
                self.assertTrue(lookup["has_more_definitions"])
                self.assertEqual(len(lookup["mentions"]), 1)
                self.assertEqual(lookup["mentions"][0]["match_type"], "lexical")
                self.assertEqual(lookup["mentions"][0]["url"], BASE + "/dev/api/guide")

    def test_family_type_and_example_filters(self):
        result = self.service.search_docs("spell", api_family="izi", document_type="docs")
        self.assertTrue(result["results"])
        self.assertTrue(all(row["api_family"] == "izi" and row["document_type"] == "docs" for row in result["results"]))
        stubs = self.service.search_docs("core.log", document_type="stub")
        self.assertTrue(stubs["results"])
        self.assertTrue(all(row["source_kind"] == "lua_source" for row in stubs["results"]))
        examples = self.service.search_examples("core.log", api_family="izi")
        self.assertEqual({row["source_kind"] for row in examples["results"]}, {"documentation", "lua_source"})
        self.assertTrue(all(row["document_type"] != "stub" for row in examples["results"]))
        inline = self.service.search_examples("hello", api_family="legacy")
        self.assertEqual(inline["results"][0]["document_type"], "docs")

    def test_dev_example_overview_is_an_example_without_code(self):
        self.add_page("/dev/examples", "Examples", "# Examples\n\nObject Manager Scanning overview.\n",
                      [(["Examples"], "Object Manager Scanning overview.")])
        self.add_page("/dev/examples/object-manager", "Object Manager", "# Object Manager\n\nObject Manager Scanning walkthrough.\n",
                      [(["Object Manager"], "Object Manager Scanning walkthrough.")])
        self.add_page("/dev/examples-other", "Not Examples", "# Not Examples\n\nObject Manager Scanning reference.\n",
                      [(["Not Examples"], "Object Manager Scanning reference.")])
        self.rebuild()
        expected = {BASE + "/dev/examples", BASE + "/dev/examples/object-manager"}
        filtered = self.service.search_docs("Object Manager Scanning", document_type="example")["results"]
        examples = self.service.search_examples("Object Manager Scanning")["results"]
        for results in (filtered, examples):
            self.assertEqual({row["url"] for row in results}, expected)
            self.assertTrue(all(row["document_type"] == "example" and row["is_example"] for row in results))
        self.assertEqual(self.service.get_section(BASE + "/dev/examples/object-manager")["document_type"], "example")

    def test_lua_class_fields_and_provenance(self):
        result = self.service.lookup_symbol("core.count")["definitions"][0]
        self.assertEqual(result["document_type"], "stub")
        self.assertEqual(result["repository_revision"], REVISION)
        self.assertEqual(result["source_sha256"], digest(self.source.read_bytes()))
        self.assertIn("/blob/" + REVISION + "/legacy/_api/core.lua", result["source_url"])
        self.assertTrue(result["heading_path"])
        docs = self.service.lookup_symbol("core.log")["definitions"][0]
        self.assertEqual(docs["source_sha256"], self.pages[0]["source_sha256"])
        self.assertEqual(docs["fetched_at"], self.pages[0]["fetched_at"])

    def test_section_subtree_order_and_fragments(self):
        section = self.service.get_section(BASE + "/dev/api/core/", "core.log")
        self.assertIn("### Notes", section["content"])
        self.assertNotIn("## Other", section["content"])
        self.assertLess(section["content"].index("hello"), section["content"].index("Child explanation"))
        duplicate = self.service.get_section(BASE + "/dev/sections#shared-1")
        self.assertIn("second content", duplicate["content"])
        self.assertNotIn("first content", duplicate["content"])
        with self.assertRaisesRegex(RetrievalError, "Ambiguous heading"):
            self.service.get_section(BASE + "/dev/sections", "Shared")
        first = self.service.get_section(BASE + "/dev/sections", "Sections > First > Shared")
        self.assertIn("## Not a heading", first["content"])
        with self.assertRaisesRegex(RetrievalError, "Heading not found"):
            self.service.get_section(BASE + "/dev/sections", "Not a heading")
        with self.assertRaisesRegex(RetrievalError, "not in the documentation index"):
            self.service.get_section("https://example.org/dev")

    def test_sections_preserve_source_heading_ids_and_duplicate_fragments(self):
        content = "# Anchors\n\n## Callback Functions 🔄\n\nFirst callback section.\n\n## Callback Functions 🔄\n\nSecond callback section.\n\n## Custom\n\nExplicit source anchor.\n"
        self.add_page("/dev/anchors", "Anchors", content, [(["Anchors", "Custom"], "Explicit source anchor.")])
        raw = b'<h2 id="outside-article">Ignore navigation</h2><article><div class="theme-doc-markdown"><h1>Anchors</h1><h2 id="callback-functions-">Callback Functions \xf0\x9f\x94\x84</h2><h2 id="callback-functions--1">Callback Functions \xf0\x9f\x94\x84</h2><h2 id="source-custom-id">Custom</h2></div></article>'
        raw = raw.replace(b'<h2 id="source-custom-id">', b'<h2></h2><h2>&#8203;</h2><h2 id="source-custom-id">')
        self.pages[-1]["raw_path"] = "raw/anchors.html"
        self.pages[-1]["source_sha256"] = digest(raw)
        (self.corpus / "raw").mkdir()
        (self.corpus / "raw/anchors.html").write_bytes(raw)
        self.rebuild()
        first = self.service.get_section(BASE + "/dev/anchors#callback-functions-")
        self.assertEqual(first["heading_slug"], "callback-functions-")
        self.assertIn("First callback section.", first["content"])
        self.assertNotIn("Second callback section.", first["content"])
        second = self.service.get_section(BASE + "/dev/anchors#callback-functions--1")
        self.assertIn("Second callback section.", second["content"])
        self.assertEqual(self.service.get_section(BASE + "/dev/anchors", "#source-custom-id")["heading_path"], ["Anchors", "Custom"])
        with self.assertRaisesRegex(RetrievalError, "Ambiguous heading"):
            self.service.get_section(BASE + "/dev/anchors", "Callback Functions 🔄")

    def test_source_read_is_bounded_and_detects_changed_bytes(self):
        result = self.service.read_api_source("legacy/_api/core.lua", 2, 3)
        self.assertEqual(result["content"], "--- Log a message.\nfunction core.log(message) end\n")
        self.assertEqual(result["next_line"], 4)
        self.assertEqual(result["returned_content_sha256"], digest(result["content"].encode()))
        self.source.write_text("changed", encoding="utf-8", newline="\n")
        with self.assertRaisesRegex(RetrievalError, "changed since indexing"):
            self.service.read_api_source("legacy/_api/core.lua")

    def test_traversal_absolute_unindexed_and_symlink_paths_rejected(self):
        for path in ("../secret", "/etc/passwd", "legacy/../../secret", "legacy\\_api\\core.lua", "legacy//_api/core.lua", "legacy/./_api/core.lua", ".git/config"):
            with self.subTest(path=path), self.assertRaises(RetrievalError):
                self.service.read_api_source(path)
        outside = self.root / "outside.lua"
        outside.write_bytes(self.source.read_bytes())
        self.source.unlink()
        self.source.symlink_to(outside)
        with self.assertRaisesRegex(RetrievalError, "symlink"):
            self.service.read_api_source("legacy/_api/core.lua")
        self.source.unlink()
        self.source.write_bytes(outside.read_bytes())
        directory = self.source.parent
        directory.rename(directory.with_name("saved"))
        directory.symlink_to(directory.with_name("saved"), target_is_directory=True)
        with self.assertRaisesRegex(RetrievalError, "symlink"):
            self.service.read_api_source("legacy/_api/core.lua")

    def test_portable_reads_reject_the_same_paths(self):
        # The fallback used where descriptor-relative opens are unavailable (Windows).
        with unittest.mock.patch("sylvanas_docs.index.DESCRIPTOR_READS", False):
            self.assertTrue(self.service.read_api_source("legacy/_api/core.lua")["content"])
            self.test_traversal_absolute_unindexed_and_symlink_paths_rejected()

    def test_invalid_limits_filters_and_fts_syntax(self):
        for kwargs in ({"limit": 0}, {"limit": 21}, {"limit": True}, {"api_family": "unknown"}, {"document_type": "runtime"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(RetrievalError):
                self.service.search_docs("core", **kwargs)
        for query in ("", "***", "x" * 513):
            with self.subTest(query=query), self.assertRaises(RetrievalError):
                self.service.search_docs(query)
        # Operators and quotes are literal terms, not executable FTS expressions.
        # NOT would exclude everything if executed; as a literal word it matches only text containing "not".
        literal = self.service.search_docs('" OR NOT * :')['results']
        self.assertEqual([item["context"] for item in literal], ["Guide > Tips"])
        self.assertIn("Core > core.log", [item["context"] for item in self.service.search_docs('"zzz" NOT core')['results']])
        with self.assertRaises(RetrievalError):
            self.service.read_api_source("legacy/_api/core.lua", 1, 401)

    def test_snapshot_is_read_only_and_failed_build_is_atomic(self):
        # sqlite3's own context manager does not close the connection, and Windows
        # cannot delete the temporary directory while the file is open.
        with closing(self.service.connect()) as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("DELETE FROM pages")
        original = self.database.read_bytes()
        self.chunks[0]["content_sha256"] = "broken"
        self.write_records()
        with self.assertRaisesRegex(RetrievalError, "content hash"):
            build_index(self.corpus, self.sources, self.database, REVISION)
        self.assertEqual(self.database.read_bytes(), original)
        status = self.service.corpus_status()
        self.assertEqual(status["page_count"], 5)
        self.assertEqual(status["source_file_count"], 2)
        self.assertEqual(status["asset_failure_count"], 7)
        self.assertTrue(status["read_only"])
        self.assertFalse(status["embeddings"])


if __name__ == "__main__":
    unittest.main()
