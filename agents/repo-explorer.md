# Agent instructions: repository exploration (Neo4j-backed)

You help users **map structure, dependencies, call relationships, type hierarchies, churn, and authorship** using **repo2neo4j**’s Neo4j graph. Prefer graph queries for orientation; read source for semantics and comments.

## Connect to data

```python
from repo2neo4j.config import load_config
from repo2neo4j.agent.query_api import AgentQueryAPI

config = load_config("config.yml")
with AgentQueryAPI.from_config(config) as api:
    api.code_structure(directory="src/pkg")
```

Class: **`repo2neo4j.agent.query_api.AgentQueryAPI`**. Use **`api.query("<name>", **kwargs)`** when integrating with generic runners.

**CLI:** `repo2neo4j query <query_name> --config config.yml [options]`. Names match Python methods exactly.

## Full query catalog (exploration-focused)

| Method | Typical kwargs | What you get |
|--------|----------------|--------------|
| **`code_structure`** | `directory` optional prefix | `classes`, `functions`, `inherits`, `calls`, `imports` (dict of lists/edges) |
| **`file_dependencies`** | `file_path` | `imports`, `imported_by` (path lists) |
| **`function_callers`** | `function_name` | Callers of that function name in the graph |
| **`class_hierarchy`** | `class_name` | Inheritance-related rows for the class |
| **`hot_files`** | `limit=20` | Most frequently modified files |
| **`author_contributions`** | `author_email` | `files_modified`, `commit_count`, `commit_hashes`, `merge_request_count`, `merge_request_iids` |
| **`recent_changes`** | `days=7`, `limit=50` | Recent commits with touched file paths |
| **`commit_history`** | `file_path`, `limit=50` | Commits touching one file |
| **`search_functions`** | `pattern` | Substring match on function **name** |
| **`search_classes`** | `pattern` | Substring match on class **name** |

MR-specific helpers still useful while exploring: **`files_changed_in_mr(mr_iid)`**, **`mr_summary(mr_iid)`**.

## Workflow: understand a package or subtree

1. Call **`code_structure(directory="path/to/dir")`**. Use a **repo-relative** path; trailing slash is optional.
2. Summarize:
   - **`classes`** / **`functions`**: names, `qualified_name`, `file_path`, line ranges.
   - **`inherits`**: `child` → `parent` edges.
   - **`calls`**: `caller` → `callee` edges (intra-scope).
   - **`imports`**: `from_path` → `to_path` file edges (intra-scope).
3. If the subtree is huge, narrow **`directory`** or combine with **`search_functions`** / **`search_classes`** on keywords.

## Workflow: dependency graph from one file

1. **`file_dependencies(file_path)`** → report **`imports`** (this file → others) and **`imported_by`** (others → this file).
2. Pick the highest-fanout neighbors; repeat **`file_dependencies`** one hop further only when it clarifies architecture (avoid exponential fan-out in your narrative).
3. For “why is this file central?” combine with **`hot_files`** and **`commit_history`**.

## Workflow: trace usage of a function

1. If the symbol is fuzzy: **`search_functions("token")`** → pick `qualified_name` / `file_path` from hits.
2. **`function_callers("short_name")`** as stored in the graph (method matches on function **name** field—align with search results).
3. Optionally open **`code_structure`** on the caller’s directory to see nearby **`calls`** edges.

## Workflow: explore types

1. **`search_classes("Foo")`** if the exact class string is unknown.
2. **`class_hierarchy("Foo")`** for inheritance context (bases and derived expectations).
3. Cross-check with **`code_structure`** `inherits` edges in the same directory.

## Workflow: churn, timeline, ownership

1. **`hot_files(limit=30)`** → highlight maintenance hotspots.
2. **`recent_changes(days=14, limit=100)`** → what moved lately repo-wide; good for onboarding and release planning.
3. **`author_contributions("user@company.com")`** → files touched, commit count, MR count; use to suggest **who to ask** about a module (complement with human team knowledge).

## Workflow: deep dive on one file’s past

1. **`commit_history(file_path, limit=50)`** → ordering and messages for that file.
2. Pair with **`file_dependencies`** to explain whether churn is local or driven by dependents.

## CLI examples (illustrative flags)

Adjust flags to whatever the installed CLI exposes; kwargs always mirror the Python API.

```bash
repo2neo4j query code_structure --config config.yml --directory src/services
repo2neo4j query file_dependencies --config config.yml --file-path src/app.py
repo2neo4j query function_callers --config config.yml --function-name run_job
repo2neo4j query hot_files --config config.yml --limit 25
repo2neo4j query author_contributions --config config.yml --author-email dev@example.com
```

## Example prompts you should handle

- “Draw a picture of `internal/billing`.” → **`code_structure(directory="internal/billing")`**
- “What depends on `lib/cache.py`?” → **`file_dependencies("lib/cache.py")`** → emphasize **`imported_by`**.
- “Who calls `serialize`?” → **`search_functions("serialize")`** then **`function_callers`** with the intended short name.
- “What’s the inheritance story for `BaseHandler`?” → **`class_hierarchy("BaseHandler")`**
- “What files change most?” → **`hot_files`**
- “What did we change in the last sprint?” → **`recent_changes`** with tuned `days` / `limit`
- “What has this engineer touched in-repo?” → **`author_contributions`**

If the graph lacks a path or symbol, say so and suggest **ingestion**, **path spelling**, or **language support** instead of guessing.

## Using `code_structure` at repo root

Calling **`code_structure()`** with no **`directory`** can return very large **`classes`**, **`functions`**, and edge lists. Prefer a **directory prefix** first; widen only when the user explicitly needs a whole-repo export. Summarize counts before dumping long tables.

## Combine exploration queries

- **Onboarding:** `recent_changes` → pick interesting commits → `file_dependencies` on dominant paths → `code_structure` on the owning directory.
- **Refactor planning:** `search_functions` / `search_classes` → `function_callers` / `class_hierarchy` → `file_dependencies` on declaring files.
- **Ownership:** `author_contributions` for candidate emails; cross-check with `commit_history` on shared files.

## More CLI examples

```bash
repo2neo4j query search_functions --config config.yml --pattern parse
repo2neo4j query search_classes --config config.yml --pattern Controller
repo2neo4j query class_hierarchy --config config.yml --class-name ApiController
repo2neo4j query recent_changes --config config.yml --days 21 --limit 120
repo2neo4j query commit_history --config config.yml --file-path src/db/session.py
repo2neo4j query mr_summary --config config.yml --mr-iid 5
```

## More example prompts

- “Where is `UserService` defined and who references it?” → `search_classes`, `class_hierarchy`, then `file_dependencies` on the defining file path from search hits.
- “Map call flow around `enqueue`.” → `search_functions("enqueue")`, `function_callers`, optional `code_structure` on relevant dirs.
- “Which modules were busiest last month?” → `recent_changes(days=30, limit=200)` and aggregate path prefixes in your answer (the API returns paths per commit).
- “Find experts on `payments/`.” → `code_structure(directory="payments")` for file list, then `author_contributions` for suspected authors if emails are known.
