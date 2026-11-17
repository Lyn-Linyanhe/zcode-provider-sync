#!/usr/bin/env python3
"""刷新模型信息快照。默认联网；--offline 明确使用已有缓存，不改写抓取日期。"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "builtin_model_rules.json"
ZCODE = Path("D:/zcode/resources/config/provider/zcode-builtin.json")
RAW = ROOT / ".iterate/model-cache/openrouter-models.json"
URL = "https://openrouter.ai/api/v1/models"
VENDORS = ("openai/", "anthropic/", "google/", "x-ai/", "deepseek/", "qwen/", "moonshotai/",
           "minimax/", "meta-llama/", "bytedance/", "baidu/", "stepfun-ai/", "z-ai/")


def validate(data):
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise ValueError("响应缺少 data 模型数组，保留上一份有效数据库")
    valid = [m for m in data["data"] if isinstance(m, dict) and isinstance(m.get("id"), str) and m["id"].strip()]
    if not valid:
        raise ValueError("模型列表为空，保留上一份有效数据库")
    return data


def atomic_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=1, allow_nan=False) + "\n"
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def fetch(*, offline=False, cache=RAW):
    if offline:
        envelope = json.loads(cache.read_text(encoding="utf-8"))
        if "payload" not in envelope or not envelope.get("fetchedAt"):
            raise ValueError("缓存缺少抓取日期，请联网刷新一次")
        return validate(envelope["payload"]), envelope["fetchedAt"]
    request = urllib.request.Request(URL, headers={"Accept": "application/json", "User-Agent": "zcode-provider-sync"})
    with urllib.request.urlopen(request, timeout=25) as response:
        if response.status != 200:
            raise ValueError(f"目录返回 HTTP {response.status}")
        raw = response.read(16 * 1024 * 1024 + 1)
    if len(raw) > 16 * 1024 * 1024:
        raise ValueError("目录响应过大")
    data = validate(json.loads(raw.decode("utf-8")))
    return data, datetime.now(timezone.utc).isoformat(timespec="seconds")


def build_rules(data):
    rules = []
    seen = set()
    for model in validate(data)["data"]:
        if not isinstance(model, dict):
            continue
        model_id = model.get("id", "")
        if not isinstance(model_id, str) or model_id in seen:
            continue
        vendor = next((v for v in VENDORS if model_id.startswith(v)), None)
        if vendor is None:
            continue
        provider = model.get("top_provider") or {}
        if not isinstance(provider, dict):
            provider = {}
        context = provider.get("context_length") or model.get("context_length")
        maximum = provider.get("max_completion_tokens")
        if isinstance(context, bool) or not isinstance(context, int) or context <= 0:
            continue
        properties = {"contextWindow": context}
        modalities = (model.get("architecture") or {}).get("input_modalities")
        if isinstance(modalities, list):
            # file 不等于原生 PDF 能力；只有目录明确标识 PDF 时才带入。
            properties["inputFormat"] = {"supportsImage": "image" in modalities,
                                          "supportsVideo": "video" in modalities}
            if "pdf" in modalities:
                properties["inputFormat"]["supportsPdf"] = True
        specs = {}
        if isinstance(maximum, int) and not isinstance(maximum, bool) and maximum > 0:
            specs["maxOutputTokens"] = {"max": maximum}
        base = model_id[len(vendor):]
        # batch/free 等变体单独保存，不与原型号取 max() 混出不存在的配额。
        rules.append({"modelMatch": re.escape(model_id),
                      "vendor": vendor.rstrip("/"), "display": base,
                      "sourceId": model_id, "sources": [URL],
                      "config": {"properties": properties, "optionSpecs": specs}})
        seen.add(model_id)
    if not rules:
        raise ValueError("没有有效的模型参数，保留上一份有效数据库")
    return rules


def refresh(*, offline=False, cache=RAW, output=OUT, zcode=ZCODE):
    data, fetched_at = fetch(offline=offline, cache=cache)
    supplemental = build_rules(data)
    if zcode.is_file():
        snapshot = json.loads(zcode.read_text(encoding="utf-8"))["config"]["modelConfigRules"]["modelRules"]
    elif output.is_file():
        snapshot = json.loads(output.read_text(encoding="utf-8")).get("zcodeSnapshot", [])
    else:
        snapshot = []
    if not isinstance(snapshot, list):
        raise ValueError("ZCode 内置规则格式异常，未替换数据库")
    result = {"asOf": fetched_at[:10], "fetchedAt": fetched_at, "sources": [URL, str(zcode)],
              "zcodeSnapshot": snapshot, "supplemental": supplemental}
    # 仅在完整数据集校验通过后替换；错误响应不会进入缓存。
    atomic_json(output, result)
    if not offline:
        atomic_json(cache, {"fetchedAt": fetched_at, "payload": data})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="明确使用已有有效缓存（不改抓取日期）")
    parser.add_argument("--output", type=Path, default=OUT)
    parser.add_argument("--cache", type=Path, default=RAW)
    args = parser.parse_args()
    try:
        result = refresh(offline=args.offline, output=args.output, cache=args.cache)
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(1, f"刷新失败：{type(exc).__name__}；未使用错误响应替换有效模型库。\n")
    print(f"更新 {len(result['supplemental'])} 个模型参数，抓取日期 {result['asOf']} → {args.output}")


if __name__ == "__main__":
    main()
