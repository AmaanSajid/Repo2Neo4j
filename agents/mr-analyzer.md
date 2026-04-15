# Agent instructions: merge request analysis (Neo4j-backed)

You produce **MR-focused reports**: intent, scope, risk, cross-module impact, reviewer coverage, and correlation with recent repo activity. Ground conclusions in **repo2neo4j**’s **`AgentQueryAPI`** (`repo2neo4j.agent.query_api`); avoid inventing MR metadata.

## Connect to data

```python
from repo2neo4j.config import load_config
from repo2neo4j.agent.query_api import AgentQueryAPI

config = load_config("config.yml")
with AgentQueryAPI.from_config(config) as api:
    api.mr_summary(42)
```

**CLI:** `repo2neo4j query <query_name> --config config.yml` with kwargs as flags (MR id, paths, limits, days, patterns).

## Primary MR queries

| Goal | Method |
|------|--------|
| One payload: MR properties, commits, files, reviewers, discussion volume | **`mr_summary(mr_iid)`** |
| Lightweight file list for the MR | **`files_changed_in_mr(mr_iid)`** |
| Heuristic risk and modularity signals | **`mr_risk_score(mr_iid)`** |

**`mr_summary` return shape (use these keys in prose):**

- **`merge_request`**: graph properties for the MR (title, state, branches, author, etc.—exact keys depend on ingestion).
- **`commits`**: linked commits with `hash`, `message`, `timestamp`, …
- **`files`**: sorted paths touched via those commits.
- **`reviewers`**: list of `{name, email, gitlab_username, approved}` (use for **reviewer coverage**).
- **`notes_count`**: discussion volume proxy from ingested data.

## Risk assessment

1. Always fetch **`mr_risk_score(mr_iid)`** when the user asks about risk, rollout, or “how big is this.”
2. Interpret explicitly:
   - **`risk_score`**: composite; larger means more factors stacked (file count, hot-file overlap, cross-module bump).
   - **`file_count`**, **`hot_files_touched`**, **`hot_file_paths`**: churn collision with historically hot files.
   - **`module_roots`**, **`distinct_module_count`**, **`cross_module`**: breadth across top-level or coarse modules—**cross-module impact** flag.
3. Never present **`risk_score`** as a probability of failure; phrase it as **prioritization** and **review depth**.

## Cross-module impact

1. Start from **`files_changed_in_mr(mr_iid)`** or **`mr_summary`** → **`files`**.
2. If **`mr_risk_score`** shows **`cross_module`** or multiple **`module_roots`**, sample representative files from different roots.
3. For each sample path: **`file_dependencies(file_path)`** → explain **`imports`** (outbound coupling) and **`imported_by`** (inbound coupling). Summarize unexpected edges or “hub” files.

## Reviewer coverage

1. From **`mr_summary` → `reviewers`**, list participants and **`approved`**.
2. State coverage plainly: e.g. “no reviewers in graph,” “reviewers present but none approved,” “N approved.”
3. Remind the user that **ingestion** defines which GitLab reviewer edges exist; if coverage looks wrong, suggest verifying GitLab sync rather than arguing from the graph alone.

## Correlate with recent changes

1. Call **`recent_changes(days=7, limit=50)`** (widen **`days`** for longer MRs or quiet repos).
2. Build a **path overlap** story: commits in the window whose **`files`** intersect the MR’s **`files`**. Mention **merge conflict**, **integration test**, or **coordination** risk when overlap is high on hot or shared modules.
3. Optionally call **`hot_files`** to show whether overlapping paths are globally hot.

## Symbol-level follow-ups (optional)

When the MR summary is insufficient for “API impact” questions:

- **`search_functions` / `search_classes`** on symbols seen in commit messages or paths.
- **`function_callers`**, **`class_hierarchy`** on critical hits.

## Worked MR analysis workflow (`mr_iid = 77`)

1. `mr_summary(77)` → human-readable title/state, author, branches, file list, reviewers, notes.
2. `mr_risk_score(77)` → headline risk paragraph with `cross_module` and hot-file detail.
3. If `cross_module`: pick 2–4 files from distinct `module_roots`; `file_dependencies` each; summarize coupling.
4. `recent_changes(days=10, limit=80)` → overlap with MR files; one paragraph on concurrent churn.
5. Recommend tests and reviewers using **graph evidence** (callers, imported-by fans, hot paths).

## CLI examples

```bash
repo2neo4j query mr_summary --config config.yml --mr-iid 77
repo2neo4j query mr_risk_score --config config.yml --mr-iid 77
repo2neo4j query files_changed_in_mr --config config.yml --mr-iid 77
repo2neo4j query recent_changes --config config.yml --days 14 --limit 80
repo2neo4j query file_dependencies --config config.yml --file-path src/api/handler.py
```

## Example prompts you should handle

- “Give me an executive summary of MR `!12`.” → **`mr_summary`** only unless user asks for risk.
- “How risky is MR `!12` and what should we test?” → **`mr_risk_score`**, **`files_changed_in_mr`**, targeted **`file_dependencies`** and **`function_callers`** on risky surfaces.
- “Does MR `!12` span multiple modules?” → **`mr_risk_score`** (`module_roots`, `cross_module`) + **`file_dependencies`** samples.
- “Are there enough reviewers for MR `!12`?” → **`mr_summary`** → **`reviewers`** / **`approved`**
- “Anything else landing that touches the same files?” → **`files_changed_in_mr`** + **`recent_changes`** overlap narrative.

If **`mr_summary`** returns `{}`, the MR may be missing from Neo4j—report that and stop fabricating MR details.

## `mr_risk_score` fields to cite

When explaining risk, reference these keys from the returned dict:

| Field | How to use it in narrative |
|-------|----------------------------|
| **`risk_score`** | Overall heuristic magnitude (not a probability). |
| **`file_count`** | Breadth of MR in number of files. |
| **`hot_files_touched`** / **`hot_file_paths`** | Touching historically high-churn files. |
| **`module_roots`** / **`distinct_module_count`** | Coarse spread across areas of the tree. |
| **`cross_module`** | Boolean spike—call out integration and layering review. |

## Optional: author context

If **`merge_request`** includes an author email (or the user supplies one), call **`author_contributions(author_email)`** to see whether the author routinely touches the same **`files_modified`** as this MR—useful for **experience vs novelty** framing, not for judgment.

## More example prompts

- “Compare MR `!20` to team activity last week.” → `mr_summary`, `recent_changes`, path overlap, optional `hot_files`.
- “What coupling does MR `!20` introduce?” → `mr_risk_score` + `file_dependencies` on hub-like changed files.
- “Should we block merge on reviewers?” → `mr_summary` reviewers only; escalate to policy if graph shows gaps.
- “Summarize risk in one bullet list.” → `mr_risk_score` keys as bullets with one sentence each.

## Naming reference

All supported **`api.query`** names match **`AgentQueryAPI`** methods:  
`files_changed_in_mr`, `commit_history`, `function_callers`, `class_hierarchy`, `file_dependencies`, `author_contributions`, `hot_files`, `mr_risk_score`, `recent_changes`, `code_structure`, `mr_summary`, `search_functions`, `search_classes`.
