"""Configuration loader with YAML parsing and environment variable substitution."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


def _resolve_env_vars(value: Any) -> Any:
    """Recursively substitute ${VAR} patterns with environment variables."""
    if isinstance(value, str):
        pattern = re.compile(r"\$\{(\w+)(?::([^}]*))?\}")

        def _replace(match: re.Match[str]) -> str:
            var_name = match.group(1)
            default = match.group(2)
            env_val = os.environ.get(var_name)
            if env_val is not None:
                return env_val
            if default is not None:
                return default
            raise ValueError(
                f"Environment variable '{var_name}' is not set and no default provided"
            )

        return pattern.sub(_replace, value)
    if isinstance(value, dict):
        return {k: _resolve_env_vars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env_vars(item) for item in value]
    return value


class RepositoryConfig(BaseModel):
    path: str
    name: str


class GitLabConfig(BaseModel):
    url: str
    project_id: int
    private_token: str


class Neo4jConfig(BaseModel):
    uri: str = "bolt://localhost:7687"
    username: str = "neo4j"
    password: str = "changeme"
    database: str = "neo4j"
    max_connection_pool_size: int = 50
    connection_acquisition_timeout: float = 60.0


class ParsingConfig(BaseModel):
    ast_enabled: bool = True
    languages: list[str] = Field(default_factory=lambda: ["python"])
    ignore_patterns: list[str] = Field(
        default_factory=lambda: [
            "node_modules/**",
            "__pycache__/**",
            ".git/**",
            "*.pyc",
            "*.min.js",
        ]
    )


class SyncConfig(BaseModel):
    batch_size: int = 500
    max_commits: int | None = None


class AppConfig(BaseModel):
    repository: RepositoryConfig
    gitlab: GitLabConfig | None = None
    neo4j: Neo4jConfig = Field(default_factory=Neo4jConfig)
    parsing: ParsingConfig = Field(default_factory=ParsingConfig)
    sync: SyncConfig = Field(default_factory=SyncConfig)


def load_config(config_path: str | Path) -> AppConfig:
    """Load and validate configuration from a YAML file."""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found: {path}")

    with open(path) as f:
        raw = yaml.safe_load(f)

    resolved = _resolve_env_vars(raw)
    return AppConfig.model_validate(resolved)
