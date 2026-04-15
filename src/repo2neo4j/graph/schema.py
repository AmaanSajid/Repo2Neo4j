"""Neo4j constraints, indexes, and schema lifecycle for repo2neo4j.

Node labels (unique keys):
    Repository: name
    Branch: (name, repo_name)
    Commit: hash
    Author: email
    File: (path, repo_name)
    Directory: (path, repo_name)
    MergeRequest: (iid, repo_name)
    Class: (qualified_name, repo_name)
    Function: (qualified_name, repo_name)
    SyncState: repo_name

Relationship types used by the graph model:
    HAS_BRANCH, HAS_COMMIT, AUTHORED_BY, PARENT_OF, MODIFIES,
    CONTAINS_COMMIT, CREATED_BY, TARGETS, SOURCES, REVIEWED_BY,
    IN_DIRECTORY, CHILD_OF, IMPORTS, CONTAINS_CLASS, CONTAINS_FUNCTION,
    HAS_METHOD, INHERITS, CALLS
"""

from __future__ import annotations

import logging
import re
from typing import Final

from neo4j import Driver, GraphDatabase  # noqa: F401 - driver factory (project import convention)
from neo4j.exceptions import Neo4jError

logger = logging.getLogger(__name__)

__all__ = [
    "CONSTRAINTS",
    "INDEXES",
    "RELATIONSHIP_TYPES",
    "initialize_schema",
    "drop_schema",
    "verify_schema",
]

RELATIONSHIP_TYPES: Final[frozenset[str]] = frozenset(
    {
        "HAS_BRANCH",
        "HAS_COMMIT",
        "AUTHORED_BY",
        "PARENT_OF",
        "MODIFIES",
        "CONTAINS_COMMIT",
        "CREATED_BY",
        "TARGETS",
        "SOURCES",
        "REVIEWED_BY",
        "IN_DIRECTORY",
        "CHILD_OF",
        "IMPORTS",
        "CONTAINS_CLASS",
        "CONTAINS_FUNCTION",
        "HAS_METHOD",
        "INHERITS",
        "CALLS",
    }
)

CONSTRAINTS: list[str] = [
    (
        "CREATE CONSTRAINT repository_name_unique IF NOT EXISTS "
        "FOR (n:Repository) REQUIRE n.name IS UNIQUE"
    ),
    (
        "CREATE CONSTRAINT branch_repo_name_unique IF NOT EXISTS "
        "FOR (n:Branch) REQUIRE (n.name, n.repo_name) IS UNIQUE"
    ),
    (
        "CREATE CONSTRAINT commit_hash_unique IF NOT EXISTS "
        "FOR (n:Commit) REQUIRE n.`hash` IS UNIQUE"
    ),
    (
        "CREATE CONSTRAINT author_email_unique IF NOT EXISTS "
        "FOR (n:Author) REQUIRE n.email IS UNIQUE"
    ),
    (
        "CREATE CONSTRAINT file_repo_path_unique IF NOT EXISTS "
        "FOR (n:File) REQUIRE (n.path, n.repo_name) IS UNIQUE"
    ),
    (
        "CREATE CONSTRAINT directory_repo_path_unique IF NOT EXISTS "
        "FOR (n:Directory) REQUIRE (n.path, n.repo_name) IS UNIQUE"
    ),
    (
        "CREATE CONSTRAINT merge_request_repo_iid_unique IF NOT EXISTS "
        "FOR (n:MergeRequest) REQUIRE (n.iid, n.repo_name) IS UNIQUE"
    ),
    (
        "CREATE CONSTRAINT class_repo_qualified_name_unique IF NOT EXISTS "
        "FOR (n:Class) REQUIRE (n.qualified_name, n.repo_name) IS UNIQUE"
    ),
    (
        "CREATE CONSTRAINT function_repo_qualified_name_unique IF NOT EXISTS "
        "FOR (n:Function) REQUIRE (n.qualified_name, n.repo_name) IS UNIQUE"
    ),
    (
        "CREATE CONSTRAINT sync_state_repo_name_unique IF NOT EXISTS "
        "FOR (n:SyncState) REQUIRE n.repo_name IS UNIQUE"
    ),
]

