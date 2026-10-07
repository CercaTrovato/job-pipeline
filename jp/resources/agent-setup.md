# Agent 与运行工作区配置

Job Pipeline 首次使用时，配置文件位于用户数据目录的 `job-pipeline/config.yaml`；Windows 使用 `%LOCALAPPDATA%\job-pipeline`，也可用 `JP_WORKSPACE` 或 `--workspace <目录>` 指定独立目录。`Workspace.initialize()` 幂等创建配置、硬条件、公司 watchlist 和空事实索引；现有文件不会被覆盖。CLI 没有单独的 `initialize` 子命令，执行 `doctor`、`configure`、`agent-prompt` 等命令时会先自动初始化。默认后端是 `manual`，CN/HK 来源列表为空，因此不会调用模型或抓取来源。

先读提示词给出的 `AGENTS.md`、本指南和 `jp/resources/config.schema.json`，再查看默认配置。允许检查本项目配置、当前 Agent 已知的模型配置文件及用户明确提供的候选，只提取地址、模型 ID、协议及凭据引用等必要字段，不输出配置全文。不要遍历用户目录、简历、浏览器 Cookie 或认证目录来猜配置，不读取任何 `auth.json`。所有配置命令通过 `--workspace` 指向用户工作区。可以只读复用已有非敏感设置，不修改 Codex、Claude、浏览器、系统环境或其他工具的设置。

选择顺序固定为：用户明确指定 → 本项目已有有效配置 → 当前 Agent 可复用的配置 → 其他明确给出的候选。不要把现有有效 API 配置改成你偏好的 CLI。当前 Agent 已知的 API 地址、模型和凭据引用可以复用，不必要求用户重复手填；有多个同等可用候选时集中问一次。Codex 后端通过 CLI 自己的登录状态检查，不提取订阅令牌。`claude` 是手动任务包模式，不是 Claude Code 自动执行后端。没有可用自动后端时保留 manual，告诉用户还缺什么，不猜模型名或计费方式。

## 配置结构

配置完成后，用 `job-pipeline --workspace <工作区目录> start` 打开本地看板。缺少可用 Python 时提示用户安装 Python 3.11–3.13；已有 Python 但项目未安装时，可在源码目录运行 `python -m pip install .`。不要把 CLI 登录成功或配置保存成功报告为模型调用成功。没有终端但能操作电脑的 Agent 可以在看板的「手动配置模型」填写同样信息，点击「保存并测试连接」；只修改本项目设置。

配置采用 schema version 1，可参照 `jp/resources/config.schema.json`。`llm.backend` 支持 `manual`、`claude`、`codex`、`api`；`protocol` 支持 `responses`、`chat_completions`、`anthropic`。`base_url` 由用户提供的 API 服务填写。`sources.CN` 仅支持 `boss`、`nowcoder`、`watchlist`，`sources.HK` 仅支持 `linkedin`、`jobsdb`、`watchlist`。空列表代表关闭。

`budget.max_jobs` 限制每轮最多处理 100 条岗位，默认值为 3。`fetch.rate` 是各来源请求之间的等待秒数范围，`fetch.cooldown_hours` 是触发风控后的冷却时长。每个已开启的非 watchlist 来源都要在对应地区配置一个查询块或查询块列表。每块须提供非空 `keywords`，可选 `city`、正整数 `max_pages`、`page_size`、`detail_limit`、对象 `extra` 和 `track: intern|campus`。watchlist 公司清单独立放在 `profile/watchlist.yaml`，不需要地区关键词。

配置命令的全局 `--workspace` 参数放在子命令前。先初始化并检查默认状态，再把候选配置保存到工作区：

```powershell
job-pipeline --workspace <工作区目录> doctor --json
job-pipeline --workspace <工作区目录> agent-prompt
job-pipeline --workspace <工作区目录> configure --from <候选配置文件>
```

