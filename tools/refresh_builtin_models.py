#!/usr/bin/env python3
"""刷新内置模型库（builtin_model_rules.json）的生成脚本。

数据源：
1. OpenRouter 公开端点 /api/v1/models（无需 Key）——主流厂商在售模型的
   context_length / max_completion_tokens / 输入模态，实测值。
2. D:/zcode/resources/config/provider/zcode-builtin.json —— ZCode 自带的
   modelMatch 正则规则（权威，随 ZCode 更新）。

用法：直接运行本脚本，覆盖项目根目录的 builtin_model_rules.json。
（2026-09-27 由会话生成，直连失败时可加 -x http://127.0.0.1:7890 走代理。）
"""

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "builtin_model_rules.json"
ZCODE = Path("D:/zcode/resources/config/provider/zcode-builtin.json")
RAW = ROOT / ".iterate/round-7/openrouter-models.json"
URL = "https://openrouter.ai/api/v1/models"

VENDORS = ("openai/", "anthropic/", "google/", "x-ai/", "deepseek/", "qwen/", "moonshotai/",
           "minimax/", "meta-llama/", "bytedance/", "baidu/", "stepfun-ai/", "z-ai/")
SKIP_SUFFIX = (":free", ":online", ":search", ":thinking", ":extended")


def fetch() -> dict:
    if RAW.exists():
        return json.loads(RAW.read_text(encoding="utf-8"))
    result = subprocess.run(["curl", "-sS", "--max-time", "25", URL],
                            capture_output=True, timeout=30)
    if result.returncode != 0 or not result.stdout:
        result = subprocess.run(["curl", "-sS", "--max-time", "30",
                                 "-x", "http://127.0.0.1:7890", URL],
                                capture_output=True, timeout=40)
    data = json.loads(result.stdout)
    RAW.parent.mkdir(parents=True, exist_ok=True)
    RAW.write_bytes(result.stdout)
    return data


def main() -> None:
    or_data = fetch()
    zrules = json.loads(ZCODE.read_text(encoding="utf-8"))["config"]["modelConfigRules"]["modelRules"]
    groups: dict[tuple, dict] = {}
    for m in or_data.get("data", []):
        mid = m.get("id", "")
        vendor = next((v for v in VENDORS if mid.startswith(v)), None)
        if not vendor:
            continue
        base = mid[len(vendor):]
        if base.endswith(":batch"):
            base = base[: -len(":batch")]
        if base.endswith(SKIP_SUFFIX):
            continue
        tp = m.get("top_provider") or {}
        ctx = tp.get("context_length")
        mx = tp.get("max_completion_tokens")
        if not isinstance(ctx, (int, float)) or ctx < 4096:
            continue
        if mx is not None and (not isinstance(mx, (int, float)) or mx < 512):
            mx = None
        mods = set((m.get("architecture") or {}).get("input_modalities") or [])
        entry = groups.setdefault((vendor, base), {
            "ctx": int(ctx), "max": int(mx) if mx else None,
            "image": False, "video": False, "pdf": False})
        entry["ctx"] = max(entry["ctx"], int(ctx))
        if mx:
            entry["max"] = max(entry["max"] or 0, int(mx))
        entry["image"] |= "image" in mods
        entry["video"] |= "video" in mods
        entry["pdf"] |= "file" in mods

    supplemental = []
    for (vendor, base), spec in sorted(groups.items()):
        opt = {}
        if spec["max"]:
            opt["maxOutputTokens"] = {"max": spec["max"]}
        supplemental.append({
            "modelMatch": ".*" + re.escape(base) + "(?:[.\\-:/\\[].*)?",
            "vendor": vendor.rstrip("/"),
            "display": base,
            "config": {"properties": {
                "contextWindow": spec["ctx"],
                "inputFormat": {"supportsImage": bool(spec["image"]),
                                "supportsVideo": bool(spec["video"]),
                                "supportsPdf": bool(spec["pdf"])},
            }, "optionSpecs": opt},
        })

    out = {"asOf": "2026-09-27",
           "sources": [URL, str(ZCODE)],
           "zcodeSnapshot": zrules,
           "supplemental": supplemental}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"zcode={len(zrules)} supplemental={len(supplemental)} -> {OUT}")


if __name__ == "__main__":
    main()
