---
name: mcp-tools-test-preexisting-fail
description: tests/test_mcp.py::test_all_ten_tools_registered fails on branch feature/phase2-codex, pre-existing
metadata:
  type: project
---

On branch `feature/phase2-codex`, `tests/test_mcp.py::test_all_ten_tools_registered`
fails: the MCP server now registers extra tools (`para_delete`, `para_complete`,
`para_reclassify`, `para_update`) that the test's `ALL_TOOLS` expected-set doesn't
include. This predates the Phase-2 Codex work (IMP-01/02/18) — confirmed by
`git stash` + rerun on 2026-07-26. The rest of the suite (149 tests) passes.

**Why:** avoid re-investigating this as a regression from unrelated changes.
**How to apply:** if touching MCP, update `ALL_TOOLS` in test_mcp.py to match the
registered tools; otherwise ignore this single failure.
