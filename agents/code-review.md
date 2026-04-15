# Agent instructions: code review (Neo4j-backed)

You assist with **code review** for a repository whose Git history, code structure, and GitLab merge requests are stored in **Neo4j** via **repo2neo4j**. Use the graph for **scope, risk, dependencies, callers, churn, and type hierarchy**; use the actual source diff for line-level correctness.

## Connect to data

**Python (preferred for agents with a runtime):**

```python
from repo2neo4j.config import load_config
from repo2neo4j.agent.query_api import AgentQueryAPI

config = load_config("config.yml")
with AgentQueryAPI.from_config(config) as api:
    api.files_changed_in_mr(123)
```

`AgentQueryAPI` lives in **`repo2neo4j.agent.query_api`**. The repo name is bound from config—you pass **`mr_iid`**, paths, and limits only.

**Dynamic dispatch:** `api.query("files_changed_in_mr", mr_iid=123)` runs the same named method and is useful for thin tooling.

**CLI:** `repo2neo4j query <query_name> --config config.yml` with flags that mirror kwargs (for example MR id, file path, `limit`, `days`, `pattern`, `directory`). Query names are exactly the method names below.

## Queries to use (in order of a typical review)

| Step | Method | Purpose |
|------|--------|---------|
| 1 | **`files_changed_in_mr(mr_iid)`** | Canonical list of paths in the MR—start here. |
| 2 | **`mr_risk_score(mr_iid)`** | Heuristic priority: `risk_score`, `cross_module`, `module_roots`, `hot_files_touched`, `hot_file_paths`, file counts. |
| 3 | **`file_dependencies(file_path)`** | Returns **`imports`** and **`imported_by`** (lists of paths). Map blast radius and layering. |
| 4 | **`function_callers(function_name)`** | Upstream usage of changed or public functions; disambiguate with **`search_functions(pattern)`** first if needed. |
| 5 | **`class_hierarchy(class_name)`** | Inheritance impact for OO changes; pair with **`search_classes(pattern)`** when the name is ambiguous. |
| 6 | **`hot_files(limit=20)`** | Global churn leaders; intersect with MR paths to flag fragile areas. |
| 7 | **`commit_history(file_path, limit=50)`** | Local narrative for sensitive files (ownership, prior fixes). |

Optional: **`code_structure(directory=...)`** if you need classes/functions/edges in a subtree without opening every file.

## Worked example workflow (MR `!123`)

1. `files_changed_in_mr(123)` → enumerate paths for review and testing suggestions.
2. `mr_risk_score(123)` → if `cross_module` is true or `distinct_module_count` is high, spend extra time on integration tests and public APIs.
3. For each public or shared changed file: `file_dependencies(path)` → summarize **`imports`** (what it depends on) and **`imported_by`** (who depends on it).
4. For each renamed or behavior-changing function: `search_functions("foo")` then `function_callers("foo")` (use the graph’s short **`name`** field; adjust if multiple matches).
5. For class refactors: `class_hierarchy("MyClass")` → list bases/subtypes to check overrides and `super()` chains.
6. `hot_files(30)` → mark MR paths that appear in the hot set; call out regression and test-debt risk.

## Graph-informed review checklist

- **Scope:** Every finding about “what changed” should align with **`files_changed_in_mr`** unless you cite untracked or generated files explicitly.
- **Risk score:** Treat **`mr_risk_score`** as a **triage signal**, not a verdict. Explain score drivers (file count, hot touches, cross-module).
- **Call chains:** If **`function_callers`** shows many callers or cross-package callers, stress backward compatibility, defaults, and error contracts.
- **Hot files:** Overlap between MR paths and **`hot_file_paths`** from risk or **`hot_files`** → deeper review, broader tests, watch for flakiness.
- **Hierarchy:** From **`class_hierarchy`**, verify abstract contracts, duplicated logic, and template-method patterns.
- **Dependencies:** From **`file_dependencies`**, flag surprising **`imports`** (lower layer importing upper) or large **`imported_by`** fans.
- **History:** Use **`commit_history`** when the diff is small but the file’s past suggests instability.

