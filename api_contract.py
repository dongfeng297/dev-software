"""Single source of truth for v1 backend ports, schemas, and requirement coverage.

Reserved operations return 501. A declared interface is not an implemented feature.
"""
import copy
import re

from backnest import compress as _compress
from backnest import container as _container
from backnest import crypto as _crypto

VERSION = "1.1.0"


def obj(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


def ref(name):
    return {"$ref": "#/components/schemas/" + name}


STRING = {"type": "string", "minLength": 1}
PATH = {"type": "string", "minLength": 1, "description": "Native absolute path; backend must canonicalize and validate it."}
SCHEMAS = {
    "Error": obj({"error": STRING, "code": STRING, "operationId": STRING, "capability": STRING, "retryable": {"type": "boolean"}}, ["error", "code"]),
    "JobAccepted": obj({"id": STRING}, ["id"]),
    "Job": obj({"id": STRING, "operation": STRING, "state": {"enum": ["running", "completed", "failed", "cancelled"]}, "phase": STRING,
                "files": {"type": "integer", "minimum": 0}, "totalFiles": {"type": "integer", "minimum": 0},
                "bytes": {"type": "integer", "minimum": 0}, "totalBytes": {"type": "integer", "minimum": 0},
                "path": {"type": "string"}, "result": ref("OperationResult"), "error": STRING}, ["id", "operation", "state"]),
    "OperationResult": obj({"operation": STRING, "path": PATH, "source": {"type": "string"}, "files": {"type": "integer", "minimum": 0},
                           "directories": {"type": "integer", "minimum": 0}, "bytes": {"type": "integer", "minimum": 0},
                           "verified": {"type": "boolean"}, "completedAt": {"type": "string", "format": "date-time"}},
                          ["operation", "path", "files", "directories", "bytes", "verified", "completedAt"]),
    "FilterSpec": obj({
        "paths": obj({"include": {"type": "array", "items": STRING}, "exclude": {"type": "array", "items": STRING}}),
        "types": {"type": "array", "items": STRING, "description": "File categories/extensions; special-file semantics use FileTypePolicy."},
        "names": {"type": "array", "items": STRING, "description": "File-name glob patterns."},
        "time": obj({"field": {"enum": ["modified", "created", "accessed"]}, "from": {"type": "string", "format": "date-time"}, "to": {"type": "string", "format": "date-time"}}, ["field"]),
        "size": obj({"minBytes": {"type": "integer", "minimum": 0}, "maxBytes": {"type": "integer", "minimum": 0}}),
        "users": {"type": "array", "items": ref("OwnerSelector")},
    }),
    "OwnerSelector": obj({"platform": {"enum": ["posix", "windows"]}, "id": STRING, "displayName": STRING}, ["platform", "id"]),
    "FileTypePolicy": obj({"symlinks": {"enum": ["reject", "preserve"]}, "hardlinks": {"enum": ["copy_contents", "preserve"]}, "specialFiles": {"enum": ["reject", "preserve"]}}),
    "MetadataPolicy": obj({"preserve": {"type": "array", "items": {"enum": ["times", "permissions", "owner", "acl", "xattrs"]}}, "unsupported": {"enum": ["fail", "report"]}}),
    "CodecOptions": obj({"algorithm": STRING, "blockBytes": {"type": "integer", "minimum": 1024}, "maxOutputBytes": {"type": "integer", "minimum": 0}}, ["algorithm"]),
    "EncryptionOptions": obj({"algorithm": STRING, "secretRef": {**STRING, "writeOnly": True}}, ["algorithm", "secretRef"]),
    "BackupOptions": obj({"mode": {"enum": ["full", "incremental"]}, "filters": ref("FilterSpec"), "fileTypes": ref("FileTypePolicy"),
                          "metadata": ref("MetadataPolicy"), "archive": ref("CodecOptions"), "compression": ref("CodecOptions"), "encryption": ref("EncryptionOptions")}),
    "BackupRequest": obj({"source": PATH, "destination": PATH, "options": ref("BackupOptions")}, ["source", "destination"]),
    "RestoreRequest": obj({"snapshot": PATH, "destination": PATH, "conflictPolicy": {"enum": ["require_empty", "skip", "rename", "overwrite"]},
                           "metadata": ref("MetadataPolicy"), "secretRef": {**STRING, "writeOnly": True}}, ["snapshot", "destination"]),
    "SnapshotRequest": obj({"snapshot": PATH}, ["snapshot"]),
    "ScanRequest": obj({"source": PATH, "filters": ref("FilterSpec"), "fileTypes": ref("FileTypePolicy")}, ["source"]),
    "MetadataRequest": obj({"path": PATH, "attributes": {"type": "object"}, "policy": ref("MetadataPolicy")}, ["path"]),
    "CodecRequest": obj({"source": PATH, "destination": PATH, "options": ref("CodecOptions"), "secretRef": {**STRING, "writeOnly": True}}, ["source", "destination", "options"]),
    "Task": obj({"id": STRING, "name": STRING, "source": PATH, "destination": PATH, "options": ref("BackupOptions")}, ["name", "source", "destination"]),
    "Schedule": obj({"taskId": STRING, "timezone": STRING, "expression": STRING, "enabled": {"type": "boolean"}, "missedRunPolicy": {"enum": ["skip", "run_once"]}}, ["taskId", "timezone", "expression"]),
    "WatchRequest": obj({"taskId": STRING, "source": PATH, "debounceMs": {"type": "integer", "minimum": 100}, "excludedPaths": {"type": "array", "items": PATH}}, ["taskId", "source"]),
    "RemoteConfig": obj({"name": STRING, "endpoint": {"type": "string", "pattern": "^https://"}, "credentialRef": {**STRING, "writeOnly": True}}, ["name", "endpoint"]),
    "TransferRequest": obj({"snapshot": PATH, "destination": PATH, "snapshotId": STRING, "resumeToken": STRING}),
    "UserRequest": obj({"username": STRING, "roles": {"type": "array", "items": STRING}, "secretRef": {**STRING, "writeOnly": True}}, ["username"]),
    "LoginRequest": obj({"username": STRING, "password": {**STRING, "format": "password", "writeOnly": True}}, ["username", "password"]),
    "SecretRequest": obj({"value": {**STRING, "format": "password", "writeOnly": True}, "purpose": {"enum": ["encryption", "remote_auth"]}}, ["value", "purpose"]),
    "Settings": obj({"destination": {"type": "string"}, "defaultOptions": ref("BackupOptions"), "keepVersions": {"type": "integer", "minimum": 0}, "notifications": {"type": "boolean"}}),
    "CatalogEntry": obj({"snapshot": PATH, "taskId": STRING, "remoteId": STRING}, ["snapshot"]),
    "DirectoryListing": obj({"path": PATH, "parent": PATH, "directories": {"type": "array", "items": obj({"name": STRING, "path": PATH}, ["name", "path"])}}, ["path", "parent", "directories"]),
    # Reserved domain responses are intentionally explicit; backend must refine before activation.
    "DomainRecord": {"type": "object", "description": "Reserved domain payload; refine its fields and bump the contract before advertising implementation."},
    "DomainList": {"type": "array", "items": ref("DomainRecord")},
    "Capabilities": {"type": "object", "required": ["apiVersion", "operations", "requirements"]},
    "Contract": {"type": "object"},
}

OPERATIONS = []


def add(operation_id, method, path, requirement, summary, request=None, response="DomainRecord", implemented=False, asynchronous=False):
    OPERATIONS.append({"id": operation_id, "method": method, "path": "/api/v1" + path, "requirement": requirement, "summary": summary,
                       "request": request, "response": "JobAccepted" if asynchronous else response,
                       "implemented": implemented, "async": asynchronous})


add("system.capabilities", "GET", "/capabilities", "gui", "查询能力与真实实现状态", response="Capabilities", implemented=True)
add("system.contract", "GET", "/openapi.json", "gui", "读取接口契约", response="Contract", implemented=True)
add("filesystem.directories", "GET", "/filesystem/directories", "gui", "列出本机可选目录", response="DirectoryListing", implemented=True)
add("backup.start", "POST", "/backups", "backup", "执行目录备份", "BackupRequest", implemented=True, asynchronous=True)
add("restore.start", "POST", "/restores", "restore", "执行目录还原", "RestoreRequest", implemented=True, asynchronous=True)
add("snapshots.verify", "POST", "/snapshots/verify", "restore", "校验独立备份版本，供 GUI 导入", "SnapshotRequest", implemented=True, asynchronous=True)
add("jobs.get", "GET", "/jobs/{jobId}", "gui", "查询真实运行进度", response="Job", implemented=True)
add("jobs.cancel", "POST", "/jobs/{jobId}/cancel", "gui", "请求取消运行", asynchronous=True)
add("fileTypes.inspect", "POST", "/filesystem/types/inspect", "file_types", "检查普通、链接和特殊文件支持情况", "ScanRequest")
add("metadata.read", "POST", "/metadata/read", "metadata", "读取文件元数据", "MetadataRequest")
add("metadata.apply", "POST", "/metadata/apply", "metadata", "恢复文件元数据", "MetadataRequest", asynchronous=True)
add("filters.preview", "POST", "/filters/preview", "custom_backup", "预览六维筛选结果", "ScanRequest")
add("filters.validate", "POST", "/filters/validate", "custom_backup", "校验六维规则及组合", "ScanRequest")
for group, requirement in [("archive", "archive"), ("compression", "compression"), ("encryption", "encryption")]:
    add(group + ".algorithms", "GET", "/" + group + "/algorithms", requirement, "列出已实现算法", response="DomainList")
for name, action, requirement in [("archive", "pack", "archive"), ("archive", "unpack", "archive"), ("compression", "compress", "compression"),
                                  ("compression", "decompress", "compression"), ("encryption", "encrypt", "encryption"), ("encryption", "decrypt", "encryption")]:
    add(name + "." + action, "POST", "/" + name + "/" + action, requirement, action + " 文件数据", "CodecRequest", asynchronous=True)
add("archive.list", "POST", "/archive/list", "archive", "预览包内条目", "SnapshotRequest", response="DomainList")
add("secrets.create", "POST", "/secrets", "encryption", "将敏感输入换为短期 secretRef", "SecretRequest")
add("secrets.revoke", "DELETE", "/secrets/{secretId}", "encryption", "销毁临时密码引用")
for group, requirement, schema in [("tasks", "gui", "Task"), ("schedules", "scheduled_backup", "Schedule"), ("remotes", "network_backup", "RemoteConfig"), ("users", "network_backup", "UserRequest")]:
    add(group + ".list", "GET", "/" + group, requirement, "列出 " + group, response="DomainList")
    add(group + ".create", "POST", "/" + group, requirement, "新增 " + group, schema)
    add(group + ".update", "PATCH", "/" + group + "/{id}", requirement, "更新 " + group, schema)
    add(group + ".delete", "DELETE", "/" + group + "/{id}", requirement, "删除 " + group + " 配置（默认不删除磁盘备份）")
add("schedules.nextRuns", "GET", "/schedules/{id}/next-runs", "scheduled_backup", "查询下次执行时间", response="DomainList")
add("watchers.start", "POST", "/watchers", "realtime_backup", "开始实时监听", "WatchRequest")
add("watchers.get", "GET", "/watchers/{watchId}", "realtime_backup", "查询监听状态")
add("watchers.stop", "DELETE", "/watchers/{watchId}", "realtime_backup", "停止实时监听")
add("remotes.test", "POST", "/remotes/{id}/test", "network_backup", "测试远端连接")
add("remotes.upload", "POST", "/remotes/{id}/upload", "network_backup", "上传备份，预留续传", "TransferRequest", asynchronous=True)
add("remotes.download", "POST", "/remotes/{id}/download", "network_backup", "下载备份，预留续传", "TransferRequest", asynchronous=True)
add("remoteCatalog.list", "GET", "/remotes/{id}/snapshots", "network_backup", "查询远端备份元数据", response="DomainList")
add("remoteCatalog.register", "POST", "/remotes/{id}/snapshots", "network_backup", "登记远端备份元数据", "CatalogEntry")
add("remoteCatalog.get", "GET", "/remotes/{id}/snapshots/{snapshotId}", "network_backup", "读取远端备份元数据")
add("remoteCatalog.delete", "DELETE", "/remotes/{id}/snapshots/{snapshotId}", "network_backup", "移除远端元数据记录，默认不删除数据")
add("auth.login", "POST", "/auth/sessions", "network_backup", "远端用户登录", "LoginRequest")
add("auth.logout", "DELETE", "/auth/sessions/{sessionId}", "network_backup", "撤销远端登录会话")
add("snapshots.list", "GET", "/snapshots", "gui", "后端版本目录索引", response="DomainList")
add("snapshots.register", "POST", "/snapshots", "gui", "后端登记已验证版本", "CatalogEntry")
add("snapshots.delete", "DELETE", "/snapshots/{snapshotId}", "other", "删除版本索引，默认不删除磁盘数据")
add("settings.get", "GET", "/settings", "gui", "读取后端偏好", response="Settings")
add("settings.update", "PATCH", "/settings", "gui", "保存后端偏好", "Settings", response="Settings")
add("activity.list", "GET", "/activity", "gui", "读取后端操作日志", response="DomainList")
add("activity.export", "POST", "/activity/export", "gui", "导出后端操作日志")
add("workspace.overview", "GET", "/workspace/overview", "gui", "获取后端统计")
add("incremental.preview", "POST", "/incremental/preview", "other", "预留增量比较", "BackupRequest")

# P4/P5/P6 extension ports that now have real implementations.
IMPLEMENTED_EXTENSIONS = {
    "archive.algorithms", "archive.pack", "archive.unpack", "archive.list",
    "compression.algorithms", "compression.compress", "compression.decompress",
    "encryption.algorithms", "encryption.encrypt", "encryption.decrypt",
    "secrets.create", "secrets.revoke",
    "tasks.list", "tasks.create", "tasks.update", "tasks.delete",
    "schedules.list", "schedules.create", "schedules.update", "schedules.delete", "schedules.nextRuns",
    "watchers.start", "watchers.get", "watchers.stop",
    "incremental.preview", "settings.get", "settings.update",
    "snapshots.list", "snapshots.register", "snapshots.delete",
    "activity.list", "activity.export", "workspace.overview",
}
for _operation in OPERATIONS:
    if _operation["id"] in IMPLEMENTED_EXTENSIONS:
        _operation["implemented"] = True

REQUIREMENTS = [
    {"id": "backup", "name": "目录树数据备份", "points": "基础共40分", "status": "implemented_basic"},
    {"id": "restore", "name": "目录树数据还原", "points": "基础共40分", "status": "implemented_basic"},
    {"id": "file_types", "name": "文件类型支持", "points": "满分10分", "status": "regular_files_and_directories_only"},
    {"id": "metadata", "name": "属主、时间、权限等元数据", "points": "满分10分", "status": "reserved"},
    {"id": "custom_backup", "name": "自定义备份：路径、类型、名字、时间、尺寸、用户", "points": "照片分值不清", "status": "reserved"},
    {"id": "archive", "name": "打包解包", "points": "每种算法10分", "status": "implemented_self_bkn_archive_v1"},
    {"id": "compression", "name": "压缩解压", "points": "每种算法10分", "status": "implemented_self_lzss_v1"},
    {"id": "encryption", "name": "加密解密", "points": "每种算法10分", "status": "implemented_aes_256_gcm_library"},
    {"id": "gui", "name": "图形界面", "points": "满分10分", "status": "web_ui_implemented_qt_pending"},
    {"id": "scheduled_backup", "name": "定时备份", "points": "满分10分", "status": "implemented_in_process_cron"},
    {"id": "realtime_backup", "name": "实时备份", "points": "满分15分", "status": "implemented_polling_debounce"},
    {"id": "network_backup", "name": "网络备份，含用户与元数据管理", "points": "具体而定，部分遮挡", "status": "reserved"},
    {"id": "other", "name": "其他功能：增量、版本管理等", "points": "按难度讨论", "status": "incremental_implemented_hardlink"},
]

# Persisted task options deliberately exclude ephemeral encryption secretRef.
SCHEMAS["TaskOptions"] = obj({**SCHEMAS["BackupOptions"]["properties"],
                             "encryption": obj({"enabled": {"type": "boolean"}, "algorithm": STRING}, ["enabled"])})
SCHEMAS["Task"]["properties"]["options"] = ref("TaskOptions")
SCHEMAS["CryptoRequest"] = obj({**SCHEMAS["CodecRequest"]["properties"]}, ["source", "destination", "options", "secretRef"])
# Concrete reserved response DTOs are agreed here before backend activation.
SCHEMAS.update({
    "EntryMetadata": obj({"path": PATH, "kind": STRING, "bytes": {"type": "integer", "minimum": 0},
                          "owner": ref("OwnerSelector"), "modifiedAt": STRING, "createdAt": STRING, "accessedAt": STRING,
                          "permissions": {"type": "object"}, "acl": {"type": "array", "items": {"type": "object"}},
                          "xattrs": {"type": "object"}}, ["path", "kind", "bytes"]),
    "EntryList": {"type": "array", "items": ref("EntryMetadata")},
    "FileTypeReport": obj({"entries": ref("EntryList"), "unsupported": ref("EntryList"), "warnings": {"type": "array", "items": STRING}}, ["entries", "unsupported"]),
    "FilterPreview": obj({"included": ref("EntryList"), "excluded": ref("EntryList"), "files": {"type": "integer", "minimum": 0},
                          "bytes": {"type": "integer", "minimum": 0}, "warnings": {"type": "array", "items": STRING}}, ["included", "excluded", "files", "bytes"]),
    "FilterValidation": obj({"valid": {"type": "boolean"}, "errors": {"type": "array", "items": STRING}}, ["valid", "errors"]),
    "Algorithm": obj({"id": STRING, "version": STRING, "selfImplemented": {"type": "boolean"}, "supportedPlatforms": {"type": "array", "items": STRING}}, ["id", "version", "selfImplemented"]),
    "AlgorithmList": {"type": "array", "items": ref("Algorithm")},
    "SecretReference": obj({"secretRef": STRING, "expiresAt": {"type": "string", "format": "date-time"}}, ["secretRef", "expiresAt"]),
    "TaskRecord": obj({**SCHEMAS["Task"]["properties"], "state": {"enum": ["ready", "running", "completed", "failed"]}, "lastRunAt": STRING}, ["id", "name", "source", "destination", "options", "state"]),
    "ScheduleRecord": obj({**SCHEMAS["Schedule"]["properties"], "id": STRING, "nextRunAt": STRING, "lastRunAt": STRING}, ["id", "taskId", "timezone", "expression", "enabled"]),
    "WatchState": obj({"id": STRING, "taskId": STRING, "source": PATH, "state": {"enum": ["running", "stopped", "failed"]}, "lastEventAt": STRING, "error": STRING}, ["id", "taskId", "source", "state"]),
    "RemoteRecord": obj({"id": STRING, "name": STRING, "endpoint": STRING, "status": {"enum": ["configured", "connected", "failed"]}}, ["id", "name", "endpoint", "status"]),
    "RemoteConnection": obj({"connected": {"type": "boolean"}, "tlsVerified": {"type": "boolean"}, "error": STRING}, ["connected", "tlsVerified"]),
    "SnapshotRecord": obj({"id": STRING, "taskId": STRING, "remoteId": STRING, "path": PATH, "createdAt": STRING,
                           "files": {"type": "integer", "minimum": 0}, "bytes": {"type": "integer", "minimum": 0}, "verified": {"type": "boolean"}}, ["id", "files", "bytes", "verified"]),
    "UserRecord": obj({"id": STRING, "username": STRING, "roles": {"type": "array", "items": STRING}}, ["id", "username", "roles"]),
    "AuthSession": obj({"id": STRING, "accessToken": STRING, "expiresAt": STRING, "user": ref("UserRecord")}, ["id", "accessToken", "expiresAt", "user"]),
    "DeleteResult": obj({"id": STRING, "recordDeleted": {"type": "boolean"}, "dataDeleted": {"type": "boolean"}}, ["id", "recordDeleted", "dataDeleted"]),
    "ActivityRecord": obj({"id": STRING, "taskId": STRING, "jobId": STRING, "time": STRING, "level": {"enum": ["info", "success", "warning", "error"]}, "title": STRING, "detail": {"type": "string"}}, ["id", "time", "level", "title"]),
    "ActivityExport": obj({"format": {"enum": ["json"]}, "records": {"type": "array", "items": ref("ActivityRecord")}}, ["format", "records"]),
    "Overview": obj({"tasks": {"type": "integer", "minimum": 0}, "snapshots": {"type": "integer", "minimum": 0}, "backedUpFiles": {"type": "integer", "minimum": 0}, "lastActivity": ref("ActivityRecord")}, ["tasks", "snapshots", "backedUpFiles"]),
    "IncrementalPreview": obj({"added": ref("EntryList"), "changed": ref("EntryList"), "deleted": ref("EntryList"), "baseSnapshotId": STRING}, ["added", "changed", "deleted"]),
})
for item in ["TaskRecord", "ScheduleRecord", "RemoteRecord", "SnapshotRecord", "UserRecord", "ActivityRecord"]:
    SCHEMAS[item + "List"] = {"type": "array", "items": ref(item)}
SCHEMAS["NextRuns"] = {"type": "array", "items": {"type": "string", "format": "date-time"}}
RESPONSE_TYPES = {
    "fileTypes.inspect": "FileTypeReport", "metadata.read": "EntryMetadata", "filters.preview": "FilterPreview", "filters.validate": "FilterValidation",
    "archive.list": "EntryList", "secrets.create": "SecretReference", "secrets.revoke": "DeleteResult",
    "schedules.nextRuns": "NextRuns", "watchers.start": "WatchState", "watchers.get": "WatchState", "watchers.stop": "DeleteResult",
    "remotes.test": "RemoteConnection", "remoteCatalog.list": "SnapshotRecordList", "remoteCatalog.register": "SnapshotRecord", "remoteCatalog.get": "SnapshotRecord", "remoteCatalog.delete": "DeleteResult",
    "auth.login": "AuthSession", "auth.logout": "DeleteResult", "snapshots.list": "SnapshotRecordList", "snapshots.register": "SnapshotRecord", "snapshots.delete": "DeleteResult",
    "activity.list": "ActivityRecordList", "activity.export": "ActivityExport", "workspace.overview": "Overview", "incremental.preview": "IncrementalPreview",
}
for group, record in [("tasks", "TaskRecord"), ("schedules", "ScheduleRecord"), ("remotes", "RemoteRecord"), ("users", "UserRecord")]:
    for action in ["list", "create", "update", "delete"]:
        RESPONSE_TYPES[group + "." + action] = record + "List" if action == "list" else "DeleteResult" if action == "delete" else record
for group in ["archive", "compression", "encryption"]:
    RESPONSE_TYPES[group + ".algorithms"] = "AlgorithmList"
for operation in OPERATIONS:
    if operation["id"] in RESPONSE_TYPES:
        operation["response"] = RESPONSE_TYPES[operation["id"]]
    if operation["id"] in ["encryption.encrypt", "encryption.decrypt"]:
        operation["request"] = "CryptoRequest"


def match_operation(method, path):
    for operation in OPERATIONS:
        pattern = "/".join("([^/]+)" if part.startswith("{") and part.endswith("}") else re.escape(part)
                           for part in operation["path"].split("/"))
        match = re.fullmatch(pattern, path)
        if match and operation["method"] == method:
            names = re.findall(r"\{([^}]+)\}", operation["path"])
            return operation, dict(zip(names, match.groups()))
    return None, {}


def capabilities():
    return {"apiVersion": VERSION, "operations": copy.deepcopy(OPERATIONS), "requirements": copy.deepcopy(REQUIREMENTS),
            "algorithms": {"archive": copy.deepcopy(_container.ALGORITHMS),
                           "compression": copy.deepcopy(_compress.ALGORITHMS),
                           "encryption": copy.deepcopy(_crypto.ALGORITHMS),
                           "integrity": ["crc32-self-implemented"]},
            "algorithmPolicy": {"archive": "self_implemented_only", "compression": "self_implemented_only", "encryption": "libraries_allowed"},
            "supportedRestorePolicies": ["require_empty"],
            "automation": {"scheduler": "in_process_cron", "watcher": "polling_debounce",
                           "incremental": "hardlink_or_copy", "runs_after_process_exit": False},
            "persistence": "browser_local_and_backend_json_for_automation",
            "platforms": {"macOS": "engine_entry_only_not_retested", "Windows": "tested_python_engine_and_features"}}


def validate(value, schema, path="body"):
    """Validate the request-schema subset used here, without adding a dependency."""
    if "$ref" in schema:
        return validate(value, SCHEMAS[schema["$ref"].rsplit("/", 1)[-1]], path)
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(path + ": value is not allowed")
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            raise ValueError(path + ": expected object")
        properties = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                raise ValueError(path + ": missing " + name)
        for name, item in value.items():
            if name not in properties and schema.get("additionalProperties") is False:
                raise ValueError(path + ": unknown field " + name)
            if name in properties:
                validate(item, properties[name], path + "." + name)
    elif kind == "array":
        if not isinstance(value, list):
            raise ValueError(path + ": expected array")
        for item in value:
            validate(item, schema["items"], path + "[]")
    elif kind == "string":
        if not isinstance(value, str) or len(value) < schema.get("minLength", 0) or "\0" in value:
            raise ValueError(path + ": invalid string")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            raise ValueError(path + ": invalid format")
    elif kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int) or value < schema.get("minimum", 0):
            raise ValueError(path + ": invalid integer")
    elif kind == "boolean" and not isinstance(value, bool):
        raise ValueError(path + ": expected boolean")


