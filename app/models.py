"""Pydantic 请求/响应模型（同时为 /docs 提供示例）。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class FilePayload(BaseModel):
    path: str = Field(
        ...,
        description="工程内相对路径，使用 / 分隔；禁止绝对路径与 .. 越界",
        examples=["src/main.c"],
    )
    content: str = Field(..., description="文件文本内容（UTF-8）")


class BuildRequest(BaseModel):
    files: list[FilePayload] = Field(
        ..., min_length=1, description="整份小型 C 工程源码（含头文件）"
    )
    include_dirs: list[str] = Field(
        default_factory=list,
        description="GCC 头文件搜索目录（工程内相对路径），顺序即 -I 顺序；"
        "引号 include 始终先查当前文件目录",
        examples=[["include"]],
    )
    cflags: list[str] = Field(
        default_factory=list,
        description="透传给 gcc 的额外编译参数，例如 -Wall、-O2",
        examples=[["-Wall", "-O2"]],
    )
    program_name: str = Field(
        default="program", description="产物名称（下载时使用的文件名）"
    )


class FileStatus(BaseModel):
    path: str
    kind: Literal["source", "header"]
    status: Literal["compiled", "reused", "failed", "recompiled", "unknown"]


class UnitStatus(BaseModel):
    source: str
    status: Literal["compiled", "reused", "failed"]
    cache_key: str
    included_headers: list[str]
    error: str | None = None


class BuildRecord(BaseModel):
    build_id: str
    status: Literal["running", "succeeded", "failed"]
    compiler: str
    include_dirs: list[str]
    cflags: list[str]
    program_name: str
    files: list[FileStatus]
    units: list[UnitStatus]
    error: str | None = None
    download_path: str | None = None
    publish_warnings: list[str] = Field(
        default_factory=list,
        description="链接成功但个别对象写入共享缓存失败时的告警（不影响构建结果）",
    )


class CacheEntry(BaseModel):
    cache_key: str
    source: str
    source_sha256: str
    headers: list[dict]
    compiler: str
    cflags: list[str]
