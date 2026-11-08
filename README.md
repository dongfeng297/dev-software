# 归档 BackNest：C++ / Python 备份与扩展能力 demo

一个可真实运行的本地备份工作台：目录树备份与还原、单文件打包、自实现压缩、认证加密、定时/实时/增量备份，均通过 `/api/v1` 统一接口执行。

- **基础闭环（P0–P1）**：C++17 引擎负责扫描、复制、清单和 CRC32 校验；Web UI 通过仅监听 `127.0.0.1` 的 Python 通信桥调用。无 C++ 编译器的机器会自动使用兼容的 Python 引擎，仍然执行真实文件操作。
- **P4 打包压缩**：自实现 `BNAR` 单文件容器与分块 LZSS（`BNLZ`）。
- **P5 加密**：AES-256-GCM + PBKDF2-HMAC-SHA256；密码只换短期内存 `secretRef`。
- **P6 自动化**：5 字段 cron、轮询去抖实时监听、硬链接增量、可选版本保留、互斥与不重入。

> 算法约束仍然有效：**打包、压缩必须由项目自行实现；加密允许调用成熟库**。产品路径没有 zlib/zip/tar/gzip/7z 或子进程压缩工具。

## 当前状态

| 范围 | 状态 |
| --- | --- |
| 基础备份/还原/校验 | 已实现；C++17 引擎与 Python 兼容引擎共用 `BACKNEST 1` 清单 |
| 接口契约 | 63 个 operationId，40 个已接通，23 个明确返回 501 |
| P4 打包/压缩 | 已实现并测试：BNAR v1、LZSS v1 |
| P5 加密 | 已实现并测试：AES-256-GCM、PBKDF2、secretRef |
| P6 定时/实时/增量 | 已实现并测试：cron、去抖轮询、硬链接增量、版本保留 |
| 平台 | Windows 实测（Python 引擎 + 扩展能力）；macOS C++ 基础引擎沿用原实现，本轮未重新实机验收 |
| 未实现 | 六维筛选、元数据/特殊文件保真、网络备份与远端用户、Qt 桌面、系统服务式后台 |

详细状态、格式、验收和限制见 [P4/P5/P6 实现说明](docs/FEATURES_P4_P6.md)。

## 启动界面

需要 Python 3.10+。有 C++17 编译器时首次启动自动构建 C++ 引擎；没有编译器时自动回退到 Python 兼容引擎，不需要 Node.js、CMake 或第三方 Python 包。

```sh
python server.py
```

打开 <http://127.0.0.1:4173/>。右上角显示“本机引擎 · C++17”或“本机引擎 · Python”。按 `Ctrl+C` 停止服务；执行文件操作时请等待完成后再退出。

```sh
python server.py --port 4175
python server.py --state-dir .backnest/my-state   # 自定义任务/计划配置目录
```

单独运行 `python -m http.server` 只能查看界面，不能执行备份。

## 使用流程

### 基础备份与还原

1. **新建备份**：填写名称，选择源目录和备份位置；目标可以是尚不存在的新目录。
2. **执行备份**：创建任务本身不复制文件；点击“运行”后进度来自实际文件操作。
3. **查看版本**：每次成功备份生成独立 `snapshot-*` 目录；失败不会新增成功版本。
4. **数据还原**：选择版本，填写新目录或空目录；引擎先校验再写入。
5. **导入备份**：界面记录清空后，可在“数据还原”页校验并导入已有 `snapshot-*`。

### 扩展能力页（P4/P5/P6）

左侧“扩展能力”页提供真实操作：

- **打包**：源目录 → `.bkn` 单文件；**解包**：`.bkn` → 新/空目录。
- **压缩/解压**：任意普通文件；可设置输出上限（API）。
- **加密/解密**：输入密码；前端只调用 `secrets.create` 换取短期 `secretRef`，任务结束自动撤销并清空密码框。
- **增量预览**：显示新增/变更/删除数量与基准版本。
- **定时计划**：填写 5 字段 cron 与时区，创建后端任务和计划，显示下次执行时间。
- **实时监听**：对源目录启动去抖监听，变化稳定后自动执行增量备份；可停止。

