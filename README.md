# CI 增量编译 API

接收小型 C 工程的整份源码，由 GCC 按样例的目录顺序解析 `#include`，
以**编译单元 + 内容哈希**做对象级增量编译的 FastAPI 服务。

## 能力一览

- 整份源码经 HTTP 提交，每次构建在独立目录中落一份**不可变快照**；
- 按 GCC 语义解析头文件：`"x.h"` 先查当前文件目录，再按 `include_dirs`
  （即 `-I`）顺序查；`<x.h>` 只走 `-I`，工程内未命中视为系统头；
- 源文件或其**直接/间接**引用的任一工程内头文件内容变化 → 该单元重编，
  其他单元的对象文件照常复用；
- **同名头抢先**：新增更靠前目录中的同名头，即使旧头与源码都没改，解析到的
  头路径变化也会改变缓存键并触发重编；撤掉抢先头后重新命中原缓存；
- 并发构建各用各提交时的快照；同一缓存键全局互斥，N 个相同并发构建只有一个
  真正调用 GCC，其余复用；
- 编译/链接失败**不写缓存槽**，原有缓存继续可用，重试好源码仍全部复用；
- 成功后可下载可执行文件，并能在构建记录里查看每个源文件 `compiled` /
  `reused`（以及头文件 `recompiled` / `reused`）。

## 目录结构

```
app/
  main.py       FastAPI 路由（提交/查询/下载/缓存/样例）
  models.py     Pydantic 模型（/docs 示例）
  builder.py    增量构建核心：快照、缓存键、键级互斥、GCC 调用、链接
  scanner.py    #include 扫描与 GCC 目录顺序解析（含注释/字符串剥离）
  storage.py    磁盘布局、路径安全校验、原子写、键级锁
  sample.py     内置样例工程
tests/
  selftest.py   7 个场景、37 项断言的端到端自测
Dockerfile      gcc:14 + python3 + uvicorn
docker-compose.yml
```

数据布局（`DATA_DIR`，容器内为命名卷 `/data`）：

```
data/
  builds/<build_id>/{src,work,program,record.json}
  objects/<cache_key>/{object.o,meta.json}
```

## 缓存键设计

每个编译单元的缓存键是对以下内容的 SHA-256：

| 组成 | 作用 |
| --- | --- |
| 编译器签名（`gcc --version`） | 编译器变化全部失效 |
| 源文件工程路径与内容哈希 | 源文件改动重编 |
| 解析后的头文件闭包 `[(工程路径, 内容哈希)]` | 直接/间接头改动重编；同名头抢先改路径也重编 |
| `include_dirs` 顺序、`cflags` | 搜索顺序与编译参数变化重编 |
| 缓存格式版本号 | 算法升级隔离旧缓存 |

命中时把缓存对象硬链接（失败则拷贝）到构建私有 `work/` 目录再链接，
构建之间互不写对方目录。缓存发布用临时文件 + `os.replace` 原子替换，
失败路径完全不触碰缓存槽。

## API

| 方法/路径 | 说明 |
| --- | --- |
| `POST /api/builds` | 提交构建，202 返回带 `build_id` 的 running 记录 |
| `GET /api/builds/{id}` | 查询状态与逐文件 `compiled`/`reused` 明细 |
| `GET /api/builds/{id}/download` | 下载成功构建的可执行文件（失败返回 409） |
| `GET /api/builds` | 构建列表 |
| `GET /api/cache` | 当前对象缓存条目 |
| `GET /api/sample` | 可直接 POST 的样例工程 |
| `GET /docs` | Swagger UI，可直接试编样例 |
| `GET /health` | 健康检查（含编译器签名） |

请求体示例：

```json
{
  "files": [
    {"path": "include/util.h", "content": "..."},
    {"path": "src/util.c", "content": "..."},
    {"path": "src/main.c", "content": "..."}
  ],
  "include_dirs": ["include"],
  "cflags": ["-Wall", "-Wextra", "-O2"],
  "program_name": "demo"
}
```

路径必须是工程内相对路径；绝对路径、`..` 越界、重复文件在提交时直接 422。

## 用 Docker Compose 启动

```bash
docker compose up --build
# 打开 http://localhost:8000/docs
# 1) GET /api/sample 复制响应；2) POST /api/builds 粘贴执行；
# 3) 用返回的 build_id 调 GET /api/builds/{id} 与 .../download
```

缓存持久化在 compose 命名卷 `ci-cache` 中，`docker compose down` 后仍保留；
`docker compose down -v` 清空。

## 本地（无 Docker）运行与自测

```bash
python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000
python3 tests/selftest.py --spawn          # 自起临时服务跑全部场景
# 或对已运行的服务：
python3 tests/selftest.py --base-url http://127.0.0.1:8000
```

## 实测结果

环境：Debian 12 / GCC 12.2.0 / Python 3.11 / FastAPI 0.165 / uvicorn 0.54。
`python3 tests/selftest.py --spawn` 全量结果 **37 passed, 0 failed**，覆盖：

1. **样例首编/下载/复用**：首编两个单元均 `compiled`，产物运行输出
   `result=42`；原样重提全部 `reused` 且缓存键不变；
2. **部分重编**：只改 `main.c`，`main.c` 重编、`util.c` 复用，链接产物反映
   新源码（`v2=2`）；
3. **间接头变更**：`app.c → a.h → b.h`，只改 `b.h` 即触发 `app.c` 重编、
   缓存键变化，输出由 `2` 变 `41`；
4. **同名头抢先**：`-I inc_first inc_a inc_b` 下新增 `inc_first/greet.h`，
   旧头未改仍重编且解析路径变为新头，输出 `A → FIRST`；恢复目录后重新复用
   最初缓存键的对象，输出回到 `A`；
5. **失败缓存存活**：提交语法错误的源码 → 构建 failed、带 GCC stderr、无下载
   链接，`/api/cache` 槽位零变化；回退好源码后全部 `reused`；
6. **并发去重**：8 个相同构建并发提交，仅 2 个对象真正 `compiled`、其余 14 个
   `reused`，8 个产物均可运行；
7. **快照隔离**：5 份不同源码并发构建，产物与各自提交一一对应、互不串。

边界行为手工验证：路径穿越/绝对路径/重复文件/非法 include 目录均在提交时
返回 422；未知构建 404；失败构建下载 409；空 `include_dirs` 下引号 include
按当前目录语义正常编译。

> 注：本开发环境未安装 Docker 守护进程，`Dockerfile` 与
> `docker-compose.yml` 已通过 YAML/指令核对，未在本机实际构建镜像；
> 应用本身以本地 uvicorn + 系统 GCC 完成了上述全部实测。

## 余项与已知限制

- 扫描器不做宏展开，`#include` 由宏拼接产生的情况不支持（条件分支保守全扫）；
- 源码以 UTF-8 文本接收，未支持二进制文件/预编译头；
- 对象缓存无 TTL 与容量淘汰，长期运行需外部清理任务（`data/objects` 可安全
  整目录删除，下次构建自动重建）；
- 单元内并发受 `BUILD_WORKERS` 限制；构建记录为 JSON 文件，未做数据库化与
  分页；
- 未做鉴权与配额，定位为受信网络内的内部 CI 组件；
- 链接固定使用驱动编译器（`gcc *.o -o program`），暂不支持自定义链接参数/
  静态库归档。
