# Agent 配置提示词与 Codex 接入验收

本记录验证通用配置提示词能否引导 Agent 在独立用户工作区完成可复查的配置保存。模型测试保持关闭；本次没有发起模型请求。

## 实际步骤

通过项目虚拟环境 Python 运行 CLI。以下用 `<VENV_PYTHON>` 和 `<WORKSPACE_B>` 代指执行环境中的 Python 和隔离工作区，避免记录本机绝对路径：

```powershell
<VENV_PYTHON> -m jp.cli --workspace <WORKSPACE_B> doctor --json
<VENV_PYTHON> -m jp.cli --workspace <WORKSPACE_B> configure --from <WORKSPACE_B>/config-candidate.yaml
<VENV_PYTHON> -m jp.cli --workspace <WORKSPACE_B> doctor --json
<VENV_PYTHON> -m jp.cli --workspace <WORKSPACE_B> agent-prompt
```

CLI 首次诊断初始化了默认 `manual` 工作区。候选配置只把 `llm.backend` 改为 `codex`，把 `llm.model` 设为用户指定的 `gpt-6.1-sol`，再由 `configure` 命令保存。没有传 `--test-model`，没有读取密钥、Codex 配置文件或其他工具设置。

第一次保存返回 `saved: true`、`tested: false`。再次保存同一候选后，`config.yaml` 的 SHA-256 保持不变；单个 `.bak` 文件会滚动保存本次覆盖前的配置，重复保存后它与当前配置相同。最终 `doctor --json` 返回 `status: configured`、`tested: false`，只确认 Codex CLI 登录状态，没有确认模型可用性。本次没有模型请求，也没有额度消耗。CLI 的 `start --help` 显示看板可由 `start` 子命令打开；没有实际启动服务。

## 提示词与说明检查

CLI 实际输出的提示词包含项目规则、配置指南和指定工作区路径，也保留了候选优先级、禁止扫描无关凭据、不向其他工具写配置、先验证再保存、测试额度提示和中文结果要求。当前源码工作区中的 `docs/agent-setup.md` 给出了 `--workspace` 参数位置、默认工作区初始化、无测试保存与 `doctor` 检查流程，足够完成这次已执行的 Codex 配置保存。

仍有两项说明缺口可供后续补齐：

- `jp/resources/agent-setup.md` 仍是较早的短版指南；安装 wheel 后 `settings.agent_prompt()` 会使用该资源文件，因而安装版 Agent 得不到源码指南中的完整 `--workspace` 流程和分步验证说明。应让打包指南与 `docs/agent-setup.md` 保持一致。
- 当前指南没有列出看板启动命令。CLI 帮助显示可以运行 `job-pipeline --workspace <工作区> start`；若提示词要求最终说明如何打开看板，建议把该命令写进指南。

## 验收边界

`gpt-6.1-sol` 只是写入的显式模型 ID，本次没有实际调用它，因此不能据此确认可用性。登录状态检查只表明当前 Codex CLI 已登录。后续真实虚构任务诊断应单独记录 `tested` 结果，不与本次配置保存混为一谈。
