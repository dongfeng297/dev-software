# P4 / P5 / P6 实现说明（打包压缩、加密、自动化与增量）

更新日期：2026-11-08。本文对应课程表格中的 P4、P5、P6 三行，记录实际实现、格式、接口、验收方式和仍然存在的限制。

## 0. 结论

| 阶段 | 状态 | 真实实现 |
| --- | --- | --- |
| P4 自实现容器与压缩 | 已完成 | 自实现 `BNAR` 单文件容器；自实现分块 LZSS `BNLZ` 压缩/解压 |
| P5 加密 | 已完成 | AES-256-GCM + PBKDF2-HMAC-SHA256；短期内存 `secretRef`；错误密码/篡改明确失败 |
| P6 自动化与增量 | 已完成 | 5 字段 cron 定时、轮询去抖实时监听、硬链接增量、可选版本保留、互斥与不重入 |

实现位于 `backnest/`，通过 `server.py` 的既有 `/api/v1` operationId 暴露；没有新增无版本路径。Web UI 增加“扩展能力”页，可直接操作。

## 1. 运行模型

```text
Web UI / curl
    └─ /api/v1/<operationId>  (server.py: 本机会话令牌、Host/Origin 检查、任务线程)
        ├─ 完整备份/还原/校验：优先调用 C++17 引擎（存在 build/backnest 时）
        │                        否则调用 backnest.engine 兼容引擎
        └─ P4/P5/P6：backnest.container / compress / crypto / scheduler / watcher
```

- P4、P5 的算法与加解密不依赖 C++ 编译器，也不依赖第三方 Python 包（Windows 加密使用系统 CNG）。
- 完整备份在无 C++ 编译器时使用 Python 兼容引擎；两种引擎写同一种 `BACKNEST 1` 清单格式，可互相读取。
- 算法要求仍遵守原约定：**打包/压缩由项目自行实现，加密允许调用成熟库**。产品路径没有 zlib、zip、tar、gzip、7z 或子进程压缩工具。

## 2. P4：自实现单文件容器（BNAR v1）

### 2.1 格式

所有整数小端：

| 位置 | 字段 |
| --- | --- |
| header | magic `BNAR`（4）、u16 version=1、u16 flags=0、u32 entryCount、u64 totalFileBytes、u32 createdUnixSeconds、u32 headerCrc32 |
| entry × N | u8 kind（0=目录，1=文件）、u16 mode、i64 mtimeNs、u64 size、u32 crc32、u16 pathLength、path（UTF-8）、payload |
| trailer | `BNAREND\0` |

- 目录条目没有 payload；文件条目紧跟原始字节。
- 路径统一 `/` 分隔、相对路径；拒绝绝对路径、`..`、`.`、空层级、反斜杠、NUL、驱动器前缀、Windows 保留名、结尾空格/点。
- 读取时校验文件头 CRC、条目数量、路径长度、大小上限、重复路径、父目录存在、每文件 CRC、总大小、结尾标记和多余数据。
- 上限：200,000 条目、单路径 4,096 字节、总内容 1 PiB 量级（防御性上限）。
- 写入使用目标目录内 `.partial-*` 临时文件，完成后 `os.replace`；失败清理。
- 解包先写入同级 `.backnest-unpack-*` 暂存目录，全部校验通过后才发布到新目录/空目录；失败不留下半成品。

代码：`backnest/container.py`。算法标识：`bkn-archive-v1`，`selfImplemented: true`。

### 2.2 接口

| operationId | 请求 | 行为 |
| --- | --- | --- |
| `archive.algorithms` | GET | 返回真实算法清单 |
| `archive.pack` | `CodecRequest`，source=目录，destination=新文件 | 异步任务，生成单文件 |
| `archive.list` | `SnapshotRequest`，snapshot=归档文件 | 同步返回条目元数据 |
| `archive.unpack` | `CodecRequest`，source=归档文件，destination=新/空目录 | 异步任务，校验后解包 |

示例：

```json
{"source":"C:/data/photos","destination":"C:/backups/photos.bkn",
 "options":{"algorithm":"bkn-archive-v1"}}
```

## 3. P4：自实现 LZSS 压缩（BNLZ v1）

### 3.1 格式

| 位置 | 字段 |
| --- | --- |
| header | magic `BNLZ`（4）、u8 version=1、u8 flags=0、u16 reserved=0、u32 blockBytes |
| block × N | u8 mode（0=原样存储，1=LZSS）、u32 originalLength、u32 payloadLength、u32 crc32(original)、payload |

LZSS 细节：

