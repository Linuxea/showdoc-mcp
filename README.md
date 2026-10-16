# showdoc-mcp

专属 ShowDoc MCP 服务器（stdio）。基于实测的 ShowDoc HTTP API，提供项目/目录/页面的读取、单条写入与批量管理能力。

基于 MCP Python SDK v2（`mcp>=2.3`，`MCPServer` 装饰器 API）。SDK v2 的服务器同时兼容两个协议时代：
2026-07-28 新协议与老式 `initialize` 握手（ZCode、Claude Desktop 等现有客户端走的路径）都直接可用，无需任何配置。

## 工具清单（11 个）

工具名统一带 `showdoc_` 前缀（MCP 2026 命名指南：`{service}_{action}_{resource}`，避免与其他服务器撞名）。
风险列是 MCP ToolAnnotations 的浓缩：**R**=只读，**D**=破坏性，**I**=幂等（可安全重试）；全部工具均为闭环域（`openWorldHint=false`，只访问配置的那一个 ShowDoc 站点）。

| 工具 | 风险 | 说明 |
|---|---|---|
| `showdoc_list_items` | RI | 列出账号有权限的项目（item_id） |
| `showdoc_get_item_tree` | RI | 项目完整目录树 + 页面清单（查 cat_id / page_id / s_number） |
| `showdoc_get_page` | RI | 按 page_id 取页面（标题 + markdown 正文） |
| `showdoc_create_catalog` | I | 创建目录（同名幂等，直接返回已有 cat_id） |
| `showdoc_update_catalog` | I | 目录改名 / 改排序 |
| `showdoc_delete_catalog` | D | 删除目录（非空目录拒绝执行，防内容孤儿化） |
| `showdoc_save_page` | I | 创建/更新页面（page_id=0 新建，回查返回 page_id） |
| `showdoc_delete_page` | D | 删除页面（进 ShowDoc 回收站，网页端可恢复） |
| `showdoc_upload_markdown_dir` | I | 本地 markdown 目录批量上传成目录树（两级；标题取正文首个 `# 标题`；同名目录/同标题页面自动跳过或更新，可安全重跑；`dry_run` 预览） |
| `showdoc_clear_item` | D | 清空项目全部页面与目录（默认 dry-run，必须 `confirm=true` 才执行） |
| `showdoc_rename_numbered_catalogs` | I | 顶级目录「N名称」批量改为「N.名称」 |

所有涉及项目的工具 `item_id` 参数可省略，用 env 里的默认值。每个参数在 inputSchema 里带独立 description（`Annotated` + `Field`），模型不用猜字段含义。

> v0.1 → v0.2 是**破坏性改名**：工具名从 `list_items`/`get_page` 等改为带 `showdoc_` 前缀。旧会话里记的工具名需要重新发现。

## 注册（ZCode）

本地代码方式，写入 `~/.zcode/cli/config.json` 的 `mcp.servers`：

```json
"showdoc": {
  "command": "uv",
  "args": ["run", "--directory", "/path/to/showdoc-mcp", "showdoc-mcp"],
  "env": {
    "SHOWDOC_BASE_URL": "https://your-showdoc.example.com",
    "SHOWDOC_COOKIE_TOKEN": "<cookie_token>",
    "SHOWDOC_PHPSESSID": "<PHPSESSID>",
    "SHOWDOC_DEFAULT_ITEM_ID": "<item_id>"
  },
  "timeoutMs": 60000
}
```

推到 GitHub 后可改为无本地代码方式（uvx 直接从 git 构建临时环境运行）：

```json
"showdoc": {
  "command": "uvx",
  "args": ["--from", "git+https://github.com/<user>/showdoc-mcp", "showdoc-mcp"],
  "env": { "...同上..." },
  "timeoutMs": 60000
}
```

改完配置需重启会话（或 ZCode），在 Settings → MCP 确认 showdoc 已连接。

## 环境变量

| 变量 | 必填 | 说明 |
|---|---|---|
| `SHOWDOC_BASE_URL` | 是 | ShowDoc 站点根地址，如 `https://your-showdoc.example.com` |
| `SHOWDOC_COOKIE_TOKEN` | 是 | 登录 cookie（浏览器 F12 → 复制 cookie） |
| `SHOWDOC_PHPSESSID` | 是 | 登录会话 cookie |
| `SHOWDOC_DEFAULT_ITEM_ID` | 否 | 默认项目 id，工具省略 item_id 时使用 |
| `SHOWDOC_TRUST_ENV` | 否 | `1` 时走环境代理（默认忽略 `http_proxy`/`all_proxy`，内网直连） |

## cookie 过期怎么办

工具会返回「登录态已失效」的明确提示。恢复步骤：

1. 浏览器打开 ShowDoc 并确认已登录；
2. F12 → Network（或 Application → Cookies）复制 `cookie_token` 与 `PHPSESSID`；
3. 更新 `~/.zcode/cli/config.json` 里 `showdoc.env` 的两个值；
4. 重启会话。

## 开发与测试

```bash
uv sync                                   # 安装依赖
uv run python scripts/protocol_test.py    # 协议层自测（离线，不打真实环境）
uv run python scripts/smoke_test.py       # 冒烟测试（打真实环境，写操作只用临时目录，跑完即删）
```

`protocol_test.py` 用 SDK v2 的内存 `Client` 直连服务器，同时验证两种协议模式（`auto` = 2026-07-28 新协议、`legacy` = 老式 initialize 握手）：工具清单与命名、风险注解、参数级 description、缺配置时的错误封装（isError 而非协议错误）。

## 实现说明

- Python 3.12+，官方 MCP SDK v2（`mcp>=2.3,<3`，`MCPServer` 装饰器），stdio 传输，日志只写 stderr（stdout 专用于 MCP 协议）。
- HTTP 层用 `httpx2`（SDK v2 同款，`trust_env` 控制是否走环境代理）。注意 httpx2 用系统信任库校验 TLS：自建 CA 的 ShowDoc 站点需设 `SSL_CERT_FILE`。
- 每个工具声明 `ToolAnnotations`（readOnly / destructive / idempotent / openWorld=false），客户端可据此做确认弹窗与重试策略。
- 业务错误（含 cookie 失效 `error_code=10101`）统一转 `ToolError`：结果带 `isError=true`、消息可读可自纠，不带堆栈噪音；输入校验类问题也走 ToolError 而非协议错误（spec 2025-11-25 §Tools 建议）。
- OpenTelemetry 中间件默认开启（无 exporter 时零开销）；如需接入观测，`logfire.configure()` 或任意 OTel exporter 即可拿到每个工具调用的 span。
- 认证：`cookie_token` + `PHPSESSID` cookie（实测可用）。
- 页面正文按 ShowDoc 约定 `is_urlencode=1`（内容先 quote 一次再表单编码）。
- 新建目录后通过回查 `catListGroup` 获取 cat_id（比解析 save 返回值可靠）；批量上传的幂等性基于「同名目录复用 + 同标题页面更新」，无需状态文件，中断后直接重跑。