## When results are empty or surprising

- Empty **`files_changed_in_mr`** or **`mr_summary`** may mean the MR is not ingested or **`mr_iid`** is wrong—verify config and ingestion, do not invent files.
- **`file_dependencies`** returns `{"imports": [], "imported_by": []}` if the path is missing in the graph (typo or parser gap)—normalize path or check ingestion.

## Example prompts you should handle

- “Review MR `!456` with emphasis on regression risk.” → `files_changed_in_mr`, `mr_risk_score`, `hot_files`, then targeted `file_dependencies` and `function_callers`.
- “Will editing `auth/verify.py` break other packages?” → `file_dependencies("auth/verify.py")`, then `function_callers` on exported functions.
- “Is `db/migrations/001.py` a footgun?” → `commit_history`, `hot_files`, `file_dependencies`.
- “Impact of changing class `ConnectionPool`?” → `search_classes("ConnectionPool")`, `class_hierarchy`, then callers on key methods via `search_functions` + `function_callers`.

End each major claim with **which method** and **which fields** supported it so reviewers can reproduce the analysis.

## Complete `AgentQueryAPI` surface (names only)

Use these exact strings for **`api.query(...)`** or CLI `<query_name>`:  
`files_changed_in_mr`, `commit_history`, `function_callers`, `class_hierarchy`, `file_dependencies`, `author_contributions`, `hot_files`, `mr_risk_score`, `recent_changes`, `code_structure`, `mr_summary`, `search_functions`, `search_classes`.

## Tie graph signals to tests

- **`imported_by`** fans → suggest contract tests, smoke tests for dependents, or staged rollout notes.
- **`function_callers`** breadth → matrix tests on argument combinations or error codes.
- **`class_hierarchy`** depth → tests for overrides, serialization, and polymorphic dispatch.
- **`hot_files`** overlap → flaky-test audit, snapshot updates, and performance checks if those files are on critical paths.

## CLI snippets (same review order)

```bash
repo2neo4j query files_changed_in_mr --config config.yml --mr-iid 456
repo2neo4j query mr_risk_score --config config.yml --mr-iid 456
repo2neo4j query file_dependencies --config config.yml --file-path src/core/job.py
repo2neo4j query function_callers --config config.yml --function-name handle_event
repo2neo4j query class_hierarchy --config config.yml --class-name JobRunner
repo2neo4j query hot_files --config config.yml --limit 30
repo2neo4j query commit_history --config config.yml --file-path src/core/job.py --limit 50
```

## More example prompts

- “List everything MR `!88` touches, then rank by risk.” → `files_changed_in_mr`, then per-file `file_dependencies` for top fan-out paths; `mr_risk_score` for global framing.
- “Any subclasses we must update?” → `search_classes` + `class_hierarchy` for each candidate type in the diff.
- “Is this refactor safe for callers outside this package?” → `file_dependencies` on the changed file, then `function_callers` for each exported function.
- “What changed recently in the same files?” → `recent_changes` with a window that covers the MR branch lifetime, compared to `files_changed_in_mr`.

## Anti-patterns

- Do not infer MR file lists from local `git diff` alone when **`files_changed_in_mr`** is available—the graph is the shared source of truth for ingested MRs.
- Do not treat **`function_callers`** as complete if static analysis missed dynamic calls; label graph results as **static** coverage.
- Do not dismiss **`mr_risk_score`** without reading its components; a high score with a tiny diff still deserves a sentence on **why** (for example hot files or `cross_module`).

## Output shape for humans

Open with **scope** (`files_changed_in_mr`), then **risk** (`mr_risk_score`), then **impact** (`file_dependencies`, `function_callers`, `class_hierarchy` as needed), then **tests and follow-ups**. Keep each section short and cite queries in parentheses.
