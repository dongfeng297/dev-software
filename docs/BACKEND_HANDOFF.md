# 后端开发交接文档

更新日期：2026-11-08。本文面向接手 C++ 引擎、Qt 桌面层或后端服务的开发者。P4/P5/P6 的详细格式与验收见 [实现说明](FEATURES_P4_P6.md)。

## 1. 首先阅读：算法实现约束

**用户明确要求：“压缩解压等算法不要调用库（加密解密可以调用），要自己实现。”**

| 范围 | 实现要求 | 当前落点 |
| --- | --- | --- |
| 打包、解包 | 自行设计/实现容器组织、头部、条目索引、长度编码及读取逻辑 | `backnest/container.py`（BNAR v1） |
| 压缩、解压 | 自行实现编码/解码算法、必要数据结构和格式 | `backnest/compress.py`（LZSS/BNLZ v1） |
| 目录备份/还原、增量比较、校验 | 由项目代码实现流程、规则和数据结构 | `core/main.cpp`、`backnest/engine.py` |
| 加密、解密、密码派生及认证 | **允许调用成熟密码学库** | `backnest/crypto.py`：Windows CNG；macOS/Linux `cryptography` |
| 文件系统、线程、时间、JSON、HTTP、数据库 | 可使用标准库/系统 API/普通基础设施库 | Python 标准库、C++17 标准库 |
| 测试对照 | 可用独立实现核验结果；禁止把对照库引入产品压缩/打包路径 | 测试中 `zlib.crc32`、Node.js AES-GCM、`hashlib` 仅作对照 |

禁止的捷径：用 zlib、libarchive、miniz、现成 ZIP/TAR 库实现压缩/打包；或调用 `zip`、`unzip`、`tar`、`gzip`、`7z` 等系统工具后声称算法已自行实现。当前产品路径没有这些依赖。

## 2. 当前真实交付范围

### 已实现

- C++17 普通文件、子目录、空目录的完整备份、校验与还原；`BACKNEST 1` 清单 + CRC32。
- Python 兼容引擎：同一清单格式；无 C++ 编译器时自动回退，支持完整/增量备份、校验、还原。
- P4：自实现 BNAR 单文件容器；自实现分块 LZSS 压缩/解压；算法清单、打包、解包、列表接口。
- P5：AES-256-GCM + PBKDF2-HMAC-SHA256；短期内存 `secretRef`；错误密码/篡改失败；密码不持久化。
- P6：5 字段 cron 定时、轮询去抖监听、硬链接增量、可选版本保留、互斥与不重入。
- 任务/计划/设置/活动/版本索引 JSON 仓储（`.backnest/backend.json`，git 忽略）；重启保留配置。
- Web UI：基础备份/还原/校验/导入，以及“扩展能力”页。
- `/api/v1` 统一入口、鉴权、能力查询、OpenAPI、前端调用端口、C++ 扩展接口声明。

### 尚未实现

六维筛选、元数据/特殊文件保真、网络备份、远端账户/元数据、系统服务式后台、Qt 桌面、可靠取消、Windows C++ 实机编译、macOS 本轮 P4–P6 复测、P4/P5 串联进单次备份流水线。

目录型 `manifest + data` 与单文件 BNAR 是两种产物；`backup.start` 的 `archive/compression/encryption` 选项仍返回 501，不能声称“一次备份自动打包压缩加密”已完成。

## 3. 需求到接口的索引

完整机器生成表见 [REQUIREMENTS_AUDIT.md](REQUIREMENTS_AUDIT.md)。

当前 **63 个 operationId：40 个已接通、23 个预留**。以 `api_contract.py` 为唯一事实来源，不能手改生成文件后不更新注册表。

| 文件 | 责任 |
| --- | --- |
| `api_contract.py` | operationId、方法/路径、实现状态、DTO、需求映射、能力、请求校验与未实现选项保护 |
| `api/openapi.json` | 生成的 OpenAPI 3.1 契约 |
| `api/backend-api.d.ts` | 生成的 TypeScript DTO 与调用端类型 |
| `backend-api.js` | 生成的统一前端 `BackNestBackend.invoke()` |
| `core/backend_interfaces.hpp` | C++ 备份、打包、压缩、加密、定时、监听、远端、用户等抽象接口 |
| `backnest/` | P4/P5/P6 算法、存储、调度、监听与兼容引擎 |
| `workspace-store.js` | 浏览器持久化默认适配器与后端替换端口 |
| `native.js` | 页面到后端端口、目录浏览、任务进度 |
| `server.py` | 本机 HTTP 适配器、鉴权、任务线程、C++/Python 执行器与 v1 分发 |
| `app.js` / `styles.css` | 任务界面与扩展能力页 |

C++ 抽象接口仍是扩展边界，不返回假成功。当前 P4–P6 在 Python 层落地；后续若迁移进 C++，必须保持 operationId、DTO 和磁盘格式稳定，并保留现有回归测试。

## 4. 调用协议与兼容约定

### 4.1 初始化与鉴权

