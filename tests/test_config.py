"""Tests for configuration loading and env substitution."""

from __future__ import annotations

from pathlib import Path

import pytest

from repo2neo4j.config import AppConfig, load_config


def test_load_config_valid_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("R2N_TOKEN", raising=False)
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        """
repository:
  path: /tmp/repo
  name: acme
gitlab:
  url: https://gitlab.example.com
  project_id: 99
  private_token: ${R2N_TOKEN:placeholder}
neo4j:
  uri: bolt://localhost:7687
  username: neo4j
  password: secret
""",
        encoding="utf-8",
    )

    cfg = load_config(cfg_path)
    assert isinstance(cfg, AppConfig)
    assert cfg.repository.name == "acme"
    assert cfg.repository.path == "/tmp/repo"
    assert cfg.gitlab is not None
    assert cfg.gitlab.project_id == 99
    assert cfg.gitlab.private_token == "placeholder"


def test_env_var_substitution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("R2N_NEO4J_PASS", "from-env")
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        """
repository:
  path: /data/r
  name: r1
neo4j:
  password: ${R2N_NEO4J_PASS}
""",
        encoding="utf-8",
    )

    cfg = load_config(cfg_path)
    assert cfg.neo4j.password == "from-env"


def test_missing_env_var_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MISSING_FOR_TEST", raising=False)
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        """
repository:
  path: /data/r
  name: r1
neo4j:
  password: ${MISSING_FOR_TEST}
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="MISSING_FOR_TEST"):
        load_config(cfg_path)


def test_default_values_applied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANY_OPTIONAL", raising=False)
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        """
repository:
  path: /repos/x
  name: x
""",
        encoding="utf-8",
    )

    cfg = load_config(cfg_path)
    assert cfg.neo4j.uri == "bolt://localhost:7687"
    assert cfg.neo4j.username == "neo4j"
    assert cfg.neo4j.password == "changeme"
    assert cfg.neo4j.database == "neo4j"
    assert cfg.parsing.ast_enabled is True
    assert "python" in cfg.parsing.languages
    assert cfg.sync.batch_size == 500
    assert cfg.gitlab is None
