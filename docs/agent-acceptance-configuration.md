# Agent 配置独立验收记录

验收日期：2026-10-07。操作系统：Windows。验收工作区位于独立 scratch 下的 `scratchpad/job-pipeline-public/agent-config-a/workspace`；候选文件位于同一任务目录。没有使用默认用户数据目录，也没有更改项目中的个人工作区或其他工具设置。

## 执行过程与结果

项目 CLI 当前没有 `initialize` 子命令。`doctor`、`agent-prompt` 和 `configure` 启动时都会执行 `Workspace.initialize()`，因此用默认 `doctor` 完成 CLI 实际初始化：

```powershell
python -m jp.cli --workspace <WORKSPACE> doctor --json
```

第一次结果为 `status: manual`、`tested: false`，来源列表为空。随后重复执行同一命令，对 `config.yaml`、`profile/constraints.yaml`、`profile/watchlist.yaml` 和 `data/facts_index.json` 比较 SHA-256，结果均未变化（`idempotent=True`）。事实索引为空，来源没有启用。

接着执行 `agent-prompt`，输出包含项目目录、`docs/agent-setup.md`、`AGENTS.md` 和独立工作区路径；本次按这些路径读取了配置指南和项目规则。它们指示只使用项目配置资源和明确指定的工作区，不读取其他工具配置或凭据文件。

从初始化后的默认配置在 scratch 中生成候选配置，只把后端设为 `codex`，模型设为 `gpt-6.1-sol`，没有启用 CN/HK 来源。候选文件没有密钥值。执行：

```powershell
python -m jp.cli --workspace <WORKSPACE> configure --from <CANDIDATE>
python -m jp.cli --workspace <WORKSPACE> doctor --json
```

`configure` 返回 `saved: true`、`tested: false`，来源仍为空。之后只重读候选文件的 SHA-256，没有输出其完整内容；重复导入后候选哈希不变，工作区配置哈希也保持不变，并生成有效旧配置备份。最终 `doctor` 返回 `status: configured`、`tested: false`，其脱敏检查结果显示 Codex CLI 登录状态可用。

## 验证边界

这次只验证了同一 Python/Codex CLI harness 下的隔离工作区初始化、默认诊断、源码模式提示词路径、配置导入、重复执行和备份行为；这不代表另一个厂商或品牌的独立 Agent 已运行。wheel 模式的资源回退路径已写入配置指南，本次没有构建或安装 wheel，因此未验证安装包中的指南与规则文件。选择的模型字符串是 `gpt-6.1-sol`，本次未向该模型发送请求，因此模型可用性、API 连接和结构化输出均未验证。没有执行抓取、真实岗位处理或来源连接测试。

首次 CLI 导入因当前 Python 环境缺少 `jieba` 而失败。为完成实际 CLI 验收，只将 `jieba` 安装到该任务的 E 盘 scratch 依赖目录，并通过 `PYTHONPATH` 使用；没有安装到全局 Python，也没有在仓库内生成临时文件。未读取 API key、环境变量值、Codex/Claude 私有配置、登录文件或 `auth.json`。`doctor` 只调用 Codex CLI 的登录状态检查，没有调用模型。