- 4,096 字节滑动窗口，最短匹配 3，最长匹配 18。
- 每 8 个 token 一个标志字节，最低位优先；1=字面量，0=匹配。
- 匹配 token 为 2 字节小端：高 12 位为回退距离，低 4 位为 `length-3`。
- 某块压缩后不小于原块时写 mode=0 原样存储，避免膨胀。
- 解码严格拒绝：截断、非法头、非法块长、越界距离、匹配越界、CRC 不符、尾随数据、超过 `maxOutputBytes`。
- 64 KiB 分块流式处理，不整文件读入内存。

代码：`backnest/compress.py`。算法标识：`lzss-v1`，`selfImplemented: true`。

接口：

| operationId | 请求 |
| --- | --- |
| `compression.algorithms` | GET |
| `compression.compress` | `CodecRequest`，`options.algorithm=lzss-v1`，可选 `blockBytes` |
| `compression.decompress` | 同上，可选 `maxOutputBytes` |

## 4. P5：认证加密（BNE1 v1）

### 4.1 密码学选择

- KDF：PBKDF2-HMAC-SHA256（`hashlib.pbkdf2_hmac`），默认 200,000 次，16 字节随机盐，32 字节密钥。
- AEAD：AES-256-GCM，12 字节随机 nonce，16 字节 tag。
- Windows：通过 `ctypes` 调用系统 CNG（`bcrypt.dll`），不引入第三方包，也不手写 AES。
- macOS/Linux：使用可选的 `cryptography` 包；若未安装，明确报错并提示安装，不会降级到不安全的自写算法。
- 密码只存在于内存 `secretRef` 中（默认 TTL 600 秒），不写入任务记录、清单、日志、URL 或 localStorage。

### 4.2 文件格式

```text
BNE1 header (32 bytes, 全部作为 AEAD AAD)
  magic "BNE1", version=1, algorithm=1, kdf=1, reserved=0,
  u32 iterations, u16 salt=16, u16 nonce=12, u16 tag=16,
  u64 plaintextLength, 6 bytes zero padding
salt(16) || nonce(12) || ciphertext || tag(16)
```

每个字段、零填充、精确文件长度在解密前校验；错误密码、salt/nonce/密文/tag/header 篡改都会返回失败且不产生目标文件。

代码：`backnest/crypto.py`。算法标识：`aes-256-gcm`，`selfImplemented: false`（使用成熟库）。

### 4.3 接口与 secretRef 流程

1. `POST /api/v1/secrets`，body `{"value":"密码","purpose":"encryption"}`，返回 `{"secretRef":"secret-…","expiresAt":"…"}`。
2. `POST /api/v1/encryption/encrypt` 或 `decrypt`，`CryptoRequest` 中带 `secretRef`，不传明文密码。
3. `DELETE /api/v1/secrets/{secretId}` 立即销毁；UI 在任务结束后自动销毁，并清空密码输入框。

注意：当前实现为了演示，加解密时会把整个文件读入内存；超大文件应改用分块 AEAD（尚未实现）。产品路径仍是真实的 CNG/`cryptography`，不是占位实现。

## 5. P6：自动化与增量

### 5.1 增量备份

- `backup.start` 的 `options.mode` 支持 `full` 与 `incremental`。
- 增量策略：与目标位置最新的 `snapshot-*` 清单比较；大小和 CRC 都相同的文件，在新版本 `data/` 中用硬链接指向旧版本数据；新增/变更文件正常复制。硬链接失败（文件系统不支持、跨卷）时回退为复制。
- 新版本仍然写**完整清单**，所以：
  - 每个保留版本都可独立校验/还原；
  - 删除旧版本不会破坏新版本（硬链接数据仍然存在）；
  - 现有 C++ 引擎可以校验/还原 Python 生成的增量版本（格式相同、数据是普通文件）。
- 备份完成前会 `verify` 暂存目录，任何 CRC 不符都会失败并清理 `.partial-*`。
- `incremental.preview` 返回 added / changed / deleted 与基准版本名，预览与执行使用同一扫描/校验逻辑。

### 5.2 定时

- 接口：`schedules.list/create/update/delete`、`schedules.nextRuns`。
- 表达式：5 字段 cron（分 时 日 月 周），支持 `*`、列表、范围、步长；周日可用 0 或 7。
- 时区：优先 IANA `zoneinfo`；没有 tzdata 的 Windows 环境支持 `UTC`、`local`、`+08:00` 和常见无夏令时的固定偏移（如 `Asia/Shanghai`）。无法解析的时区明确报错。
- 行为：
  - 只在 `server.py` 进程存活时执行；进程退出后不会在后台继续，也不会注册系统服务。
  - 每个计划保存 `nextRunAt`，触发后立即推进，避免同一分钟重复触发。
  - 错过执行：`missedRunPolicy=skip` 跳过；`run_once` 在下次检查时补跑一次。
  - 与手动备份共享同一个 `engine_lock`；忙时记录“已跳过”，不重入、不排队风暴。
  - 非法表达式会把计划停用并记录 `lastError`，避免每 5 秒循环报错。