1. `GET /api/session` 获取短期本机会话令牌，并返回 `engine`（`C++17` 或 `Python`）。
2. 业务请求带 `X-BackNest-Token`，访问 `/api/v1/...`。
3. `GET /api/v1/capabilities` 查询实现状态、算法列表、自动化行为和平台状态。
4. `GET /api/v1/openapi.json` 返回契约。

本地桥只监听 `127.0.0.1`，检查 Host、Origin、`Sec-Fetch-Site`，不允许跨站调用。它是 demo 适配器，不是公网服务。

旧 `/api/backup`、`/api/restore`、`/api/verify`、`/api/jobs/{id}`、`/api/directories` 保留兼容；新增功能不要继续添加无版本路径。

### 4.2 已实现的调用（按组）

| 组 | operationId | 说明 |
| --- | --- | --- |
| 系统 | `system.capabilities`、`system.contract`、`filesystem.directories` | 能力、契约、目录列表 |
| 基础 | `backup.start`、`restore.start`、`snapshots.verify`、`jobs.get` | 真实文件操作与进度；`backup.start` 支持 `mode=full/incremental` |
| 打包 | `archive.algorithms/pack/unpack/list` | BNAR v1 |
| 压缩 | `compression.algorithms/compress/decompress` | LZSS v1 |
| 加密 | `encryption.algorithms/encrypt/decrypt`、`secrets.create/revoke` | AES-256-GCM；短期 secretRef |
| 任务 | `tasks.list/create/update/delete` | 后端任务配置 |
| 定时 | `schedules.list/create/update/delete/nextRuns` | cron、时区、错过策略 |
| 实时 | `watchers.start/get/stop` | 轮询去抖、目标目录排除 |
| 增量 | `incremental.preview` | added/changed/deleted |
| 设置 | `settings.get/update` | 含可选 `keepVersions` |
| 索引 | `snapshots.list/register/delete` | 只删索引，默认不删磁盘 |
| 其他 | `activity.list/export`、`workspace.overview` | 活动与统计 |

其余 23 个（六维筛选、元数据、文件类型、网络/remotes/users/auth、`jobs.cancel` 等）明确返回 501。

```json
{"source": "/absolute/source", "destination": "/absolute/backups",
 "options": {"mode": "incremental"}}
```

```json
{"source": "/absolute/photos", "destination": "/absolute/photos.bkn",
 "options": {"algorithm": "bkn-archive-v1"}}
```

```json
{"source": "/absolute/plain.bin", "destination": "/absolute/secret.bne",
 "options": {"algorithm": "aes-256-gcm"}, "secretRef": "secret-…"}
```

### 4.3 异步结果与错误

- `202` 只表示任务已接收；客户端必须轮询 `jobs.get`。
- 终态 `completed` / `failed`；`cancelled` 枚举保留但当前没有可靠取消。
- 进度字段：`phase / files / totalFiles / bytes / totalBytes / path`。
- 阶段包含 `starting / scanning / copying / verifying / packing / unpacking / compressing / decompressing / encrypting / decrypting / completed`。
- 文件操作失败的 job 查询可返回 HTTP 200，但 `state=failed`，UI 必须按失败处理。
- 参数错误 400；鉴权/来源错误 403；未知资源 404；方法错误 405；预留/不支持 501；并发忙目前 400。
- **禁止默默忽略参数。** `backup.start` 带 `filters/metadata/archive/compression/encryption` 或非默认 `fileTypes` 时仍返回 501；`restore.start` 的非空目标、metadata、secretRef 也返回 501。

### 4.4 前端和 Qt 的统一端口

```js
const api = new BackNestBackend({ token });
const accepted = await api.invoke('backup.start', {
  body: { source, destination, options: { mode: 'incremental' } }
});
const job = await api.invoke('jobs.get', { path: { jobId: accepted.id } });
```

Qt WebChannel 可通过构造参数 `transport(operationId, args)` 替换 HTTP transport。Qt 必须继续按 operationId 显式分发、校验路径/DTO，并返回同样结构；不能让网页传任意命令或 shell 字符串。

### 4.5 任务、设置、日志与版本索引

- 后端：`.backnest/backend.json` 保存 tasks/schedules/settings/snapshots/activity；写入使用临时文件 + `os.replace`。
- 浏览器：仍用 localStorage 保存 UI 任务/日志/偏好；两者尚未自动同步。
- 任务删除和版本索引删除只移除配置/索引，磁盘备份保留。
- `snapshots.register` 会实际校验版本内容后才登记，避免登记伪造路径。

### 4.6 secretRef

- `secrets.create` 只把密码放在进程内存，返回 `secretRef` 和 `expiresAt`（默认 600 秒）。
- `encryption.encrypt/decrypt` 只接受 `secretRef`，不接受明文密码字段。
- `secrets.revoke` 立即销毁；过期值惰性清理。
- 不得把明文密码放进路径、命令行参数、任务记录、manifest、日志、URL 或 localStorage。

## 5. 六类自定义备份（仍为预留）

`FilterSpec` 包含 `paths / types / names / time / size / users`。建议语义保持不变：同维度多选、跨维度交集；先 `filters.validate`，再 `filters.preview`，执行与预览共用实现。当前这些端点均为 501，不能认为规则已生效。

