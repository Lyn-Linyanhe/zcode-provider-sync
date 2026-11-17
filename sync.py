#!/usr/bin/env python3
"""Fetch remote model IDs and write ZCode personal provider_config.json."""

from __future__ import annotations

import copy
import hashlib
import json
from contextlib import contextmanager
import os
import re
import shutil
import socket
import ssl
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_CONFIG = Path.home() / ".zcode" / "v2" / "provider_config.json"
DEFAULT_CONTEXT_WINDOW = 128000
REQUEST_TIMEOUT_SEC = 8
FETCH_TOTAL_TIMEOUT_SEC = 35
# 每轮先尝试全部候选地址，再对暂时性错误重试；限制累计等待。
FETCH_RETRY_ATTEMPTS = 2
FETCH_RETRY_BACKOFF_SEC = 0.5

API_TYPES = {
    "chat": "openai-chat-completions",
    "openai-chat-completions": "openai-chat-completions",
    "chat-completions": "openai-chat-completions",
    "responses": "openai-responses",
    "openai-responses": "openai-responses",
    "anthropic": "anthropic-messages",
    "anthropic-messages": "anthropic-messages",
}

# R6-16：官方梯含 minimal（chat.toolbar.thoughtLevel.value.minimal=`极低`），头插补全。
REASONING_LADDER = ["minimal", "low", "medium", "high", "xhigh", "max", "ultra"]
REASONING_LABELS = {
    "": "未设置",
    "minimal": "极低",
    "low": "低",
    "medium": "中",
    "high": "高",
    "xhigh": "极高",
    "max": "最高",
    "ultra": "极致",
    # ZCode 内置规则里的非梯档位：界面显示中文，存储/传参仍是英文原值
    "none": "无",
    "disabled": "禁用",
    "enabled": "启用",
    "off": "关闭",
}

NON_CHAT_RE = re.compile(
    r"(embedding|embed|whisper|tts|dall-e|dalle|imagen|moderation|"
    r"rerank|bge-|text-embedding|transcribe|realtime|speech-to|"
    r"tts-1|davinci|babbage|ada-|codec)",
    re.I,
)

# R4-P1-10 [C-12]：模型 ID 入库前剥离换行/控制字符
MODEL_ID_CTRL_RE = re.compile(r"[\r\n\t\x00-\x1f\x7f]+")


class SyncError(Exception):
    pass


def config_revision(data: dict) -> str:
    raw = json.dumps(data, sort_keys=True, ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def validate_model_id(value: str) -> str:
    text = str(value).strip()
    if MODEL_ID_CTRL_RE.search(str(value)):
        raise SyncError("模型 ID 不能包含换行或控制字符")
    if len(text) > 200:
        raise SyncError(f"模型 ID 最多 200 个字符，当前为 {len(text)} 个；请核对完整 ID")
    return text


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(newurl)
        def origin(parts):
            return parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)
        if origin(old) != origin(new):
            raise SyncError("已拒绝跨源或协议降级重定向，避免向其他地址发送 API Key")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def safe_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    if parts.port:
        host += f":{parts.port}"
    return urllib.parse.urlunsplit((parts.scheme, host, parts.path, "", ""))


def _open_request(req, timeout):
    opener = urllib.request.build_opener(
        SafeRedirectHandler(), urllib.request.HTTPSHandler(context=ssl.create_default_context())
    )
    return opener.open(req, timeout=timeout)


@contextmanager
def _config_lock(path: Path):
    # 同一工具的并发保存使用 OS 文件锁；ZCode 不使用此锁，另做磁盘版本检查。
    lock_path = path.with_name(path.name + ".sync-lock")
    with lock_path.open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise SyncError("另一个窗口正在保存，请稍后重试") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def mask_key(key: str) -> str:
    if not key:
        return "(empty)"
    if len(key) <= 10:
        return key[:2] + "…"
    return f"{key[:6]}…{key[-4:]}"


def reasoning_from_values(values: list[str] | None) -> str:
    if not values:
        return ""
    for level in reversed(REASONING_LADDER):
        if level in values:
            return level
    return str(values[-1])


def values_from_reasoning(level: str) -> list[str] | None:
    """没有 reasoningValues 的旧调用方仍用前缀梯。UI 保存路径不再调它。"""
    if not level:
        return None
    if level in REASONING_LADDER:
        return REASONING_LADDER[: REASONING_LADDER.index(level) + 1]
    return [level]


def floor_from_values(values: list[str] | None) -> str:
    """梯上第一个出现的标准档；没有则 ""（不限）。"""
    present = values or []
    for level in REASONING_LADDER:
        if level in present:
            return level
    return ""


def default_from_values(values: list[str] | None) -> str:
    """列表末尾就是聊天新建会话用的档。没有列表才是「随最高勾选」。"""
    if values:
        return str(values[-1])
    return ""


