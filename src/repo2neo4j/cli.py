"""Typer CLI for repo2neo4j: schema lifecycle, ingestion, incremental updates, and queries."""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn, TypeVar

import typer
from git import exc as git_exc
from gitlab.exceptions import GitlabError
from neo4j import GraphDatabase
from neo4j.exceptions import Neo4jError
from neo4j.time import DateTime as Neo4jDateTime
from rich.console import Console
from rich.logging import RichHandler
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn
from rich.table import Table

from repo2neo4j.agent.query_api import AgentQueryAPI
from repo2neo4j.config import AppConfig, load_config
from repo2neo4j.graph.ingester import GraphIngester
from repo2neo4j.graph.schema import drop_schema, initialize_schema, verify_schema
from repo2neo4j.models.gitlab import MergeRequestModel
from repo2neo4j.parsers.code_parser import CodeParser
from repo2neo4j.parsers.git_parser import GitParser
from repo2neo4j.parsers.gitlab_client import GitLabClient

logger = logging.getLogger(__name__)
T = TypeVar("T")
console = Console(stderr=True)

app = typer.Typer(
    name="repo2neo4j",
    help="Ingest Git repositories into Neo4j for code intelligence and analytics.",
    rich_markup_mode="rich",
)

schema_app = typer.Typer(name="schema", help="Manage Neo4j constraints and indexes.")
app.add_typer(schema_app, name="schema")


def _setup_logging(verbose: int) -> None:
    level = logging.DEBUG if verbose >= 1 else logging.INFO
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    handler = RichHandler(
        console=console,
        show_time=True,
        show_path=verbose >= 1,
        markup=False,
    )
    handler.setLevel(level)
    root.addHandler(handler)
    logging.getLogger("neo4j").setLevel(logging.WARNING if verbose < 2 else logging.DEBUG)
    logging.getLogger("git").setLevel(logging.WARNING if verbose < 2 else logging.DEBUG)
    logging.getLogger("gitlab").setLevel(logging.WARNING if verbose < 2 else logging.DEBUG)


@app.callback()
def _main_callback(
    ctx: typer.Context,
    verbose: int = typer.Option(0, "--verbose", "-v", count=True, help="Increase logging verbosity."),
) -> None:
    if ctx.invoked_subcommand is None:
        return
    _setup_logging(verbose)


def _exit_error(message: str, *, exc: BaseException | None = None) -> NoReturn:
    console.print(f"[bold red]Error:[/bold red] {message}")
    if exc is not None and logger.isEnabledFor(logging.DEBUG):
        logger.debug("%s: %s", type(exc).__name__, exc, exc_info=(type(exc), exc, exc.__traceback__))
    raise typer.Exit(code=1)


def _load_app_config(config: Path) -> AppConfig:
    try:
        return load_config(config)
    except FileNotFoundError as exc:
        _exit_error(str(exc), exc=exc)
    except Exception as exc:  # noqa: BLE001 — surface validation errors
        _exit_error(f"Invalid configuration {config}: {exc}", exc=exc)


def _neo4j_driver(cfg: AppConfig):
    neo = cfg.neo4j
    return GraphDatabase.driver(
        neo.uri,
        auth=(neo.username, neo.password),
        max_connection_pool_size=neo.max_connection_pool_size,
        connection_acquisition_timeout=neo.connection_acquisition_timeout,
    )


def _sync_dt_to_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value
    if isinstance(value, Neo4jDateTime):
        return datetime.fromisoformat(value.iso_format().replace("Z", "+00:00"))
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return None


def _default_branch_tip(git_parser: GitParser) -> str:
    branches = git_parser.get_branches()
    if not branches:
        return "HEAD"
    default = next((b for b in branches if b.is_default), branches[0])
    return default.name if default.head_commit_hash else "HEAD"


def _tracked_iter(
    iterable: Iterable[T],
    description: str,
    *,
    total: int | None,
) -> Iterator[T]:
    columns = (
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
    )
    with Progress(*columns, console=console, transient=False) as progress:
        task_id = progress.add_task(description, total=total)
        for item in iterable:
            yield item
            progress.advance(task_id, 1)


def _ingest_code_structure(
    cfg: AppConfig,
    ingester: GraphIngester,
    repo_root: Path,
) -> int:
    if not cfg.parsing.ast_enabled:
        logger.info("AST parsing disabled; skipping code structure ingestion.")
        return 0
    parser = CodeParser(cfg.parsing.languages, cfg.parsing.ignore_patterns)
    files = list(
        _tracked_iter(
            parser.iter_parse_directory(repo_root, repo_root),
            "Parsing source files",
            total=None,
        )
    )
    if files:
        ingester.ingest_files(files)
    return len(files)


