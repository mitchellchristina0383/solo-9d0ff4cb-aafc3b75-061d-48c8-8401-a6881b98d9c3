"""磁盘布局、路径安全、原子写与跨线程锁。

目录结构（DATA_DIR，默认 ./data）::

    data/
      builds/<build_id>/
        src/<project-relative paths>   # 本次提交的源码快照，构建期间固定不变
        work/<source>.o                 # 链接所用对象（命中时为缓存对象的硬链接/拷贝）
        program                        # 最终可执行文件
        record.json                    # 构建记录（原子写）
      objects/<cache_key>/             # 每个编译单元的缓存槽
        object.o
        meta.json                      # 编译器、命令、参与哈希的文件闭包
"""

from __future__ import annotations

import json
import os
import posixpath
import threading
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any


def safe_rel_path(raw: str) -> str:
    """校验用户给出的工程内相对路径，拒绝绝对路径与 ``..`` 越界。"""
    if not isinstance(raw, str) or not raw:
        raise ValueError("path must be a non-empty string")
    if raw.startswith("/") or raw.startswith("\\"):
        raise ValueError(f"absolute path is not allowed: {raw}")
    p = raw.replace("\\", "/")
    # posixpath.normpath 会把 ".." 暴露出来；逐一检查分量更严格。
    parts = [seg for seg in p.split("/") if seg not in ("", ".")]
    if any(seg == ".." for seg in parts):
        raise ValueError(f"path traversal is not allowed: {raw}")
    if not parts:
        raise ValueError(f"invalid path: {raw}")
    normalized = posixpath.join(*parts)
    if normalized in (".", ".."):
        raise ValueError(f"invalid path: {raw}")
    return normalized


class Store:
    """封装 DATA_DIR 下的路径约定。"""

    def __init__(self, data_dir: str | os.PathLike[str]) -> None:
        self.root = Path(data_dir).resolve()
        self.builds_dir = self.root / "builds"
        self.objects_dir = self.root / "objects"
        self.root.mkdir(parents=True, exist_ok=True)
        self.builds_dir.mkdir(parents=True, exist_ok=True)
        self.objects_dir.mkdir(parents=True, exist_ok=True)

    def build_dir(self, build_id: str) -> Path:
        return self.builds_dir / build_id

    def build_src_dir(self, build_id: str) -> Path:
        return self.build_dir(build_id) / "src"

    def build_work_dir(self, build_id: str) -> Path:
        return self.build_dir(build_id) / "work"

    def build_record_path(self, build_id: str) -> Path:
        return self.build_dir(build_id) / "record.json"

    def build_program_path(self, build_id: str) -> Path:
        return self.build_dir(build_id) / "program"

    def object_slot(self, cache_key: str) -> Path:
        return self.objects_dir / cache_key

    def write_json_atomic(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    def read_json(self, path: Path) -> dict[str, Any]:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)


class KeyedLocks:
    """按缓存键分锁：不同对象并行编译，同一对象全局串行。

    ``acquire_many`` 按缓存键的字典序依次加锁，所有构建都遵循同一全局
    顺序，因此不会出现“A 持 k1 等 k2、B 持 k2 等 k1”的死锁；互不相交的
    键集合之间互不阻塞。
    """

    def __init__(self) -> None:
        self._master = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def get(self, key: str) -> threading.Lock:
        with self._master:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock

    @contextmanager
    def acquire_many(self, keys: list[str] | set[str]) -> Iterator[None]:
        """一次性持有整组缓存键锁，直到 with 块结束。"""
        with ExitStack() as stack:
            for key in sorted(set(keys)):
                stack.enter_context(self.get(key))
            yield
