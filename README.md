# showdoc-mcp

专属 ShowDoc MCP 服务器（stdio）。基于实测的 ShowDoc HTTP API，提供项目/目录/页面的读取、单条写入与批量管理能力。

## 工具清单（11 个）

| 工具 | 说明 |
|---|---|
| `list_items` | 列出账号有权限的项目（item_id） |
| `get_item_tree` | 项目完整目录树 + 页面清单（查 cat_id / page_id / s_number） |
| `get_page` | 按 page_id 取页面（标题 + markdown 正文） |
| `create_catalog` | 创建目录（同名幂等，直接返回已有 cat_id） |
| `update_catalog` | 目录改名 / 改排序 |
| `delete_catalog` | 删除目录（非空目录拒绝执行，防内容孤儿化） |
| `save_page` | 创建/更新页面（page_id=0 新建，回查返回 page_id） |
| `delete_page` | 删除页面（进 ShowDoc 回收站，网页端可恢复） |
| `upload_markdown_dir` | 本地 markdown 目录批量上传成目录树（两级；标题取正文首个 `# 标题`；同名目录/同标题页面自动跳过或更新，可安全重跑；`dry_run` 预览） |
| `clear_item` | 清空项目全部页面与目录（默认 dry-run，必须 `confirm=true` 才执行） |
| `rename_numbered_catalogs` | 顶级目录「N名称」批量改为「N.名称」 |

所有涉及项目的工具 `item_id` 参数可省略，用 env 里的默认值。

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
uv run python scripts/smoke_test.py       # 冒烟测试（打真实环境，写操作只用临时目录，跑完即删）
```

协议层手动验证（模拟 MCP 客户端握手）：

```bash
(printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'; sleep 3) \
| SHOWDOC_BASE_URL=... SHOWDOC_COOKIE_TOKEN=... SHOWDOC_PHPSESSID=... uv run showdoc-mcp
```

## 实现说明

- Python + 官方 MCP SDK v1（`mcp>=1.28,<2`，FastMCP 装饰器），stdio 传输，日志只写 stderr。
- 认证：`cookie_token` + `PHPSESSID` cookie（实测可用）；`error_code=10101` 等登录失效特征会转成可操作的中文提示。
- 页面正文按 ShowDoc 约定 `is_urlencode=1`（内容先 quote 一次再表单编码）。
- 新建目录后通过回查 `catListGroup` 获取 cat_id（比解析 save 返回值可靠）；批量上传的幂等性基于「同名目录复用 + 同标题页面更新」，无需状态文件，中断后直接重跑。
