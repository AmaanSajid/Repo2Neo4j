"""Neo4j graph ingester for repository data.

This is the MOST CRITICAL component of repo2neo4j. It efficiently writes parsed data
into Neo4j using batch operations, MERGE statements for idempotency, and supports
incremental synchronization via SyncState tracking.
"""

from __future__ import annotations

import itertools
import logging
from collections.abc import Iterable
from datetime import datetime
from typing import Any

import neo4j
from neo4j import Driver, Session

from repo2neo4j.models.code import FileModel
from repo2neo4j.models.git import BranchModel, CommitModel
from repo2neo4j.models.gitlab import MergeRequestModel

logger = logging.getLogger(__name__)


class GraphIngester:
    """High-performance Neo4j ingester for repository data.
    
    Uses batch operations with UNWIND for maximum efficiency and MERGE statements
    for idempotency. Supports incremental sync via SyncState node tracking.
    """

    def __init__(
        self,
        driver: Driver,
        database: str = "neo4j",
        repo_name: str = "",
        batch_size: int = 500,
    ) -> None:
        """Initialize the graph ingester.
        
        Args:
            driver: Neo4j driver instance
            database: Target database name
            repo_name: Repository name for composite uniqueness
            batch_size: Number of items to process per batch transaction
        """
        self.driver = driver
        self.database = database
        self.repo_name = repo_name
        self.batch_size = batch_size

    def ingest_repository(
        self,
        name: str,
        url: str = "",
        default_branch: str = "main",
    ) -> None:
        """MERGE Repository node with given properties.
        
        Args:
            name: Repository name (unique identifier)
            url: Repository URL
            default_branch: Default branch name
        """
        logger.info("Ingesting repository: %s", name)
        
        def _ingest_repo_tx(tx: neo4j.Transaction) -> None:
            query = """
            MERGE (r:Repository {name: $name})
            SET r.url = $url,
                r.default_branch = $default_branch,
                r.updated_at = datetime()
            """
            tx.run(query, name=name, url=url, default_branch=default_branch)

        with self.driver.session(database=self.database) as session:
            session.execute_write(_ingest_repo_tx)
        
        logger.info("Repository ingested: %s", name)

    def ingest_branches(self, branches: list[BranchModel]) -> None:
        """Batch MERGE Branch nodes and HAS_BRANCH relationships.
        
        Args:
            branches: List of branch models to ingest
        """
        if not branches:
            logger.debug("No branches to ingest")
            return

        logger.info("Ingesting %d branches for repo: %s", len(branches), self.repo_name)

        def _ingest_branches_tx(tx: neo4j.Transaction, batch: list[dict[str, Any]]) -> None:
            # MERGE Branch nodes
            branch_query = """
            UNWIND $batch AS row
            MERGE (b:Branch {name: row.name, repo_name: row.repo_name})
            SET b.is_default = row.is_default,
                b.head_commit_hash = row.head_commit_hash,
                b.updated_at = datetime()
            """
            tx.run(branch_query, batch=batch)

            # Create HAS_BRANCH relationships
            rel_query = """
            UNWIND $batch AS row
            MATCH (r:Repository {name: row.repo_name})
            MATCH (b:Branch {name: row.name, repo_name: row.repo_name})
            MERGE (r)-[:HAS_BRANCH]->(b)
            """
            tx.run(rel_query, batch=batch)

        # Convert to batch format
        branch_data = [
            {
                "name": branch.name,
                "repo_name": self.repo_name,
                "is_default": branch.is_default,
                "head_commit_hash": branch.head_commit_hash,
            }
            for branch in branches
        ]

        # Process in batches
        for batch in self._chunk_list(branch_data, self.batch_size):
            with self.driver.session(database=self.database) as session:
                session.execute_write(_ingest_branches_tx, batch)

        logger.info("Ingested %d branches for repo: %s", len(branches), self.repo_name)

    def ingest_commits(self, commits: Iterable[CommitModel]) -> None:
        """Batch MERGE Commits, Authors, and relationships.
        
        This is the MOST performance-critical method. Processes commits in batches
        to handle large repositories efficiently.
        
        Args:
            commits: Iterable of commit models to ingest
        """
        commit_count = 0
        
        for batch in self._chunk_iterable(commits, self.batch_size):
            if not batch:
                continue
                
            batch_size = len(batch)
            commit_count += batch_size
            
            logger.debug("Processing commit batch of size %d", batch_size)

            def _ingest_commits_tx(tx: neo4j.Transaction, commit_batch: list[CommitModel]) -> None:
                # Prepare batch data
                commit_data = []
                author_data = []
                committer_data = []
                parent_data = []
                diff_data = []

                for commit in commit_batch:
                    commit_data.append({
                        "hash": commit.hash,
                        "short_hash": commit.short_hash,
                        "message": commit.message,
                        "timestamp": commit.timestamp.isoformat(),
                        "branch": commit.branch,
                        "repo_name": self.repo_name,
                    })

                    # Author data (deduplicated by email)
                    author_data.append({
                        "email": commit.author.email,
                        "name": commit.author.name,
                    })

                    # Committer data (deduplicated by email)
                    committer_data.append({
                        "email": commit.committer.email,
                        "name": commit.committer.name,
                    })

                    # Parent relationships
                    for parent_hash in commit.parent_hashes:
                        parent_data.append({
                            "child_hash": commit.hash,
                            "parent_hash": parent_hash,
                        })

                    # File modifications
                    for diff in commit.diffs:
                        diff_data.append({
                            "commit_hash": commit.hash,
                            "file_path": diff.path,
                            "old_path": diff.old_path,
                            "status": diff.status.value,
                            "additions": diff.additions,
                            "deletions": diff.deletions,
                            "repo_name": self.repo_name,
                        })

                # MERGE Commits
                commit_query = """
                UNWIND $batch AS row
                MERGE (c:Commit {hash: row.hash})
                SET c.short_hash = row.short_hash,
                    c.message = row.message,
                    c.timestamp = datetime(row.timestamp),
                    c.branch = row.branch,
                    c.repo_name = row.repo_name,
                    c.updated_at = datetime()
                """
                tx.run(commit_query, batch=commit_data)

                # MERGE Authors (deduplicated)
                if author_data:
                    author_query = """
                    UNWIND $batch AS row
                    MERGE (a:Author {email: row.email})
                    SET a.name = row.name,
                        a.updated_at = datetime()
                    """
                    # Deduplicate authors by email
                    unique_authors = {author["email"]: author for author in author_data}.values()
                    tx.run(author_query, batch=list(unique_authors))

                # MERGE Committers (deduplicated)
                if committer_data:
                    committer_query = """
                    UNWIND $batch AS row
                    MERGE (a:Author {email: row.email})
                    SET a.name = row.name,
                        a.updated_at = datetime()
                    """
                    # Deduplicate committers by email
                    unique_committers = {committer["email"]: committer for committer in committer_data}.values()
                    tx.run(committer_query, batch=list(unique_committers))

                # Create AUTHORED_BY relationships
                authored_query = """
                UNWIND $batch AS row
                MATCH (c:Commit {hash: row.hash})
                MATCH (a:Author {email: row.author_email})
                MERGE (c)-[:AUTHORED_BY]->(a)
                """
                authored_data = [
                    {"hash": commit.hash, "author_email": commit.author.email}
                    for commit in commit_batch
                ]
                tx.run(authored_query, batch=authored_data)

                # Create COMMITTED_BY relationships (if different from author)
                committed_data = [
                    {"hash": commit.hash, "committer_email": commit.committer.email}
                    for commit in commit_batch
                    if commit.committer.email != commit.author.email
                ]
                if committed_data:
                    committed_query = """
                    UNWIND $batch AS row
                    MATCH (c:Commit {hash: row.hash})
                    MATCH (a:Author {email: row.committer_email})
                    MERGE (c)-[:COMMITTED_BY]->(a)
                    """
                    tx.run(committed_query, batch=committed_data)

                # Create PARENT_OF relationships
                if parent_data:
                    parent_query = """
                    UNWIND $batch AS row
                    MATCH (child:Commit {hash: row.child_hash})
                    MATCH (parent:Commit {hash: row.parent_hash})
                    MERGE (parent)-[:PARENT_OF]->(child)
                    """
                    tx.run(parent_query, batch=parent_data)

                # Create MODIFIES relationships to files
                if diff_data:
                    modifies_query = """
                    UNWIND $batch AS row
                    MATCH (c:Commit {hash: row.commit_hash})
                    MERGE (f:File {path: row.file_path, repo_name: row.repo_name})
                    MERGE (c)-[m:MODIFIES]->(f)
                    SET m.status = row.status,
                        m.old_path = row.old_path,
                        m.additions = row.additions,
                        m.deletions = row.deletions
                    """
                    tx.run(modifies_query, batch=diff_data)

                # Link commits to repository
                repo_query = """
                UNWIND $batch AS row
                MATCH (r:Repository {name: row.repo_name})
                MATCH (c:Commit {hash: row.hash})
                MERGE (r)-[:HAS_COMMIT]->(c)
                """
                tx.run(repo_query, batch=commit_data)

            with self.driver.session(database=self.database) as session:
                session.execute_write(_ingest_commits_tx, batch)

        logger.info("Ingested %d commits for repo: %s", commit_count, self.repo_name)

    def ingest_files(self, files: Iterable[FileModel]) -> None:
        """Batch MERGE Files, Directories, Classes, Functions and relationships.
        
        Args:
            files: Iterable of file models to ingest
        """
        file_count = 0
        
        for batch in self._chunk_iterable(files, self.batch_size):
            if not batch:
                continue
                
            batch_size = len(batch)
            file_count += batch_size
            
            logger.debug("Processing file batch of size %d", batch_size)

            def _ingest_files_tx(tx: neo4j.Transaction, file_batch: list[FileModel]) -> None:
                # Prepare batch data
                file_data = []
                directory_data = []
                class_data = []
                function_data = []
                import_data = []
                directory_rels = []
                
                for file_model in file_batch:
                    file_data.append({
                        "path": file_model.path,
                        "repo_name": self.repo_name,
                        "language": file_model.language,
                        "size": file_model.size,
                    })

                    # Build directory hierarchy
                    dir_chain = self._build_directory_chain(file_model.path)
                    for child_path, parent_path in dir_chain:
                        directory_data.append({
                            "path": child_path,
                            "repo_name": self.repo_name,
                        })
                        if parent_path:
                            directory_rels.append({
                                "child_path": child_path,
                                "parent_path": parent_path,
                                "repo_name": self.repo_name,
                            })

                    # Classes
                    for class_model in file_model.classes:
                        class_data.append({
                            "qualified_name": class_model.qualified_name,
                            "name": class_model.name,
                            "file_path": class_model.file_path,
                            "repo_name": self.repo_name,
                            "start_line": class_model.start_line,
                            "end_line": class_model.end_line,
                            "bases": class_model.bases,
                        })

                        # Class methods
                        for method in class_model.methods:
                            function_data.append({
                                "qualified_name": method.qualified_name,
                                "name": method.name,
                                "file_path": method.file_path,
                                "repo_name": self.repo_name,
                                "start_line": method.start_line,
                                "end_line": method.end_line,
                                "parameters": method.parameters,
                                "return_type": method.return_type,
                                "is_method": method.is_method,
                                "class_name": method.class_name,
                                "calls": method.calls,
                            })

                    # Standalone functions
                    for func_model in file_model.functions:
                        function_data.append({
                            "qualified_name": func_model.qualified_name,
                            "name": func_model.name,
                            "file_path": func_model.file_path,
                            "repo_name": self.repo_name,
                            "start_line": func_model.start_line,
                            "end_line": func_model.end_line,
                            "parameters": func_model.parameters,
                            "return_type": func_model.return_type,
                            "is_method": func_model.is_method,
                            "class_name": func_model.class_name,
                            "calls": func_model.calls,
                        })

                    # Imports
                    for import_model in file_model.imports:
                        import_data.append({
                            "source_file": import_model.source_file,
                            "imported_name": import_model.imported_name,
                            "module_path": import_model.module_path,
                            "alias": import_model.alias,
                            "repo_name": self.repo_name,
                        })

                # MERGE Files
                file_query = """
                UNWIND $batch AS row
                MERGE (f:File {path: row.path, repo_name: row.repo_name})
                SET f.language = row.language,
                    f.size = row.size,
                    f.updated_at = datetime()
                """
                tx.run(file_query, batch=file_data)

                # MERGE Directories (deduplicated)
                if directory_data:
                    unique_dirs = {dir_item["path"]: dir_item for dir_item in directory_data}.values()
                    dir_query = """
                    UNWIND $batch AS row
                    MERGE (d:Directory {path: row.path, repo_name: row.repo_name})
                    SET d.updated_at = datetime()
                    """
                    tx.run(dir_query, batch=list(unique_dirs))

                # Create directory hierarchy relationships
                if directory_rels:
                    child_query = """
                    UNWIND $batch AS row
                    MATCH (child:Directory {path: row.child_path, repo_name: row.repo_name})
                    MATCH (parent:Directory {path: row.parent_path, repo_name: row.repo_name})
                    MERGE (parent)-[:CHILD_OF]->(child)
                    """
                    tx.run(child_query, batch=directory_rels)

                # Link files to directories
                file_dir_query = """
                UNWIND $batch AS row
                MATCH (f:File {path: row.path, repo_name: row.repo_name})
                WITH f, row, split(row.path, '/') AS parts
                WITH f, row, parts[0..-1] AS dir_parts
                WITH f, row, reduce(s = '', part IN dir_parts | s + CASE WHEN s = '' THEN part ELSE '/' + part END) AS dir_path
                WHERE dir_path <> ''
                MATCH (d:Directory {path: dir_path, repo_name: row.repo_name})
                MERGE (d)-[:CONTAINS_FILE]->(f)
                """
                tx.run(file_dir_query, batch=file_data)

                # MERGE Classes
                if class_data:
                    class_query = """
                    UNWIND $batch AS row
                    MERGE (c:Class {qualified_name: row.qualified_name, repo_name: row.repo_name})
                    SET c.name = row.name,
                        c.file_path = row.file_path,
                        c.start_line = row.start_line,
                        c.end_line = row.end_line,
                        c.bases = row.bases,
                        c.updated_at = datetime()
                    """
                    tx.run(class_query, batch=class_data)

                    # Link classes to files
                    class_file_query = """
                    UNWIND $batch AS row
                    MATCH (f:File {path: row.file_path, repo_name: row.repo_name})
                    MATCH (c:Class {qualified_name: row.qualified_name, repo_name: row.repo_name})
                    MERGE (f)-[:CONTAINS_CLASS]->(c)
                    """
                    tx.run(class_file_query, batch=class_data)

                    # Create inheritance relationships
                    inheritance_data = [
                        {
                            "child_qualified_name": class_item["qualified_name"],
                            "parent_name": base,
                            "repo_name": self.repo_name,
                        }
                        for class_item in class_data
                        for base in class_item["bases"]
                        if base
                    ]
                    if inheritance_data:
                        inherit_query = """
                        UNWIND $batch AS row
                        MATCH (child:Class {qualified_name: row.child_qualified_name, repo_name: row.repo_name})
                        MATCH (parent:Class {name: row.parent_name, repo_name: row.repo_name})
                        MERGE (child)-[:INHERITS]->(parent)
                        """
                        tx.run(inherit_query, batch=inheritance_data)

                # MERGE Functions
                if function_data:
                    function_query = """
                    UNWIND $batch AS row
                    MERGE (f:Function {qualified_name: row.qualified_name, repo_name: row.repo_name})
                    SET f.name = row.name,
                        f.file_path = row.file_path,
                        f.start_line = row.start_line,
                        f.end_line = row.end_line,
                        f.parameters = row.parameters,
                        f.return_type = row.return_type,
                        f.is_method = row.is_method,
                        f.class_name = row.class_name,
                        f.calls = row.calls,
                        f.updated_at = datetime()
                    """
                    tx.run(function_query, batch=function_data)

                    # Link functions to files
                    func_file_query = """
                    UNWIND $batch AS row
                    MATCH (file:File {path: row.file_path, repo_name: row.repo_name})
                    MATCH (func:Function {qualified_name: row.qualified_name, repo_name: row.repo_name})
                    MERGE (file)-[:CONTAINS_FUNCTION]->(func)
                    """
                    tx.run(func_file_query, batch=function_data)

                    # Link methods to classes
                    method_data = [f for f in function_data if f["is_method"] and f["class_name"]]
                    if method_data:
                        method_class_query = """
                        UNWIND $batch AS row
                        MATCH (c:Class {name: row.class_name, repo_name: row.repo_name})
                        MATCH (f:Function {qualified_name: row.qualified_name, repo_name: row.repo_name})
                        MERGE (c)-[:HAS_METHOD]->(f)
                        """
                        tx.run(method_class_query, batch=method_data)

                    # Create function call relationships
                    call_data = [
                        {
                            "caller_qualified_name": func_item["qualified_name"],
                            "called_name": call,
                            "repo_name": self.repo_name,
                        }
                        for func_item in function_data
                        for call in func_item["calls"]
                        if call
                    ]
                    if call_data:
                        call_query = """
                        UNWIND $batch AS row
                        MATCH (caller:Function {qualified_name: row.caller_qualified_name, repo_name: row.repo_name})
                        MATCH (called:Function {name: row.called_name, repo_name: row.repo_name})
                        MERGE (caller)-[:CALLS]->(called)
                        """
                        tx.run(call_query, batch=call_data)

                # Handle imports
                if import_data:
                    import_query = """
                    UNWIND $batch AS row
                    MATCH (source:File {path: row.source_file, repo_name: row.repo_name})
                    MERGE (target:File {path: row.module_path, repo_name: row.repo_name})
                    MERGE (source)-[i:IMPORTS]->(target)
                    SET i.imported_name = row.imported_name,
                        i.alias = row.alias
                    """
                    # Only process imports with valid module paths
                    valid_imports = [imp for imp in import_data if imp["module_path"]]
                    if valid_imports:
                        tx.run(import_query, batch=valid_imports)

            with self.driver.session(database=self.database) as session:
                session.execute_write(_ingest_files_tx, batch)

        logger.info("Ingested %d files for repo: %s", file_count, self.repo_name)

    def ingest_merge_requests(self, merge_requests: Iterable[MergeRequestModel]) -> None:
        """Batch MERGE MergeRequests and related relationships.
        
        Args:
            merge_requests: Iterable of merge request models to ingest
        """
        mr_count = 0
        
        for batch in self._chunk_iterable(merge_requests, self.batch_size):
            if not batch:
                continue
                
            batch_size = len(batch)
            mr_count += batch_size
            
            logger.debug("Processing merge request batch of size %d", batch_size)

            def _ingest_mrs_tx(tx: neo4j.Transaction, mr_batch: list[MergeRequestModel]) -> None:
                # Prepare batch data
                mr_data = []
                author_data = []
                review_data = []
                commit_rels = []

                for mr in mr_batch:
                    mr_data.append({
                        "iid": mr.iid,
                        "repo_name": self.repo_name,
                        "title": mr.title,
                        "description": mr.description,
                        "state": mr.state.value,
                        "source_branch": mr.source_branch,
                        "target_branch": mr.target_branch,
                        "author_name": mr.author_name,
                        "author_username": mr.author_username,
                        "created_at": mr.created_at.isoformat(),
                        "updated_at": mr.updated_at.isoformat() if mr.updated_at else None,
                        "merged_at": mr.merged_at.isoformat() if mr.merged_at else None,
                        "closed_at": mr.closed_at.isoformat() if mr.closed_at else None,
                        "web_url": mr.web_url,
                        "labels": mr.labels,
                    })

                    # Author data (using username as unique key for MR authors)
                    author_data.append({
                        "username": mr.author_username,
                        "name": mr.author_name,
                    })

                    # Review data
                    for review in mr.reviews:
                        review_data.append({
                            "mr_iid": mr.iid,
                            "reviewer_username": review.reviewer_username,
                            "reviewer_name": review.reviewer_name,
                            "reviewer_email": review.reviewer_email,
                            "approved": review.approved,
                            "created_at": review.created_at.isoformat() if review.created_at else None,
                            "repo_name": self.repo_name,
                        })

                    # Commit relationships
                    for commit_hash in mr.commit_hashes:
                        commit_rels.append({
                            "mr_iid": mr.iid,
                            "commit_hash": commit_hash,
                            "repo_name": self.repo_name,
                        })

                # MERGE MergeRequests
                mr_query = """
                UNWIND $batch AS row
                MERGE (mr:MergeRequest {iid: row.iid, repo_name: row.repo_name})
                SET mr.title = row.title,
                    mr.description = row.description,
                    mr.state = row.state,
                    mr.source_branch = row.source_branch,
                    mr.target_branch = row.target_branch,
                    mr.author_name = row.author_name,
                    mr.author_username = row.author_username,
                    mr.created_at = datetime(row.created_at),
                    mr.updated_at = CASE WHEN row.updated_at IS NOT NULL THEN datetime(row.updated_at) ELSE NULL END,
                    mr.merged_at = CASE WHEN row.merged_at IS NOT NULL THEN datetime(row.merged_at) ELSE NULL END,
                    mr.closed_at = CASE WHEN row.closed_at IS NOT NULL THEN datetime(row.closed_at) ELSE NULL END,
                    mr.web_url = row.web_url,
                    mr.labels = row.labels,
                    mr.updated_at_sync = datetime()
                """
                tx.run(mr_query, batch=mr_data)

                # MERGE MR Authors (using username, not email)
                if author_data:
                    unique_authors = {author["username"]: author for author in author_data}.values()
                    author_query = """
                    UNWIND $batch AS row
                    MERGE (u:User {username: row.username})
                    SET u.name = row.name,
                        u.updated_at = datetime()
                    """
                    tx.run(author_query, batch=list(unique_authors))

                # Create CREATED_BY relationships
                created_by_query = """
                UNWIND $batch AS row
                MATCH (mr:MergeRequest {iid: row.iid, repo_name: row.repo_name})
                MATCH (u:User {username: row.author_username})
                MERGE (mr)-[:CREATED_BY]->(u)
                """
                tx.run(created_by_query, batch=mr_data)

                # Create branch relationships
                source_branch_query = """
                UNWIND $batch AS row
                MATCH (mr:MergeRequest {iid: row.iid, repo_name: row.repo_name})
                MATCH (b:Branch {name: row.source_branch, repo_name: row.repo_name})
                MERGE (mr)-[:SOURCES]->(b)
                """
                tx.run(source_branch_query, batch=mr_data)

                target_branch_query = """
                UNWIND $batch AS row
                MATCH (mr:MergeRequest {iid: row.iid, repo_name: row.repo_name})
                MATCH (b:Branch {name: row.target_branch, repo_name: row.repo_name})
                MERGE (mr)-[:TARGETS]->(b)
                """
                tx.run(target_branch_query, batch=mr_data)

                # Create CONTAINS_COMMIT relationships
                if commit_rels:
                    commit_query = """
                    UNWIND $batch AS row
                    MATCH (mr:MergeRequest {iid: row.mr_iid, repo_name: row.repo_name})
                    MATCH (c:Commit {hash: row.commit_hash})
                    MERGE (mr)-[:CONTAINS_COMMIT]->(c)
                    """
                    tx.run(commit_query, batch=commit_rels)

                # Handle reviews
                if review_data:
                    # MERGE reviewers as Users
                    unique_reviewers = {
                        review["reviewer_username"]: {
                            "username": review["reviewer_username"],
                            "name": review["reviewer_name"],
                            "email": review["reviewer_email"],
                        }
                        for review in review_data
                        if review["reviewer_username"]
                    }.values()
                    
                    if unique_reviewers:
                        reviewer_query = """
                        UNWIND $batch AS row
                        MERGE (u:User {username: row.username})
                        SET u.name = row.name,
                            u.email = row.email,
                            u.updated_at = datetime()
                        """
                        tx.run(reviewer_query, batch=list(unique_reviewers))

                        # Create REVIEWED_BY relationships
                        reviewed_by_query = """
                        UNWIND $batch AS row
                        MATCH (mr:MergeRequest {iid: row.mr_iid, repo_name: row.repo_name})
                        MATCH (u:User {username: row.reviewer_username})
                        MERGE (mr)-[r:REVIEWED_BY]->(u)
                        SET r.approved = row.approved,
                            r.created_at = CASE WHEN row.created_at IS NOT NULL THEN datetime(row.created_at) ELSE NULL END
                        """
                        tx.run(reviewed_by_query, batch=review_data)

                # Link to repository
                repo_query = """
                UNWIND $batch AS row
                MATCH (r:Repository {name: row.repo_name})
                MATCH (mr:MergeRequest {iid: row.iid, repo_name: row.repo_name})
                MERGE (r)-[:HAS_MERGE_REQUEST]->(mr)
                """
                tx.run(repo_query, batch=mr_data)

            with self.driver.session(database=self.database) as session:
                session.execute_write(_ingest_mrs_tx, batch)

        logger.info("Ingested %d merge requests for repo: %s", mr_count, self.repo_name)

    def get_sync_state(self) -> dict[str, Any] | None:
        """Read SyncState node for this repository.
        
        Returns:
            Dictionary with sync state data or None if not found
        """
        def _get_sync_state_tx(tx: neo4j.Transaction) -> dict[str, Any] | None:
            query = """
            MATCH (s:SyncState {repo_name: $repo_name})
            RETURN s.last_commit_hash AS last_commit_hash,
                   s.last_mr_updated_at AS last_mr_updated_at,
                   s.updated_at AS updated_at
            """
            result = tx.run(query, repo_name=self.repo_name)
            record = result.single()
            if record:
                return {
                    "last_commit_hash": record["last_commit_hash"],
                    "last_mr_updated_at": record["last_mr_updated_at"],
                    "updated_at": record["updated_at"],
                }
            return None

        with self.driver.session(database=self.database) as session:
            return session.execute_read(_get_sync_state_tx)

    def update_sync_state(
        self,
        last_commit_hash: str | None = None,
        last_mr_updated_at: str | None = None,
    ) -> None:
        """Update SyncState node for incremental synchronization.
        
        Args:
            last_commit_hash: Hash of the last processed commit
            last_mr_updated_at: ISO timestamp of last processed MR update
        """
        def _update_sync_state_tx(tx: neo4j.Transaction) -> None:
            query = """
            MERGE (s:SyncState {repo_name: $repo_name})
            SET s.updated_at = datetime()
            """
            params = {"repo_name": self.repo_name}
            
            if last_commit_hash is not None:
                query += ", s.last_commit_hash = $last_commit_hash"
                params["last_commit_hash"] = last_commit_hash
                
            if last_mr_updated_at is not None:
                query += ", s.last_mr_updated_at = $last_mr_updated_at"
                params["last_mr_updated_at"] = last_mr_updated_at
            
            tx.run(query, **params)

        with self.driver.session(database=self.database) as session:
            session.execute_write(_update_sync_state_tx)
        
        logger.info(
            "Updated sync state for repo %s: commit=%s, mr_updated=%s",
            self.repo_name,
            last_commit_hash,
            last_mr_updated_at,
        )

    def _build_directory_chain(self, file_path: str) -> list[tuple[str, str]]:
        """Build directory hierarchy chain for a file path.
        
        Args:
            file_path: Full file path (e.g., "src/components/Button.tsx")
            
        Returns:
            List of (child_path, parent_path) tuples for directory hierarchy
        """
        if "/" not in file_path:
            return []
            
        parts = file_path.split("/")[:-1]  # Exclude filename
        if not parts:
            return []
            
        chain = []
        for i in range(len(parts)):
            child_path = "/".join(parts[: i + 1])
            parent_path = "/".join(parts[:i]) if i > 0 else ""
            chain.append((child_path, parent_path))
            
        return chain

    def _chunk_list(self, items: list[Any], chunk_size: int) -> Iterable[list[Any]]:
        """Chunk a list into batches of specified size."""
        for i in range(0, len(items), chunk_size):
            yield items[i : i + chunk_size]

    def _chunk_iterable(self, items: Iterable[Any], chunk_size: int) -> Iterable[list[Any]]:
        """Chunk an iterable into batches of specified size."""
        iterator = iter(items)
        while True:
            batch = list(itertools.islice(iterator, chunk_size))
            if not batch:
                break
            yield batch