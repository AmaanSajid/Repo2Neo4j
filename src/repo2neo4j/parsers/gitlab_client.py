"""GitLab API client for merge request ingestion with pagination and resilient HTTP."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from functools import partial
from typing import Any, TypeVar

import gitlab
from gitlab.exceptions import (
    GitlabAuthenticationError,
    GitlabConnectionError,
    GitlabError,
    GitlabGetError,
)
from gitlab.v4.objects import ProjectMergeRequest

from repo2neo4j.models.git import (
    AuthorModel,
    BranchModel,
    CommitModel,
    DiffStatus,
    FileDiffModel,
)
from repo2neo4j.models.gitlab import (
    MergeRequestModel,
    MRDiffModel,
    MRNoteModel,
    MRReviewModel,
    MRState,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

_ALLOWED_MR_ORDER_BY = frozenset({"created_at", "updated_at"})
_TRANSIENT_HTTP_STATUSES = frozenset({502, 503, 504, 520, 522, 524})
_DEFAULT_PER_PAGE = 100
_MAX_API_ATTEMPTS = 8
_INITIAL_BACKOFF_SEC = 0.5


def _normalize_gitlab_url(url: str) -> str:
    return url.rstrip("/")


def _datetime_to_gitlab_param(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


def _get_attr(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _user_triple(user_obj: Any) -> tuple[str, str | None, str]:
    if not user_obj:
        return ("unknown", None, "unknown")
    if isinstance(user_obj, Mapping):
        name = str(user_obj.get("name") or "unknown")
        email = user_obj.get("email")
        username = str(user_obj.get("username") or "unknown")
        return (name, str(email) if email else None, username)
    name = str(getattr(user_obj, "name", None) or "unknown")
    email = getattr(user_obj, "email", None)
    username = str(getattr(user_obj, "username", None) or "unknown")
    return (name, str(email) if email else None, username)


def _unwrap_approval_user(entry: Any) -> Any:
    if isinstance(entry, Mapping) and "user" in entry:
        return entry["user"]
    return entry


def _parse_optional_datetime(val: Any) -> datetime | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    return datetime.fromisoformat(str(val).replace("Z", "+00:00"))


class GitLabClient:
    """Fetches GitLab merge request data using python-gitlab with streaming pagination."""

    def __init__(self, url: str, project_id: int, private_token: str) -> None:
        self._url = _normalize_gitlab_url(url)
        self._project_id = project_id
        self._gl = gitlab.Gitlab(
            self._url,
            private_token=private_token,
            api_version="4",
            retry_transient_errors=True,
            keep_base_url=True,
        )
        self._project = self._with_retry("projects.get", lambda: self._gl.projects.get(project_id))
        logger.info(
            "Connected to GitLab project id=%s (host=%s); authentication token is not logged",
            project_id,
            self._url,
        )

    def iter_merge_requests(
        self,
        state: str | None = None,
        updated_after: datetime | None = None,
        order_by: str = "updated_at",
        target_branch: str | None = None,
    ) -> Iterator[MergeRequestModel]:
        """Yield merge requests one at a time using server-side pagination (iterator).

        :param target_branch: If set, only return MRs targeting this branch (e.g. ``main``).
        """
        if order_by not in _ALLOWED_MR_ORDER_BY:
            logger.warning(
                "Unsupported order_by=%r; falling back to updated_at (allowed: %s)",
                order_by,
                sorted(_ALLOWED_MR_ORDER_BY),
            )
            order_by = "updated_at"

        list_kwargs: dict[str, Any] = {
            "iterator": True,
            "order_by": order_by,
            "sort": "desc",
            "per_page": _DEFAULT_PER_PAGE,
        }
        if state is not None:
            list_kwargs["state"] = state
        else:
            list_kwargs["state"] = "all"
        if updated_after is not None:
            list_kwargs["updated_after"] = _datetime_to_gitlab_param(updated_after)
        if target_branch is not None:
            list_kwargs["target_branch"] = target_branch

        logger.info(
            "Streaming merge requests for project id=%s (state=%s, updated_after=%s, order_by=%s)",
            self._project_id,
            list_kwargs.get("state"),
            list_kwargs.get("updated_after"),
            order_by,
        )

        mr_list = self._with_retry(
            "mergerequests.list",
            lambda: self._project.mergerequests.list(**list_kwargs),
        )
        for mr in mr_list:
            assert isinstance(mr, ProjectMergeRequest)
            yield self._with_retry(
                f"build_mr_model(iid={mr.iid})",
                partial(self._build_mr_model, mr),
            )

    def get_merge_request(self, mr_iid: int) -> MergeRequestModel:
        """Load a single merge request with commits, notes, approvals, and file changes."""
        mr = self._with_retry(
            f"mergerequests.get({mr_iid})",
            lambda: self._project.mergerequests.get(mr_iid),
        )
        return self._build_mr_model(mr)

    def _fetch_mr_commits(self, mr: ProjectMergeRequest) -> list[str]:
        hashes: list[str] = []
        commit_list = self._with_retry(f"mr.commits(iid={mr.iid})", mr.commits)
        for commit in commit_list:
            cid = getattr(commit, "id", None) or getattr(commit, "short_id", None)
            if cid:
                hashes.append(str(cid))
        return hashes

    def _fetch_mr_notes(self, mr: ProjectMergeRequest) -> list[MRNoteModel]:
        """Flatten discussion threads into individual notes (inline + MR comments)."""
        notes_out: list[MRNoteModel] = []
        discussions = self._with_retry(
            f"mr.discussions(iid={mr.iid})",
            lambda: mr.discussions.list(iterator=True, per_page=_DEFAULT_PER_PAGE),
        )
        for discussion in discussions:
            disc_notes = self._with_retry(
                f"discussion.notes(iid={mr.iid})",
                partial(
                    discussion.notes.list,
                    iterator=True,
                    per_page=_DEFAULT_PER_PAGE,
                ),
            )
            for note in disc_notes:
                author = _get_attr(note, "author") or {}
                author_name, _, author_username = _user_triple(author)
                body = _get_attr(note, "body")
                created_raw = _get_attr(note, "created_at")
                if created_raw is None:
                    logger.debug("Skipping note without created_at on MR %s", mr.iid)
                    continue
                created_at = (
                    created_raw
                    if isinstance(created_raw, datetime)
                    else datetime.fromisoformat(str(created_raw).replace("Z", "+00:00"))
                )
                notes_out.append(
                    MRNoteModel(
                        author_name=author_name,
                        author_username=author_username,
                        body=str(body or ""),
                        created_at=created_at,
                        is_system=bool(_get_attr(note, "system", False)),
                        noteable_type=str(_get_attr(note, "noteable_type") or "MergeRequest"),
                    )
                )
        notes_out.sort(key=lambda n: n.created_at)
        return notes_out

    def _fetch_mr_approvals(self, mr: ProjectMergeRequest) -> list[MRReviewModel]:
        """Collect approval records from the approvals endpoint and approval_state (raw)."""
        by_username: dict[str, MRReviewModel] = {}

        try:
            approval = self._with_retry(f"mr.approvals.get(iid={mr.iid})", mr.approvals.get)
        except GitlabGetError as exc:
            if getattr(exc, "response_code", None) == 404:
                logger.debug("Approvals not available for MR %s (404)", mr.iid)
                approval = None
            else:
                raise

        if approval is not None:
            approved_by = _get_attr(approval, "approved_by") or []
            for entry in approved_by:
                user_obj = _unwrap_approval_user(entry)
                name, email, username = _user_triple(user_obj)
                created_at = _parse_optional_datetime(_get_attr(entry, "created_at"))
                by_username[username] = MRReviewModel(
                    reviewer_name=name,
                    reviewer_email=email,
                    reviewer_username=username,
                    approved=True,
                    created_at=created_at,
                )

        rules = self._fetch_approval_state_rules(mr)
        for rule in rules:
            for entry in _get_attr(rule, "approved_by") or []:
                user_obj = _unwrap_approval_user(entry)
                name, email, username = _user_triple(user_obj)
                created_at = _parse_optional_datetime(_get_attr(entry, "created_at"))
                by_username[username] = MRReviewModel(
                    reviewer_name=name,
                    reviewer_email=email,
                    reviewer_username=username,
                    approved=True,
                    created_at=created_at,
                )
            for key in ("eligible_approvers", "users"):
                for entry in _get_attr(rule, key) or []:
                    user_obj = _unwrap_approval_user(entry)
                    name, email, username = _user_triple(user_obj)
                    if username in by_username:
                        continue
                    created_at = _parse_optional_datetime(_get_attr(entry, "created_at"))
                    by_username[username] = MRReviewModel(
                        reviewer_name=name,
                        reviewer_email=email,
                        reviewer_username=username,
                        approved=False,
                        created_at=created_at,
                    )

        return list(by_username.values())

    def _fetch_approval_state_rules(self, mr: ProjectMergeRequest) -> list[dict[str, Any]]:
        path = mr.approval_state.path
        try:
            raw = self._with_retry(
                f"approval_state http_get(iid={mr.iid})",
                lambda: self._gl.http_get(path),
            )
        except GitlabError as exc:
            logger.debug("approval_state unavailable for MR %s: %s", mr.iid, exc)
            return []

        if isinstance(raw, list):
            return [r for r in raw if isinstance(r, dict)]
        if isinstance(raw, dict):
            rules = raw.get("rules")
            if isinstance(rules, list):
                return [r for r in rules if isinstance(r, dict)]
        return []

    def _fetch_mr_diffs(self, mr: ProjectMergeRequest) -> list[MRDiffModel]:
        changes = self._with_retry(f"mr.changes(iid={mr.iid})", lambda: mr.changes())
        if not isinstance(changes, dict):
            logger.warning("Unexpected changes payload for MR %s: %r", mr.iid, type(changes))
            return []
        rows = changes.get("changes") or []
        diffs: list[MRDiffModel] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            diffs.append(
                MRDiffModel(
                    old_path=str(row.get("old_path") or ""),
                    new_path=str(row.get("new_path") or ""),
                    new_file=bool(row.get("new_file", False)),
                    renamed_file=bool(row.get("renamed_file", False)),
                    deleted_file=bool(row.get("deleted_file", False)),
                )
            )
        return diffs

    def _build_mr_model(self, mr: ProjectMergeRequest) -> MergeRequestModel:
        author = getattr(mr, "author", None) or {}
        author_name, _, author_username = _user_triple(author)

        labels_raw = getattr(mr, "labels", None) or []
        labels: list[str] = []
        for item in labels_raw:
            if isinstance(item, str):
                labels.append(item)
            elif isinstance(item, dict) and "name" in item:
                labels.append(str(item["name"]))

        state_raw = str(getattr(mr, "state", "") or "").lower()
        try:
            state = MRState(state_raw)
        except ValueError:
            logger.warning("Unknown MR state %r for iid=%s; mapping to CLOSED", state_raw, mr.iid)
            state = MRState.CLOSED

        commit_hashes = self._fetch_mr_commits(mr)
        notes = self._fetch_mr_notes(mr)
        reviews = self._fetch_mr_approvals(mr)
        diffs = self._fetch_mr_diffs(mr)

        def _dt(val: Any) -> datetime | None:
            if val is None:
                return None
            if isinstance(val, datetime):
                return val
            return datetime.fromisoformat(str(val).replace("Z", "+00:00"))

        return MergeRequestModel(
            iid=int(mr.iid),
            title=str(getattr(mr, "title", "") or ""),
            description=getattr(mr, "description", None),
            state=state,
            source_branch=str(getattr(mr, "source_branch", "") or ""),
            target_branch=str(getattr(mr, "target_branch", "") or ""),
            author_name=author_name,
            author_username=author_username,
            created_at=_dt(getattr(mr, "created_at", None)) or datetime.now(UTC),
            updated_at=_dt(getattr(mr, "updated_at", None)),
            merged_at=_dt(getattr(mr, "merged_at", None)),
            closed_at=_dt(getattr(mr, "closed_at", None)),
            web_url=str(getattr(mr, "web_url", "") or ""),
            commit_hashes=commit_hashes,
            reviews=reviews,
            notes=notes,
            diffs=diffs,
            labels=labels,
        )

    # ── Remote repository methods (no local clone needed) ──

    def get_branches(self, branch: str | None = None) -> list[BranchModel]:
        """Fetch branches from GitLab API. If *branch* is set, return only that one."""
        if branch:
            raw = self._with_retry(
                f"branches.get({branch})",
                lambda: self._project.branches.get(branch),
            )
            return [BranchModel(
                name=raw.name,
                is_default=(raw.name == getattr(self._project, "default_branch", "main")),
                head_commit_hash=raw.commit["id"] if raw.commit else None,
            )]

        raw_list = self._with_retry(
            "branches.list",
            lambda: self._project.branches.list(iterator=True, per_page=_DEFAULT_PER_PAGE),
        )
        default_branch = getattr(self._project, "default_branch", "main")
        models: list[BranchModel] = []
        for b in raw_list:
            models.append(BranchModel(
                name=b.name,
                is_default=(b.name == default_branch),
                head_commit_hash=b.commit["id"] if b.commit else None,
            ))
        return sorted(models, key=lambda m: m.name)

    def iter_commits_remote(
        self,
        branch: str | None = None,
        since_hash: str | None = None,
        max_count: int | None = None,
    ) -> Iterator[CommitModel]:
        """Yield commits from the GitLab API (newest-first), no local clone needed."""
        ref = branch or getattr(self._project, "default_branch", "main")
        list_kwargs: dict[str, Any] = {
            "ref_name": ref,
            "iterator": True,
            "per_page": _DEFAULT_PER_PAGE,
        }

        commit_list = self._with_retry(
            "commits.list",
            lambda: self._project.commits.list(**list_kwargs),
        )

        count = 0
        for raw_commit in commit_list:
            sha = raw_commit.id
            if since_hash and sha == since_hash:
                break

            diff_list = self._with_retry(
                f"commit.diff({sha[:8]})",
                lambda sha=sha: self._project.commits.get(sha).diff(),
            )
            diffs: list[FileDiffModel] = []
            for d in diff_list:
                status = DiffStatus.ADDED if d.get("new_file") else (
                    DiffStatus.DELETED if d.get("deleted_file") else (
                        DiffStatus.RENAMED if d.get("renamed_file") else DiffStatus.MODIFIED
                    )
                )
                path = d.get("new_path") or d.get("old_path") or ""
                old_path = d.get("old_path") if status == DiffStatus.RENAMED else None
                diff_text = d.get("diff", "")
                additions = sum(1 for line in diff_text.splitlines() if line.startswith("+") and not line.startswith("+++"))
                deletions = sum(1 for line in diff_text.splitlines() if line.startswith("-") and not line.startswith("---"))
                diffs.append(FileDiffModel(
                    path=path,
                    old_path=old_path,
                    status=status,
                    additions=additions,
                    deletions=deletions,
                ))

            author_name = raw_commit.author_name or ""
            author_email = raw_commit.author_email or ""
            committer_name = raw_commit.committer_name or author_name
            committer_email = raw_commit.committer_email or author_email
            ts = datetime.fromisoformat(raw_commit.committed_date.replace("Z", "+00:00"))

            yield CommitModel(
                hash=sha,
                short_hash=sha[:8],
                message=raw_commit.message or "",
                author=AuthorModel(name=author_name, email=author_email),
                committer=AuthorModel(name=committer_name, email=committer_email),
                timestamp=ts,
                parent_hashes=raw_commit.parent_ids or [],
                diffs=diffs,
                branch=ref,
            )
            count += 1
            if max_count and count >= max_count:
                break

    def get_file_tree_remote(self, branch: str | None = None) -> list[str]:
        """Return sorted file paths at the tip of *branch* via GitLab API."""
        ref = branch or getattr(self._project, "default_branch", "main")
        tree = self._with_retry(
            "repository_tree",
            lambda: self._project.repository_tree(
                ref=ref, recursive=True, iterator=True, per_page=_DEFAULT_PER_PAGE,
            ),
        )
        return sorted(
            item["path"] for item in tree if item.get("type") == "blob"
        )

    def get_file_content(self, file_path: str, branch: str | None = None) -> str | None:
        """Fetch raw file content via GitLab API. Returns None on 404."""
        ref = branch or getattr(self._project, "default_branch", "main")
        try:
            f = self._with_retry(
                f"files.get({file_path})",
                lambda: self._project.files.get(file_path=file_path, ref=ref),
            )
            return f.decode().decode("utf-8", errors="replace")
        except GitlabGetError as exc:
            if getattr(exc, "response_code", None) == 404:
                return None
            raise

    def _with_retry(self, operation: str, fn: Callable[[], T]) -> T:
        """Extra retries beyond python-gitlab's built-in 429 / transient handling."""
        attempt = 0
        backoff = _INITIAL_BACKOFF_SEC
        while True:
            try:
                return fn()
            except GitlabConnectionError as exc:
                attempt += 1
                if attempt >= _MAX_API_ATTEMPTS:
                    logger.error("Giving up on %s after %s attempts: %s", operation, attempt, exc)
                    raise
                sleep_for = backoff + min(0.25, backoff * 0.1)
                logger.warning(
                    "GitLab connection error in %s (attempt %s/%s); retrying in %.2fs: %s",
                    operation,
                    attempt,
                    _MAX_API_ATTEMPTS,
                    sleep_for,
                    exc,
                )
                time.sleep(sleep_for)
                backoff = min(backoff * 2, 30.0)
            except GitlabAuthenticationError:
                raise
            except GitlabError as exc:
                code = getattr(exc, "response_code", None)
                if code == 429 or code in _TRANSIENT_HTTP_STATUSES:
                    attempt += 1
                    if attempt >= _MAX_API_ATTEMPTS:
                        logger.error(
                            "Giving up on %s after HTTP %s (%s attempts)",
                            operation,
                            code,
                            attempt,
                        )
                        raise
                    sleep_for = backoff + min(0.25, backoff * 0.1)
                    if code == 429:
                        logger.warning(
                            "Rate limited (HTTP 429) in %s; sleeping %.2fs before retry "
                            "(attempt %s/%s)",
                            operation,
                            sleep_for,
                            attempt,
                            _MAX_API_ATTEMPTS,
                        )
                    else:
                        logger.warning(
                            "Transient HTTP %s in %s; sleeping %.2fs (attempt %s/%s)",
                            code,
                            operation,
                            sleep_for,
                            attempt,
                            _MAX_API_ATTEMPTS,
                        )
                    time.sleep(sleep_for)
                    backoff = min(backoff * 2, 30.0)
                    continue
                raise
