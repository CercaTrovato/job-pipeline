# Job Pipeline · 求职工作台

**把招聘信息放在一个地方，按你的要求筛选，再用你自己的 AI 比较岗位要求和经历。**

A local app for collecting jobs, comparing them with your experience, and deciding which ones to apply for.

[简体中文](README.md) · [English](docs/README.en.md) · [下载 v0.1.0 测试版](https://github.com/CercaTrovato/job-pipeline/releases/tag/v0.1.0) · [让 Agent 帮我安装](#agent-install)

## 它能帮你做什么？

- **整理岗位**：粘贴招聘内容，或配置来源后从招聘网站和公司官网收集岗位。
- **减少重复信息**：把重复岗位放在一起，保留原链接和判断原因。
- **按你的情况筛选**：设置目标城市、毕业年份、到岗天数和实习时长。
- **看懂匹配结果**：上传简历并确认提取的经历后，查看每个岗位有哪些要求与你的经历相关，哪些还需要补充。
- **自己作决定**：在看板里选择、跳过或稍后再看。申请和联系招聘方由你完成。

程序在你的电脑上运行，通过本地网页操作。下面是虚构岗位的演示画面：

![求职看板演示：查看岗位、匹配理由并人工选择](docs/screenshots/board-desktop.png)

## 开始之前

当前是 **v0.1.0 公开测试版**，提供 Python 安装包。Windows、macOS 和 Linux 都需要 **Python 3.11–3.13**。已有兼容版本可以直接使用；第一次安装可选 [Python 3.13 安装器](https://www.python.org/downloads/release/python-31316/)。本项目暂不支持 Python 3.14 或更高版本。

你可以先运行离线演示，**不用配置模型或填写 API key**。要分析自己的岗位，再接入模型；要采集招聘网站，再设置来源和浏览器连接。

<a id="agent-install"></a>
## 最省事的方式：让 Agent 帮你下载安装

把下面整段交给能读写文件、运行命令或操作电脑的 Codex、Claude Code 或其他 Agent。普通聊天网页如果不能操作本机文件，请使用后面的手动安装步骤。

```text
请帮我下载、安装并打开 Job Pipeline 求职工作台。
仓库：https://github.com/CercaTrovato/job-pipeline
下载：https://github.com/CercaTrovato/job-pipeline/releases/tag/v0.1.0

先阅读仓库 README.md、AGENTS.md 和 docs/agent-setup.md，再检查我的系统和现有 Python。
需要 Python 3.11–3.13，优先复用已有兼容版本。若缺少 Python，使用官方来源安装到用户目录；
遇到必须由我处理的系统权限时，说明具体步骤。不要覆盖其他项目的 Python 环境或改全局 PATH。

在我指定的目录，或磁盘空间足够的用户目录中，创建独立虚拟环境与工作区。
从 v0.1.0 Release 下载 job_pipeline-0.1.0-py3-none-any.whl 和 checksums.json，
核对 SHA-256 后安装；校验不一致时停止。记录程序、环境和数据分别保存在哪里。

先运行 doctor --json 和离线 demo，再启动本地看板，告诉我打开哪个网址。
不要启动真实岗位采集，不读取我的简历，不发送消息或投递。

如果我希望配置模型，优先保留本项目已有有效设置，再复用我正在使用的 Agent 的可用设置。
只读取必要的模型名称、地址、协议和凭据引用；不修改其他工具配置，不输出或复制登录令牌。
订阅登录不是通用 API key。缺少登录或密钥时告诉我缺什么，不替我注册、充值或切换计费方式。
如果我要求配置并测试模型，只做一次虚构内容的最小连接测试，说明会消耗少量额度，不批量调用。

最后用中文说明：安装是否成功、如何再次启动和退出、文件保存位置，以及还需要我做什么。
```

这里只让 Agent 安装和检查演示。安装成功后，在「设置 → 让我的 AI 帮我配置」复制针对当前工作区的模型配置提示词即可。

<a id="install"></a>
## 自己安装：复制几条命令

**还没有 Python？** Windows 用户打开上面的 Python 3.13 页面，在 Files 中选 **Windows installer (64-bit)**，安装时保留 Python Launcher（`py`）。macOS 用户选 **macOS installer**。Linux 用户通过发行版的软件包管理器安装兼容版本。已经安装 Python 的用户可跳过这一步。

打开终端，Windows 用 `py -3.13 --version`，macOS/Linux 用 `python3 --version` 检查版本。如果已有 3.11 或 3.12，Windows 示例中的 `-3.13` 可以改成你的版本；macOS/Linux 的 `python3` 应指向受支持的版本。

在终端用 `cd "你想保存程序的文件夹"` 切换目录，再复制下面对应的命令。这会在该文件夹创建一个独立环境，供 Job Pipeline 使用。

### Windows / PowerShell

```powershell
py -3.13 -m venv job-pipeline-env
.\job-pipeline-env\Scripts\python.exe -m pip install "https://github.com/CercaTrovato/job-pipeline/releases/download/v0.1.0/job_pipeline-0.1.0-py3-none-any.whl"
.\job-pipeline-env\Scripts\job-pipeline.exe start
```

### macOS / Linux

```bash
python3 -m venv job-pipeline-env
./job-pipeline-env/bin/python -m pip install "https://github.com/CercaTrovato/job-pipeline/releases/download/v0.1.0/job_pipeline-0.1.0-py3-none-any.whl"
./job-pipeline-env/bin/job-pipeline start
```

Linux 若提示缺少 `venv` 或 `ensurepip`，先安装与当前 Python 对应的虚拟环境组件，再重试。例如使用系统默认 Python 的 Ubuntu/Debian 用户可运行 `sudo apt install python3-venv`；单独安装的版本需对应组件，例如 `python3.13-venv`。[Ubuntu 官方说明](https://ubuntu.com/developers/docs/howto/python-setup/)

浏览器会打开本地看板。没有自动打开时，复制终端显示的网址到浏览器。使用期间保留终端窗口；结束时按 **Ctrl+C** 停止服务。

下次使用，在同一个文件夹执行最后一条 `start` 命令即可。端口被占用时，在后面加 `--port 5051`。

想从源码安装，可下载并解压 [源码包](https://github.com/CercaTrovato/job-pipeline/releases/download/v0.1.0/job_pipeline-0.1.0.tar.gz)，在其中另建虚拟环境，用该环境的 Python 执行 `-m pip install .`。例如 Windows 使用 `.\job-pipeline-env\Scripts\python.exe -m pip install .`；macOS/Linux 使用 `./job-pipeline-env/bin/python -m pip install .`。开发与高级用法见 [贡献说明](CONTRIBUTING.md)。

## 打开后，先做这几件事

1. **先看演示**：打开「设置 → 开始运行 → 导入离线演示」，再到「待挑选」看看虚构岗位。不调用模型。
2. **配置模型**：让 Agent 按网页提示词帮你配置，或在「手动配置模型」填写。保存前的连接测试会消耗少量模型额度。
3. **确认自己的经历**：上传文本型 PDF/Word 简历，或粘贴文字；检查提取内容，再确认哪些经历可以用于匹配。扫描 PDF 请先转成可复制的文字。
4. **添加岗位**：在「内推入口」粘贴招聘内容，或到「岗位来源」设置关键词、城市和来源。
5. **运行分析**：到「开始运行」启动任务，再去看板查看结果和理由。默认每批分析 3 个岗位，预算可调整。

匹配分供你排序和复核，不代表岗位一定适合，也不保证能获得面试。

## 我正在用的模型能接进来吗？

| 你已有的工具或服务 | 怎么用 |
| --- | --- |
| 已登录的 Codex CLI | 可作为自动分析后端，填写账号可用的模型名称。 |
| OpenAI、DeepSeek 或 Anthropic API | 在网页配置服务地址、模型名称和 API key 来源，或让 Agent 按说明配置。 |
| Claude Code 或其他有电脑操作能力的 Agent | 可以帮助下载安装和配置。本版不把 Claude Code 订阅登录当作自动分析后端；还可用手动任务包模式，详见配置指南。 |
| 暂时没有可用模型 | 先用离线演示、手动导入和看板整理岗位，之后再配置。 |

API 费用由你选择的服务商收取；聊天订阅与 API 额度不是一回事。[详细配置指南](docs/agent-setup.md)

## 岗位来源与数据保存

现有采集器包括 Boss 直聘、牛客、LinkedIn、JobsDB 和部分公司官网。采集默认关闭。浏览器来源需要 OpenCLI 工具和扩展，并由你手动登录平台；设置步骤见 [岗位来源说明](docs/sources.md)。

资料保存在本机用户目录。使用云模型时，你确认后的相关经历和招聘内容会发送给所选服务商处理；简历预览会遮盖常见敏感字段，请自行检查姓名等其他信息。密钥通过环境变量、系统凭据库或当前服务会话使用。

自定义数据目录时，把 `--workspace "目录"` 放在命令前面，例如 `job-pipeline --workspace "my-jobs" start`。使用虚拟环境时仍使用上面对应的完整程序路径。

## 当前验证到哪一步？

Windows 浏览器流程已实测；Windows/macOS/Linux × Python 3.11/3.12/3.13 的 **9 个 CI 组合**通过了安装、离线测试、构建和演示检查。macOS/Linux 真实浏览器、真实 API key 和招聘平台采集仍待逐项验证。[完整记录](docs/PROGRESS.md)

遇到问题，可以带上操作系统、复现步骤和脱敏错误到 [Issues](https://github.com/CercaTrovato/job-pipeline/issues) 反馈，不要上传真实简历、密钥或整个工作区。

## 关键词 / Search keywords

求职工具、招聘信息整理、岗位看板、简历匹配、AI 求职助手、实习、校园招聘。

Job search, job tracker, resume matching, AI job assistant, internships, campus recruitment, local-first application.

MIT 许可证。允许修改、分发和商用，使用时保留版权与许可证声明。
