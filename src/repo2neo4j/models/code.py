"""Data models for code structure extracted via AST parsing."""

from __future__ import annotations

from pydantic import BaseModel, Field


class FunctionModel(BaseModel):
    """A function or method extracted from source code."""

    name: str
    qualified_name: str
    file_path: str
    start_line: int
    end_line: int
    parameters: list[str] = Field(default_factory=list)
    return_type: str | None = None
    is_method: bool = False
    class_name: str | None = None
    calls: list[str] = Field(default_factory=list)


class ClassModel(BaseModel):
    """A class extracted from source code."""

    name: str
    qualified_name: str
    file_path: str
    start_line: int
    end_line: int
    bases: list[str] = Field(default_factory=list)
    methods: list[FunctionModel] = Field(default_factory=list)


class ImportModel(BaseModel):
    """An import statement."""

    source_file: str
    imported_name: str
    module_path: str | None = None
    alias: str | None = None


class FileModel(BaseModel):
    """A source file with its parsed structure."""

    path: str
    language: str
    size: int = 0
    classes: list[ClassModel] = Field(default_factory=list)
    functions: list[FunctionModel] = Field(default_factory=list)
    imports: list[ImportModel] = Field(default_factory=list)
