# 在 OpenMausBot 中管理 BOSS 求职工作台

1. 在同一台 Mac 启动工作台。默认地址为 `http://127.0.0.1:8771`。运行 `.venv/bin/boss-hiring status` 检查服务。
2. 在 OpenMausBot 0.1.173 的插件页面打开 MCP 服务器管理，选择「新增服务器」，将 [配置文件](../integrations/openmausbot-mcp.json) 的整段 JSON 粘贴到服务器配置输入框。将示例中的命令和数据目录换成本机部署路径，不需要复制 token。使用自定义端口时，在 `env` 里将 `BOSS_BASE_URL` 指向相同的本机端口。
3. 导入后启用 `boss-hiring`，将它开放给你自己的求职 Bot。随后在该 Bot 的本机所有者对话里询问「查看 BOSS 工作台状态」，确认状态与命令行一致。
4. 需要登录时请 Bot 调用 `open_login`，在 Mac 上打开的 BOSS 浏览器里扫码或通过平台验证。候选清单的内容与执行模式在工作台网页里核对和确认。

普通 Bot API 请求在受限会话中运行，无法调用自定义 MCP。管理操作在具有该 MCP 权限的所有者对话中进行。此项目提供导入配置，OpenMausBot 内的启用和 Bot 权限由所有者设置。

| 操作 | MCP 工具 | 命令行 |
| --- | --- | --- |
| 查询工作台状态 | `get_status` | `boss-hiring status` |
| 打开登录浏览器 | `open_login` | `boss-hiring login` |
| 按当前设置排队采集 | `collect_jobs` | `boss-hiring collect` |
| 发起简历匹配 | `start_resume_match` | 工作台「简历匹配」 |
| 查看匹配进度与摘要 | `get_resume_matches` | 工作台「简历匹配」 |
| 查看已采集岗位 | `list_jobs` | `boss-hiring jobs --city 上海 --q Agent` |
| 查看清单及确认状态 | `list_batches` | `boss-hiring batches` |
| 执行已确认的清单 | `run_approved_batch` | `boss-hiring run-batch 1` |
| 暂停任务 | `pause_worker` | `boss-hiring pause` |
| 解除全局暂停 | `resume_worker` | `boss-hiring resume` |
| 查看最近事件 | `get_events` | `boss-hiring events --limit 30` |

以上命令可使用 `.venv/bin/boss-hiring` 执行。查询和任务命令默认输出 JSON，加 `--json` 可使用单行格式。需要指定服务的数据目录时，可设置 `BOSS_STATE_DIR` 或在子命令后使用 `--state-dir`。令牌从运行服务生成的 `api-token` 文件读取。

`run_approved_batch` 按网页确认时固定的模式、动作和候选岗位执行。采集和执行提交成功时返回任务编号，完成情况可通过状态、清单和事件查看。解除暂停后，再调用一次执行工具即可继续已确认清单的未处理项。

使用简历匹配前，在工作台保存简历和偏好并选择是否允许发送脱敏片段到配置的模型。MausBot 的匹配摘要只展示岗位、推荐等级和待核对条件。简历原文与匹配依据在工作台查看。
