# 开源改造进度

2026-10-07：开始从本地项目白名单导出公开版本，不携带原 Git 历史或个人运行数据。

| 里程碑 | 状态 |
| --- | --- |
| 独立安装与演示 | 已实现，Windows Python 3.12 wheel 全新环境实测通过 |
| 通用 Agent 配置入口 | 已实现，两名独立 Codex 代理完成隔离配置流程 |
| 简历与模型接入 | 已实现，三协议 SDK 模拟验证，Codex 真实最小调用通过 |
| 网页完整流水线 | 已实现，Windows Edge 浏览器操作实测通过 |
| 发布验证 | 本机验证已完成，跨系统与真实平台/API 接入待实测 |

2026-10-07：用户已授权公开发布，准备创建 `CercaTrovato/job-pipeline` 并发布 v0.1.0 预发布版本。提交、CI 和 Release 结果将在核实后记录。
跨系统实测及真实模型/来源状态在发布验证时逐项记录，不沿用原项目历史测试数。

## 本次证据

- 全套离线测试：350 passed、1 skipped（旧的外部 OpenCLI 补丁辅助工具未分发；采集器测试仍运行）。
- 实际浏览器：复制提示词、简历脱敏、手动任务包、演示岗位、人工选择、来源保存、390px 布局；无页面脚本错误。
- 真实模型：仅一次 Codex `gpt-6.1-sol` 虚构最小 JSON/schema 测试，`configure --test-model` 返回 tested=true/saved=true。未读取认证文件或调用真实岗位分析。
- 三协议 API：官方 SDK 的 mock 请求和错误路径测试；未使用真实 OpenAI/Anthropic/DeepSeek API key。
- wheel：源码目录之外的新环境安装，`doctor`、`demo`、资源指南及模板渲染验证通过。
- 两名配置验收代理均使用 Codex harness；不是对 Claude Code 或其他品牌 harness 的实测。详见两份 agent-acceptance 报告。
- 个人实库、原 Git 历史、个人配置、内部交接资料未复制；adapter fixtures 已替换为合成数据。
- 实际平台采集本次未执行。macOS/Linux、Python 3.11/3.13 的 CI 矩阵已写入，但没有远端运行证据。首版只能称为发布候选，不能宣称三系统实机验证完成。

## 后续发布检查

1. 在 macOS/Linux 执行安装、离线测试及浏览器桥连接流程，补充各系统截图和来源结果。
2. 用使用者自己的 API 凭据，分别执行一次 Responses、Chat Completions、Anthropic Messages 最小测试。
3. 启用每个实际需要的岗位来源，按小预算验证登录、增量、限速与冷却；不要绕过验证码。
4. 用户决定远端与发布后再提交、推送或发版。现有 GitHub Actions 不代表已经运行。
