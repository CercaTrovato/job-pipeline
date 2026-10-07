# 开源改造进度

2026-10-07：开始从本地项目白名单导出公开版本，不携带原 Git 历史或个人运行数据。

| 里程碑 | 状态 |
| --- | --- |
| 独立安装与演示 | 已实现，本地 Windows wheel 实测和三系统 CI 安装/构建/演示通过 |
| 通用 Agent 配置入口 | 已实现，两名独立 Codex 代理完成隔离配置流程 |
| 简历与模型接入 | 已实现，三协议 SDK 模拟验证，Codex 真实最小调用通过 |
| 网页完整流水线 | 已实现，Windows Edge 浏览器操作实测通过 |
| 发布验证 | 三系统 Python 3.11–3.13 的 9 个 CI 组合通过，真实平台/API 接入仍待实测 |

2026-10-07：已公开发布。仓库：https://github.com/CercaTrovato/job-pipeline 。初次提交 `8ecd957`，Windows 中文终端修复 `7492444`。v0.1.0 预发布版本：https://github.com/CercaTrovato/job-pipeline/releases/tag/v0.1.0 。GitHub 已读回确认 draft=false、prerelease=true，发布于北京时间 2026-10-07 16:57:35；版本标签指向 `bd9446dddd4f4b5811ee2aeb442f63cb0fd41e6e`。

发布附件：wheel、源码 tar.gz、checksums.json，均为 uploaded。GitHub 返回的附件 SHA-256 与本地一致；发行包从版本对应的已跟踪源码构建，未包含缓存、实库或认证文件。后续这份进度记录的文档提交不改动已发布标签及附件。
跨系统实测及真实模型/来源状态在发布验证时逐项记录，不沿用原项目历史测试数。

## 本次证据

- 全套离线测试：351 passed、1 skipped（旧的外部 OpenCLI 补丁辅助工具未分发；采集器测试仍运行）。新增测试覆盖英文 Windows 重定向终端下的中文诊断输出。
- 实际浏览器：复制提示词、简历脱敏、手动任务包、演示岗位、人工选择、来源保存、390px 布局；无页面脚本错误。
- 真实模型：仅一次 Codex `gpt-6.1-sol` 虚构最小 JSON/schema 测试，`configure --test-model` 返回 tested=true/saved=true。未读取认证文件或调用真实岗位分析。
- 三协议 API：官方 SDK 的 mock 请求和错误路径测试；未使用真实 OpenAI/Anthropic/DeepSeek API key。
- wheel：源码目录之外的新环境安装，`doctor`、`demo`、资源指南及模板渲染验证通过。
- 两名配置验收代理均使用 Codex harness；不是对 Claude Code 或其他品牌 harness 的实测。详见两份 agent-acceptance 报告。
- 个人实库、原 Git 历史、个人配置、内部交接资料未复制；adapter fixtures 已替换为合成数据。
- GitHub Actions：[运行 37596223338](https://github.com/CercaTrovato/job-pipeline/actions/runs/37596223338) 的 9 个组合全部通过：Windows/macOS/Linux × Python 3.11/3.12/3.13。覆盖依赖安装、离线测试、包构建、doctor 和 demo。代码提交为 `749244410f4ae0a292ed08c8a240feb2557a1cef`；后续仅文档更新不代表增加了实机证据。
- 实际平台采集本次未执行。macOS/Linux 的真实浏览器和平台登录未实测，CI 通过不能代替这些验证。版本标记为公开测试版。

## 后续发布检查

1. 在 macOS/Linux 执行真实浏览器桥连接流程，补充各系统截图和来源结果；跨系统离线安装与构建已通过 CI。
2. 用使用者自己的 API 凭据，分别执行一次 Responses、Chat Completions、Anthropic Messages 最小测试。
3. 启用每个实际需要的岗位来源，按小预算验证登录、增量、限速与冷却；不要绕过验证码。
4. 后续代码变更需重新检查 CI；没有真实采集/API 证据前继续保留测试版定位。
