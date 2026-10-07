# 岗位来源设置

来源默认关闭。先在「设置 → 岗位来源」选择地区、关键词、城市和来源，保存后再到「开始运行」启动小预算采集。

## 浏览器来源

Boss、牛客和 LinkedIn 通过 OpenCLI 复用用户浏览器登录态。安装方式和扩展来自 [OpenCLI 上游说明](https://github.com/jackwener/opencli)。npm 安装需要 Node.js 20.18.1 或以上：

```console
npm install -g @jackwener/opencli
opencli doctor
```

用户自行安装 Browser Bridge 扩展、打开浏览器并登录平台。多个浏览器 Profile 时，填写要使用的 Profile 别名或 contextId；工作台只对该调用传递 `--profile`，不改 OpenCLI 的全局默认设置。

连接检查只确认浏览器桥，不代表平台登录成功。平台登录和数据解析会在实际采集时验证。旧开发版依赖的 LinkedIn 本地补丁脚本未分发；不自动修补其他工具。语言或上游版本导致解析失败时，保留错误并核对来源兼容性。

## JobsDB 和官网 ATS

JobsDB 使用 HTTP 数据源。官网清单可以由用户的 Agent 按 `jp/adapters/watchlist` 的现有参数填写，支持 BambooHR、Workday、Pinpoint、WorkAtSea、飞书和智业等现有抓取器。不要把无法核实的 ATS 参数当作已连接。

在官网高级设置填写 `watchlist` 数组；每项包含公司名、CN/HK 地区、官网网址、ATS 名和该适配器需要的租户参数。不存在通用的所有公司参数，官网地址和租户需要逐家核实。

限速、增量和风控冷却仍生效。验证码、403/429 或账号异常会停止对应来源；不要通过改账户、代理或关闭冷却绕过。首版尚无各来源跨系统真实采集证据，当前状态见 PROGRESS.md。
