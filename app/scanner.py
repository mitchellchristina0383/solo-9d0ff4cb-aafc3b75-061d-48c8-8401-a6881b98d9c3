"""轻量 C/C++ 预处理器：只做依赖发现，不做宏展开。

按 GCC 的目录搜索规则解析 ``#include``：

* ``#include "x.h"``：先找当前文件所在目录，再按 ``-I`` 顺序找；
* ``#include <x.h>``：只按 ``-I`` 顺序找（工程内未命中即视为系统头，跳过）。

块注释（含跨行）内的伪指令会被忽略；行注释与字符串/字符字面量中的
``#include`` 因不满足“行首指令”语法天然不匹配。条件编译分支保守地全部
扫描，宏拼接产生的 include 不在支持范围内。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

_INCLUDE_RE = re.compile(
    r'^\s*#\s*include\s*(?P<kind>[<"])(?P<header>[^>"]+)[>"]'
)


@dataclass(frozen=True)
class IncludedFile:
    rel_path: str  # 工程内相对路径（POSIX 风格）
    sha256: str


def parse_includes(text: str) -> list[tuple[str, str]]:
    """返回 ``(kind, header)`` 列表，kind 为 ``'"'`` 或 ``'<'``。

    采用两段式：先扫描出块注释覆盖的行区间（块注释内的伪指令全部忽略），
    再在其余**原始行**上用行首正则匹配 include。行注释与字符串字面量中的
    ``#include`` 不可能出现在行首，因此天然不会误匹配；引号/尖括号与文件名
    也无需经过字符替换，匹配结果与原文本逐字一致。
    """
    lines = text.splitlines()
    in_block = False
    in_block_lines: set[int] = set()
    for idx, line in enumerate(lines):
        i = 0
        n = len(line)
        line_inside = in_block
        while i < n:
            if in_block:
                if line[i] == "*" and i + 1 < n and line[i + 1] == "/":
                    in_block = False
                    i += 2
                else:
                    i += 1
                continue
            if line[i] == "/" and i + 1 < n and line[i + 1] == "*":
                in_block = True
                line_inside = True
                i += 2
                continue
            # 行注释：本行剩余内容无需再看
            if line[i] == "/" and i + 1 < n and line[i + 1] == "/":
                break
            i += 1
        if line_inside:
            in_block_lines.add(idx)

    results: list[tuple[str, str]] = []
    for idx, line in enumerate(lines):
        if idx in in_block_lines:
            continue
        match = _INCLUDE_RE.match(line)
        if match:
            results.append((match.group("kind"), match.group("header").strip()))
    return results


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


class IncludeResolver:
    """在一次提交的源码快照内解析 include 闭包。"""

    def __init__(self, src_root: Path, include_dirs: list[str]) -> None:
        self.src_root = src_root
        # include 目录的顺序必须保留（GCC -I 语义）；空目录项视为根目录。
        self.include_dirs = [d if d not in ("", ".") else "." for d in include_dirs]

    def _resolve(
        self, header: str, kind: str, current_file_dir: str
    ) -> tuple[str, Path] | None:
        """按 GCC 搜索顺序返回 (工程内相对路径, 绝对路径)，找不到返回 None。"""
        candidates: list[str] = []
        if kind == '"':
            candidates.append(posix_join(current_file_dir, header))
        candidates.extend(posix_join(d, header) for d in self.include_dirs)

        seen: set[str] = set()
        for rel in candidates:
            if rel in seen:
                continue
            seen.add(rel)
            abs_path = (self.src_root / rel).resolve()
            # 防越界：解析结果必须仍在快照目录内。
            if self.src_root not in abs_path.parents and abs_path != self.src_root:
                continue
            if abs_path.is_file():
                return rel, abs_path
        return None

    def closure(self, entry_rel: str) -> list[IncludedFile]:
        """从某个源文件出发，收集直接/间接引用到的工程内头文件。"""
        found: dict[str, IncludedFile] = {}
        stack: list[str] = [entry_rel]
        visited: set[str] = set()

        while stack:
            current_rel = stack.pop()
            if current_rel in visited:
                continue
            visited.add(current_rel)
            current_abs = self.src_root / current_rel
            if not current_abs.is_file():
                continue
            current_dir = posix_dirname(current_rel)
            text = current_abs.read_text(encoding="utf-8", errors="replace")
            for kind, header in parse_includes(text):
                resolved = self._resolve(header, kind, current_dir)
                if resolved is None:
                    # 工程内不存在：交给 GCC 按系统头处理（找不到会编译报错）。
                    continue
                rel, abs_path = resolved
                if rel not in found:
                    found[rel] = IncludedFile(rel, sha256_file(abs_path))
                if rel not in visited:
                    stack.append(rel)
        return [found[k] for k in sorted(found)]


def posix_dirname(path: str) -> str:
    if "/" not in path:
        return ""
    return path.rsplit("/", 1)[0]


def posix_join(base: str, leaf: str) -> str:
    if not base:
        return leaf
    return f"{base}/{leaf}"