def _order_for_chat(tokens, original, ceiling, default) -> list[str]:
    """标准档按梯序；非梯档锚在 original 里它前面那个仍保留的标准档之后（锚点已删则放最前）。

    default 非空且在列表里：values[-1] = default，锚在它后面的非梯档跟到它前面，不得成为末尾。
    default 为空：values[-1] = ceiling（ceiling 在列表里时）。这就是「随上限」的恢复，不是把上限从当前顺序里拿到末尾。
    去重：先按上述顺序，再把 tail 从前面的副本里拿掉、固定追到末尾。schema 拒绝重复。
    """
    orig = [str(v) for v in (original or [])]
    present: list[str] = []
    seen: set[str] = set()
    for token in tokens or []:
        text = str(token)
        if text in seen:
            continue
        seen.add(text)
        present.append(text)
    present_set = set(present)
    std_present = [level for level in REASONING_LADDER if level in present_set]
    std_index = {level: index for index, level in enumerate(std_present)}

    def anchor_of(token: str) -> int:
        try:
            index = orig.index(token)
        except ValueError:
            return -1
        anchor = -1
        for prev in orig[:index]:
            if prev in REASONING_LADDER and prev in present_set:
                anchor = std_index[prev]
        return anchor

    buckets: dict[int, list[str]] = {index: [] for index in range(-1, len(std_present))}
    for token in present:
        if token in REASONING_LADDER:
            continue
        buckets[anchor_of(token)].append(token)

    ordered: list[str] = []
    ordered.extend(buckets[-1])
    for index, level in enumerate(std_present):
        ordered.append(level)
        ordered.extend(buckets[index])

    tail = ""
    if default and default in ordered:
        tail = default
    elif ceiling and ceiling in ordered:
        tail = ceiling
    if not tail:
        return ordered

    followers: list[str] = []
    if tail in orig:
        followers = [
            item
            for item in orig[orig.index(tail) + 1 :]
            if item not in REASONING_LADDER and item in ordered and item != tail
        ]
    for item in followers:
        ordered.remove(item)
    ordered.remove(tail)
    for item in followers:
        if item not in ordered:
            ordered.append(item)
    ordered.append(tail)
    return ordered


def apply_reasoning_edit(
    original, floor, ceiling, default
) -> tuple[bool, list[str] | None]:
    """以原 values 为底裁剪/补档/重排。返回 (是否改动, 新列表或 None)。

    changed=False 时第二项只是拷贝，调用方必须回写 meta 里的原对象。
    上限为空 → (有原列表, None)，调用方删 reasoningLevel。
    """
    orig = [str(v) for v in (original or [])]
    loaded_floor = floor_from_values(orig)
    loaded_ceil = reasoning_from_values(orig)
    loaded_default = default_from_values(orig)
    floor = "" if floor is None else str(floor)
    ceiling = "" if ceiling is None else str(ceiling)
    default = "" if default is None else str(default)

    # 上限未设置：不读 floor，不读 default。
    if not ceiling:
        return (bool(orig), None)

    # ceiling 不在梯上：不和 floor 组合扩成标准段。
    if ceiling not in REASONING_LADDER:
        if ceiling == loaded_ceil and floor == loaded_floor and default == loaded_default:
            return (False, list(orig))
        if ceiling != loaded_ceil or floor != loaded_floor:
            return (True, [ceiling])
        return (True, _order_for_chat(orig, orig, ceiling, default))

    ceil_i = REASONING_LADDER.index(ceiling)
    if floor in REASONING_LADDER:
        floor_i = min(REASONING_LADDER.index(floor), ceil_i)
        floor = REASONING_LADDER[floor_i]
    else:
        floor_i = 0
        floor = ""

    if floor == loaded_floor and ceiling == loaded_ceil and default == loaded_default:
        return (False, list(orig))

    new_set = set(REASONING_LADDER[floor_i : ceil_i + 1])
    interval_same = floor == loaded_floor and ceiling == loaded_ceil
    if interval_same:
        tokens = list(orig)  # 只改默认档：不增不删
    else:
        if loaded_ceil in REASONING_LADDER:
            old_floor_i = (
                REASONING_LADDER.index(loaded_floor) if loaded_floor in REASONING_LADDER else 0
            )
            old_ceil_i = REASONING_LADDER.index(loaded_ceil)
            old_set = set(
                REASONING_LADDER[min(old_floor_i, old_ceil_i) : max(old_floor_i, old_ceil_i) + 1]
            )
        else:
            old_set = set()
        present = set(orig)
        to_add: list[str] = []
        for level in REASONING_LADDER[floor_i : ceil_i + 1]:
            if level in present:
                continue
            # 新进入旧区间之外，或就是用户选中的 floor/ceiling
            if level not in old_set or level == floor or level == ceiling:
                to_add.append(level)
        kept = [item for item in orig if not (item in REASONING_LADDER and item not in new_set)]
        tokens = kept + to_add

    # 默认档在新区间内但列表里没有：只补这一档，不补其它缺口
    if default in REASONING_LADDER and default in new_set and default not in tokens:
        tokens.append(default)
    if default in REASONING_LADDER and default not in new_set:
        default = ""

    ordered = _order_for_chat(tokens, orig, ceiling, default)
    if not ordered:
        return (True, None)  # schema 要求 values 非空；空列表改为删键
    return (True, ordered)


