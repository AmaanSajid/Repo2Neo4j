# repo2neo4j

Convert any Git repository into a Neo4j knowledge graph for code intelligence, merge request analysis, and agent-driven code review.

## Quick Start

### 1. Start Neo4j

```bash
export NEO4J_PASSWORD=your-secure-password
docker compose up -d
```

### 2. Install

```bash
pip install -e ".[dev]"
```

### 3. Configure

```bash
cp config.example.yml config.yml
# Edit config.yml with your repository path, GitLab credentials, and Neo4j connection
```

### 4. Ingest a Repository

```bash
# Initialize the graph schema
repo2neo4j schema --init --config config.yml

# Full ingestion
repo2neo4j ingest --config config.yml

# Incremental update (only new commits/MRs)
repo2neo4j update --config config.yml
```

### 5. Query

```bash
# Run a predefined query
repo2neo4j query files-changed-in-mr --mr-iid 42 --config config.yml
repo2neo4j query commit-history --file-path src/main.py --config config.yml
repo2neo4j query function-callers --name handle_request --config config.yml
```

## Graph Data Model

The tool creates the following node types in Neo4j:

| Node | Key Property | Description |
|------|-------------|-------------|
| Repository | name | The repository being analyzed |
| Branch | name | Git branches |
| Commit | hash | Individual commits with metadata |
| Author | email | Developers (deduplicated by email) |
| File | path | Source files at HEAD |
| Directory | path | Directory tree structure |
| MergeRequest | iid | GitLab merge requests |
| Class | qualified_name | Classes extracted via AST |
| Function | qualified_name | Functions/methods extracted via AST |

## Agent Integration

The `agents/` directory contains markdown instruction files for GitHub Copilot agents:

- `code-review.md` — Performs code review using graph context
- `repo-explorer.md` — Explores repository structure and dependencies
- `mr-analyzer.md` — Analyzes merge request impact and risk

## Configuration Reference

See `config.example.yml` for all available options.

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check src/
mypy src/
```
