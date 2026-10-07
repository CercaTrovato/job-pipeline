# Job Pipeline v0.1.0 · 公开测试版

本地运行的求职工作台：使用自己的经历和求职条件，采集岗位、查看证据匹配并人工挑选。

## 本版能力

- 网页推荐使用通用 Agent 配置提示词；可交给具备本机操作能力的 Codex、Claude Code 或其他 Agent，手动向导作为备用入口。
- 支持文本 PDF/DOCX 本地提取、脱敏预览、经历草稿与用户确认。
- 保留岗位来源适配器、去重、硬条件筛选、任务包和事实引用校验。
- 支持 Responses、Chat Completions、Anthropic Messages，以及 Codex CLI 和手动任务包后端。
- 网页后台任务提供进度、停止和恢复。默认关闭采集来源，每批分析 3 个岗位。

## 安装

需要 Python 3.11–3.13。下载 Release 中的 wheel 后运行：

```console
python -m pip install job_pipeline-0.1.0-py3-none-any.whl
job-pipeline start
```

也可从源码安装 `python -m pip install .`。打开本地设置页，复制配置提示词交给正在使用的 Agent。

## 验证范围

Windows Python 3.12 安装和浏览器流程已实测。离线测试为 351 passed、1 skipped；跳过项是未分发的旧 OpenCLI 补丁辅助工具。已完成一次真实 Codex 最小 JSON/schema 请求。

[GitHub Actions](https://github.com/CercaTrovato/job-pipeline/actions/runs/37596223338) 的 Windows/macOS/Linux × Python 3.11/3.12/3.13 九个组合全部通过，覆盖依赖安装、测试、构建、doctor 和 demo。已修复英文 Windows 重定向终端无法输出中文诊断的问题。CI 不代表真实浏览器采集已实测。

macOS/Linux 实机浏览器、其他品牌 Agent harness、真实 API key 和招聘平台采集仍待验证。首次使用建议从离线 demo 和最小连接测试开始。这是预发布版本，暂不作为稳定版保证。

## 数据与边界

公开版本不包含个人实库、私有 Git 历史、认证文件和运行数据；岗位夹具已替换为合成样本。API 密钥不写普通配置，订阅登录令牌不作为 API key。不自动登录、处理验证码、发送消息或投递。

MIT 许可证。安装包和源码包的 SHA-256 见 Release 附件 `checksums.json`。
