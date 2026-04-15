"""Tests for :class:`repo2neo4j.agent.query_api.AgentQueryAPI`."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from repo2neo4j.agent.query_api import AgentQueryAPI
from repo2neo4j.config import AppConfig, GitLabConfig, Neo4jConfig, RepositoryConfig


@pytest.fixture
def sample_app_config() -> AppConfig:
    return AppConfig(
        repository=RepositoryConfig(path="/repos/demo", name="demo"),
        gitlab=GitLabConfig(
            url="https://gitlab.example.com",
            project_id=1,
            private_token="token",
        ),
        neo4j=Neo4jConfig(
            uri="bolt://neo4j.test:7687",
            username="neo4j",
            password="pw",
            database="graph.db",
        ),
    )


@patch("repo2neo4j.agent.query_api.GraphDatabase.driver")
def test_from_config_creates_instance_with_expected_driver_args(
    mock_driver: MagicMock, sample_app_config: AppConfig
) -> None:
    mock_driver.return_value = MagicMock()

    api = AgentQueryAPI.from_config(sample_app_config)

    mock_driver.assert_called_once_with(
        "bolt://neo4j.test:7687",
        auth=("neo4j", "pw"),
    )
    assert api._repo_name == "demo"
    api.close()


@patch("repo2neo4j.agent.query_api.GraphDatabase.driver")
def test_query_dispatch_delegates(mock_driver: MagicMock, sample_app_config: AppConfig) -> None:
    mock_driver.return_value = MagicMock()
    api = AgentQueryAPI.from_config(sample_app_config)

    with patch.object(api, "hot_files", autospec=True) as hot:
        hot.return_value = [{"path": "a.py"}]
        out = api.query("hot_files", limit=7)

    assert out == [{"path": "a.py"}]
    hot.assert_called_once_with(limit=7)
    api.close()


@patch("repo2neo4j.agent.query_api.GraphDatabase.driver")
def test_unknown_query_raises_value_error(mock_driver: MagicMock, sample_app_config: AppConfig) -> None:
    mock_driver.return_value = MagicMock()
    api = AgentQueryAPI.from_config(sample_app_config)

    with pytest.raises(ValueError, match="Unknown query"):
        api.query("not_a_real_query_name")

    api.close()
