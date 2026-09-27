# 参与开发

欢迎提 issue 和 PR。这份文档说清怎么跑起来、代码怎么分、改动要守哪些规矩。

## 环境

- Windows 10/11，Python 3.10+（开发用 3.12）
- **仅标准库**：不引入任何第三方依赖（Tkinter、urllib、json 等够用）
- 无需安装步骤：`pythonw app.py` 或双击「获取模型列表.bat」

## 代码结构

| 文件 | 职责 |
|---|---|
| `app.py` | 全部界面（Tkinter 深色主题）：供应商列表、表单、模型行、下拉弹层、状态栏 |
| `sync.py` | 全部逻辑：拉取 /models、读写 `provider_config.json`、模型级连通测试、内置模型库匹配 |
| `tools/refresh_builtin_models.py` | 刷新内置模型快照 `builtin_model_rules.json` |

## 提 PR 前的验收规矩（重要）

这个工具会写 ZCode 的真实配置文件，所以：

1. **绝不写真实配置**：`C:\Users\<用户>\.zcode\v2\provider_config.json` 只许读。测试/探针一律用 `tempfile` 副本，`save_provider` 传 `path=` 指向副本；改动前后对真实文件做 SHA256 对比是惯例。
2. **不打印 API Key**：日志、截图、issue 里都不许出现真实 Key。
3. **行为契约不许回退**：关窗进程必须退出；不留后台服务/托盘；思考等级未改动时保留原 values；保存前 `.bak` 备份；下拉弹层点选不闪退、无原生白滚动条；窗口 1680×1040、最小 1280×820。
4. **自检**：`python -m py_compile app.py sync.py` 过，再真启动一次窗口确认存活后关闭。
5. UI 改动请附前后截图；对齐 ZCode 的样式以截图取色/实测为准。

## 提交

- 一个 PR 只做一件事；改动说明写清「改了什么、为什么、怎么验证的」
- 大改动先开 issue 讨论
- 适合上手的任务看 [`good first issue`](https://github.com/Lyn-Linyanhe/zcode-provider-sync/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22) 标签
