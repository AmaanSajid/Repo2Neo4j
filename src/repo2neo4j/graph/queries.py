"""Reusable, parameterized Cypher queries for repo2neo4j graph analytics."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from neo4j import Driver
from neo4j.graph import Node, Relationship
from neo4j.time import Date, DateTime, Duration, Time

logger = logging.getLogger(__name__)

__all__ = ["QueryLibrary"]


def _serialize_value(value: Any) -> Any:
    """Convert Neo4j driver / graph types into JSON-serializable Python values."""
    if value is None:
        return None
    if isinstance(value, DateTime | Date | Time):
        return value.iso_format()
    if isinstance(value, Duration):
        return str(value)
    if isinstance(value, Node):
        return {**dict(value), "_labels": list(value.labels)}
    if isinstance(value, Relationship):
        return {
            **dict(value),
            "_type": value.type,
            "_start": value.start_node.element_id,
            "_end": value.end_node.element_id,
        }
    if isinstance(value, Mapping):
        return {str(k): _serialize_value(v) for k, v in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_serialize_value(v) for v in value]
    return value


class QueryLibrary:
    """High-level read helpers over the repo2neo4j Neo4j graph."""

    def __init__(self, driver: Driver, database: str = "neo4j") -> None:
        self._driver = driver
        self._database = database

    def _read(self, work: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        with self._driver.session(database=self._database) as session:
            return session.execute_read(work, *args, **kwargs)

    @staticmethod
    def _run_list(tx: Any, cypher: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        result = tx.run(cypher, dict(params))
        rows: list[dict[str, Any]] = []
        for record in result:
            rows.append({k: _serialize_value(record[k]) for k in record.keys()})
        return rows

    @staticmethod
    def _run_single(tx: Any, cypher: str, params: Mapping[str, Any]) -> dict[str, Any] | None:
        result = tx.run(cypher, dict(params))
        record = result.single()
        if record is None:
            return None
        return {k: _serialize_value(record[k]) for k in record.keys()}

    def files_changed_in_mr(self, mr_iid: int, repo_name: str) -> list[dict[str, Any]]:
        """Return files touched by an MR (via contained commits) with per-file change rollups."""
        logger.debug(
            "files_changed_in_mr mr_iid=%s repo_name=%r database=%r",
            mr_iid,
            repo_name,
            self._database,
        )
        cypher = """
        MATCH (mr:MergeRequest {iid: $mr_iid, repo_name: $repo_name})
        MATCH (mr)-[:CONTAINS_COMMIT]->(commit:Commit)-[mod:MODIFIES]->(f:File)
        WITH f,
             sum(coalesce(mod.additions, 0)) AS additions,
             sum(coalesce(mod.deletions, 0)) AS deletions,
             collect(DISTINCT mod.status) AS statuses,
             collect(DISTINCT commit.hash) AS commit_hashes
        RETURN f.path AS path,
               f.language AS language,
               additions,
               deletions,
               statuses,
               commit_hashes,
               size(commit_hashes) AS commit_touch_count
        ORDER BY path
        """
        rows = self._read(self._run_list, cypher, {"mr_iid": mr_iid, "repo_name": repo_name})
        if not rows:
            logger.info(
                "files_changed_in_mr: no rows for mr_iid=%s repo_name=%r",
                mr_iid,
                repo_name,
            )
        return rows

    def commit_history_for_file(
        self,
        file_path: str,
        repo_name: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return recent commits that modified a file, newest first."""
        logger.debug(
            "commit_history_for_file path=%r repo_name=%r limit=%s",
            file_path,
            repo_name,
            limit,
        )
        cypher = """
        MATCH (f:File {path: $file_path, repo_name: $repo_name})<-[mod:MODIFIES]-(c:Commit)
        OPTIONAL MATCH (c)-[:AUTHORED_BY]->(a:Author)
        RETURN c.hash AS hash,
               c.short_hash AS short_hash,
               c.message AS message,
               c.timestamp AS timestamp,
               c.repo_name AS repo_name,
               coalesce(mod.additions, 0) AS additions,
               coalesce(mod.deletions, 0) AS deletions,
               mod.status AS status,
               a.name AS author_name,
               a.email AS author_email,
               a.gitlab_username AS author_gitlab_username
        ORDER BY c.timestamp DESC
        LIMIT $limit
        """
        rows = self._read(
            self._run_list,
            cypher,
            {"file_path": file_path, "repo_name": repo_name, "limit": limit},
        )
        if not rows:
            logger.info(
                "commit_history_for_file: no history for path=%r repo_name=%r",
                file_path,
                repo_name,
            )
        return rows

    def function_callers(self, function_name: str, repo_name: str) -> list[dict[str, Any]]:
        """Return functions that call a target function (same short name, same repo)."""
        logger.debug(
            "function_callers name=%r repo_name=%r",
            function_name,
            repo_name,
        )
        cypher = """
        MATCH (callee:Function {repo_name: $repo_name})
        WHERE callee.name = $function_name
        MATCH (caller:Function {repo_name: $repo_name})-[:CALLS]->(callee)
        RETURN DISTINCT caller.name AS caller_name,
               caller.qualified_name AS caller_qualified_name,
               caller.file_path AS caller_file_path,
               callee.name AS callee_name,
               callee.qualified_name AS callee_qualified_name,
               callee.file_path AS callee_file_path
        ORDER BY caller_qualified_name
        """
        rows = self._read(
            self._run_list,
            cypher,
            {"function_name": function_name, "repo_name": repo_name},
        )
        if not rows:
            logger.info(
                "function_callers: no callers for name=%r repo_name=%r",
                function_name,
                repo_name,
            )
        return rows

    def class_hierarchy(self, class_name: str, repo_name: str) -> list[dict[str, Any]]:
        """
        Return inheritance-related class nodes for every Class with the given short name.

        Rows use ``role`` of ``self``, ``ancestor`` (this class inherits from), or
        ``descendant`` (inherits from this class).
        """
        logger.debug("class_hierarchy name=%r repo_name=%r", class_name, repo_name)
        cypher = """
        MATCH (c:Class {repo_name: $repo_name, name: $class_name})
        OPTIONAL MATCH (c)-[:INHERITS*1..]->(anc:Class)
        WITH c, collect(DISTINCT anc) AS ancestors
        OPTIONAL MATCH (desc:Class)-[:INHERITS*1..]->(c)
        WITH c, ancestors, collect(DISTINCT desc) AS descendants
        RETURN c AS self_node,
               ancestors,
               descendants
        """
        rows = self._read(
            self._run_list,
            cypher,
            {"class_name": class_name, "repo_name": repo_name},
        )
        if not rows:
            logger.info(
                "class_hierarchy: no class for name=%r repo_name=%r",
                class_name,
                repo_name,
            )
            return []

        def _class_row(node: dict[str, Any], role: str) -> dict[str, Any]:
            return {
                "role": role,
                "name": node.get("name"),
                "qualified_name": node.get("qualified_name"),
                "file_path": node.get("file_path"),
                "repo_name": node.get("repo_name"),
            }

        out: list[dict[str, Any]] = []
        for raw in rows:
            self_node = raw.get("self_node")
            if isinstance(self_node, dict):
                out.append(_class_row(self_node, "self"))
            for anc in raw.get("ancestors") or []:
                if isinstance(anc, dict):
                    out.append(_class_row(anc, "ancestor"))
            for desc in raw.get("descendants") or []:
                if isinstance(desc, dict):
                    out.append(_class_row(desc, "descendant"))
        if rows and all(
            len([x for x in r.get("ancestors") or [] if isinstance(x, dict)]) == 0
            and len([x for x in r.get("descendants") or [] if isinstance(x, dict)]) == 0
            for r in rows
        ):
            logger.debug(
                "class_hierarchy: no inheritance edges for name=%r repo_name=%r",
                class_name,
                repo_name,
            )
        return out

    def file_dependencies(self, file_path: str, repo_name: str) -> dict[str, Any]:
        """Return files this file imports and files that import it."""
        logger.debug("file_dependencies path=%r repo_name=%r", file_path, repo_name)
        cypher = """
        MATCH (f:File {path: $file_path, repo_name: $repo_name})
        OPTIONAL MATCH (f)-[:IMPORTS]->(out:File)
        WITH f, collect(DISTINCT out.path) AS imports
        OPTIONAL MATCH (inc:File)-[:IMPORTS]->(f)
        RETURN imports,
               collect(DISTINCT inc.path) AS imported_by
        """
        row = self._read(
            self._run_single,
            cypher,
            {"file_path": file_path, "repo_name": repo_name},
        )
        if not row:
            logger.info(
                "file_dependencies: file not found path=%r repo_name=%r",
                file_path,
                repo_name,
            )
            return {"imports": [], "imported_by": []}
        imports = [p for p in (row.get("imports") or []) if p is not None]
        imported_by = [p for p in (row.get("imported_by") or []) if p is not None]
        return {"imports": sorted(imports), "imported_by": sorted(imported_by)}

    def author_contributions(self, author_email: str, repo_name: str) -> dict[str, Any]:
        """Summarize files touched, commits authored, and MRs created for an author in a repo."""
        logger.debug("author_contributions email=%r repo_name=%r", author_email, repo_name)
        cypher = """
        MATCH (a:Author {email: $author_email})
        OPTIONAL MATCH (a)<-[:AUTHORED_BY]-(c:Commit {repo_name: $repo_name})
        OPTIONAL MATCH (c)-[:MODIFIES]->(f:File {repo_name: $repo_name})
        WITH a,
             collect(DISTINCT f.path) AS file_paths,
             collect(DISTINCT c.hash) AS commit_hashes
        OPTIONAL MATCH (mr:MergeRequest {repo_name: $repo_name})-[:CREATED_BY]->(a)
        WITH file_paths,
             commit_hashes,
             collect(DISTINCT mr.iid) AS mr_iids
        RETURN [p IN file_paths WHERE p IS NOT NULL] AS files_modified,
               size([h IN commit_hashes WHERE h IS NOT NULL]) AS commit_count,
               [h IN commit_hashes WHERE h IS NOT NULL] AS commit_hashes,
               size([i IN mr_iids WHERE i IS NOT NULL]) AS merge_request_count,
               [i IN mr_iids WHERE i IS NOT NULL] AS merge_request_iids
        """
        row = self._read(
            self._run_single,
            cypher,
            {"author_email": author_email, "repo_name": repo_name},
        )
        if not row:
            logger.info(
                "author_contributions: unknown author email=%r",
                author_email,
            )
            return {
                "files_modified": [],
                "commit_count": 0,
                "commit_hashes": [],
                "merge_request_count": 0,
                "merge_request_iids": [],
            }
        files_modified = sorted(
            p for p in (row.get("files_modified") or []) if p is not None
        )
        mr_iids = sorted(
            int(i) for i in (row.get("merge_request_iids") or []) if i is not None
        )
        commit_hashes = [h for h in (row.get("commit_hashes") or []) if h is not None]
        return {
            "files_modified": files_modified,
            "commit_count": int(row.get("commit_count") or 0),
            "commit_hashes": commit_hashes,
            "merge_request_count": int(row.get("merge_request_count") or 0),
            "merge_request_iids": mr_iids,
        }

    def hot_files(self, repo_name: str, limit: int = 20) -> list[dict[str, Any]]:
        """Rank files by how often commits modify them (change churn)."""
        logger.debug("hot_files repo_name=%r limit=%s", repo_name, limit)
        cypher = """
        MATCH (:Commit {repo_name: $repo_name})-[m:MODIFIES]->(f:File {repo_name: $repo_name})
        WITH f.path AS path,
             f.language AS language,
             count(m) AS modification_count
        RETURN path,
               language,
               modification_count
        ORDER BY modification_count DESC
        LIMIT $limit
        """
        return self._read(
            self._run_list,
            cypher,
            {"repo_name": repo_name, "limit": limit},
        )

    def mr_risk_score(self, mr_iid: int, repo_name: str) -> dict[str, Any]:
        """
        Heuristic MR risk: file count, overlap with repo-wide hot files, and cross-module spread.

        A *module* is the first path segment (top-level directory or file).
        """
        logger.debug("mr_risk_score mr_iid=%s repo_name=%r", mr_iid, repo_name)
        cypher = """
        MATCH (:Commit {repo_name: $repo_name})-[hm:MODIFIES]->(hf:File {repo_name: $repo_name})
        WITH hf.path AS hot_path, count(hm) AS hot_cnt
        ORDER BY hot_cnt DESC
        LIMIT $hot_limit
        WITH collect(hot_path) AS hot_paths
        MATCH (mr:MergeRequest {iid: $mr_iid, repo_name: $repo_name})
        OPTIONAL MATCH (mr)-[:CONTAINS_COMMIT]->(:Commit)-[mod:MODIFIES]->(imf:File)
        WITH mr, hot_paths,
             collect(DISTINCT imf.path) AS mr_paths
        WITH hot_paths,
             [p IN mr_paths WHERE p IS NOT NULL] AS paths,
             size([p IN mr_paths WHERE p IS NOT NULL]) AS file_count
        UNWIND (CASE WHEN size(paths) = 0 THEN [null] ELSE paths END) AS p
        WITH hot_paths,
             paths,
             file_count,
             CASE WHEN p IS NULL OR p = '' THEN null
                  ELSE split(p, '/')[0]
             END AS top
        WITH hot_paths,
             paths,
             file_count,
             collect(DISTINCT top) AS tops_raw
        WITH hot_paths,
             paths,
             file_count,
             [t IN tops_raw WHERE t IS NOT NULL] AS tops
        WITH hot_paths,
             paths,
             file_count,
             tops,
             [p IN paths WHERE p IS NOT NULL AND p IN hot_paths] AS hot_hits
        RETURN file_count,
               size(hot_hits) AS hot_files_touched,
               hot_hits AS hot_file_paths,
               tops AS module_roots,
               size(tops) AS distinct_module_count,
               (size(tops) > 1) AS cross_module
        """
        hot_limit = 50
        row = self._read(
            self._run_single,
            cypher,
            {
                "mr_iid": mr_iid,
                "repo_name": repo_name,
                "hot_limit": hot_limit,
            },
        )
        if not row:
            logger.info(
                "mr_risk_score: MR not found mr_iid=%s repo_name=%r",
                mr_iid,
                repo_name,
            )
            return {
                "file_count": 0,
                "hot_files_touched": 0,
                "hot_file_paths": [],
                "module_roots": [],
                "distinct_module_count": 0,
                "cross_module": False,
                "risk_score": 0.0,
            }

        file_count = int(row.get("file_count") or 0)
        hot_hits = [p for p in (row.get("hot_file_paths") or []) if p is not None]
        modules = [m for m in (row.get("module_roots") or []) if m is not None]
        cross_module = bool(row.get("cross_module"))
        hot_touch = int(row.get("hot_files_touched") or 0)

        score = float(file_count)
        score += 2.0 * float(hot_touch)
        if cross_module:
            score += 5.0

        return {
            "file_count": file_count,
            "hot_files_touched": hot_touch,
            "hot_file_paths": sorted(set(hot_hits)),
            "module_roots": sorted(set(modules)),
            "distinct_module_count": int(row.get("distinct_module_count") or 0),
            "cross_module": cross_module,
            "risk_score": round(score, 2),
        }

    def recent_changes(
        self,
        repo_name: str,
        days: int = 7,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Recent commits with collected touched file paths."""
        logger.debug(
            "recent_changes repo_name=%r days=%s limit=%s",
            repo_name,
            days,
            limit,
        )
        cypher = """
        MATCH (c:Commit {repo_name: $repo_name})
        WHERE c.timestamp IS NOT NULL
          AND datetime(c.timestamp) >= datetime() - duration({days: $days})
        OPTIONAL MATCH (c)-[:MODIFIES]->(f:File)
        WITH c, collect(DISTINCT f.path) AS file_paths
        RETURN c.hash AS hash,
               c.short_hash AS short_hash,
               c.message AS message,
               c.timestamp AS timestamp,
               c.repo_name AS repo_name,
               [p IN file_paths WHERE p IS NOT NULL] AS files
        ORDER BY c.timestamp DESC
        LIMIT $limit
        """
        rows = self._read(
            self._run_list,
            cypher,
            {"repo_name": repo_name, "days": days, "limit": limit},
        )
        if not rows:
            logger.info(
                "recent_changes: no commits in window repo_name=%r days=%s",
                repo_name,
                days,
            )
        for row in rows:
            files = row.get("files") or []
            row["files"] = sorted({p for p in files if p is not None})
        return rows

    def code_structure(
        self,
        repo_name: str,
        directory: str | None = None,
    ) -> dict[str, Any]:
        """Return classes, functions, and structural edges, optionally limited to a directory prefix."""
        logger.debug("code_structure repo_name=%r directory=%r", repo_name, directory)
        prefix_param: str | None
        if directory is None or directory == "":
            prefix_param = None
        else:
            prefix_param = directory[:-1] if directory.endswith("/") else directory

        cypher = """
        OPTIONAL MATCH (c:Class {repo_name: $repo_name})
        WHERE c IS NULL OR $prefix IS NULL OR c.file_path = $prefix OR c.file_path STARTS WITH ($prefix + '/')
        WITH collect(DISTINCT c) AS classes_raw
        WITH [x IN classes_raw WHERE x IS NOT NULL] AS classes
        OPTIONAL MATCH (fn:Function {repo_name: $repo_name})
        WHERE fn IS NULL OR $prefix IS NULL OR fn.file_path = $prefix OR fn.file_path STARTS WITH ($prefix + '/')
        WITH classes, collect(DISTINCT fn) AS functions_raw
        WITH classes, [x IN functions_raw WHERE x IS NOT NULL] AS functions
        OPTIONAL MATCH (c1:Class)-[:INHERITS]->(c2:Class)
        WHERE c1 IN classes AND c2 IN classes
        WITH classes, functions,
             collect(DISTINCT {child: c1.qualified_name, parent: c2.qualified_name}) AS inherits
        OPTIONAL MATCH (f1:Function)-[:CALLS]->(f2:Function)
        WHERE f1 IN functions AND f2 IN functions
        WITH classes, functions, inherits,
             collect(DISTINCT {caller: f1.qualified_name, callee: f2.qualified_name}) AS calls
        OPTIONAL MATCH (file:File {repo_name: $repo_name})
        WHERE file IS NULL OR $prefix IS NULL OR file.path = $prefix OR file.path STARTS WITH ($prefix + '/')
        OPTIONAL MATCH (file)-[:IMPORTS]->(target:File)
        WITH classes, functions, inherits, calls,
             collect(DISTINCT {from_path: file.path, to_path: target.path}) AS imports
        RETURN [x IN classes | {
          name: x.name,
          qualified_name: x.qualified_name,
          file_path: x.file_path,
          start_line: x.start_line,
          end_line: x.end_line
        }] AS classes,
        [x IN functions | {
          name: x.name,
          qualified_name: x.qualified_name,
          file_path: x.file_path,
          start_line: x.start_line,
          end_line: x.end_line
        }] AS functions,
        [e IN inherits WHERE e.child IS NOT NULL AND e.parent IS NOT NULL] AS inherits,
        [e IN calls WHERE e.caller IS NOT NULL AND e.callee IS NOT NULL] AS calls,
        [e IN imports WHERE e.from_path IS NOT NULL AND e.to_path IS NOT NULL] AS imports
        """

        row = self._read(
            self._run_single,
            cypher,
            {"repo_name": repo_name, "prefix": prefix_param},
        )
        if not row:
            return {
                "classes": [],
                "functions": [],
                "inherits": [],
                "calls": [],
                "imports": [],
            }

        def _sort_edges(items: list[dict[str, Any]], keys: tuple[str, str]) -> list[dict[str, Any]]:
            k1, k2 = keys
            return sorted(
                (e for e in items if isinstance(e, dict)),
                key=lambda d: (str(d.get(k1) or ""), str(d.get(k2) or "")),
            )

        classes = sorted(
            (c for c in (row.get("classes") or []) if isinstance(c, dict)),
            key=lambda d: str(d.get("qualified_name") or ""),
        )
        functions = sorted(
            (f for f in (row.get("functions") or []) if isinstance(f, dict)),
            key=lambda d: str(d.get("qualified_name") or ""),
        )
        return {
            "classes": classes,
            "functions": functions,
            "inherits": _sort_edges(list(row.get("inherits") or []), ("child", "parent")),
            "calls": _sort_edges(list(row.get("calls") or []), ("caller", "callee")),
            "imports": _sort_edges(list(row.get("imports") or []), ("from_path", "to_path")),
        }

    def mr_summary(self, mr_iid: int, repo_name: str) -> dict[str, Any]:
        """Return MR metadata, linked commits, files, reviewers, and a notes count."""
        logger.debug("mr_summary mr_iid=%s repo_name=%r", mr_iid, repo_name)
        cypher = """
        MATCH (mr:MergeRequest {iid: $mr_iid, repo_name: $repo_name})
        OPTIONAL MATCH (mr)-[:CONTAINS_COMMIT]->(c:Commit)
        WITH mr, collect(DISTINCT c) AS commits
        OPTIONAL MATCH (mr)-[:CONTAINS_COMMIT]->(c2:Commit)-[:MODIFIES]->(f:File)
        WITH mr, commits, collect(DISTINCT f.path) AS file_paths
        OPTIONAL MATCH (mr)-[rr:REVIEWED_BY]->(rev:Author)
        WITH mr,
             commits,
             file_paths,
             collect(DISTINCT {
               name: rev.name,
               email: rev.email,
               gitlab_username: rev.gitlab_username,
               approved: coalesce(rr.approved, false)
             }) AS reviewers
        RETURN properties(mr) AS mr_props,
               [x IN commits WHERE x IS NOT NULL | {
                 hash: x.hash,
                 short_hash: x.short_hash,
                 message: x.message,
                 timestamp: x.timestamp,
                 repo_name: x.repo_name
               }] AS commits,
               [p IN file_paths WHERE p IS NOT NULL] AS files,
               reviewers,
               coalesce(mr.notes_count, 0) AS notes_count
        """
        row = self._read(
            self._run_single,
            cypher,
            {"mr_iid": mr_iid, "repo_name": repo_name},
        )
        if not row:
            logger.info(
                "mr_summary: MR not found mr_iid=%s repo_name=%r",
                mr_iid,
                repo_name,
            )
            return {}

        mr_props = dict(row.get("mr_props") or {})
        mr_props = _serialize_value(mr_props)
        if not isinstance(mr_props, dict):
            mr_props = {}

        commits = [c for c in (row.get("commits") or []) if isinstance(c, dict)]
        commits = sorted(commits, key=lambda c: str(c.get("hash") or ""))

        files = sorted({p for p in (row.get("files") or []) if p is not None})

        reviewers_raw = row.get("reviewers") or []
        merged: dict[tuple[str, str], dict[str, Any]] = {}
        for r in reviewers_raw:
            if not isinstance(r, dict):
                continue
            if not any(r.get(k) for k in ("email", "gitlab_username", "name")):
                continue
            key = (str(r.get("email") or ""), str(r.get("gitlab_username") or ""))
            appr = bool(r.get("approved"))
            if key not in merged:
                merged[key] = {
                    "name": r.get("name"),
                    "email": r.get("email"),
                    "gitlab_username": r.get("gitlab_username"),
                    "approved": appr,
                }
            else:
                merged[key]["approved"] = merged[key]["approved"] or appr
        reviewers = sorted(
            merged.values(),
            key=lambda d: str(d.get("email") or d.get("gitlab_username") or d.get("name") or ""),
        )

        notes_count = row.get("notes_count")
        if notes_count is None:
            notes_count = 0
        try:
            notes_count_int = int(notes_count)
        except (TypeError, ValueError):
            notes_count_int = 0

        return {
            "merge_request": mr_props,
            "commits": commits,
            "files": files,
            "reviewers": reviewers,
            "notes_count": notes_count_int,
        }

    def search_functions(self, pattern: str, repo_name: str) -> list[dict[str, Any]]:
        """Case-insensitive substring search on function short names."""
        logger.debug("search_functions pattern=%r repo_name=%r", pattern, repo_name)
        cypher = """
        MATCH (fn:Function {repo_name: $repo_name})
        WHERE toLower(fn.name) CONTAINS toLower($pattern)
        RETURN fn.name AS name,
               fn.qualified_name AS qualified_name,
               fn.file_path AS file_path,
               fn.start_line AS start_line,
               fn.end_line AS end_line
        ORDER BY fn.name, fn.qualified_name
        """
        return self._read(
            self._run_list,
            cypher,
            {"pattern": pattern, "repo_name": repo_name},
        )

    def search_classes(self, pattern: str, repo_name: str) -> list[dict[str, Any]]:
        """Case-insensitive substring search on class short names."""
        logger.debug("search_classes pattern=%r repo_name=%r", pattern, repo_name)
        cypher = """
        MATCH (cls:Class {repo_name: $repo_name})
        WHERE toLower(cls.name) CONTAINS toLower($pattern)
        RETURN cls.name AS name,
               cls.qualified_name AS qualified_name,
               cls.file_path AS file_path,
               cls.start_line AS start_line,
               cls.end_line AS end_line
        ORDER BY cls.name, cls.qualified_name
        """
        return self._read(
            self._run_list,
            cypher,
            {"pattern": pattern, "repo_name": repo_name},
        )
