# Job Pipeline · 求职工作台

[源码仓库](https://github.com/CercaTrovato/job-pipeline) · [测试版下载](https://github.com/CercaTrovato/job-pipeline/releases) · [检查运行](https://github.com/CercaTrovato/job-pipeline/actions)

本地运行的岗位采集、证据匹配与人工挑选工具。个人资料保留在用户工作区；模型调用会把所选脱敏内容发送给你配置的服务。

## 安装与打开

需要 Python 3.11–3.13。在源码目录执行：

```console
python -m pip install .
job-pipeline start
```

打开终端显示的本机网址。安装代码不会自动登录、采集、调用模型或投递。

## 让你正在用的 Agent 帮忙配置

打开「设置」→「让我的 AI 帮我配置」→复制提示词，交给能操作本机文件的 Codex、Claude Code 或其他 Agent。
统一执行说明见 [docs/agent-setup.md](docs/agent-setup.md)。聊天网页没有本机操作能力时，请使用网页手动向导。
提示词的唯一模板是 `jp/resources/agent-prompt.txt`；网页与下面的命令都读取同一文件，避免说明与行为不一致：

```console
job-pipeline agent-prompt
```

```console
job-pipeline doctor --json
job-pipeline doctor --test-model
job-pipeline demo
```

连接测试仅使用虚构内容，会消耗少量模型额度。离线 demo 不需要密钥。

## 数据与来源

默认使用系统用户数据目录，可在命令前加 `--workspace <目录>` 指定独立工作区。
简历支持文本 PDF、DOCX 或手动粘贴。先确认提取内容，再生成和确认经历草稿，最后用于匹配。
采集来源默认关闭：Boss、牛客、LinkedIn、JobsDB 与官网 ATS。浏览器来源需要自行安装 OpenCLI 浏览器扩展并手动登录；账号、验证码和投递由用户处理。
安装步骤、官网清单与兼容边界见 [docs/sources.md](docs/sources.md)。

已有 resume-workflow 的用户可选择导入脱敏事实；原简历仓库不会被修改：

```console
job-pipeline import-facts --from <resume-workflow/facts目录>
```

这会替换工作区事实索引并保留备份，用户自行确认导入内容。高级命令通过 `job-pipeline legacy <原命令与参数>` 使用，例如 `legacy status`、`legacy recheck` 和 `legacy resume`。

## English

A local-first job research workspace: collect job listings, apply deterministic constraints, inspect evidence-based model matching, and make human decisions. Configure it through your existing computer-capable agent or the local setup wizard. No automatic applications or account hosting.

## 验证与贡献

当前验证状态见 [docs/PROGRESS.md](docs/PROGRESS.md)。未实测的平台或来源不能视为可用保证。
首版是公开测试版：Windows 浏览器已实测，Windows/macOS/Linux × Python 3.11/3.12/3.13 的 9 个 CI 组合均通过安装、离线测试和构建。macOS/Linux 真实浏览器、真实 API key 与招聘平台采集尚待逐项验证。
贡献说明见 [CONTRIBUTING.md](CONTRIBUTING.md)。MIT 许可；第三方依赖和平台内容各自遵循其授权与使用条件。
