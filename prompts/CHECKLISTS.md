# Pre-Commit Review Checklists

Single source of truth for all review checklists (BIBLE P5: DRY).
Loaded into static context and injected into multi_model_review prompt.

### Review-exempt operations

- `git reset --hard` — rollback to known state
- `git revert` — mechanical inverse of reviewed commit
- Pure VERSION/pyproject.toml bump (release only, no logic change)

---

## Repo Commit Checklist

| # | item | what to check | severity |
|---|------|---------------|----------|
| 1 | bible_compliance | Does the diff violate any BIBLE.md principle (P0–P1)? | critical |
| 2 | safety_files_intact | Safety-critical files (BIBLE.md, safety.py, registry.py, SYSTEM.md, CONSCIOUSNESS.md) modified without explicit approval? | critical |
| 3 | no_secrets | API keys, tokens, passwords, .env values in diff? | critical |
| 4 | code_quality | Syntax errors, import errors, logic bugs in changed files? `python -c "import ouroboros.MODULE"` passes? | critical |
| 5 | tests_pass | `pytest tests/` green? New module has tests? | critical |
| 6 | version_bump | Functional change → VERSION updated? pyproject.toml matches VERSION? (PASS if no behavior change) | conditional-critical |
| 7 | tool_registration | New/removed/renamed tool → registry updated? Schemas valid? (PASS if no tool change) | conditional-critical |
| 8 | context_building | Changed context.py or prompts → system prompt assembles without error? (PASS if not applicable) | conditional-critical |
| 9 | shrink_guard | Any file shrunk >70% without explanation? | conditional-critical |
| 10 | scratchpad_updated | Scratchpad reflects what was done. | advisory |
| 11 | knowledge_updated | New pattern/insight recorded in knowledge base? | advisory |
| 12 | changelog_entry | Commit message describes what changed and why. | advisory |

### Severity rules

- Items 1–5: always critical — FAIL blocks commit.
- Items 6–9: conditionally critical — FAIL only when condition applies.
- Items 10–12: advisory — FAIL produces warning, does not block.

For each item: assess **PASS**, **FAIL**, or **N/A**.
Overall verdict: **PASS** (all critical items pass) or **FAIL** (any critical fails).