def _read_err(reason: str) -> str:
    """R4-P1-9/U-8：配置类报错统一包裹句式，保留原始原因。"""
    return f"ZCode 配置文件读取异常，为安全起见未做修改：{reason}"


def load_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise SyncError(f"找不到配置文件: {path}")
    try:
        # R4-P1-7/C-14：utf-8-sig 兼容记事本写出的 BOM；IO/编码异常包成中文 SyncError。
        data = json.loads(path.read_text(encoding="utf-8-sig"), parse_constant=lambda v: (_ for _ in ()).throw(ValueError("非法 JSON 数值")))
    except ValueError as exc:
        raise SyncError(_read_err(f"不是合法 JSON（{exc}）")) from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise SyncError(_read_err(f"文件无法读取（{exc}）")) from exc
    if not isinstance(data, dict) or data.get("schemaVersion") != 1:
        raise SyncError("ZCode 配置版本不受支持（schemaVersion 不是 1），为安全起见未做修改。")
    if not isinstance(data.get("config"), dict):
        raise SyncError(_read_err("缺少 config 对象"))
    return data


def _dict_or_none(value: Any) -> dict[str, Any]:
    """R4-P0-3 [C-1]：键存在但为 None/非 dict 时回退 {}，不让 .get() 穿透崩溃。"""
    return value if isinstance(value, dict) else {}


def provider_rules(data: dict[str, Any]) -> list[dict[str, Any]]:
    config = _dict_or_none(data.get("config") if isinstance(data, dict) else None)
    bucket = _dict_or_none(config.get("providerConfigRules"))
    rules = bucket.get("providerRules")
    if not isinstance(rules, list):
        raise SyncError(_read_err("缺少 providerConfigRules.providerRules"))
    return rules


def model_rules(data: dict[str, Any]) -> list[dict[str, Any]]:
    config = _dict_or_none(data.get("config") if isinstance(data, dict) else None)
    bucket = _dict_or_none(config.get("modelConfigRules"))
    rules = bucket.get("providerModelRules")
    if not isinstance(rules, list):
        raise SyncError(_read_err("缺少 modelConfigRules.providerModelRules"))
    return rules


def ensure_model_config_bucket(data: dict[str, Any]) -> None:
    # R4-P0-3 [C-1/A-8]：畸形键值（None/非 dict/非 list）在写入侧归位，保存不再裸抛。
    config = data["config"]
    bucket = _dict_or_none(config.get("modelConfigRules"))
    config["modelConfigRules"] = bucket
    if not isinstance(bucket.get("providerModelRules"), list):
        bucket["providerModelRules"] = []
    if not isinstance(bucket.get("manualProviderModelRules"), list):
        bucket["manualProviderModelRules"] = []
    if not isinstance(config.get("providerOrder"), list):
        config["providerOrder"] = []
    pcr = _dict_or_none(config.get("providerConfigRules"))
    config["providerConfigRules"] = pcr
    if not isinstance(pcr.get("providerRules"), list):
        pcr["providerRules"] = []


def model_detail(data: dict[str, Any], provider_id: str, model_id: str) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "id": model_id,
        "contextWindow": None,
        "reasoning": "",
        "reasoningValues": None,
        "reasoningFloor": "",
        "reasoningDefault": "",
        "reasoningMap": None,
        "maxOutputTokens": None,
        "supportsImage": False,
        "inputVideo": False,
        "inputPdf": False,
        "capStructured": False,
        "capWebSearch": False,
        "capMidConv": False,
        "enabled": True,
    }
    for rule in model_rules(data):
        if rule.get("providerId") != provider_id or rule.get("modelId") != model_id:
            continue
        cfg = rule.get("config") or {}
        props = cfg.get("properties") or {}
        detail["contextWindow"] = props.get("contextWindow")
        fmt = props.get("inputFormat") or {}
        detail["supportsImage"] = bool(fmt.get("supportsImage"))
        detail["inputVideo"] = bool(fmt.get("supportsVideo"))
        detail["inputPdf"] = bool(fmt.get("supportsPdf"))
        detail["capStructured"] = bool(props.get("supportsJsonSchemaOutput"))
        detail["capWebSearch"] = bool(props.get("supportsNativeWebSearch"))
        detail["capMidConv"] = bool(props.get("supportsMidConversationSystem"))
        if cfg.get("enabled") is False:
            detail["enabled"] = False
        option_specs = cfg.get("optionSpecs") or {}
        specs = option_specs.get("reasoningLevel") or {}
        values = specs.get("values")
        if isinstance(values, list) and values:
            detail["reasoningValues"] = [str(v) for v in values]
            detail["reasoning"] = reasoning_from_values(detail["reasoningValues"])
            detail["reasoningFloor"] = floor_from_values(detail["reasoningValues"])
            detail["reasoningDefault"] = default_from_values(detail["reasoningValues"])
        rmap = specs.get("map")
        # R6-2/A2：ZCode 的 map 是「单个 CEL 表达式字符串」（app.asar schema Tv=t.string()，
        # builtin 101 处全是 str）；旧版本工具写过 dict——读取侧两者都兼容，
        # dict 仅用于「未改动」保存时原样带回（_upsert_model_rule），不再新增。
        if isinstance(rmap, str) and rmap.strip():
            detail["reasoningMap"] = rmap
        elif isinstance(rmap, dict) and rmap:
            detail["reasoningMap"] = rmap
        max_spec = option_specs.get("maxOutputTokens") or {}
        if isinstance(max_spec.get("max"), (int, float)) and max_spec["max"] > 0:
            detail["maxOutputTokens"] = int(max_spec["max"])
        break
    return detail


