#!/usr/bin/env python3
"""端到端自测：增量编译、换头抢先、失败缓存存活、并发与快照隔离。

用法：
  python3 tests/selftest.py --spawn                 # 临时 DATA_DIR 自起 uvicorn
  python3 tests/selftest.py --base-url http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name} {detail}")


def wait_health(base: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            r = httpx.get(f"{base}/health", timeout=2)
            if r.status_code == 200:
                print(f"server healthy: {r.json()}")
                return
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(0.3)
    raise RuntimeError(f"server did not become healthy: {last}")


def spawn_server() -> tuple[str, tempfile.TemporaryDirectory, subprocess.Popen]:
    data_dir = tempfile.mkdtemp(prefix="cibuild-data-")
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env["DATA_DIR"] = data_dir
    env["BUILD_WORKERS"] = "4"
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=REPO_ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        wait_health(base)
    except Exception:
        proc.terminate()
        print(proc.stdout.read() if proc.stdout else "")
        raise
    return base, tempfile.TemporaryDirectory(dir=data_dir), proc


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def submit(base: str, payload: dict) -> dict:
    r = httpx.post(f"{base}/api/builds", json=payload, timeout=30)
    r.raise_for_status()
    return r.json()


def wait_build(base: str, build_id: str, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        rec = httpx.get(f"{base}/api/builds/{build_id}", timeout=10).json()
        if rec["status"] != "running":
            return rec
        time.sleep(0.2)
    raise TimeoutError(build_id)


def unit(rec: dict, source: str) -> dict:
    return next(u for u in rec["units"] if u["source"] == source)


def file_status(rec: dict, path: str) -> str:
    return next(f["status"] for f in rec["files"] if f["path"] == path)


def download(base: str, rec: dict, dest: Path) -> Path:
    r = httpx.get(f"{base}/api/builds/{rec['build_id']}/download", timeout=30)
    r.raise_for_status()
    dest.write_bytes(r.content)
    dest.chmod(0o755)
    return dest


def run_prog(path: Path) -> str:
    return subprocess.run([str(path)], capture_output=True, text=True, timeout=10).stdout


# --------------------------------------------------------------------- #
# 样例工程
# --------------------------------------------------------------------- #
def sample_project() -> dict:
    return {
        "files": [
            {"path": "include/util.h",
             "content": "#ifndef UTIL_H\n#define UTIL_H\nint add(int a, int b);\n#endif\n"},
            {"path": "src/util.c",
             "content": '#include "util.h"\nint add(int a, int b) { return a + b; }\n'},
            {"path": "src/main.c",
             "content": '#include <stdio.h>\n#include "util.h"\n'
                        "int main(void){printf(\"result=%d\\n\", add(2, 40));return 0;}\n"},
        ],
        "include_dirs": ["include"],
        "cflags": ["-Wall", "-Wextra", "-O2"],
        "program_name": "demo",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url")
    ap.add_argument("--spawn", action="store_true")
    args = ap.parse_args()

    proc = None
    tmp = None
    if args.spawn:
        base, tmp, proc = spawn_server()
    else:
        if not args.base_url:
            print("give --spawn or --base-url", file=sys.stderr)
            return 2
        base = args.base_url.rstrip("/")
        wait_health(base)

    work = Path(tempfile.mkdtemp(prefix="cibuild-test-"))
    try:
        scenario_sample_build(base, work)
        scenario_partial_rebuild(base, work)
        scenario_indirect_header(base, work)
        scenario_header_shadow(base, work)
        scenario_failure_keeps_cache(base, work)
        scenario_concurrency_dedup(base, work)
        scenario_snapshot_isolation(base, work)
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(work, ignore_errors=True)

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


# --------------------------------------------------------------------- #
# 场景 1：样例首编 + 下载运行 + 全量复用
# --------------------------------------------------------------------- #
def scenario_sample_build(base: str, work: Path) -> None:
    print("\n[1] 样例首编 / 下载 / 全量复用")
    proj = sample_project()
    rec1 = wait_build(base, submit(base, proj)["build_id"])
    check("首编成功", rec1["status"] == "succeeded", rec1.get("error"))
    check("两个源文件均 compiled",
          all(u["status"] == "compiled" for u in rec1["units"]))
    check("文件级状态可见",
          file_status(rec1, "src/main.c") == "compiled"
          and file_status(rec1, "include/util.h") == "recompiled")

    prog = download(base, rec1, work / "demo1")
    check("下载产物可执行且输出 result=42", run_prog(prog).strip() == "result=42",
          run_prog(prog))

    rec2 = wait_build(base, submit(base, proj)["build_id"])
    check("原样重提全部 reused",
          all(u["status"] == "reused" for u in rec2["units"]))
    check("缓存键稳定",
          unit(rec1, "src/main.c")["cache_key"]
          == unit(rec2, "src/main.c")["cache_key"])
    check("复用构建同样可下载",
          run_prog(download(base, rec2, work / "demo2")).strip() == "result=42")


# --------------------------------------------------------------------- #
# 场景 2：只改一个源文件，未受影响对象复用
# --------------------------------------------------------------------- #
def scenario_partial_rebuild(base: str, work: Path) -> None:
    print("\n[2] 单源变更的部分重编")
    proj = sample_project()
    proj["files"][2]["content"] = (
        '#include <stdio.h>\n#include "util.h"\n'
        "int main(void){printf(\"v2=%d\\n\", add(1, 1));return 0;}\n"
    )
    rec = wait_build(base, submit(base, proj)["build_id"])
    check("构建成功", rec["status"] == "succeeded", rec.get("error"))
    check("main.c 重编", unit(rec, "src/main.c")["status"] == "compiled")
    check("util.c 复用", unit(rec, "src/util.c")["status"] == "reused")
    check("产物反映新源码",
          run_prog(download(base, rec, work / "demo3")).strip() == "v2=2")


# --------------------------------------------------------------------- #
# 场景 3：间接头文件变更也要重编
# --------------------------------------------------------------------- #
def scenario_indirect_header(base: str, work: Path) -> None:
    print("\n[3] 间接引用头文件变更触发重编")
    payload = {
        "files": [
            {"path": "inc/a.h",
             "content": '#include "b.h"\n#define AVAL (BVAL + 1)\n'},
            {"path": "inc/b.h",
             "content": "#define BVAL 1\n"},
            {"path": "src/app.c",
             "content": '#include <stdio.h>\n#include "a.h"\n'
                        "int main(void){printf(\"%d\\n\", AVAL);return 0;}\n"},
        ],
        "include_dirs": ["inc"],
        "program_name": "indirect",
    }
    rec1 = wait_build(base, submit(base, payload)["build_id"])
    check("首编成功", rec1["status"] == "succeeded", rec1.get("error"))
    check("闭包同时包含 a.h 与 b.h",
          set(unit(rec1, "src/app.c")["included_headers"]) == {"inc/a.h", "inc/b.h"})
    check("输出 2", run_prog(download(base, rec1, work / "ind1")).strip() == "2")

    payload["files"][1]["content"] = "#define BVAL 40\n"  # 只改 b.h
    rec2 = wait_build(base, submit(base, payload)["build_id"])
    check("b.h 变更后 app.c 重编",
          unit(rec2, "src/app.c")["status"] == "compiled")
    check("缓存键随间接头变化",
          unit(rec1, "src/app.c")["cache_key"]
          != unit(rec2, "src/app.c")["cache_key"])
    check("输出 41", run_prog(download(base, rec2, work / "ind2")).strip() == "41")


# --------------------------------------------------------------------- #
# 场景 4：新增同名头抢先被选中，旧头未改也要重编
# --------------------------------------------------------------------- #
def scenario_header_shadow(base: str, work: Path) -> None:
    print("\n[4] 同名头抢先（include 目录顺序）")
    base_payload = {
        "files": [
            {"path": "inc_a/greet.h", 'content': '#define GREETING "A"\n'},
            {"path": "inc_b/greet.h", 'content': '#define GREETING "B"\n'},
            {"path": "src/main.c",
             "content": '#include <stdio.h>\n#include "greet.h"\n'
                        "int main(void){printf(\"%s\\n\", GREETING);return 0;}\n"},
        ],
        "include_dirs": ["inc_a", "inc_b"],
        "program_name": "shadow",
    }
    rec1 = wait_build(base, submit(base, base_payload)["build_id"])
    check("按目录顺序选中 inc_a/greet.h",
          unit(rec1, "src/main.c")["included_headers"] == ["inc_a/greet.h"])
    check("输出 A", run_prog(download(base, rec1, work / "sh1")).strip() == "A")
    old_key = unit(rec1, "src/main.c")["cache_key"]

    # 新增一个更靠前目录里的同名头；旧头与源码均不动。
    shadowed = json_clone(base_payload)
    shadowed["include_dirs"] = ["inc_first", "inc_a", "inc_b"]
    shadowed["files"].insert(
        0, {"path": "inc_first/greet.h", 'content': '#define GREETING "FIRST"\n'}
    )
    rec2 = wait_build(base, submit(base, shadowed)["build_id"])
    check("新头抢先被选中",
          unit(rec2, "src/main.c")["included_headers"] == ["inc_first/greet.h"])
    check("旧头未改但触发重编",
          unit(rec2, "src/main.c")["status"] == "compiled")
    check("缓存键改变", unit(rec2, "src/main.c")["cache_key"] != old_key)
    check("输出 FIRST", run_prog(download(base, rec2, work / "sh2")).strip() == "FIRST")

    # 撤掉抢先头、恢复目录顺序：应重新命中最初的缓存对象。
    rec3 = wait_build(base, submit(base, base_payload)["build_id"])
    check("恢复后复用最初缓存",
          unit(rec3, "src/main.c")["status"] == "reused"
          and unit(rec3, "src/main.c")["cache_key"] == old_key)
    check("输出仍为 A", run_prog(download(base, rec3, work / "sh3")).strip() == "A")


def json_clone(obj):
    import json
    return json.loads(json.dumps(obj))


# --------------------------------------------------------------------- #
# 场景 5：编译失败不破坏旧缓存，原缓存仍可复用
# --------------------------------------------------------------------- #
def scenario_failure_keeps_cache(base: str, work: Path) -> None:
    print("\n[5] 失败构建后原有缓存仍可用")
    proj = sample_project()
    good = wait_build(base, submit(base, proj)["build_id"])
    good_keys = {u["source"]: u["cache_key"] for u in good["units"]}

    cache_before = {e["cache_key"] for e in httpx.get(f"{base}/api/cache").json()}

    broken = json_clone(proj)
    broken["files"][2]["content"] = "int main(void){ this is not c }\n"
    bad = wait_build(base, submit(base, broken)["build_id"])
    check("失败状态返回", bad["status"] == "failed")
    check("失败单元带 gcc 错误信息",
          unit(bad, "src/main.c")["status"] == "failed"
          and bool(unit(bad, "src/main.c")["error"]))
    check("失败构建无下载链接", bad["download_path"] is None)

    cache_after = {e["cache_key"] for e in httpx.get(f"{base}/api/cache").json()}
    check("缓存槽未被失败写入", cache_after == cache_before)

    again = wait_build(base, submit(base, proj)["build_id"])
    check("回退好源码后全部复用旧缓存",
          all(u["status"] == "reused" for u in again["units"])
          and {u["source"]: u["cache_key"] for u in again["units"]} == good_keys)


# --------------------------------------------------------------------- #
# 场景 6：并发相同构建只编译一次，且全部成功
# --------------------------------------------------------------------- #
def scenario_concurrency_dedup(base: str, work: Path) -> None:
    print("\n[6] 并发构建去重（同一缓存键全局只编译一次）")
    # 用独有路径保证冷缓存，不被前序场景预热的缓存干扰。
    proj = {
        "files": [
            {"path": "cc_dedup/util.h",
             "content": "#ifndef CCU_H\n#define CCU_H\nint dbl(int x);\n#endif\n"},
            {"path": "cc_dedup/util.c",
             "content": '#include "util.h"\nint dbl(int x){return x * 2;}\n'},
            {"path": "cc_dedup/main.c",
             "content": '#include <stdio.h>\n#include "util.h"\n'
                        "int main(void){printf(\"d=%d\\n\", dbl(21));return 0;}\n"},
        ],
        "include_dirs": ["cc_dedup"],
        "program_name": "cc",
    }
    n = 8
    ids = [submit(base, proj)["build_id"] for _ in range(n)]
    recs = [wait_build(base, bid) for bid in ids]
    check("8 个并发构建全部成功", all(r["status"] == "succeeded" for r in recs))

    compiled = sum(1 for r in recs for u in r["units"] if u["status"] == "compiled")
    reused = sum(1 for r in recs for u in r["units"] if u["status"] == "reused")
    check("仅 2 个对象真正编译，其余 14 个复用", compiled == 2 and reused == 2 * (n - 1),
          f"compiled={compiled} reused={reused}")

    keys = {(u["source"], u["cache_key"]) for r in recs for u in r["units"]}
    check("同源缓存键全局一致", len(keys) == 2)

    cache = httpx.get(f"{base}/api/cache").json()
    check("缓存中每个编译单元仅一份",
          len([e for e in cache if e["source"].startswith("cc_dedup/")]) == 2)

    for i, r in enumerate(recs):
        out = run_prog(download(base, r, work / f"cc{i}"))
        if out.strip() != "d=42":
            check(f"并发产物 {i} 可运行", False, out)
            break
    else:
        check("8 个并发产物均可运行且输出正确", True)


# --------------------------------------------------------------------- #
# 场景 7：并发不同提交各用各的快照，产物互不串
# --------------------------------------------------------------------- #
def scenario_snapshot_isolation(base: str, work: Path) -> None:
    print("\n[7] 并发不同提交的快照隔离")
    def proj(value: int) -> dict:
        return {
            "files": [
                {"path": "src/iso.c",
                 "content": '#include <stdio.h>\n'
                            f"int main(void){{printf(\"iso={value}\\n\");return 0;}}\n"},
            ],
            "program_name": f"iso{value}",
        }

    values = [11, 22, 33, 44, 55]
    ids = [submit(base, proj(v))["build_id"] for v in values]
    recs = [wait_build(base, bid) for bid in ids]
    check("5 个差异化并发构建全部成功",
          all(r["status"] == "succeeded" for r in recs))
    ok = True
    for v, r in zip(values, recs):
        out = run_prog(download(base, r, work / f"iso{v}")).strip()
        if out != f"iso={v}":
            ok = False
            print(f"    期望 iso={v}，实际 {out}")
    check("各产物与提交源码一一对应", ok)


if __name__ == "__main__":
    sys.exit(main())
