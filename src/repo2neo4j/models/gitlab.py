"""Data models for GitLab merge request entities."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class MRState(str, Enum):
    OPENED = "opened"
    CLOSED = "closed"
    MERGED = "merged"
    LOCKED = "locked"


class MRReviewModel(BaseModel):
    """A review/approval on a merge request."""

    reviewer_name: str
    reviewer_email: str | None = None
    reviewer_username: str
    approved: bool = False
    created_at: datetime | None = None


class MRNoteModel(BaseModel):
    """A comment/note on a merge request."""

    author_name: str
    author_username: str
    body: str
    created_at: datetime
    is_system: bool = False
    noteable_type: str = "MergeRequest"


class MRDiffModel(BaseModel):
    """A file diff within a merge request."""

    old_path: str
    new_path: str
    new_file: bool = False
    renamed_file: bool = False
    deleted_file: bool = False


class MergeRequestModel(BaseModel):
    """A GitLab merge request with all associated data."""

    iid: int
    title: str
    description: str | None = None
    state: MRState
    source_branch: str
    target_branch: str
    author_name: str
    author_username: str
    created_at: datetime
    updated_at: datetime | None = None
    merged_at: datetime | None = None
    closed_at: datetime | None = None
    web_url: str
    commit_hashes: list[str] = Field(default_factory=list)
    reviews: list[MRReviewModel] = Field(default_factory=list)
    notes: list[MRNoteModel] = Field(default_factory=list)
    diffs: list[MRDiffModel] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