def provider_summary(rule: dict[str, Any], data: dict[str, Any], *, include_key: bool = False) -> dict[str, Any]:
    cfg = rule.get("config") or {}
    api = cfg.get("api") or {}
    access = cfg.get("access") or {}
    model_ids = list(cfg.get("personalModelIds") or cfg.get("modelOrder") or [])
    api_key = access.get("apiKey") or ""
    provider_id = str(rule.get("providerId") or "")
    summary = {
        "providerId": provider_id,
        "providerName": rule.get("providerName"),
        "enabled": rule.get("enabled", True) is not False,
        "apiType": api.get("type"),
        "baseUrl": api.get("baseUrl"),
        "keyMasked": mask_key(str(api_key)),
        "models": [model_detail(data, provider_id, mid) for mid in model_ids],
        "modelCount": len(model_ids),
    }
    if include_key:
        summary["apiKey"] = api_key
    return summary


def list_provider_summaries(
    data: dict[str, Any], *, include_key: bool = False
) -> list[dict[str, Any]]:
    return [
        provider_summary(rule, data, include_key=include_key)
        for rule in provider_rules(data)
    ]


def find_providers(
    rules: list[dict[str, Any]],
    *,
    name: str | None = None,
    provider_id: str | None = None,
) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    for rule in rules:
        if provider_id and rule.get("providerId") == provider_id:
            hits.append(rule)
        elif name and rule.get("providerName") == name:
            hits.append(rule)
    unique: list[dict[str, Any]] = []
    seen: set[int] = set()
    for rule in hits:
        marker = id(rule)
        if marker in seen:
            continue
        seen.add(marker)
        unique.append(rule)
    return unique


def join_url(base: str, suffix: str) -> str:
    """R4-P1-10 [A-6]：base 带 ?key= 查询串/#fragment 时，路径追加到 path 段，
    查询串保持在末尾，不再拼出 ?key=k/models 这类坏 URL。"""
    parts = urllib.parse.urlsplit(base.strip())
    root = urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path.rstrip("/"), "", "")
    )
    tail = f"?{parts.query}" if parts.query else ""
    if parts.fragment:
        tail += f"#{parts.fragment}"
    return f"{root}/{suffix.lstrip('/')}{tail}"


def _base_path(base: str) -> str:
    return urllib.parse.urlsplit(base.strip()).path.lower()


def candidate_model_urls(base_url: str) -> list[str]:
    base = base_url.rstrip("/")
    # /v1 判定只看 path 段（R4-P1-10：带 ?key= 时不再误判）
    base_path = _base_path(base)
    urls = [join_url(base, "models")]
    if not base_path.endswith("/v1") and "/v1/" not in base_path + "/":
        urls.append(join_url(base, "v1/models"))
    out: list[str] = []
    seen: set[str] = set()
    for url in urls:
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def build_headers(api_type: str, api_key: str) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "User-Agent": "zcode-provider-sync/1.2",
    }
    if api_type == "anthropic-messages":
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
    headers["Authorization"] = f"Bearer {api_key}"
    return headers


def http_get_json(url: str, headers: dict[str, str], *, timeout: float = REQUEST_TIMEOUT_SEC) -> tuple[int, Any, str]:
    """单次 GET；重试与备用地址统一由 fetch_models 调度。"""
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with _open_request(req, timeout) as resp:
            raw = resp.read(8 * 1024 * 1024 + 1)
            status = getattr(resp, "status", 200)
        if len(raw) > 8 * 1024 * 1024:
            raise SyncError("模型列表响应超过 8 MiB")
    except urllib.error.HTTPError as exc:
        exc.close()
        return exc.code, None, ""  # 不显示服务器回显，防止错误正文包含凭据
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, ssl.SSLCertVerificationError):
            raise SyncError("TLS 证书验证失败（未重试，请检查证书链）") from exc
        if isinstance(reason, (TimeoutError, socket.timeout)):
            raise SyncError("连接或读取超时") from exc
        raise SyncError("网络连接失败（DNS、连接或 TLS 握手异常）") from exc
    try:
        return status, json.loads(raw.decode("utf-8")), ""
    except (ValueError, UnicodeDecodeError):
        return status, None, "响应不是合法 JSON"


