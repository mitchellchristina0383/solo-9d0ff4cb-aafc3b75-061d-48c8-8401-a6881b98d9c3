"""增量构建核心：快照、缓存键、对象复用与 GCC 调用。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from .scanner import IncludeResolver, IncludedFile, sha256_file
from .storage import KeyedLocks, Store, safe_rel_path

# 记录结构版本；meta/缓存键算法变化时抬升，避免读到不兼容的旧缓存。
CACHE_FORMAT_VERSION = 1


@dataclass
class SourceFile:
    path: str
    content: str  # 文本源码；小型 C 工程，API 以 UTF-8 字符串接收


@dataclass
class BuildRequestSpec:
    files: list[SourceFile]
    include_dirs: list[str] = field(default_factory=list)
    cflags: list[str] = field(default_factory=list)
    program_name: str = "program"


@dataclass
class UnitResult:
    source: str
    status: str  # "compiled" | "reused" | "failed"
    cache_key: str
    object_path: str | None = None
    included_headers: list[str] = field(default_factory=list)
    error: str | None = None
    # 仅在本次构建中真正调用 GCC 成功时填写；链接成功后据此发布缓存。
    publish_meta: dict | None = field(default=None, repr=False)


@dataclass
class UnitPlan:
    """加锁前算好的编译计划：缓存键在构建开始时就固定。"""

    source: str
    headers: list[IncludedFile]
    cache_key: str
    command: list[str]
    meta: dict
    work_obj: Path


class BuildError(Exception):
    """构建失败（校验、编译或链接错误）。"""


class IncrementalBuilder:
    def __init__(
        self,
        store: Store,
        keyed_locks: KeyedLocks,
        compiler: str = "gcc",
        max_workers: int = 4,
    ) -> None:
        self.store = store
        self.locks = keyed_locks
        self.compiler = compiler
        self.max_workers = max_workers
        self._compiler_signature = self._probe_compiler()

    # ------------------------------------------------------------------ #
    # 编译器探测
    # ------------------------------------------------------------------ #
    def _probe_compiler(self) -> str:
        try:
            out = subprocess.run(
                [self.compiler, "--version"],
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            )
            return out.stdout.splitlines()[0].strip()
        except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
            return f"unknown-compiler ({exc})"

    # ------------------------------------------------------------------ #
    # 入口
    # ------------------------------------------------------------------ #
    def prepare_build(self, spec: BuildRequestSpec) -> str:
        """预占构建目录并写入 running 占位记录，返回 build_id。"""
        build_id = uuid.uuid4().hex
        self.store.build_src_dir(build_id).mkdir(parents=True, exist_ok=True)
        self.store.build_work_dir(build_id).mkdir(parents=True, exist_ok=True)
        record = self._placeholder(build_id, spec)
        self._persist(record)
        return build_id

    def build(self, spec: BuildRequestSpec) -> dict:
        """一次性构建（内部自行分配 build_id）。"""
        build_id = self.prepare_build(spec)
        return self.build_in(build_id, spec)

    def _placeholder(self, build_id: str, spec: BuildRequestSpec) -> dict:
        return {
            "build_id": build_id,
            "status": "running",
            "compiler": self._compiler_signature,
            "include_dirs": list(spec.include_dirs),
            "cflags": list(spec.cflags),
            "program_name": spec.program_name,
            "files": [],
            "units": [],
            "error": None,
            "download_path": None,
            "publish_warnings": [],
        }

    def build_in(self, build_id: str, spec: BuildRequestSpec) -> dict:
        """在指定构建目录中执行构建，全程使用该目录内的源码快照。

        缓存发布严格放在链接成功之后：

        1. 先为每个单元计算缓存键与编译命令（不触碰共享缓存）；
        2. 按缓存键全局有序地一次性持锁，锁内查缓存、编译缺失对象；
        3. 全部单元编译成功后才链接；
        4. 链接成功后，才把本次真正新编译的对象发布到共享缓存。

        编译或链接失败时整批都不发布。于是并发提交同一工程时，后来的
        构建会等前一个持锁构建完成：成功则直接复用其刚发布的对象（每个
        未缓存对象只调用一次 GCC），失败则自己重新编译；而缓存键互不相交
        的构建之间不共享任何锁，可同时编译。
        """
        record = self._placeholder(build_id, spec)
        self._persist(record)

        try:
            sources = self._stage_sources(build_id, spec)
            src_dir = self.store.build_src_dir(build_id)
            resolver = IncludeResolver(src_dir, spec.include_dirs)
            plans = self._plan_units(build_id, sources, resolver, spec)

            with self.locks.acquire_many({p.cache_key for p in plans}):
                units = self._build_units(plans)
                record["units"] = [self._unit_to_dict(u) for u in units]
                record["files"] = self._file_statuses(src_dir, sources, units)

                failed = next((u for u in units if u.status == "failed"), None)
                if failed is not None:
                    raise BuildError(
                        f"compilation failed for {failed.source}: {failed.error}"
                    )

                self._link(build_id, units)
                # 链接成功是发布的前提；失败构建（编译或链接）不写任何缓存槽。
                record["publish_warnings"] = self._publish_new_objects(units)

            record["status"] = "succeeded"
            record["download_path"] = f"/api/builds/{build_id}/download"
        except BuildError as exc:
            record["status"] = "failed"
            record["error"] = str(exc)
        except Exception as exc:  # 防御：任何意外都落盘为失败，不留 running
            record["status"] = "failed"
            record["error"] = f"internal error: {exc!r}"
        finally:
            self._persist(record)
        return record

    # ------------------------------------------------------------------ #
    # 快照
    # ------------------------------------------------------------------ #
    def _stage_sources(self, build_id: str, spec: BuildRequestSpec) -> list[str]:
        src_dir = self.store.build_src_dir(build_id)
        staged: dict[str, str] = {}
        for sf in spec.files:
            rel = safe_rel_path(sf.path)
            if rel in staged:
                raise BuildError(f"duplicate file in submission: {rel}")
            staged[rel] = sf.content

        sources: list[str] = []
        for rel, content in staged.items():
            dst = src_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(content, encoding="utf-8")
            if self._is_compilable(rel):
                sources.append(rel)
        if not sources:
            raise BuildError("no compilable source files (.c/.cc/.cpp/.cxx) submitted")
        return sorted(sources)

    @staticmethod
    def _is_compilable(rel: str) -> bool:
        return rel.endswith((".c", ".cc", ".cpp", ".cxx"))

    # ------------------------------------------------------------------ #
    # 编译计划：加锁前确定缓存键与命令
    # ------------------------------------------------------------------ #
    def _plan_units(
        self,
        build_id: str,
        sources: list[str],
        resolver: IncludeResolver,
        spec: BuildRequestSpec,
    ) -> list[UnitPlan]:
        src_dir = self.store.build_src_dir(build_id)
        work_dir = self.store.build_work_dir(build_id)
        plans: list[UnitPlan] = []
        for src_rel in sources:
            src_abs = src_dir / src_rel
            headers = resolver.closure(src_rel)
            source_hash = sha256_file(src_abs)
            cache_key = self._cache_key(src_rel, source_hash, headers, spec)
            work_obj = work_dir / (src_rel + ".o")
            work_obj.parent.mkdir(parents=True, exist_ok=True)
            command = self._compile_command(src_abs, work_obj, src_dir, spec)
            meta = {
                "cache_format_version": CACHE_FORMAT_VERSION,
                "source": src_rel,
                "source_sha256": source_hash,
                "compiler": self._compiler_signature,
                "include_dirs": list(spec.include_dirs),
                "cflags": list(spec.cflags),
                "headers": [
                    {"path": h.rel_path, "sha256": h.sha256} for h in headers
                ],
                "command": command,
            }
            plans.append(
                UnitPlan(
                    source=src_rel,
                    headers=headers,
                    cache_key=cache_key,
                    command=command,
                    meta=meta,
                    work_obj=work_obj,
                )
            )
        return plans

    # ------------------------------------------------------------------ #
    # 单元编译：在整簇缓存键锁内查缓存 / 调用 GCC
    # ------------------------------------------------------------------ #
    def _build_units(self, plans: list[UnitPlan]) -> list[UnitResult]:
        if not plans:
            return []
        # 同一构建内各单元缓存键互不相同（键含源路径），可并行编译。
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            return list(pool.map(self._compile_or_reuse, plans))

    def _compile_or_reuse(self, plan: UnitPlan) -> UnitResult:
        slot = self.store.object_slot(plan.cache_key)
        cached_obj = slot / "object.o"
        # meta.json 最后写入，两者齐备才代表一次完整发布；缺失则重编。
        if cached_obj.is_file() and (slot / "meta.json").is_file():
            # 已发布的缓存（含并发成功构建刚发布的同键对象）直接复用。
            self._materialize(cached_obj, plan.work_obj)
            return UnitResult(
                source=plan.source,
                status="reused",
                cache_key=plan.cache_key,
                object_path=str(plan.work_obj),
                included_headers=[h.rel_path for h in plan.headers],
            )

        proc = subprocess.run(plan.command, capture_output=True, text=True)
        if proc.returncode != 0:
            # 失败绝不写缓存槽：其他构建与后续重试仍可使用旧缓存。
            return UnitResult(
                source=plan.source,
                status="failed",
                cache_key=plan.cache_key,
                error=(proc.stderr or proc.stdout or "gcc failed").strip(),
                included_headers=[h.rel_path for h in plan.headers],
            )

        return UnitResult(
            source=plan.source,
            status="compiled",
            cache_key=plan.cache_key,
            object_path=str(plan.work_obj),
            included_headers=[h.rel_path for h in plan.headers],
            publish_meta=plan.meta,
        )

    def _publish_new_objects(self, units: list[UnitResult]) -> list[str]:
        """链接成功后发布本次真正编译出的对象；返回发布失败的告警。"""
        warnings: list[str] = []
        for u in units:
            if u.status != "compiled" or u.publish_meta is None or u.object_path is None:
                continue
            try:
                self._publish_object(
                    self.store.object_slot(u.cache_key),
                    Path(u.object_path),
                    u.publish_meta,
                )
            except Exception as exc:  # noqa: BLE001
                # 链接与产物均已完成；发布失败只影响后续复用，不应把一次
                # 成功的构建改判为失败，故只记录告警。
                warnings.append(f"publish {u.source} -> {u.cache_key}: {exc!r}")
        return warnings

    def _cache_key(
        self,
        src_rel: str,
        source_hash: str,
        headers: list[IncludedFile],
        spec: BuildRequestSpec,
    ) -> str:
        payload = {
            "v": CACHE_FORMAT_VERSION,
            "compiler": self._compiler_signature,
            "source_path": src_rel,
            "source_sha256": source_hash,
            # 头文件按“解析后的工程路径 + 内容哈希”入键：
            # include 目录顺序变化、同名头抢先命中都会改变该列表。
            "include_dirs": list(spec.include_dirs),
            "cflags": list(spec.cflags),
            "headers": [[h.rel_path, h.sha256] for h in headers],
        }
        blob = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()

    def _compile_command(
        self,
        src_abs: Path,
        out_obj: Path,
        src_dir: Path,
        spec: BuildRequestSpec,
    ) -> list[str]:
        cmd = [self.compiler, "-c", str(src_abs), "-o", str(out_obj)]
        for inc in spec.include_dirs:
            cmd += ["-I", str(src_dir / inc)]
        cmd += list(spec.cflags)
        return cmd

    # ------------------------------------------------------------------ #
    # 缓存发布/取用（原子替换，失败不触碰旧槽）
    # ------------------------------------------------------------------ #
    def _publish_object(
        self, slot: Path, work_obj: Path, meta: dict
    ) -> None:
        slot.mkdir(parents=True, exist_ok=True)
        # 临时文件名带进程内唯一后缀：发布阶段虽持键锁，仍避免任何同槽
        # 临时名撞车；先写对象、后写 meta，meta 存在即代表对象完整。
        tmp_obj = slot / f"object.o.tmp.{uuid.uuid4().hex}"
        try:
            shutil.copy2(work_obj, tmp_obj)
            os.replace(tmp_obj, slot / "object.o")
        finally:
            if tmp_obj.is_file():
                tmp_obj.unlink(missing_ok=True)
        self.store.write_json_atomic(slot / "meta.json", meta)

    @staticmethod
    def _materialize(cached_obj: Path, work_obj: Path) -> None:
        work_obj.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(cached_obj, work_obj)  # 硬链接，零拷贝
        except OSError:
            shutil.copy2(cached_obj, work_obj)

    # ------------------------------------------------------------------ #
    # 链接
    # ------------------------------------------------------------------ #
    def _link(self, build_id: str, units: list[UnitResult]) -> None:
        objects = [Path(u.object_path) for u in units]
        program = self.store.build_program_path(build_id)
        cmd = [self.compiler, *[str(o) for o in objects], "-o", str(program)]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise BuildError(
                f"link failed: {(proc.stderr or proc.stdout).strip()}"
            )

    # ------------------------------------------------------------------ #
    # 记录与文件级状态
    # ------------------------------------------------------------------ #
    def _file_statuses(
        self, src_dir: Path, sources: list[str], units: list[UnitResult]
    ) -> list[dict]:
        by_source = {u.source: u for u in units}
        # 头文件 -> 引用它的编译单元状态；任一引用方重编则标记 affected。
        header_affected: dict[str, bool] = {}
        for u in units:
            for h in u.included_headers:
                header_affected[h] = header_affected.get(h, False) or (
                    u.status == "compiled"
                )

        statuses: list[dict] = []
        for path in sorted(p.relative_to(src_dir).as_posix()
                           for p in src_dir.rglob("*") if p.is_file()):
            if self._is_compilable(path):
                u = by_source.get(path)
                status = u.status if u else "unknown"
            else:
                status = "recompiled" if header_affected.get(path) else "reused"
            statuses.append(
                {
                    "path": path,
                    "kind": "source" if self._is_compilable(path) else "header",
                    "status": status,
                }
            )
        return statuses

    @staticmethod
    def _unit_to_dict(u: UnitResult) -> dict:
        return {
            "source": u.source,
            "status": u.status,
            "cache_key": u.cache_key,
            "included_headers": u.included_headers,
            "error": u.error,
        }

    def _persist(self, record: dict) -> None:
        self.store.write_json_atomic(
            self.store.build_record_path(record["build_id"]), record
        )
