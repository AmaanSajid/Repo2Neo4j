"""Tests for :class:`repo2neo4j.graph.ingester.GraphIngester` with mocked Neo4j driver."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from repo2neo4j.graph.ingester import GraphIngester
from repo2neo4j.models.git import AuthorModel, CommitModel


def _commit(n: int) -> CommitModel:
    author = AuthorModel(name="Dev", email="dev@example.com")
    ts = datetime(2025, 1, 1, tzinfo=UTC)
    h = f"{n:040x}"
    return CommitModel(
        hash=h,
        message=f"commit {n}",
        author=author,
        committer=author,
        timestamp=ts,
        parent_hashes=[],
        diffs=[],
        branch="main",
    )


class _CaptureTx:
    def __init__(self, runs: list[tuple[str, dict[str, Any]]]) -> None:
        self._runs = runs

    def run(self, query: str, **kwargs: Any) -> None:
        self._runs.append((query, kwargs))


class _CaptureSession:
    def __init__(self, runs: list[tuple[str, dict[str, Any]]], batch_sizes: list[int]) -> None:
        self._runs = runs
        self._batch_sizes = batch_sizes

    def __enter__(self) -> _CaptureSession:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def execute_write(self, fn: Any, *args: Any) -> None:
        if args and args[0] is not None and hasattr(args[0], "__len__"):
            # commit / branch batches are lists; repository tx has no extra args
            try:
                self._batch_sizes.append(len(args[0]))
            except TypeError:
                pass
        tx = _CaptureTx(self._runs)
        if args:
            fn(tx, *args)
        else:
            fn(tx)


@pytest.fixture
def capture_driver() -> tuple[MagicMock, list[tuple[str, dict[str, Any]]], list[int]]:
    runs: list[tuple[str, dict[str, Any]]] = []
    batch_sizes: list[int] = []

    driver = MagicMock()

    def _session(**_: Any) -> _CaptureSession:
        return _CaptureSession(runs, batch_sizes)

    driver.session.side_effect = _session
    return driver, runs, batch_sizes


def test_build_directory_chain_nested() -> None:
    ingester = GraphIngester(MagicMock(), repo_name="r")
    # Filename is excluded from the directory segments (`module.py` -> ['src', 'pkg']).
    assert ingester._build_directory_chain("src/pkg/module.py") == [
        ("src", ""),
        ("src/pkg", "src"),
    ]
    assert ingester._build_directory_chain("src/pkg/module/__init__.py") == [
        ("src", ""),
        ("src/pkg", "src"),
        ("src/pkg/module", "src/pkg"),
    ]


def test_build_directory_chain_single_segment_file() -> None:
    ingester = GraphIngester(MagicMock(), repo_name="r")
    assert ingester._build_directory_chain("README.md") == []


def test_build_directory_chain_top_level_only_dir() -> None:
    ingester = GraphIngester(MagicMock(), repo_name="r")
    assert ingester._build_directory_chain("src/file.py") == [("src", "")]


def test_chunk_iterable_batches_and_tail() -> None:
    ingester = GraphIngester(MagicMock(), repo_name="r", batch_size=999)
    batches = list(ingester._chunk_iterable(range(10), 3))
    assert batches == [[0, 1, 2], [3, 4, 5], [6, 7, 8], [9]]


def test_ingest_repository_merges_repository(capture_driver: tuple) -> None:
    driver, runs, _batch_sizes = capture_driver
    ingester = GraphIngester(driver, database="neo4j", repo_name="acme", batch_size=50)
    ingester.ingest_repository(name="acme", url="https://example.com/r.git", default_branch="develop")

    assert runs, "expected at least one Cypher execution"
    first_query = runs[0][0]
    assert "MERGE (r:Repository {name: $name})" in first_query
    assert runs[0][1] == {
        "name": "acme",
        "url": "https://example.com/r.git",
        "default_branch": "develop",
    }


def test_ingest_commits_batch_sizes(capture_driver: tuple) -> None:
    driver, _runs, batch_sizes = capture_driver
    ingester = GraphIngester(driver, database="neo4j", repo_name="acme", batch_size=2)
    commits = [_commit(i) for i in range(5)]
    ingester.ingest_commits(commits)

    assert batch_sizes == [2, 2, 1]


def test_ingest_commits_empty_iterable_no_writes(capture_driver: tuple) -> None:
    driver, runs, batch_sizes = capture_driver
    ingester = GraphIngester(driver, database="neo4j", repo_name="acme", batch_size=10)
    ingester.ingest_commits(iter(()))

    assert runs == []
    assert batch_sizes == []