- 任务配置保存在 `.backnest/backend.json`（git 忽略），重启后保留。

### 5.3 实时监听

- 接口：`watchers.start/get/stop`。
- 采用轮询树签名（相对路径 + 大小 + mtime + 类型），默认 500 ms 去抖；轮询间隔 0.2–1 s，由去抖值决定。
- 自动排除任务的目标目录和 `excludedPaths`，避免“备份产生文件 → 再次触发备份”的循环。
- 触发后立即返回，实际备份在统一任务线程中执行；若已有任务运行，则记录跳过并稍后重试。
- 监听状态在内存中，服务退出即停止；`watchers.get` 返回 running/stopped/failed、lastEventAt 和 error。

### 5.4 版本策略

- 默认 `keepVersions=0`：保留全部版本，不自动删除。
- `settings.update` 可设置 `keepVersions=N>0`：每次成功备份后，只清理**本后端登记过、位于同一目标目录、名为 `snapshot-*`** 的最旧版本，保留最新 N 个。
- 不会删除未登记的目录或用户自己的文件；删除失败会记录警告。
- 增量版本即使其基准被清理，仍可独立还原（硬链接）。

## 6. 接口覆盖

本轮新增 33 个已实现 operationId（原有 7 个，合计 40/63）：

```text
archive.algorithms/pack/unpack/list
compression.algorithms/compress/decompress
encryption.algorithms/encrypt/decrypt
secrets.create/revoke
tasks.list/create/update/delete
schedules.list/create/update/delete/nextRuns
watchers.start/get/stop
incremental.preview
settings.get/update
snapshots.list/register/delete
activity.list/export
workspace.overview
```

其余 23 个仍明确返回 501（六维筛选、元数据、网络、远端用户、Qt 桌面等），不会伪装成功。

## 7. UI

左侧新增“扩展能力”页：

- P4：源目录 → 归档文件打包；归档 → 新/空目录解包；文件压缩/解压。
- P5：输入密码后加密/解密；密码只换取短期 secretRef，任务结束自动撤销并清空输入框。
- P6：增量预览；创建 cron 计划；开始/停止实时监听；查看后端计划及下次执行时间。

UI 只调用既有 operationId，不拼接无版本路径。

## 8. 验证

```sh
# 扩展模块与 HTTP 端到端（Windows 无 C++ 编译器也可运行）
python -m unittest discover -s tests/features -v

# 全部测试
python -m unittest discover -s tests -v

# 交互式完整演示（自动生成测试文件，调用真实 HTTP → 引擎）
python scripts/demo_extensions.py
```

覆盖点：

- BNAR：中文/空格/空文件/空目录/嵌套目录往返、随机 120 KB、损坏/截断/CRC/危险路径/输出上限/已存在目标。
- BNLZ：空输入、单符号、全 256 字节、中文文件名、2 MiB 随机、4 MiB 重复、存储块、截断/损坏/CRC/输出上限。
- BNE1：空/1 字节/中文/1 MiB、错误密码、salt/nonce/密文/tag/header 篡改、截断、目标已存在、密码不泄漏；与 Node.js AES-256-GCM 独立向量交叉验证。
- 增量：硬链接复用、变更/新增/删除预览、各版本独立校验还原、删除基准后仍可还原。
- 自动化：cron 步长/列表/周日、时区回退、错过策略、忙时跳过、非法表达式停用、去抖触发、目标目录排除。
- HTTP：上述所有端点、错误码、任务状态、版本索引、活动日志、版本保留。

## 9. 已知限制（必须如实说明）

1. **备份选项流水线仍未串联**：`backup.start` 带 `archive`/`compression`/`encryption` 选项仍返回 501；P4/P5 通过独立的 `archive.*`、`compression.*`、`encryption.*` 端点演示。没有声称“一次备份自动打包+压缩+加密”已完成。
2. **加密整文件读入内存**，超大文件需要分块 AEAD，当前未实现。
3. **实时监听是轮询**，不是文件系统通知；超大目录轮询有成本，变化检测粒度为去抖后的树签名。
4. **定时只在进程存活时运行**，不是系统服务；应用退出后不执行，错过策略见上。
5. **硬链接增量依赖同一文件系统**；不支持时回退为复制，空间收益下降但结果正确。
6. **新功能在 Windows 上实测**；macOS 的 C++ 基础引擎沿用原实现，本轮未重新实机验收 P4–P6。
7. **C++ 引擎未在本机编译**（环境没有 C++ 编译器）：完整备份/还原使用兼容 Python 引擎验证，清单格式与 C++ 读取器保持兼容。有编译器的机器仍优先使用 C++。
8. 归档/压缩格式版本为 v1；后续修改必须新增读取器并保留旧格式回归测试。