界面没有预置任务、假日志或模拟成功版本；所有版本都来自真实操作。

## P4/P5/P6 摘要

### P4：自实现容器与压缩

```text
snapshot 之外的单文件格式：
BNAR v1 = 魔数 + 版本 + 条目索引 + UTF-8 相对路径 + 原始数据 + 结尾标记
BNLZ v1 = 魔数 + 分块头 + LZSS 位流 / 原样存储块 + 每块 CRC32
```

- 目录、空目录、空文件、中文/空格文件名均保留。
- 解包前校验路径穿越、重复条目、父目录、长度、CRC、结尾和输出上限。
- LZSS：4 KB 窗口、3–18 匹配、64 KiB 分块、1 标志字节/8 token；不可压缩块原样存储。
- 算法标识：`bkn-archive-v1`、`lzss-v1`，均 `selfImplemented: true`。

### P5：加密

- PBKDF2-HMAC-SHA256（默认 200,000 次，随机 16 字节盐）派生 256 位密钥。
- AES-256-GCM，随机 12 字节 nonce、16 字节 tag；32 字节头作为 AAD。
- Windows 使用系统 CNG（`bcrypt.dll`）；macOS/Linux 使用可选 `cryptography`，缺失时明确报错。
- 错误密码、篡改、截断都失败且不产生目标文件；明文密码不进入任务、清单、日志或 localStorage。

### P6：自动化与增量

- **增量**：与最新版本比较大小+CRC；未变文件硬链接复用，变更/新增复制；新版本仍有完整清单，可独立还原；删除旧基准不影响新版本。
- **定时**：5 字段 cron（分 时 日 月 周），支持列表/范围/步长；IANA 时区，缺少 tzdata 时支持 UTC/local/固定偏移。
- **行为**：只在 server 进程存活时运行；错过策略 skip/run_once；触发后推进 `nextRunAt`；与手动备份共享互斥锁；忙时记录跳过，不重入。
- **实时**：轮询树签名 + 去抖（默认 500 ms）；自动排除备份目标与显式排除路径，避免“备份引发备份”。
- **版本策略**：默认保留全部；`settings.keepVersions=N` 时只清理本后端登记、同一目标目录下的最旧 `snapshot-*`。

## 命令行

```sh
# 有 C++ 编译器时构建原生引擎
python scripts/build.py

# C++ 基础命令（输出 JSON 行）
build/backnest backup "/绝对路径/源目录" "/绝对路径/备份位置"
build/backnest verify "/绝对路径/备份位置/snapshot-实际版本标识"
build/backnest restore "/绝对路径/备份位置/snapshot-实际版本标识" "/绝对路径/新的还原目录"

# 完整基础样例
python scripts/demo.py

# P4/P5/P6 完整 HTTP 演示（自动创建测试文件并保留产物）
python scripts/demo_extensions.py
```

Windows 上 C++ 构建需要 Visual Studio C++ 工具；没有时直接运行 `python server.py` 即可使用 Python 引擎。

## 数据格式

### 目录型版本（两种引擎共用）

```text
备份位置/
└── snapshot-<唯一标识>/
    ├── manifest.bkn    # BACKNEST 1：源目录、文件大小、CRC32、UTF-8 相对路径
    └── data/           # 原始文件和目录层级，包括空目录
```

- 以 64 KB 缓冲流式复制文件。
- 先写 `.partial-*`，校验后重命名发布；失败清理，不报告成功。
- 备份前后比较源清单、大小和修改时间；检测操作期间的变化。
- 还原前检查清单、文件集合、大小与 CRC32；拒绝越界路径、重复条目、缺失/多余文件。
- 还原只接受新目录或空目录，不覆盖已有文件。
- 每个版本独立；增量版本用硬链接复用未变数据，但仍是完整清单。

### 单文件格式

