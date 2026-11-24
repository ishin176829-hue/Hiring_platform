# BOSS 求职工作台

本地 Mac 单账号求职机器人：采集杭州、上海岗位，按关键词筛选，确认清单后批量执行，记录页面反馈。无需先提供简历或模型密钥。

## 已包含的功能

- 专用浏览器资料目录，扫码登录；不会读取你的日常 Chrome 登录资料。
- 按城市、关键词采集岗位详情，保存原始链接、职责、招聘者与采集时间；实际地点未确认的岗位不能进入执行清单。
- 岗位过滤、公司排除、多选、清单预览与内容固定。
- 三种独立动作：首次沟通（使用 BOSS 账号当前预设招呼）、自定义消息、发送账号中已有的指定名称附件简历。
- 默认演练；真实发送必须在网页确认。自定义消息不会默默降级成平台默认招呼。
- 去重、每日尝试上限、动作间隔、暂停与恢复、任务与事件记录。
- 中断后将未确认的发送记录为“待核对”，不自动重发。发起沟通与附件发送分别记录。
- CLI 与 OpenMausBot stdio MCP 管理入口；管理入口不提供替你确认名单的工具。

「简历匹配」可对已有岗位排序。启动服务前设置 `OPENAI_API_KEY` 和 `BOSS_MATCH_MODEL`，选择支持 JSON 输出的 Responses API 模型。在工作台粘贴并核对简历，设置偏好，再选择是否允许模型处理脱敏后的简历和岗位文本。原文可在工作台删除。[Responses API 文档](https://developers.openai.com/api/reference/resources/responses/methods/create)

## 运行

本机部署入口：[打开工作台](http://127.0.0.1:8771)。后台运行环境安装在 `~/Library/Application Support/BOSS-Hiring`，源码仍在当前项目中，避免 macOS 阻止启动项读取 Documents。

项目已安装依赖时，也可以手动运行开发服务：

```bash
bash scripts/run.sh
```

打开 [本地工作台](http://127.0.0.1:8771)。点击“打开登录浏览器”，在专用浏览器里扫码；若出现验证码，请直接在浏览器中完成。Mac 休眠时任务不能持续执行。

安装登录启动项：

```bash
bash scripts/install-launch-agent.sh
```

停止并移除登录启动项（数据保留）：

```bash
bash scripts/install-launch-agent.sh --uninstall
```

不要同时运行手动服务和启动项。默认端口 8771，仅监听本机；机器人状态可用 `.venv/bin/boss-hiring status` 查询。

## 第一次使用

1. 打开登录浏览器，完成你自己的 BOSS 登录。
2. 检查杭州、上海及关键词设置，点击开始采集。
3. 查看岗位详情、实际地点和招聘者，勾选候选岗位。
4. 选择动作。首次沟通会使用 BOSS 当前招呼设置，请先在 BOSS 中核对；自定义消息需填写完整发送文本；附件模式需要账号中已有附件的完整名称。
5. 先以演练模式创建清单并确认，检查逐项前置检查结果。
6. 确认需要实际发送时，新建真实清单，查看收件人和内容并勾选确认。任务只执行这份已固定的清单。
7. 在执行记录中核对结果。任务受阻或结果不明确时，先检查专用浏览器及平台记录；工具不会自动重发。

暂停会阻止后续项目；正在执行的单项无法撤回。解除暂停不会自动重跑原清单，需要选择继续执行。没有执行过的 pending 项目可继续；uncertain 项目保留给人工核对。

## OpenMausBot

本机 OpenMausBot 支持自定义 MCP。见 [接入说明](docs/openmausbot.md) 与 [可导入的 MCP 配置](integrations/openmausbot-mcp.json)。本项目不修改 OpenMausBot 已有聊天、机器人、定时任务或权限。

## 安装与开发

需要 Python 3.11 以上；开发环境使用 Python 3.12。已提交 uv.lock 固定实际依赖。

```bash
uv sync --extra dev --python 3.12 --cache-dir .cache/uv
PLAYWRIGHT_BROWSERS_PATH="$PWD/.cache/ms-playwright" .venv/bin/python -m playwright install chromium
.venv/bin/python -m pytest -q
```

如已安装 Google Chrome，适配器可使用它的程序文件配合本项目专用资料目录，仍不使用个人 Chrome profile。浏览器底层只做正常页面操作；不会绕过验证码或逆向私有接口。

## 数据与验证边界

后台部署的数据库、浏览器资料与访问令牌保存在 `~/Library/Application Support/BOSS-Hiring/state/`。已安装时，手动入口和管理命令默认使用同一目录；尚未安装时使用项目中忽略的 `.state/`。开发时可用 `BOSS_STATE_DIR` 显式选择隔离目录。目录权限 0700，令牌 0600。不要分享数据目录；令牌不会出现在命令参数、URL 或页面日志中。

本地合成页面测试覆盖采集、确认、演练、页面发送反馈、重复拦截与异常暂停；它们不是 BOSS 线上投递成功的证据。真实网页结构和登录状态可能变化；首次扫码后仍需小批实际验证采集与已确认动作。开发验证不实际联系招聘者或发送附件。

见 [简历匹配 PRD](docs/resume-job-matching-prd.md) 和 [接口约定](docs/api-contract.md)。
