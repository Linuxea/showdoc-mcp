# AGENTS.md

ShowDoc MCP 服务器（stdio）。MCP Python SDK v2（`MCPServer` 装饰器 API，`mcp>=2.3,<3`），HTTP 层用 `httpx2`。Python 3.12+，一律用 `uv`（`uv sync` 装依赖；`uv run pytest ...` 跑测试）。无 lint / typecheck / CI 配置，验证手段就是下面两层离线测试。

## 命令

```bash
uv run pytest tests/ -q                 # Fake 单测（离线）+ live 集成测试（自动起 Docker 真实 ShowDoc，无 Docker 自动 skip）
uv run python scripts/protocol_test.py  # 离线协议自测（auto + legacy 双协议模式）
uv run python scripts/smoke_test.py     # 直调工具打真实 ShowDoc，需 SHOWDOC_* 环境变量就位
```

改完代码至少跑前两个。`smoke_test.py` 需要 `SHOWDOC_DEFAULT_ITEM_ID` 指向测试项目；写操作只碰临时目录，跑完即删。

`tests/test_live.py` 的 fixture 全自动管理测试环境：无 Docker 整层 skip；容器 `showdoc-test` 不存在时自动创建/安装/关注册验证码/注册账号（`mcp_pytest`），存在则直接复用；每个 session 用专属项目「pytest-live」（按名复用）并先 `clear_item` 清场，用例间唯一名字隔离、可反复跑。容器和数据卷 `showdoc-test-data` 会保留以便复用，彻底重置用 `docker rm -f showdoc-test && docker volume rm showdoc-test-data`。

`scripts/publish_docs.py` 不是测试：把工具文档发布到真实 ShowDoc 站点，幂等可重跑，别在无环境变量时执行。

## 结构

- `src/showdoc_mcp/client.py` — `ShowDocClient` HTTP 封装；所有接口与认证方式（`cookie_token` + `PHPSESSID` cookie）来自实测，业务错误抛 `ShowDocError`（登录态失效是 `ShowDocAuthError`，特征 error_code 10101/10102）
- `src/showdoc_mcp/tools.py` — 全部 15 个 MCP 工具，注册在模块级 `mcp = MCPServer(...)`；client 是 `get_client()` 惰性单例（`tools._client`）
- `src/showdoc_mcp/server.py` — stdio 入口（`showdoc-mcp` 命令 → `server:main`）
- `tests/test_tools.py` — Fake 单测：fixture 直接替换 `tools._client = FakeShowDocClient()`，测试结束重置为 `None`
- `tests/test_live.py` — 真实 ShowDoc 集成测试：session fixture 自管 Docker 环境（见「命令」节），断言基于 `client.page_info()` 等真实返回。**注意真实 API 的 cat_id 等字段是字符串**（Fake 返回 int），断言要 `int()` 转换

## 增删工具时必须同步的地方

1. `scripts/protocol_test.py` 的 `EXPECTED_TOOLS` 集合（漏改会 FAIL 并给出差集；check 消息里的「11 个工具」是陈旧文案，实际按集合断言）
2. `README.md` 的工具清单表（表格行数与开头的「15 个」计数）
3. `tests/test_tools.py` 的 import 列表 + `FakeShowDocClient` 需模拟相应 API 行为

## 硬性约定

- 工具名必须带 `showdoc_` 前缀（`{service}_{action}_{resource}`），避免与其他 MCP 服务器撞名
- stdout 专用于 MCP 协议，日志只能写 stderr
- 业务错误统一转 `ToolError`（结果 `isError=true`、消息可读），不抛协议错误；输入校验问题也走 ToolError
- 每个工具声明 `ToolAnnotations`（只读/破坏性/幂等/openWorld=false），参数用 `Annotated` + `Field` 带逐个 description

## ShowDoc API 坑（实测得来，别"修正"）

- 页面正文按 `is_urlencode=1` 约定：内容先 `quote` 一次再表单编码
- 新建目录拿不到 cat_id，必须回查 `catListGroup`（解析 save 返回值不可靠）
- httpx2 默认 `trust_env=False`（忽略环境代理变量，内网直连）；需走代理设 `SHOWDOC_TRUST_ENV=1`。自建 CA 的站点需设 `SSL_CERT_FILE`
- cookie 失效时返回体会变成非 JSON 或 error_code 10101，客户端封装已统一识别
