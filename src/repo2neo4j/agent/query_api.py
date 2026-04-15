"""High-level Neo4j query API for agents, with repo name bound from construction."""

from __future__ import annotations

import logging
from typing import Any, Final

from neo4j import Driver, GraphDatabase

from repo2neo4j.config import AppConfig
from repo2neo4j.graph.queries import QueryLibrary

logger = logging.getLogger(__name__)

_QUERY_NAMES: Final[frozenset[str]] = frozenset(
    {
        "files_changed_in_mr",
        "commit_history",
        "function_callers",
        "class_hierarchy",
        "file_dependencies",
        "author_contributions",
        "hot_files",
        "mr_risk_score",
        "recent_changes",
        "code_structure",
        "mr_summary",
        "search_functions",
        "search_classes",
    }
)


class AgentQueryAPI:
    """Agent-facing facade over :class:`QueryLibrary` with ``repo_name`` pre-bound."""

    def __init__(
        self,
        neo4j_uri: str,
        neo4j_user: str,
        neo4j_password: str,
        database: str = "neo4j",
        repo_name: str = "",
    ) -> None:
        self._repo_name = repo_name
        self._driver: Driver = GraphDatabase.driver(
            neo4j_uri,
            auth=(neo4j_user, neo4j_password),
        )
        self._queries = QueryLibrary(self._driver, database=database)
        logger.debug(
            "AgentQueryAPI connected uri=%s database=%s repo_name=%r",
            neo4j_uri,
            database,
            repo_name,
        )

    @classmethod
    def from_config(cls, config: AppConfig) -> AgentQueryAPI:
        neo = config.neo4j
        return cls(
            neo4j_uri=neo.uri,
            neo4j_user=neo.username,
            neo4j_password=neo.password,
            database=neo.database,
            repo_name=config.repository.name,
        )

    def close(self) -> None:
        self._driver.close()
        logger.debug("AgentQueryAPI closed")

    def __enter__(self) -> AgentQueryAPI:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None:
        self.close()

    def query(self, name: str, **kwargs: Any) -> Any:
        """Run a named query method on this API (for CLIs and dynamic agents)."""
        if name not in _QUERY_NAMES:
            available = ", ".join(sorted(_QUERY_NAMES))
            raise ValueError(f"Unknown query {name!r}. Available: {available}")
        method = getattr(self, name)
        return method(**kwargs)

    def files_changed_in_mr(self, mr_iid: int) -> list[dict]:
        return self._queries.files_changed_in_mr(mr_iid, self._repo_name)

    def commit_history(self, file_path: str, limit: int = 50) -> list[dict]:
        return self._queries.commit_history_for_file(
            file_path, self._repo_name, limit
        )

    def function_callers(self, function_name: str) -> list[dict]:
        return self._queries.function_callers(function_name, self._repo_name)

    def class_hierarchy(self, class_name: str) -> list[dict]:
        return self._queries.class_hierarchy(class_name, self._repo_name)

    def file_dependencies(self, file_path: str) -> dict:
        return self._queries.file_dependencies(file_path, self._repo_name)

    def author_contributions(self, author_email: str) -> dict:
        return self._queries.author_contributions(author_email, self._repo_name)

    def hot_files(self, limit: int = 20) -> list[dict]:
        return self._queries.hot_files(self._repo_name, limit)

    def mr_risk_score(self, mr_iid: int) -> dict:
        return self._queries.mr_risk_score(mr_iid, self._repo_name)

    def recent_changes(self, days: int = 7, limit: int = 50) -> list[dict]:
        return self._queries.recent_changes(self._repo_name, days, limit)

    def code_structure(self, directory: str | None = None) -> dict:
        return self._queries.code_structure(self._repo_name, directory)

    def mr_summary(self, mr_iid: int) -> dict:
        return self._queries.mr_summary(mr_iid, self._repo_name)

    def search_functions(self, pattern: str) -> list[dict]:
        return self._queries.search_functions(pattern, self._repo_name)

    def search_classes(self, pattern: str) -> list[dict]:
        return self._queries.search_classes(pattern, self._repo_name)
