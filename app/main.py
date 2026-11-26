"""FastAPI 入口：提交构建、查询状态、下载产物、观察缓存。"""

from __future__ import annotations

import os

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse

from .builder import BuildRequestSpec, IncrementalBuilder, SourceFile
from .models import BuildRecord, BuildRequest, CacheEntry
from .sample import sample_request
from .storage import KeyedLocks, Store, safe_rel_path

DATA_DIR = os.environ.get("DATA_DIR", "./data")
MAX_WORKERS = int(os.environ.get("BUILD_WORKERS", "4"))
CC = os.environ.get("CC", "gcc")

store = Store(DATA_DIR)
keyed_locks = KeyedLocks()
builder = IncrementalBuilder(
    store, keyed_locks, compiler=CC, max_workers=MAX_WORKERS
)

app = FastAPI(
    title="CI 增量编译 API",
    description=(
        "接收小型 C 工程的整份源码，按 GCC -I 目录顺序解析 include，"
        "以内容哈希做对象级增量编译。并发构建各自使用提交时的源码快照，"
        "构建失败不会破坏已有缓存。"
    ),
    version="1.0.0",
)


def _to_spec(req: BuildRequest) -> BuildRequestSpec:
    # 在请求阶段就拒绝非法路径，以 422 返回而不是留下一条 failed 构建。
    seen: set[str] = set()
    for f in req.files:
        rel = safe_rel_path(f.path)
        if rel in seen:
            raise HTTPException(status_code=422, detail=f"duplicate file: {rel}")
        seen.add(rel)
    for inc in req.include_dirs:
        safe_rel_path(inc)
    return BuildRequestSpec(
        files=[SourceFile(path=f.path, content=f.content) for f in req.files],
        include_dirs=list(req.include_dirs),
        cflags=list(req.cflags),
        program_name=req.program_name,
    )


def _load_record(build_id: str) -> dict:
    record_path = store.build_record_path(build_id)
    if not record_path.is_file():
        raise HTTPException(status_code=404, detail=f"unknown build: {build_id}")
    return store.read_json(record_path)


@app.get("/", tags=["meta"])
def root() -> dict:
    return {
        "service": "ci-incremental-build-api",
        "docs": "/docs",
        "sample": "/api/sample",
        "endpoints": [
            "POST /api/builds",
            "GET  /api/builds/{build_id}",
            "GET  /api/builds/{build_id}/download",
            "GET  /api/builds",
            "GET  /api/cache",
        ],
    }


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok", "compiler": builder._compiler_signature}


@app.get("/api/sample", tags=["sample"], response_model=BuildRequest)
def get_sample() -> BuildRequest:
    """返回可直接 POST 到 /api/builds 的样例工程。"""
    return sample_request()


@app.post("/api/builds", response_model=BuildRecord, status_code=202, tags=["builds"])
async def submit_build(req: BuildRequest, background: BackgroundTasks) -> dict:
    """提交一次构建：立即返回 build_id，编译在线程池中异步进行。"""
    try:
        spec = _to_spec(req)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    build_id = builder.prepare_build(spec)
    record = store.read_json(store.build_record_path(build_id))
    background.add_task(run_in_threadpool, _run_build, build_id, spec)
    return record


def _run_build(build_id: str, spec: BuildRequestSpec) -> None:
    """在线程池中执行构建，并把结果写回预先创建的构建目录。"""
    builder.build_in(build_id, spec)


@app.get("/api/builds", response_model=list[BuildRecord], tags=["builds"])
def list_builds() -> list[dict]:
    records = []
    for child in sorted(store.builds_dir.iterdir(), reverse=True):
        record_path = child / "record.json"
        if record_path.is_file():
            records.append(store.read_json(record_path))
    return records


@app.get("/api/builds/{build_id}", response_model=BuildRecord, tags=["builds"])
def get_build(build_id: str) -> dict:
    return _load_record(build_id)


@app.get("/api/builds/{build_id}/download", tags=["builds"])
def download_build(build_id: str) -> FileResponse:
    record = _load_record(build_id)
    if record["status"] != "succeeded":
        raise HTTPException(
            status_code=409,
            detail=f"build {build_id} is {record['status']}, no artifact available",
        )
    program = store.build_program_path(build_id)
    if not program.is_file():
        raise HTTPException(status_code=410, detail="artifact missing on disk")
    return FileResponse(
        program,
        filename=record.get("program_name") or "program",
        media_type="application/octet-stream",
    )


@app.get("/api/cache", response_model=list[CacheEntry], tags=["cache"])
def list_cache() -> list[dict]:
    entries = []
    for slot in sorted(store.objects_dir.iterdir()):
        meta_path = slot / "meta.json"
        if not meta_path.is_file():
            continue
        meta = store.read_json(meta_path)
        entries.append(
            {
                "cache_key": slot.name,
                "source": meta.get("source"),
                "source_sha256": meta.get("source_sha256"),
                "headers": meta.get("headers", []),
                "compiler": meta.get("compiler"),
                "cflags": meta.get("cflags", []),
            }
        )
    return entries
