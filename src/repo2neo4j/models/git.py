"""Data models for Git-extracted entities."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class DiffStatus(str, Enum):
    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    RENAMED = "renamed"
    COPIED = "copied"


class AuthorModel(BaseModel):
    name: str
    email: str


class FileDiffModel(BaseModel):
    """A single file's diff within a commit."""

    path: str
    old_path: str | None = None
    status: DiffStatus
    additions: int = 0
    deletions: int = 0


class CommitModel(BaseModel):
    """A single Git commit."""

    hash: str
    short_hash: str = ""
    message: str
    author: AuthorModel
    committer: AuthorModel
    timestamp: datetime
    parent_hashes: list[str] = Field(default_factory=list)
    diffs: list[FileDiffModel] = Field(default_factory=list)
    branch: str | None = None

    def model_post_init(self, __context: object) -> None:
        if not self.short_hash:
            self.short_hash = self.hash[:8]


class BranchModel(BaseModel):
    """A Git branch."""

    name: str
    is_default: bool = False
    head_commit_hash: str | None = None