def extract_model_records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        if isinstance(payload.get("data"), list):
            items = payload["data"]
        elif isinstance(payload.get("models"), list):
            items = payload["models"]
        elif isinstance(payload.get("data"), dict) and isinstance(
            payload["data"].get("data"), list
        ):
            items = payload["data"]["data"]
        else:
            items = []
    elif isinstance(payload, list):
        items = payload
    else:
        items = []

    records: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, str):
            model_id = item.strip()
            if model_id:
                records.append({"id": model_id})
            continue
        if not isinstance(item, dict):
            continue
        model_id = item.get("id") or item.get("name") or item.get("model")
        if not isinstance(model_id, str) or not model_id.strip():
            continue
        rec: dict[str, Any] = {"id": model_id.strip()}
        ctx = (
            item.get("context_length")
            or item.get("context_window")
            or item.get("max_context_length")
            or (item.get("top_provider") or {}).get("context_length")
        )
        if isinstance(ctx, (int, float)) and ctx > 0:
            rec["contextWindow"] = int(ctx)
        architecture = item.get("architecture") or {}
        modalities = architecture.get("input_modalities") or architecture.get("modality")
        supports_image = False
        if isinstance(modalities, list):
            supports_image = any(str(m).lower() in {"image", "vision"} for m in modalities)
        elif isinstance(modalities, str):
            supports_image = "image" in modalities.lower()
        if item.get("supports_image") is True:
            supports_image = True
        if supports_image:
            rec["supportsImage"] = True
        records.append(rec)
    return records


def fetch_models(base_url: str, api_key: str, api_type: str) -> tuple[str, list[dict[str, Any]]]:
    headers = build_headers(API_TYPES.get(api_type, api_type), api_key)
    urls = candidate_model_urls(base_url)
    pending = list(urls)
    errors: dict[str, str] = {}
    deadline = time.monotonic() + FETCH_TOTAL_TIMEOUT_SEC
    for attempt in range(FETCH_RETRY_ATTEMPTS):
        retry = []
        for url in pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                errors[url] = "本次获取已达到等待预算"
                continue
            try:
                status, payload, _ = http_get_json(url, headers, timeout=min(REQUEST_TIMEOUT_SEC, remaining))
            except SyncError as exc:
                errors[url] = str(exc)
                if not any(word in str(exc) for word in ("证书", "重定向", "MiB")):
                    retry.append(url)
                continue
            if status == 200 and payload is not None:
                records = extract_model_records(payload)
                if records:
                    return url, records
            reason = {
                200: "响应不是有效的模型列表", 401: "Key 未授权，请检查 API Key",
                403: "服务端拒绝访问，请检查权限或联系供应商",
                404: "端点不存在，请核对 Base URL", 429: "请求频率受限",
            }.get(status, "服务端错误" if status >= 500 else "请求未成功")
            errors[url] = f"HTTP {status}：{reason}"
            if status == 429 or status >= 500:
                retry.append(url)
        pending = retry
        if not pending:
            break
        if attempt + 1 < FETCH_RETRY_ATTEMPTS:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(FETCH_RETRY_BACKOFF_SEC, remaining))
    details = "\n".join(f"{safe_url(url)} → {reason}" for url, reason in errors.items())
    raise SyncError("获取模型列表失败：\n" + (details or "没有可尝试的地址"))