- `BNAR`：打包容器，见 [实现说明](docs/FEATURES_P4_P6.md#2-p4自实现单文件容器bnar-v1)。
- `BNLZ`：压缩流，见 [实现说明](docs/FEATURES_P4_P6.md#3-p4自实现-lzss-压缩bnlz-v1)。
- `BNE1`：加密文件，见 [实现说明](docs/FEATURES_P4_P6.md#4-p5认证加密bne1-v1)。

## 验证

```sh
python -m unittest discover -s tests -v
python scripts/demo.py
python scripts/demo_extensions.py
```

当前自动检查（Windows，无 C++ 编译器）96 项：基础 API/契约、BNAR、LZSS、BNE1、增量、cron、watcher、secretRef、版本保留和 HTTP 端到端。C++ 引擎测试在有编译器的机器上运行（本环境自动跳过）。加密用 Node.js 生成的独立 AES-GCM 向量做交叉验证（仅测试参考，不进入产品路径）。

## 后端交接与接口

- [后端交接文档](docs/BACKEND_HANDOFF.md)
- [P4/P5/P6 实现说明](docs/FEATURES_P4_P6.md)
- [全量需求核验表](docs/REQUIREMENTS_AUDIT.md)
- [OpenAPI 契约](api/openapi.json)、[前端类型](api/backend-api.d.ts)、[C++ 服务接口](core/backend_interfaces.hpp)

`api_contract.py` 是唯一接口注册表：

```sh
python scripts/generate_api_contract.py
```

当前 63 个接口中 40 个已实现、23 个明确返回 501；扩展能力不会被静默忽略。前端 `backend-api.js` 支持 HTTP 和可替换的 Qt transport；任务/计划/设置/活动/版本索引保存在 `.backnest/backend.json`（git 忽略），浏览器仍保留自己的界面状态。

## 已知限制

- P4/P5 尚未串联进 `backup.start` 的 `archive/compression/encryption` 选项（这些选项仍返回 501）；请使用独立的 `archive.*`、`compression.*`、`encryption.*` 端点或“扩展能力”页。
- 加密当前整文件读入内存；超大文件需要分块 AEAD。
- 实时监听是轮询，不是文件系统通知；超大目录有轮询成本。
- 定时不是系统服务，进程退出后不执行。
- 硬链接增量依赖同一文件系统；不支持时回退复制。
- 不支持符号链接、特殊文件、完整元数据保真、六维筛选、网络备份、远端账户、Qt 桌面和自动删除未登记版本。
- C++ 基础引擎本轮未在 Windows 实机编译；Python 兼容引擎写同一清单格式，C++ 读取器保持兼容。

## 项目文件

| 文件 | 职责 |
| --- | --- |
| `core/main.cpp` | 无第三方依赖的 C++17 备份/还原/校验引擎 |
| `core/backend_interfaces.hpp` | 各扩展模块的 C++ 抽象服务接口 |
| `backnest/engine.py` | 兼容 Python 引擎：完整/增量备份、校验、还原 |
| `backnest/container.py` | 自实现 BNAR 单文件容器 |
| `backnest/compress.py` | 自实现分块 LZSS 压缩/解压 |
| `backnest/crypto.py` | AES-256-GCM + PBKDF2（CNG / cryptography） |
| `backnest/scheduler.py` | cron 解析、下次执行、错过策略与调度线程 |
| `backnest/watcher.py` | 轮询去抖监听与目标目录排除 |
| `backnest/secrets.py` | 短期内存 secretRef |
| `backnest/store.py` | 任务/计划/设置/活动/版本索引 JSON 仓储 |
| `server.py` | 本地 HTTP 桥、鉴权、任务执行、v1 分发 |
| `api_contract.py` / `api/` | 全量版本化端口、DTO 与生成的 OpenAPI |
| `backend-api.js` | 生成的前端统一调用入口与 Qt transport 替换点 |
| `native.js` | UI 与后端端口、目录浏览、任务进度连接 |
| `app.js` / `styles.css` | 任务界面、扩展能力页与视觉样式 |
| `scripts/demo_extensions.py` | P4/P5/P6 真实 HTTP 演示 |
| `tests/` | 基础/扩展/契约/HTTP 自动检查 |
| `docs/FEATURES_P4_P6.md` | 本轮实现、格式、验收与限制 |
| `IMPLEMENTATION_PLAN.md` | 阶段计划与当前状态 |