INDEXES: list[str] = [
    (
        "CREATE INDEX idx_commit_timestamp IF NOT EXISTS "
        "FOR (n:Commit) ON (n.timestamp)"
    ),
    "CREATE INDEX idx_file_language IF NOT EXISTS FOR (n:File) ON (n.language)",
    (
        "CREATE INDEX idx_merge_request_state IF NOT EXISTS "
        "FOR (n:MergeRequest) ON (n.state)"
    ),
    (
        "CREATE INDEX idx_merge_request_created_at IF NOT EXISTS "
        "FOR (n:MergeRequest) ON (n.created_at)"
    ),
    "CREATE INDEX idx_author_name IF NOT EXISTS FOR (n:Author) ON (n.name)",
    "CREATE INDEX idx_function_name IF NOT EXISTS FOR (n:Function) ON (n.name)",
    "CREATE INDEX idx_class_name IF NOT EXISTS FOR (n:Class) ON (n.name)",
]

_DDL_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"CREATE\s+CONSTRAINT\s+(?P<name>\S+)\s+IF\s+NOT\s+EXISTS",
    flags=re.IGNORECASE | re.DOTALL,
)
_DDL_INDEX_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"CREATE\s+(?:RANGE\s+)?INDEX\s+(?P<name>\S+)\s+IF\s+NOT\s+EXISTS",
    flags=re.IGNORECASE | re.DOTALL,
)


def _expected_schema_object_names() -> list[str]:
    names: list[str] = []
    for stmt in CONSTRAINTS:
        m = _DDL_NAME_PATTERN.search(stmt)
        if not m:
            msg = f"Could not parse constraint name from DDL: {stmt!r}"
            raise ValueError(msg)
        names.append(m.group("name"))
    for stmt in INDEXES:
        m = _DDL_INDEX_NAME_PATTERN.search(stmt)
        if not m:
            msg = f"Could not parse index name from DDL: {stmt!r}"
            raise ValueError(msg)
        names.append(m.group("name"))
    return names


def _quote_cypher_identifier(name: str) -> str:
    """Quote a schema object name for safe use in DROP DDL."""
    escaped = name.replace("`", "``")
    return f"`{escaped}`"


def initialize_schema(driver: Driver, database: str = "neo4j") -> None:
    """Create all constraints and indexes if they do not already exist."""
    logger.info(
        "Initializing Neo4j schema on database %r (%d constraints, %d indexes)",
        database,
        len(CONSTRAINTS),
        len(INDEXES),
    )
    try:
        with driver.session(database=database) as session:
            for stmt in CONSTRAINTS:
                session.run(stmt)
            for stmt in INDEXES:
                session.run(stmt)
    except Neo4jError:
        logger.exception("Schema initialization failed for database %r", database)
        raise
    logger.info("Neo4j schema initialization completed for database %r", database)


def drop_schema(driver: Driver, database: str = "neo4j") -> None:
    """Drop every constraint and user index in the database (destructive reset)."""
    logger.warning("Dropping all constraints and indexes on database %r", database)
    try:
        with driver.session(database=database) as session:
            constraint_names = [record["name"] for record in session.run("SHOW CONSTRAINTS")]
            for name in constraint_names:
                stmt = f"DROP CONSTRAINT {_quote_cypher_identifier(name)} IF EXISTS"
                session.run(stmt)

            index_rows = session.run(
                "SHOW INDEXES YIELD name, type WHERE type <> 'LOOKUP' RETURN name"
            )
            index_names = [record["name"] for record in index_rows]
            for name in index_names:
                stmt = f"DROP INDEX {_quote_cypher_identifier(name)} IF EXISTS"
                session.run(stmt)
    except Neo4jError:
        logger.exception("Schema drop failed for database %r", database)
        raise
    logger.warning("Finished dropping constraints and indexes on database %r", database)


def verify_schema(driver: Driver, database: str = "neo4j") -> dict[str, bool]:
    """Return whether each expected constraint and index from this module exists."""
    expected = _expected_schema_object_names()
    try:
        with driver.session(database=database) as session:
            constraint_names = {record["name"] for record in session.run("SHOW CONSTRAINTS")}
            index_names = {record["name"] for record in session.run("SHOW INDEXES")}
    except Neo4jError:
        logger.exception("Schema verification failed for database %r", database)
        raise

    present = constraint_names | index_names
    result = {name: name in present for name in expected}
    missing = [name for name, ok in result.items() if not ok]
    if missing:
        logger.warning(
            "Schema verification on %r: missing %d object(s): %s",
            database,
            len(missing),
            ", ".join(missing),
        )
    else:
        logger.info("Schema verification on %r: all expected objects present", database)
    return result
