"""Tests for :class:`repo2neo4j.parsers.git_parser.GitParser`."""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from conftest import git_init_minimal

from repo2neo4j.models.git import DiffStatus
from repo2neo4j.parsers.git_parser import GitParser

_GIT_WORK_ROOT = Path(__file__).resolve().parent.parent / ".pytest_git_work"


def test_get_branches_marks_default_and_sorts(git_parser: GitParser, temp_git_repo: Path) -> None:
    branches = git_parser.get_branches(default_branch="main")
    names = [b.name for b in branches]
    assert names == ["feature", "main"]

    default = [b for b in branches if b.is_default]
    assert len(default) == 1
    assert default[0].name == "main"
    assert default[0].head_commit_hash == git_parser.repo.head.commit.hexsha

    feature = next(b for b in branches if b.name == "feature")
    assert feature.head_commit_hash is not None
    assert feature.head_commit_hash != default[0].head_commit_hash


def test_iter_commits_yields_commit_models_newest_first(git_parser: GitParser) -> None:
    commits = list(git_parser.iter_commits(branch="main"))
    assert len(commits) >= 3
    messages = [c.message.splitlines()[0] for c in commits]
    assert messages[0].startswith("add util")
    assert messages[-1].startswith("init:")

    tip = commits[0]
    assert len(tip.hash) == 40
    assert tip.short_hash == tip.hash[:8]
    assert tip.author.email == "author@example.com"
    assert tip.committer.email == "author@example.com"
    assert isinstance(tip.parent_hashes, list)


def test_iter_commits_since_hash_incremental(git_parser: GitParser, temp_git_repo: Path) -> None:
    meta = (temp_git_repo / ".git_fixture_meta.txt").read_text(encoding="utf-8").splitlines()
    c1, _c2, _c3 = meta[0], meta[1], meta[2]

    incremental = list(git_parser.iter_commits(branch="main", since_hash=c1))
    full = list(git_parser.iter_commits(branch="main"))

    assert len(incremental) == len(full) - 1
    assert all(cm.hash != c1 for cm in incremental)
    assert incremental[0].hash == full[0].hash


def test_iter_commits_max_count(git_parser: GitParser) -> None:
    limited = list(git_parser.iter_commits(branch="main", max_count=1))
    assert len(limited) == 1


def test_iter_commits_invalid_since_falls_back_to_full_history(git_parser: GitParser) -> None:
    full = list(git_parser.iter_commits(branch="main"))
    fallback = list(git_parser.iter_commits(branch="main", since_hash="not-a-real-sha"))
    assert len(fallback) == len(full)


def test_get_file_tree_sorted_at_head(git_parser: GitParser) -> None:
    paths = git_parser.get_file_tree()
    assert paths == sorted(paths)
    assert "README.md" in paths
    assert "src/mod.py" in paths
    assert "src/pkg/util.py" in paths


def test_get_file_tree_empty_repo() -> None:
    _GIT_WORK_ROOT.mkdir(parents=True, exist_ok=True)
    empty_dir = _GIT_WORK_ROOT / f"empty_{uuid.uuid4().hex}"
    empty_dir.mkdir()
    try:
        git_init_minimal(empty_dir)
        parser = GitParser(empty_dir)
        # No commits -> no HEAD tree
        assert parser.get_file_tree() == []
    finally:
        shutil.rmtree(empty_dir, ignore_errors=True)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", DiffStatus.MODIFIED),
        ("  ", DiffStatus.MODIFIED),
        ("A", DiffStatus.ADDED),
        ("a", DiffStatus.ADDED),
        ("D", DiffStatus.DELETED),
        ("M", DiffStatus.MODIFIED),
        ("R", DiffStatus.RENAMED),
        ("C", DiffStatus.COPIED),
        ("T", DiffStatus.MODIFIED),
        ("added", DiffStatus.ADDED),
        ("DELETED", DiffStatus.DELETED),
        ("renamed", DiffStatus.RENAMED),
        ("copied", DiffStatus.COPIED),
        ("modified", DiffStatus.MODIFIED),
        ("weird", DiffStatus.MODIFIED),
        ("X", DiffStatus.MODIFIED),
    ],
)
def test_map_change_type(raw: str, expected: DiffStatus) -> None:
    assert GitParser._map_change_type(MagicMock(), raw) == expected