def unsupported_option(operation_id, body):
    """Never silently accept an unsupported policy and then run a plain backup."""
    if operation_id == "backup.start":
        options = body.get("options", {})
        checks = [("filters", "filters.preview"), ("metadata", "metadata.apply"), ("archive", "archive.pack"),
                  ("compression", "compression.compress"), ("encryption", "encryption.encrypt")]
        for key, capability in checks:
            if options.get(key):
                return capability
        policy = options.get("fileTypes", {})
        if any(value != {"symlinks": "reject", "hardlinks": "copy_contents", "specialFiles": "reject"}.get(key) for key, value in policy.items()):
            return "fileTypes.inspect"
    if operation_id == "restore.start":
        if body.get("conflictPolicy", "require_empty") != "require_empty":
            return "restore.conflictPolicy"
        if body.get("metadata"):
            return "metadata.apply"
        if body.get("secretRef"):
            return "encryption.decrypt"
    return None


def openapi():
    paths = {}
    for operation in OPERATIONS:
        status = "202" if operation["async"] else "200"
        endpoint = {"operationId": operation["id"], "summary": operation["summary"], "tags": [operation["requirement"]],
                    "x-implemented": operation["implemented"], "x-requirement": operation["requirement"],
                    "security": [{"LocalSession": []}], "parameters": [],
                    "responses": {status: {"description": "成功（仅 x-implemented=true 当前可用）", "content": {"application/json": {"schema": ref(operation["response"])}}},
                                  **{code: {"description": description, "content": {"application/json": {"schema": ref("Error")}}}
                                     for code, description in [("400", "参数不合法"), ("403", "本机会话或来源不合法"), ("404", "记录不存在"), ("501", "接口已预留但功能未实现") ]}}}
        for name in re.findall(r"\{([^}]+)\}", operation["path"]):
            endpoint["parameters"].append({"in": "path", "name": name, "required": True, "schema": STRING})
        if operation["id"] == "filesystem.directories":
            endpoint["parameters"].append({"in": "query", "name": "path", "required": False, "schema": PATH})
        if operation["request"]:
            endpoint["requestBody"] = {"required": True, "content": {"application/json": {"schema": ref(operation["request"])}}}
        paths.setdefault(operation["path"], {})[operation["method"].lower()] = endpoint
    return {"openapi": "3.1.0", "info": {"title": "BackNest Backend Contract", "version": VERSION,
            "description": "Reserved routes return 501, never simulated success. Archive/compression algorithms must be self-implemented; crypto libraries are allowed."},
            "servers": [{"url": "http://127.0.0.1:4173"}], "paths": paths,
            "components": {"schemas": copy.deepcopy(SCHEMAS), "securitySchemes": {"LocalSession": {"type": "apiKey", "in": "header", "name": "X-BackNest-Token"}}}}