def _post_json(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: float = REQUEST_TIMEOUT_SEC,
) -> tuple[int, str]:
    raw = json.dumps(payload).encode("utf-8")
    req_headers = {
        **headers,
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(url, data=raw, headers=req_headers, method="POST")
    context = ssl.create_default_context()
    try:
        with _open_request(req, timeout) as resp:
            # R4-P0-1：200 时需解析应答结构，读足量字节（错误分支只需前 400 字符做摘录）。
            body = resp.read(65536).decode("utf-8", "replace")
            return getattr(resp, "status", 200), body
    except urllib.error.HTTPError as exc:
        body = exc.read()[:400].decode("utf-8", "replace") if exc.fp else ""
        return exc.code, body
    # P0-4：URLError/超时等网络异常不再包成 SyncError，原样上抛，
    # 由 test_connection 统一归类为中文原因（结构化返回）。


def _http_fail_reason(code: int) -> str:
    """P0-4：HTTP 状态码 → 中文原因映射。"""
    if code in (401, 403):
        return f"HTTP {code}，Key 无效或未授权"
    if code == 404:
        return f"HTTP {code}，端点不存在，检查 Base URL"
    if 500 <= code <= 599:
        return f"HTTP {code}，服务端错误"
    return f"HTTP {code}，请求被拒绝"


TEST_TIMEOUT_SEC = 15


def _looks_answered(payload: Any, keys: tuple[str, ...]) -> bool:
    """R4-P0-1：200 响应里能否解析出模型应答字段（content/choices/output 任一）。"""
    if isinstance(payload, str):
        return bool(payload.strip())
    if isinstance(payload, list):
        return any(_looks_answered(item, keys) for item in payload)
    if not isinstance(payload, dict):
        return False
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return True
        if isinstance(value, (list, dict)) and value:
            return True
    inner = payload.get("data")  # 少数网关把应答包一层 data
    if isinstance(inner, dict):
        return _looks_answered(inner, keys)
    return False


def test_connection(
    base_url: str, api_key: str, api_type: str, model_id: str = ""
) -> dict[str, Any]:
    """R4-P0-1：模型级连通测试——用当前行的模型 ID 发一次最小真实请求。

    返回 {"ok", "code", "endpoint", "elapsed", "reason"}，reason 为中文原因：
    model_id 为空 → 不发请求直接提示「请先添加模型」；
    401/403→Key 无效或未授权；404→端点不存在；超时→连接超时；
    连接被拒/DNS→网络不通或代理拦截；5xx→服务端错误；
    HTTP 200 但解析不出应答字段 → 响应结构异常。
    anthropic 基址以 /v1 结尾时不重复拼 /v1（A-7：不再出现 /v1/v1，
    404+ping 误报「端点可达」的旧逻辑已移除，正确 URL 必须真测）。不抛 SyncError。
    """
    started = time.monotonic()

    def result(
        ok: bool, code: int | None = None, endpoint: str = "", reason: str = ""
    ) -> dict[str, Any]:
        return {
            "ok": ok,
            "code": code,
            "endpoint": safe_url(endpoint) if endpoint else "",
            "elapsed": round(time.monotonic() - started, 2),
            "reason": reason,
        }

    base_url = (base_url or "").strip()
    api_key = (api_key or "").strip()
    model_id = (model_id or "").strip()
    if not base_url:
        return result(False, reason="请先填写 Base URL")
    if not api_key:
        return result(False, reason="请先填写 API Key")
    if not model_id:
        return result(False, reason="请先添加模型")
    if api_type not in API_TYPES.values() and api_type not in API_TYPES:
        return result(False, reason="请选择 API 格式")
    resolved = API_TYPES.get(api_type, api_type)
    headers = build_headers(resolved, api_key)

    base = base_url.rstrip("/")
    ends_v1 = _base_path(base).endswith("/v1")

    if resolved == "anthropic-messages":
        suffixes = ["messages"] if ends_v1 else ["v1/messages", "messages"]
        payload = {
            "model": model_id,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "hi"}],
        }
        answer_keys = ("content",)
    elif resolved == "openai-responses":
        suffixes = ["responses"] if ends_v1 else ["responses", "v1/responses"]
        payload = {"model": model_id, "input": "hi", "max_output_tokens": 16}
        answer_keys = ("output_text", "output", "content")
    else:
        suffixes = (
            ["chat/completions"]
            if ends_v1
            else ["chat/completions", "v1/chat/completions"]
        )
        payload = {
            "model": model_id,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1,
        }
        answer_keys = ("choices", "content")

    def network_reason(exc: BaseException) -> str:
        if isinstance(exc, (socket.timeout, TimeoutError)):
            return "连接超时"
        if isinstance(exc, urllib.error.URLError):
            inner = exc.reason
            if isinstance(inner, (socket.timeout, TimeoutError)) or "timed out" in str(inner).lower():
                return "连接超时"
            return "网络不通或代理拦截"
        return "网络不通或代理拦截"

    http_error: tuple[int, str] | None = None
    network = ""
    last_endpoint = ""
    for suffix in suffixes:
        url = join_url(base, suffix)
        last_endpoint = url
        try:
            status, body = _post_json(url, headers, payload, timeout=TEST_TIMEOUT_SEC)
        except (socket.timeout, TimeoutError):
            network = "连接超时"
            continue
        except urllib.error.URLError as exc:
            network = network_reason(exc)
            continue
        except OSError:
            network = "网络不通或代理拦截"
            continue
        if 200 <= status < 300:
            try:
                payload_json = json.loads(body)
            except json.JSONDecodeError:
                payload_json = None
            if _looks_answered(payload_json, answer_keys):
                return result(True, code=status, endpoint=url, reason="正常")
            return result(False, code=status, endpoint=url, reason="响应结构异常")
        if http_error is None:
            http_error = (status, url)
    if http_error is not None:
        code, url = http_error
        return result(False, code=code, endpoint=url, reason=_http_fail_reason(code))
    return result(False, endpoint=last_endpoint, reason=network or "网络不通或代理拦截")


def backup_config(path: Path) -> Path:
    # R4-P1-8 [A-5]：时间戳精确到毫秒 + 冲突递增，同秒多次保存不再互相覆盖。
    now = datetime.now()
    stamp = now.strftime("%Y%m%d-%H%M%S") + f"-{now.microsecond // 1000:03d}"
    dest = path.with_name(f"{path.name}.bak-{stamp}")
    counter = 0
    while dest.exists():
        counter += 1
        dest = path.with_name(f"{path.name}.bak-{stamp}-{counter}")
    shutil.copy2(path, dest)
    return dest


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise


