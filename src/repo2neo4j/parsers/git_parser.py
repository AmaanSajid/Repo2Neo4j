"""Git repository parsing using GitPython.

Designed for large histories: uses ``repo.iter_commits()`` and per-commit lazy
loading so callers can stream commits and batch persistence (e.g. Neo4j) without
holding the full history in memory.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import git
from git import NULL_TREE, Repo
from git.objects.blob import Blob
from gitdb.exc import BadName

if TYPE_CHECKING:
    from collections.abc import Iterator

    from git.diff import Diff

from repo2neo4j.models.git import (
    AuthorModel,
    BranchModel,
    CommitModel,
    DiffStatus,
    FileDiffModel,
)

logger = logging.getLogger(__name__)


class GitParser:
    """Parse a local Git repository into Pydantic models."""

    def __init__(self, repo_path: str | Path) -> None:
        self._repo_path = Path(repo_path).expanduser().resolve()
        try:
            self._repo = Repo(str(self._repo_path))
        except git.exc.InvalidGitRepositoryError as exc:
            msg = f"Not a valid Git repository: {self._repo_path}"
            raise git.exc.InvalidGitRepositoryError(msg) from exc

        if self._repo.bare:
            logger.debug("Opened bare repository at %s", self._repo_path)

    @property
    def repo(self) -> Repo:
        """Underlying GitPython :class:`~git.repo.base.Repo` instance."""
        return self._repo

    def get_branches(self, default_branch: str = "main") -> list[BranchModel]:
        """List local branches and mark the default.

        Resolution order for ``is_default``:

        1. ``default_branch`` if it exists
        2. ``main`` if it exists
        3. ``master`` if it exists
        4. :attr:`git.Repo.active_branch` (skipped on detached HEAD or errors)
        """
        heads = list(self._repo.heads)
        names = {h.name for h in heads}

        resolved: str | None = None
        if default_branch in names:
            resolved = default_branch
        elif "main" in names:
            resolved = "main"
        elif "master" in names:
            resolved = "master"
        else:
            try:
                resolved = self._repo.active_branch.name
            except (TypeError, ValueError, git.exc.GitCommandError) as exc:
                logger.debug("Could not resolve active branch for default hint: %s", exc)

        models: list[BranchModel] = []
        for head in sorted(heads, key=lambda h: h.name):
            try:
                tip = head.commit.hexsha
            except ValueError as exc:
                logger.warning("Branch %r has no commit: %s", head.name, exc)
                tip = None
            models.append(
                BranchModel(
                    name=head.name,
                    is_default=head.name == resolved,
                    head_commit_hash=tip,
                )
            )
        return models

    def iter_commits(
        self,
        branch: str | None = None,
        since_hash: str | None = None,
        max_count: int | None = None,
    ) -> Iterator[CommitModel]:
        """Yield commits newest-first using ``Repo.iter_commits`` (generator).

        ``since_hash`` uses Git's symmetric range syntax ``since..tip``: commits
        reachable from ``tip`` but not from ``since`` (typical incremental sync).
        ``since`` itself is not yielded. If ``since`` is not an ancestor of ``tip``,
        Git still returns a set of commits; we log at debug when the range may be
        unexpected.

        :param branch: Tip revision (branch name, tag, or SHA). Defaults to ``HEAD``.
        :param since_hash: Optional exclusive lower bound (full or abbreviated SHA).
        :param max_count: Optional cap passed to GitPython (total commits yielded).
        """
        tip = branch or "HEAD"
        rev: str
        if since_hash:
            try:
                self._repo.commit(since_hash)
            except (ValueError, git.exc.GitCommandError, OSError, BadName) as exc:
                logger.warning(
                    "since_hash %r could not be resolved (%s); scanning full history from %s",
                    since_hash,
                    exc,
                    tip,
                )
                rev = tip
            else:
                rev = f"{since_hash}..{tip}"
                logger.debug("Incremental commit range: %s", rev)
        else:
            rev = tip

        kwargs: dict[str, Any] = {"rev": rev}
        if max_count is not None:
            kwargs["max_count"] = max_count

        branch_label = branch if branch is not None else None

        try:
            for commit in self._repo.iter_commits(**kwargs):
                yield self._commit_to_model(commit, branch=branch_label)
        except git.exc.GitCommandError as exc:
            logger.error("iter_commits failed for rev=%r: %s", rev, exc)
            raise

    def get_file_tree(self) -> list[str]:
        """Return sorted file paths at the current ``HEAD`` commit tree."""
        try:
            tree = self._repo.head.commit.tree
        except ValueError as exc:
            logger.warning("No HEAD commit; empty repository? (%s)", exc)
            return []

        paths: list[str] = []
        for item in tree.traverse():
            if isinstance(item, Blob):
                paths.append(str(item.path))
        return sorted(paths)

    def _map_change_type(self, change_type: str) -> DiffStatus:
        """Map GitPython / git one-letter change codes to :class:`DiffStatus`."""
        raw = (change_type or "").strip()
        if not raw:
            return DiffStatus.MODIFIED

        if len(raw) == 1:
            code = raw.upper()
        else:
            lowered = raw.lower()
            word_map = {
                "added": DiffStatus.ADDED,
                "deleted": DiffStatus.DELETED,
                "modified": DiffStatus.MODIFIED,
                "renamed": DiffStatus.RENAMED,
                "copied": DiffStatus.COPIED,
            }
            if lowered in word_map:
                return word_map[lowered]
            code = raw[:1].upper()

        letter_map: dict[str, DiffStatus] = {
            "A": DiffStatus.ADDED,
            "D": DiffStatus.DELETED,
            "M": DiffStatus.MODIFIED,
            "R": DiffStatus.RENAMED,
            "C": DiffStatus.COPIED,
            "T": DiffStatus.MODIFIED,
        }
        if code in letter_map:
            return letter_map[code]

        logger.debug("Unknown change_type %r; treating as modified", change_type)
        return DiffStatus.MODIFIED

    def _parse_diff(
        self,
        diff: Diff,
        stats_files: dict[str, dict[str, Any]] | None = None,
    ) -> FileDiffModel:
        """Build a :class:`FileDiffModel` from a GitPython :class:`~git.diff.Diff`."""
        stats_files = stats_files or {}

        change_key = diff.change_type or self._infer_change_letter(diff)
        status = self._map_change_type(change_key)

        b_path = diff.b_path
        a_path = diff.a_path
        path = b_path or a_path or ""
        old_path: str | None = None
        if (
            status in (DiffStatus.RENAMED, DiffStatus.COPIED)
            and a_path
            and b_path
            and a_path != b_path
        ):
            old_path = a_path

        additions, deletions = self._line_stats_from_map(stats_files, diff)

        if not path:
            logger.debug(
                "Diff missing paths (change_type=%s); skipping line stats only",
                change_key,
            )

        return FileDiffModel(
            path=path,
            old_path=old_path,
            status=status,
            additions=additions,
            deletions=deletions,
        )

    def _infer_change_letter(self, diff: Diff) -> str:
        """Infer a one-letter change type when ``diff.change_type`` is unset."""
        if diff.new_file:
            return "A"
        if diff.deleted_file:
            return "D"
        if diff.renamed_file:
            return "R"
        if diff.copied_file:
            return "C"
        return "M"

    def _line_stats_from_map(
        self,
        stats_files: dict[str, dict[str, Any]],
        diff: Diff,
    ) -> tuple[int, int]:
        """Match ``git diff --numstat`` stats to this diff (handles renames / odd paths)."""
        candidates: list[str] = []
        for p in (diff.b_path, diff.a_path, diff.rename_to, diff.rename_from):
            if p:
                candidates.append(p)
        for key in candidates:
            if key in stats_files:
                entry = stats_files[key]
                try:
                    return int(entry["insertions"]), int(entry["deletions"])
                except (KeyError, TypeError, ValueError) as exc:
                    logger.debug("Malformed stats entry for %r: %s", key, exc)

        # Renames in numstat with ``no_renames`` may use a single combined name; try suffix match.
        for key, entry in stats_files.items():
            for cand in candidates:
                if cand and (key == cand or key.endswith(cand) or cand.endswith(key)):
                    try:
                        return int(entry["insertions"]), int(entry["deletions"])
                    except (KeyError, TypeError, ValueError):
                        continue

        additions = deletions = 0
        try:
            if diff.diff and isinstance(diff.diff, bytes | str):
                raw = diff.diff
                text = raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")
                for line in text.splitlines():
                    if line.startswith("+") and not line.startswith("+++"):
                        additions += 1
                    elif line.startswith("-") and not line.startswith("---"):
                        deletions += 1
        except (AttributeError, UnicodeDecodeError, ValueError) as exc:
            logger.debug("Could not parse inline diff hunks for stats: %s", exc)

        return additions, deletions

    def _diff_index_for_commit(self, commit: git.Commit) -> list[Diff]:
        """First-parent (or empty tree) diff for merge-safe history scanning."""
        index = (
            commit.diff(NULL_TREE)
            if not commit.parents
            else commit.diff(commit.parents[0])
        )
        return list(index)

    def _commit_to_model(self, commit: git.Commit, branch: str | None) -> CommitModel:
        """Map a GitPython commit to :class:`CommitModel` (loads one commit at a time)."""
        stats_files: dict[str, dict[str, Any]] = {}
        try:
            raw_files = commit.stats.files
            stats_files = {str(path): dict(info) for path, info in raw_files.items()}
        except (git.exc.GitCommandError, IndexError, ValueError, OSError) as exc:
            logger.debug("Stats unavailable for %s: %s", commit.hexsha[:8], exc)

        diffs: list[FileDiffModel] = []
        try:
            for d in self._diff_index_for_commit(commit):
                try:
                    diffs.append(self._parse_diff(d, stats_files))
                except Exception as exc:  # noqa: BLE001 — binary / odd blobs
                    logger.debug("Skipping malformed diff in %s: %s", commit.hexsha[:8], exc)
        except git.exc.GitCommandError as exc:
            logger.warning("diff failed for commit %s: %s", commit.hexsha[:8], exc)

        committed = datetime.fromtimestamp(commit.committed_date, tz=UTC)

        return CommitModel(
            hash=commit.hexsha,
            short_hash=commit.hexsha[:8],
            message=commit.message.decode("utf-8", errors="replace")
            if isinstance(commit.message, bytes)
            else str(commit.message),
            author=self._actor_to_model(commit.author),
            committer=self._actor_to_model(commit.committer),
            timestamp=committed,
            parent_hashes=[p.hexsha for p in commit.parents],
            diffs=diffs,
            branch=branch,
        )

    @staticmethod
    def _actor_to_model(actor: git.Actor) -> AuthorModel:
        name = actor.name or ""
        email = actor.email or ""
        return AuthorModel(name=name, email=email)