def _merge_request_max_updated(mrs: Iterable[MergeRequestModel]) -> datetime | None:
    latest: datetime | None = None
    for mr in mrs:
        ts = mr.updated_at
        if ts is None:
            continue
        if latest is None or ts > latest:
            latest = ts
    return latest


@schema_app.command("init")
def schema_init(
    config: Path = typer.Option(..., "--config", help="Path to repo2neo4j YAML configuration."),
) -> None:
    """Create constraints and indexes defined by repo2neo4j."""
    cfg = _load_app_config(config)
    try:
        driver = _neo4j_driver(cfg)
    except Exception as exc:  # noqa: BLE001
        _exit_error(f"Could not create Neo4j driver: {exc}", exc=exc)
    try:
        initialize_schema(driver, database=cfg.neo4j.database)
    except Neo4jError as exc:
        _exit_error(f"Schema initialization failed: {exc}", exc=exc)
    finally:
        driver.close()
    console.print("[green]Schema initialized successfully.[/green]")


@schema_app.command("drop")
def schema_drop(
    config: Path = typer.Option(..., "--config", help="Path to repo2neo4j YAML configuration."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip interactive confirmation."),
) -> None:
    """Drop all constraints and indexes in the configured database (destructive)."""
    cfg = _load_app_config(config)
    if not yes:
        typer.confirm(
            f"This will drop every constraint and index on Neo4j database {cfg.neo4j.database!r}. "
            "Continue?",
            abort=True,
        )
    try:
        driver = _neo4j_driver(cfg)
    except Exception as exc:  # noqa: BLE001
        _exit_error(f"Could not create Neo4j driver: {exc}", exc=exc)
    try:
        drop_schema(driver, database=cfg.neo4j.database)
    except Neo4jError as exc:
        _exit_error(f"Schema drop failed: {exc}", exc=exc)
    finally:
        driver.close()
    console.print("[yellow]Schema dropped.[/yellow]")


@schema_app.command("verify")
def schema_verify(
    config: Path = typer.Option(..., "--config", help="Path to repo2neo4j YAML configuration."),
) -> None:
    """Verify expected constraints and indexes exist."""
    cfg = _load_app_config(config)
    try:
        driver = _neo4j_driver(cfg)
    except Exception as exc:  # noqa: BLE001
        _exit_error(f"Could not create Neo4j driver: {exc}", exc=exc)
    try:
        result = verify_schema(driver, database=cfg.neo4j.database)
    except Neo4jError as exc:
        _exit_error(f"Schema verification failed: {exc}", exc=exc)
    finally:
        driver.close()

    table = Table(title="Schema verification")
    table.add_column("Object", style="cyan", no_wrap=True)
    table.add_column("Present", justify="center")
    for name, ok in sorted(result.items()):
        table.add_row(name, "[green]yes[/green]" if ok else "[red]no[/red]")
    console.print(table)
    if not all(result.values()):
        raise typer.Exit(code=1)


@app.command("ingest")
def ingest(
    config: Path = typer.Option(..., "--config", help="Path to repo2neo4j YAML configuration."),
) -> None:
    """Run a full ingestion: repository, branches, commits, code (optional), and GitLab MRs."""
    cfg = _load_app_config(config)
    repo_root = Path(cfg.repository.path).expanduser().resolve()
    if not repo_root.is_dir():
        _exit_error(f"Repository path is not a directory: {repo_root}")

    stats = {"commits": 0, "files": 0, "merge_requests": 0}

    try:
        git_parser = GitParser(repo_root)
    except git_exc.InvalidGitRepositoryError as exc:
        _exit_error(str(exc), exc=exc)

    try:
        driver = _neo4j_driver(cfg)
    except Exception as exc:  # noqa: BLE001
        _exit_error(f"Could not create Neo4j driver: {exc}", exc=exc)

    ingester = GraphIngester(
        driver,
        database=cfg.neo4j.database,
        repo_name=cfg.repository.name,
        batch_size=cfg.sync.batch_size,
    )

    try:
        branches = git_parser.get_branches()
        default_tip = _default_branch_tip(git_parser)
        resolved_default = next((b.name for b in branches if b.is_default), None)
        if not resolved_default and branches:
            resolved_default = branches[0].name
        elif not resolved_default:
            resolved_default = "main"

        ingester.ingest_repository(
            name=cfg.repository.name,
            url="",
            default_branch=resolved_default,
        )
        ingester.ingest_branches(branches)

        commit_iter = git_parser.iter_commits(
            branch=default_tip,
            since_hash=None,
            max_count=cfg.sync.max_commits,
        )
        commits = list(
            _tracked_iter(
                commit_iter,
                "Ingesting commits",
                total=cfg.sync.max_commits,
            )
        )
        stats["commits"] = len(commits)
        if commits:
            ingester.ingest_commits(commits)

        stats["files"] = _ingest_code_structure(cfg, ingester, repo_root)

        max_mr_updated: datetime | None = None
        if cfg.gitlab is not None:
            try:
                gl = GitLabClient(
                    cfg.gitlab.url,
                    cfg.gitlab.project_id,
                    cfg.gitlab.private_token,
                )
                mr_iter = gl.iter_merge_requests(state="all", updated_after=None, order_by="updated_at")

                def mr_gen() -> Iterator[MergeRequestModel]:
                    yield from _tracked_iter(mr_iter, "Fetching merge requests", total=None)

                mrs = list(mr_gen())
                stats["merge_requests"] = len(mrs)
                if mrs:
                    ingester.ingest_merge_requests(mrs)
                    max_mr_updated = _merge_request_max_updated(mrs)
            except GitlabError as exc:
                _exit_error(f"GitLab merge request ingestion failed: {exc}", exc=exc)

        head_hash = git_parser.repo.head.commit.hexsha
        mr_ts = max_mr_updated.isoformat() if max_mr_updated else None
        ingester.update_sync_state(last_commit_hash=head_hash, last_mr_updated_at=mr_ts)

    except Neo4jError as exc:
        _exit_error(f"Neo4j error during ingestion: {exc}", exc=exc)
    finally:
        driver.close()

    _print_ingest_summary("Ingestion complete", stats)


@app.command("update")
def update(
    config: Path = typer.Option(..., "--config", help="Path to repo2neo4j YAML configuration."),
) -> None:
    """Incremental update: new commits, updated MRs, and refreshed code structure at HEAD."""
    cfg = _load_app_config(config)
    repo_root = Path(cfg.repository.path).expanduser().resolve()
    if not repo_root.is_dir():
        _exit_error(f"Repository path is not a directory: {repo_root}")

    stats = {"commits": 0, "files": 0, "merge_requests": 0}

    try:
        git_parser = GitParser(repo_root)
    except git_exc.InvalidGitRepositoryError as exc:
        _exit_error(str(exc), exc=exc)

    try:
        driver = _neo4j_driver(cfg)
    except Exception as exc:  # noqa: BLE001
        _exit_error(f"Could not create Neo4j driver: {exc}", exc=exc)

    ingester = GraphIngester(
        driver,
        database=cfg.neo4j.database,
        repo_name=cfg.repository.name,
        batch_size=cfg.sync.batch_size,
    )

    try:
        sync = ingester.get_sync_state()
        since_hash = sync.get("last_commit_hash") if sync else None
        last_mr_dt = _sync_dt_to_datetime(sync.get("last_mr_updated_at") if sync else None)

        if sync is None:
            console.print(
                "[yellow]No SyncState found; processing commits since the beginning of history "
                "(subject to max_commits).[/yellow]"
            )

        default_tip = _default_branch_tip(git_parser)
        branches = git_parser.get_branches()
        ingester.ingest_branches(branches)

        commit_iter = git_parser.iter_commits(
            branch=default_tip,
            since_hash=str(since_hash) if since_hash else None,
            max_count=cfg.sync.max_commits,
        )
        commits = list(
            _tracked_iter(
                commit_iter,
                "Processing new commits",
                total=cfg.sync.max_commits,
            )
        )
        stats["commits"] = len(commits)
        if commits:
            ingester.ingest_commits(commits)

        stats["files"] = _ingest_code_structure(cfg, ingester, repo_root)

        max_mr_updated: datetime | None = None
        if cfg.gitlab is not None:
            try:
                gl = GitLabClient(
                    cfg.gitlab.url,
                    cfg.gitlab.project_id,
                    cfg.gitlab.private_token,
                )
                mr_iter = gl.iter_merge_requests(
                    state="all",
                    updated_after=last_mr_dt,
                    order_by="updated_at",
                )

                def mr_gen() -> Iterator[MergeRequestModel]:
                    yield from _tracked_iter(mr_iter, "Fetching updated merge requests", total=None)

                mrs = list(mr_gen())
                stats["merge_requests"] = len(mrs)
                if mrs:
                    ingester.ingest_merge_requests(mrs)
                    max_mr_updated = _merge_request_max_updated(mrs)
            except GitlabError as exc:
                _exit_error(f"GitLab merge request update failed: {exc}", exc=exc)

        head_hash = git_parser.repo.head.commit.hexsha
        mr_ts = max_mr_updated.isoformat() if max_mr_updated is not None else None
        ingester.update_sync_state(last_commit_hash=head_hash, last_mr_updated_at=mr_ts)

    except Neo4jError as exc:
        _exit_error(f"Neo4j error during update: {exc}", exc=exc)
    finally:
        driver.close()

    _print_ingest_summary("Update complete", stats)


def _print_ingest_summary(title: str, stats: dict[str, int]) -> None:
    table = Table(title=title)
    table.add_column("Metric", style="cyan")
    table.add_column("Count", justify="right", style="green")
    table.add_row("Commits processed", str(stats["commits"]))
    table.add_row("Files parsed (AST)", str(stats["files"]))
    table.add_row("Merge requests processed", str(stats["merge_requests"]))
    console.print(table)


QUERY_REQUIRED: dict[str, tuple[str, ...]] = {
    "files_changed_in_mr": ("mr_iid",),
    "commit_history": ("file_path",),
    "function_callers": ("function_name",),
    "class_hierarchy": ("class_name",),
    "file_dependencies": ("file_path",),
    "author_contributions": ("author_email",),
    "hot_files": (),
    "mr_risk_score": ("mr_iid",),
    "recent_changes": (),
    "code_structure": (),
    "mr_summary": ("mr_iid",),
    "search_functions": ("pattern",),
    "search_classes": ("pattern",),
}


@app.command("query")
def query_command(
    query_name: str = typer.Argument(..., help="Predefined query name (see AgentQueryAPI)."),
    config: Path = typer.Option(..., "--config", help="Path to repo2neo4j YAML configuration."),
    mr_iid: int | None = typer.Option(None, "--mr-iid", help="Merge request internal id."),
    file_path: str | None = typer.Option(None, "--file-path", help="Repository-relative file path."),
    function_name: str | None = typer.Option(None, "--function-name"),
    class_name: str | None = typer.Option(None, "--class-name"),
    author_email: str | None = typer.Option(None, "--author-email"),
    days: int | None = typer.Option(None, "--days"),
    limit: int | None = typer.Option(None, "--limit"),
    directory: str | None = typer.Option(None, "--directory"),
    pattern: str | None = typer.Option(None, "--pattern"),
) -> None:
    """Run a named analytics query and print JSON to stdout."""
    cfg = _load_app_config(config)
    required = QUERY_REQUIRED.get(query_name)
    if required is None:
        known = ", ".join(sorted(QUERY_REQUIRED))
        _exit_error(f"Unknown query {query_name!r}. Choose one of: {known}")

    provided: dict[str, Any] = {
        "mr_iid": mr_iid,
        "file_path": file_path,
        "function_name": function_name,
        "class_name": class_name,
        "author_email": author_email,
        "days": days,
        "limit": limit,
        "directory": directory,
        "pattern": pattern,
    }
    missing = [name for name in required if provided.get(name) in (None, "")]
    if missing:
        _exit_error(f"Query {query_name!r} requires: {', '.join(missing)}")

    kwargs: dict[str, Any] = {}
    if mr_iid is not None:
        kwargs["mr_iid"] = mr_iid
    if file_path is not None:
        kwargs["file_path"] = file_path
    if function_name is not None:
        kwargs["function_name"] = function_name
    if class_name is not None:
        kwargs["class_name"] = class_name
    if author_email is not None:
        kwargs["author_email"] = author_email
    if days is not None:
        kwargs["days"] = days
    if limit is not None:
        kwargs["limit"] = limit
    if directory is not None:
        kwargs["directory"] = directory
    if pattern is not None:
        kwargs["pattern"] = pattern

    api = AgentQueryAPI.from_config(cfg)
    try:
        try:
            result = api.query(query_name, **kwargs)
        except TypeError as exc:
            _exit_error(f"Invalid arguments for query {query_name!r}: {exc}", exc=exc)
        except ValueError as exc:
            _exit_error(str(exc), exc=exc)
        except Neo4jError as exc:
            _exit_error(f"Neo4j query error: {exc}", exc=exc)
    finally:
        api.close()

    text = json.dumps(result, indent=2, default=str)
    sys.stdout.write(text + "\n")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