def _upsert_model_rule(rules: list[dict[str, Any]], provider_id: str, rec: dict[str, Any]) -> None:
    model_id = rec["id"]
    context_raw = rec.get("contextWindow")
    context = context_raw
    if context not in (None, ""):
        try:
            context = int(context)
        except (TypeError, ValueError) as exc:
            raise SyncError("上下文窗口必须是正整数") from exc
        if context <= 0:
            raise SyncError("上下文窗口必须是正整数")

    if rec.get("reasoningChanged"):
        if "reasoningValues" in rec:
            # 勾选结果原样写入。None 或空列表表示用户清空，要删 reasoningLevel。
            # 禁止再调 values_from_reasoning，否则会把空勾选补成前缀梯。
            reasoning_values = rec.get("reasoningValues") or None
        else:
            reasoning_values = values_from_reasoning(str(rec.get("reasoning") or ""))
    else:
        reasoning_values = rec.get("reasoningValues")

    found = None
    for rule in rules:
        if rule.get("providerId") == provider_id and rule.get("modelId") == model_id:
            found = rule
            break
    if found is None:
        found = {"modelId": model_id, "config": {}, "providerId": provider_id}
        rules.append(found)

    cfg = found.setdefault("config", {})
    props = cfg.setdefault("properties", {})
    # 未填写/未改动的上下文不创建显式覆盖，避免把 ZCode 内置的 1M 等继承值降成 128K。
    if context not in (None, ""):
        props["contextWindow"] = int(context)
    elif rec.get("contextChanged") and "contextWindow" in props:
        props.pop("contextWindow", None)
    if rec.get("supportsImage"):
        props.setdefault("inputFormat", {})["supportsImage"] = True
    specs = cfg.setdefault("optionSpecs", {})
    if reasoning_values:
        # A2-1：整体替换 spec 时必须把旧 spec 的 map 原样带回（非 mapChanged 的保存
        # 不碰 map——否则任何别的行改动都会把该模型已配的映射抹掉）。
        # R6-2：map 的合法形态是「单个 CEL 表达式字符串」；旧 dict 原样带回不加工。
        new_spec: dict[str, Any] = {"values": list(reasoning_values)}
        if not rec.get("mapChanged"):
            old_spec = specs.get("reasoningLevel")
            if isinstance(old_spec, dict) and "map" in old_spec:
                new_spec["map"] = old_spec["map"]
        specs["reasoningLevel"] = new_spec
    elif rec.get("reasoningChanged") and "reasoningLevel" in specs:
        del specs["reasoningLevel"]
    # 编辑弹窗写入的扩展字段：输入类型 / 模型能力 / 推理参数映射（map 只能挂在有 values 的档位下）
    if rec.get("extrasChanged"):
        extras = rec.get("extras") or {}
        fmt = props.setdefault("inputFormat", {})
        fmt["supportsImage"] = bool(extras.get("image"))
        fmt["supportsVideo"] = bool(extras.get("video"))
        fmt["supportsPdf"] = bool(extras.get("pdf"))
        props["supportsJsonSchemaOutput"] = bool(extras.get("structured"))
        props["supportsNativeWebSearch"] = bool(extras.get("webSearch"))
        props["supportsMidConversationSystem"] = bool(extras.get("midConv"))
    if rec.get("mapChanged"):
        # R6-2：map 写入「单个 CEL 表达式字符串」（ZCode schema Tv=t.string()，strict）。
        # 兼容桥：旧 rec 里的 dict（历史版本工具产物）转成 JSON 对象字面量字符串——
        # 对象字面量本身是合法 CEL，求值结果即该对象，配置可被 strict schema 接受。
        rmap = rec.get("reasoningMap")
        rl = specs.get("reasoningLevel")
        map_text = ""
        if isinstance(rmap, str):
            map_text = rmap.strip()
        elif isinstance(rmap, dict) and rmap:
            map_text = json.dumps(rmap, ensure_ascii=False)
        if map_text:
            if isinstance(rl, dict) and rl.get("values"):
                rl["map"] = map_text
        elif isinstance(rl, dict) and "map" in rl:
            del rl["map"]
    if rec.get("maxChanged"):
        max_out = rec.get("maxOutputTokens")
        try:
            max_val = int(max_out) if max_out not in (None, "") else 0
        except (TypeError, ValueError):
            max_val = 0
        if max_val > 0:
            specs["maxOutputTokens"] = {"max": max_val}
        elif "maxOutputTokens" in specs:
            del specs["maxOutputTokens"]
    if not specs:
        cfg.pop("optionSpecs", None)


