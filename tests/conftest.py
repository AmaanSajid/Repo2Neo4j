"""Shared pytest fixtures for repo2neo4j."""

from __future__ import annotations

import shutil
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from git import Repo

from repo2neo4j.models.code import ClassModel, FileModel, FunctionModel, ImportModel
from repo2neo4j.models.git import AuthorModel, BranchModel, CommitModel, DiffStatus, FileDiffModel
from repo2neo4j.models.gitlab import MRReviewModel, MRState, MergeRequestModel
from repo2neo4j.parsers.git_parser import GitParser

# Git writes under the workspace so tests run in sandboxes that only permit
# writes inside the project tree (system temp is often blocked).
_GIT_WORK_ROOT = Path(__file__).resolve().parent.parent / ".pytest_git_work"


def git_init_minimal(repo_dir: Path) -> Repo:
    """Initialize a repo without copying template hooks (sandbox-friendly)."""
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "-c", "init.defaultBranch=main", "init", "--template=", str(repo_dir)],
        check=True,
        capture_output=True,
        text=True,
    )
    return Repo(repo_dir)


@pytest.fixture
def temp_git_repo() -> Path:
    """A real Git repo with linear history, two branches, and nested files."""
    _GIT_WORK_ROOT.mkdir(parents=True, exist_ok=True)
    repo_dir = _GIT_WORK_ROOT / f"repo_{uuid.uuid4().hex}"
    repo = git_init_minimal(repo_dir)

    with repo.config_writer() as cw:
        cw.set_value("user", "name", "Fixture Author")
        cw.set_value("user", "email", "author@example.com")

    readme = repo_dir / "README.md"
    readme.write_text("root\n", encoding="utf-8")
    repo.index.add([str(readme.relative_to(repo_dir))])
    c1 = repo.index.commit("init: readme")

    src = repo_dir / "src" / "mod.py"
    src.parent.mkdir(parents=True)
    src.write_text("x = 1\n", encoding="utf-8")
    repo.index.add([str(src.relative_to(repo_dir))])
    c2 = repo.index.commit("add module")

    nested = repo_dir / "src" / "pkg" / "util.py"
    nested.parent.mkdir(parents=True)
    nested.write_text("def f():\n    return 2\n", encoding="utf-8")
    repo.index.add([str(nested.relative_to(repo_dir))])
    c3 = repo.index.commit("add util")

    # Normalize default branch name for assertions across Git versions.
    repo.git.branch("-M", "main")

    feature = repo.create_head("feature", repo.head.commit)
    feature.checkout()
    feat_file = repo_dir / "feature.txt"
    feat_file.write_text("branch work\n", encoding="utf-8")
    repo.index.add(["feature.txt"])
    repo.index.commit("feature-only commit")

    repo.heads.main.checkout()

    # Expose hashes for incremental-range tests (oldest .. tip on main).
    (repo_dir / ".git_fixture_meta.txt").write_text(
        f"{c1.hexsha}\n{c2.hexsha}\n{c3.hexsha}\n", encoding="utf-8"
    )

    try:
        yield repo_dir
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)


@pytest.fixture
def git_parser(temp_git_repo: Path) -> GitParser:
    return GitParser(temp_git_repo)


@pytest.fixture
def sample_author() -> AuthorModel:
    return AuthorModel(name="Ada Lovelace", email="ada@example.com")


@pytest.fixture
def sample_commit_model(sample_author: AuthorModel) -> CommitModel:
    ts = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)
    return CommitModel(
        hash="a" * 40,
        message="sample: tweak parser\n",
        author=sample_author,
        committer=sample_author,
        timestamp=ts,
        parent_hashes=["b" * 40],
        diffs=[
            FileDiffModel(
                path="src/foo.py",
                old_path=None,
                status=DiffStatus.MODIFIED,
                additions=3,
                deletions=1,
            )
        ],
        branch="main",
    )


@pytest.fixture
def sample_branch_model() -> BranchModel:
    return BranchModel(name="develop", is_default=False, head_commit_hash="c" * 40)


@pytest.fixture
def sample_file_model() -> FileModel:
    fn = FunctionModel(
        name="greet",
        qualified_name="app.greet",
        file_path="app.py",
        start_line=1,
        end_line=3,
        parameters=["name"],
        return_type="str",
        is_method=False,
        class_name=None,
        calls=["print"],
    )
    cls = ClassModel(
        name="Widget",
        qualified_name="app.Widget",
        file_path="app.py",
        start_line=10,
        end_line=40,
        bases=["BaseWidget"],
        methods=[
            FunctionModel(
                name="run",
                qualified_name="app.Widget.run",
                file_path="app.py",
                start_line=12,
                end_line=20,
                parameters=["self"],
                return_type=None,
                is_method=True,
                class_name="Widget",
                calls=["helper"],
            )
        ],
    )
    return FileModel(
        path="app.py",
        language="python",
        size=120,
        classes=[cls],
        functions=[fn],
        imports=[
            ImportModel(
                source_file="app.py",
                imported_name="os",
                module_path="stdlib/os.py",
                alias=None,
            )
        ],
    )


@pytest.fixture
def sample_merge_request_model() -> MergeRequestModel:
    created = datetime(2024, 7, 15, 9, 30, tzinfo=UTC)
    review = MRReviewModel(
        reviewer_name="Bob",
        reviewer_email="bob@example.com",
        reviewer_username="bob",
        approved=True,
        created_at=datetime(2024, 7, 16, 10, 0, tzinfo=UTC),
    )
    return MergeRequestModel(
        iid=42,
        title="Add feature X",
        description="Implements X with tests.",
        state=MRState.OPENED,
        source_branch="feature",
        target_branch="main",
        author_name="Ada Lovelace",
        author_username="ada",
        created_at=created,
        updated_at=created,
        web_url="https://gitlab.example.com/proj/-/merge_requests/42",
        commit_hashes=["deadbeef" + "0" * 32],
        reviews=[review],
        labels=["backend", "needs-review"],
    )