`--from` 指定待导入的 version 1 配置文件。未传 `--test-model` 时只做本地校验，不发送模型请求，成功后原子替换工作区配置并备份有效旧配置。要在保存前测试支持的模型后端，可运行：

```powershell
job-pipeline --workspace <工作区目录> configure --from <候选配置文件> --test-model
job-pipeline --workspace <工作区目录> doctor --json
```

只有 tester 返回 `status: configured` 且 `tested: true` 时才保存。测试失败或结果未确认会保留原配置；错误消息不应包含配置值。API 与 Codex 后端的 `--test-model` 会发送一条虚构的最小任务并校验 JSON Schema，因此可能消耗少量额度；手动模式没有模型测试，使用不带 `--test-model` 的命令完成本地校验。成功替换前会保留 `config.yaml.bak`；旧配置无效时拒绝覆盖。候选文件由调用者提供，不会被修改。

`backend: api` 必须填写 `model` 和 `base_url`。非 loopback 地址必须使用 HTTPS；HTTP 仅允许 `localhost`、`127.0.0.1` 或 IPv6 loopback。URL 不得包含用户名、密码、query 或 fragment。`backend: codex` 必须填写模型名。

API 协议中，`responses` 与 `anthropic` 发送 JSON Schema 结构化输出约束；`chat_completions` 使用 `response_format: {"type":"json_object"}`，并在提示中附上 schema 和 JSON 示例，以兼容只支持 JSON mode 的服务（例如 [DeepSeek JSON Output](https://api-docs.deepseek.com/guides/json_mode/)）。JSON mode 只保证 JSON 格式，不代替完整 schema 校验。所有协议的返回值都会在本地按任务包 schema 校验，analyze 引文还会与 JD 原文核对；协议错误不会自动切换服务商或协议。

## 凭据

凭据只支持 `env`、`keyring`、`session` 三种来源。默认 `env` 配置只保存环境变量名称 `JP_LLM_API_KEY`，运行时才读取其值。`keyring` 需要可用的系统凭据库，`credential.name` 使用 `service:account` 格式。`session` 由当前调用方在内存中提供，不能存入 YAML。无论哪种方式，都不得将 API key 保存到文件、命令行、日志或任务包；环境变量路径也只允许填写变量名，不是密钥值或文件路径。不得从 Codex/Claude 登录文件、浏览器配置或 `auth.json` 提取凭据。订阅 OAuth 登录态不是 API key，不能转换成或当作 API key 使用。缺少凭据或可选系统组件时，程序只给出不含密钥值的错误。

不要将凭据写入配置、命令行、任务包、日志或仓库，也不要读取 `auth.json`。配置错误仅提示字段位置，不回显配置值。`settings.configure()` 会先校验和运行可选连通性检查，再保留旧有效配置备份并原子替换；测试失败时原文件保持不变。

## Agent 工作方式

Agent 只处理明确交给它的岗位任务包，并校验任务包 schema、需求 ID、原文引用和事实引用。硬条件由规则引擎判定。人工审批、跳过和延后选择必须保留；不得代替用户投递。风险提示、验证码或账号异常出现时停止对应来源并遵守冷却时间。

「设置」页可复制 Agent 提示词；CLI 对应命令是 `job-pipeline --workspace <工作区目录> agent-prompt`。提示词包含项目路径、指南路径、项目规则路径和工作区路径。源码运行时使用仓库 `docs/agent-setup.md` 和 `AGENTS.md`；安装 wheel 后，项目代码目录位于 Python 的 `site-packages`，指南和规则应从已打包的 `jp/resources/agent-setup.md` 与 `jp/resources/AGENTS.md` 读取。通过 `settings.Workspace(path).runtime_config()` 可取得带绝对路径的 legacy 运行配置。

本仓库当前隔离配置验收记录见 [docs/agent-acceptance-configuration.md](agent-acceptance-configuration.md)。其中会区分本地配置校验、Codex CLI 登录状态和真实模型请求的验证范围。
