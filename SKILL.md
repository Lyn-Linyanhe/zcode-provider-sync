---
name: zcode-provider-sync
description: 打开一次性「获取模型列表」选单窗口，勾选后写入 ZCode。用户要 CCS 那种下拉/勾选、不要常驻、不要手填模型 ID 时使用。
disable-model-invocation: true
---

# 获取模型列表

官方设置页没有获取按钮。本技能打开一个一次性窗口：选模型用列表，关掉窗口进程就退出，不占后台。

项目已放在：

`E:\fascinating project\zcode-provider-sync`

## 打开

桌面快捷方式「获取模型列表」，或双击：

`E:\fascinating project\zcode-provider-sync\获取模型列表.bat`

或：

```bash
pythonw "E:/fascinating project/zcode-provider-sync/app.py"
```

不要后台起 HTTP 服务，不要托盘。

## 窗口里

1. 左边点已有供应商，或「新建」。
2. 确认名称、API 格式、端点、Key。
3. 点「获取模型列表」。
4. 搜索，从下拉选择模型。
5. 去 ZCode 设置页刷新。关掉本窗口即结束。

## 完成标准

窗口里能看到拉到的模型并写入；任务管理器里关窗后没有残留 python/pythonw。