## 6. 算法与数据格式

### 6.1 目录型版本（BACKNEST 1）

```text
snapshot-*/
├── manifest.bkn    # BACKNEST 1：files dirs bytes / quoted source / D/F 条目
└── data/           # 原始文件与目录层级
```

C++ 与 Python 引擎共用该格式。新格式必须保留旧读取器；不要直接改变 v1 解释。

### 6.2 单文件容器（BNAR v1）

魔数 `BNAR`、版本、条目数、总字节数、头 CRC；每条目含类型、mode、mtime、大小、CRC、UTF-8 相对路径和数据；结尾 `BNAREND\0`。解包前校验路径、重复、父目录、长度、CRC、结尾与输出上限。详见 [实现说明](FEATURES_P4_P6.md#2-p4自实现单文件容器bnar-v1)。

### 6.3 压缩（BNLZ v1）

`BNLZ` 头 + 分块（mode/originalLength/payloadLength/CRC + payload）。LZSS 4 KB 窗口、3–18 匹配、64 KiB 分块；不可压缩块原样存储。解码严格校验并受 `maxOutputBytes` 限制。

### 6.4 加密（BNE1 v1）

32 字节头（含 6 字节零填充）作为 AAD；PBKDF2-HMAC-SHA256 + AES-256-GCM；salt 16、nonce 12、tag 16。Windows 用 CNG，macOS/Linux 用 `cryptography`；缺失时明确失败。

### 6.5 增量

与最新版本比较大小+CRC；未变文件硬链接，变更/新增复制；新版本仍写完整清单。硬链接失败回退复制。暂存目录完整校验后才发布。删除旧基准不影响新版本还原。

### 6.6 接入顺序（若未来串联）

扫描 → 筛选 → 条目/元数据 → 自实现打包 → 自实现压缩 → 库加密 → 校验/提交；还原逆序。关闭的可选模块必须明确为关闭，不能隐式选择算法。当前独立端点已实现，但尚未串联进 `backup.start`。

## 7. 定时、实时与网络

- 定时：只在 server 进程存活时执行；`nextRunAt` 持久化，触发后推进；错过策略 skip/run_once；非法表达式停用计划并记录 `lastError`；与手动备份共享互斥锁。
- 实时：轮询树签名 + 去抖；自动排除目标目录与 `excludedPaths`；忙时跳过并稍后重试；服务退出即停止。
- 版本策略：默认保留全部；`keepVersions>0` 只清理本后端登记、同一目标目录下的最旧 `snapshot-*`。
- 网络/remotes/users/auth：仍为预留；不得把本机 demo 桥直接公开到公网。
- 传输、远端元数据、账户权限、续传和加密通道均未实现，不能据接口预留声称完成。

## 8. 平台与已知限制

- Windows 已实测 Python 引擎和 P4–P6；C++ 仅有构建入口，本机无编译器，未实机编译。
- macOS C++ 基础引擎沿用原实现；P4–P6 本轮未在 macOS 复测。
- 当前源目录变化检测不是原子快照或完整 TOCTOU 防护。
- 不支持符号链接、特殊文件、完整元数据保真、六维筛选。
- 还原只接受新/空目录。
- job 在服务进程内存中，重启后无法恢复；没有可靠取消。
- 加密整文件读入内存；超大文件需分块 AEAD。
- 实时监听是轮询，不是文件系统通知；超大目录有成本。
- 定时不是系统服务，进程退出后不执行。
- 硬链接增量依赖同一文件系统；不支持时回退复制。
- 不承诺跨系统完整恢复属主/权限；不自动删除未登记版本。

## 9. 开发与验收

```sh
python server.py
python -m unittest discover -s tests -v
python -m unittest discover -s tests/features -v
python scripts/generate_api_contract.py
python scripts/demo.py
python scripts/demo_extensions.py
```

修改接口时：先改 `api_contract.py` → 生成 OpenAPI/JS/类型/需求表 → 改 handler/算法 → 加真实算法与 HTTP 测试 → 更新能力标记 → 浏览器验证。不要先将 `implemented` 改为 true 以点亮按钮。

每个扩展交付至少包括：实现代码、真实输入输出、错误/损坏输入处理、边界测试、协议/格式说明，以及“算法是否自行实现”的证据。禁止用延时器模拟完成、固定统计、空实现返回成功或只写接口就标记完成。

当前自动检查（Windows，无 C++ 编译器）：96 项，3 项跳过（C++ 构建入口、两个符号链接权限测试）。Node.js AES-GCM 与 `hashlib`/`zlib` 仅用于测试对照，不进入产品路径。

## 10. 接手后的优先顺序

1. 在目标平台完成真实基础往返，确认 C++/Python 引擎选择符合预期。
2. 补齐 `backup.start` 的打包/压缩/加密流水线与 P3 六维筛选，保持独立端点兼容。
3. 落地可靠取消、持久化 job 状态与 Windows C++ 实机验证。
4. 实现网络/远端用户与元数据管理，继续隔离本机 demo 桥。
5. 再推进 Qt 桌面、平台安装包与 P8 发布材料。
