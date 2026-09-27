# ZCode 获取模型列表

一次性选单：填 Base URL + API Key，拉取模型后下拉选择，写入 ZCode 的 `provider_config.json`。关掉窗口进程即退出，不常驻。

## 打开

- 桌面快捷方式「获取模型列表」
- 或双击本目录的 `获取模型列表.bat`
- 或 `pythonw app.py`

改完点窗口里的「保存」，再回 ZCode 设置页刷新。

## 内置模型库与自动补全

- 获取模型列表后，行内空缺的 上下文 / 最大输出 / 推理等级 会按下面的优先级自动补全（已填的值一律不动）：
  1. 跨供应商按 id 比对你已有的配置
  2. 内置模型库（`builtin_model_rules.json`）= ZCode 内置 modelMatch 规则（优先读安装目录的活文件）+ OpenRouter 实测参数快照（2026-09-27，213 个家族）
  3. 服务商目录元数据（/models 返回的 context_length 等）
  4. 都没有 → 上下文 128000，最大输出留空（用模型默认）
- 手动输入模型 ID（回车 / 焦点离开）走同一条链。
- 刷新内置库：`python tools/refresh_builtin_models.py`（直连 OpenRouter，失败自动走 127.0.0.1:7890 代理）。