def _save_provider(
    *,
    name: str,
    base_url: str,
    api_key: str,
    api_type: str,
    models: list[dict[str, Any]],
    provider_id: str | None = None,
    enabled: bool = True,  # 仅新建供应商时生效；更新时保留原值（R4-P1-3 [A-2]）
    path: Path = DEFAULT_CONFIG,
    expected_revision: str | None = None,
) -> dict[str, Any]:
    name = name.strip()
    base_url = base_url.strip()
    api_key = api_key.strip()
    if not name:
        raise SyncError("请填写供应商名称")
    if not base_url:
        raise SyncError("请先填写 API 端点")
    if not api_key:
        raise SyncError("请先填写 API Key")
    if api_type not in API_TYPES:
        raise SyncError("请选择 API 格式")
    resolved_type = API_TYPES[api_type]

    cleaned: list[dict[str, Any]] = []
    seen: set[str] = set()
    for rec in models:
        # R4-P1-10 [C-12]：入库前 strip、剥离换行等控制字符、限长 200。
        model_id = validate_model_id(str(rec.get("id") or ""))
        if not model_id:
            continue
        if model_id in seen:
            raise SyncError(f"模型 {model_id} 重复，请合并或删除多余行")
        seen.add(model_id)
        cleaned.append({**rec, "id": model_id})

    data = load_config(path)
    loaded_revision = config_revision(data)
    if expected_revision is not None and loaded_revision != expected_revision:
        raise SyncError("配置已被其他窗口修改，未保存。请重新读取后再编辑")
    ensure_model_config_bucket(data)
    rules = provider_rules(data)
    provider = None
    created = False
    if provider_id:
        hits = find_providers(rules, provider_id=provider_id)
        if hits:
            provider = hits[0]
        else:
            # ID 失效时禁止退化为按名称更新，避免新建表单静默覆盖同名供应商。
            raise SyncError("供应商已不存在，请重新选择或新建一个不同名称的供应商")
    if provider is None and not provider_id:
        hits = find_providers(rules, name=name)
        if hits:
            raise SyncError(f"供应商「{name}」已存在，请从左侧选择后更新，或使用不同名称新建")
    if provider is None:
        provider_id = str(uuid.uuid4())
        provider = {
            "providerId": provider_id,
            "providerName": name,
            "enabled": enabled,
            "config": {
                "group": "standard-personal",
                "access": {"type": "api-key", "apiKey": api_key},
                "api": {"type": resolved_type, "baseUrl": base_url.rstrip("/")},
                "personalModelIds": [],
                "modelOrder": [],
            },
        }
        rules.append(provider)
        order = data["config"].setdefault("providerOrder", [])
        if provider_id not in order:
            order.insert(0, provider_id)
        created = True
    else:
        provider["providerName"] = name
        # R4-P1-3 [A-2]：更新既有供应商时保留原 enabled（UI 没有 enabled 编辑，
        # 无条件覆盖会把用户在 ZCode 里禁用的供应商悄悄重新启用）。
        cfg = provider.setdefault("config", {})
        cfg.setdefault("group", "standard-personal")
        cfg.setdefault("access", {}).update({"type": "api-key", "apiKey": api_key})
        cfg.setdefault("api", {}).update({"type": resolved_type, "baseUrl": base_url.rstrip("/")})
        provider_id = str(provider.get("providerId"))

    model_ids = [rec["id"] for rec in cleaned]
    cfg = provider.setdefault("config", {})
    cfg["personalModelIds"] = model_ids
    cfg["modelOrder"] = list(model_ids)

    existing_rules = model_rules(data)
    old_by_id: dict[str, dict] = {}
    for rule in existing_rules:
        if rule.get("providerId") == provider_id:
            old_id = str(rule.get("modelId") or "")
            if old_id in old_by_id:
                raise SyncError(f"配置含重复模型规则：{old_id}，请先在 ZCode 中整理")
            old_by_id[old_id] = rule
    final_rules = []
    used_origins = set()
    for rec in cleaned:
        origin = str(rec.get("origId") or rec["id"])
        if origin in old_by_id:
            if origin in used_origins:
                raise SyncError("多个模型行引用同一旧模型，请重新读取配置")
            used_origins.add(origin)
            rule = copy.deepcopy(old_by_id[origin])
            rule["modelId"] = rec["id"]
            final_rules.append(rule)
        _upsert_model_rule(final_rules, provider_id, rec)
    existing_rules[:] = [r for r in existing_rules if r.get("providerId") != provider_id] + final_rules

    if config_revision(load_config(path)) != loaded_revision:
        raise SyncError("保存期间配置发生变化，未写入。请重新读取后重试")
    backup = backup_config(path)
    if config_revision(load_config(path)) != loaded_revision:
        raise SyncError("保存期间配置发生变化，未写入。请重新读取后重试")
    atomic_write_json(path, data)
    return {
        "revision": config_revision(data),
        "ok": True,
        "created": created,
        "providerId": provider_id,
        "providerName": name,
        "backup": str(backup),
        "modelCount": len(model_ids),
        "providers": list_provider_summaries(data, include_key=True),
    }


def save_provider(*, path: Path = DEFAULT_CONFIG, expected_revision: str | None = None, **kwargs) -> dict[str, Any]:
    with _config_lock(path):
        return _save_provider(path=path, expected_revision=expected_revision, **kwargs)
