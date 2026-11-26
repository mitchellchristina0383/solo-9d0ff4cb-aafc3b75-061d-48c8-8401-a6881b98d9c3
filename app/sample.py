"""内置样例工程，供 /docs 一键试编。

结构::

    include/util.h   —— 加法工具声明
    src/util.c       —— 实现
    src/main.c       —— 入口，打印 add(2, 40)
"""

from __future__ import annotations

from .models import BuildRequest, FilePayload

SAMPLE_FILES = [
    FilePayload(
        path="include/util.h",
        content=(
            "#ifndef UTIL_H\n"
            "#define UTIL_H\n"
            "int add(int a, int b);\n"
            "#endif /* UTIL_H */\n"
        ),
    ),
    FilePayload(
        path="src/util.c",
        content=(
            '#include "util.h"\n'
            "\n"
            "int add(int a, int b) {\n"
            "    return a + b;\n"
            "}\n"
        ),
    ),
    FilePayload(
        path="src/main.c",
        content=(
            "#include <stdio.h>\n"
            '#include "util.h"\n'
            "\n"
            "int main(void) {\n"
            '    printf("result=%d\\n", add(2, 40));\n'
            "    return 0;\n"
            "}\n"
        ),
    ),
]


def sample_request() -> BuildRequest:
    return BuildRequest(
        files=SAMPLE_FILES,
        include_dirs=["include"],
        cflags=["-Wall", "-Wextra", "-O2"],
        program_name="demo",
    )
