"""把 showdoc-mcp 的工具文档发布到 ShowDoc：每个 MCP 工具一个页面 + 项目概述。

- 工具清单/参数 schema/风险注解 从运行中的 MCPServer 在线读取（与代码永远一致）；
- 每个工具的说明、示例、注意项为 CURATED 里的手写内容；
- 幂等：按项目名复用 item，按名称复用目录，按 (cat_id, 标题) 更新页面，可反复重跑。

用法：SHOWDOC_* 环境变量就位后
  uv run python scripts/publish_docs.py [--item-id N]
不传 --item-id 时自动查找/创建名为 DOCS_ITEM_NAME 的项目。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mcp import Client  # noqa: E402

from showdoc_mcp import tools  # noqa: E402
from showdoc_mcp.tools import create_catalog, get_client, save_page  # noqa: E402

logging.basicConfig(level=logging.WARNING)

DOCS_ITEM_NAME = "showdoc-mcp 工具文档"

# 目录 -> [工具名]（顺序即文档顺序）
SECTIONS: list[tuple[str, list[str]]] = [
    ("01 读取工具", [
        "showdoc_list_items", "showdoc_get_item_tree", "showdoc_get_page",
        "showdoc_search_pages", "showdoc_get_page_history",
    ]),
    ("02 写入工具", [
        "showdoc_create_catalog", "showdoc_update_catalog", "showdoc_delete_catalog",
        "showdoc_save_page", "showdoc_delete_page",
    ]),
    ("03 回收站工具", [
        "showdoc_list_recycle", "showdoc_recover_page",
    ]),
    ("04 批量工具", [
        "showdoc_upload_markdown_dir", "showdoc_clear_item", "showdoc_rename_numbered_catalogs",
    ]),
]

# 每个工具的手写内容：intro 功能 / returns 返回 / example 示例（代码块）/ notes 注意项
CURATED: dict[str, dict] = {
    "showdoc_list_items": {
        "intro": "列出当前账号有权限访问的所有 ShowDoc 项目，返回 item_id 与项目名。**任何流程的第一步**：不确定目标项目的 item_id 时，先调用这个工具。",
        "returns": "项目清单（每行 `item_id + 名称 + 描述`）；账号下无可见项目时给出明确提示。单页上限 100 个。",
        "example": "调用：无参数\n\n```\n有权限的项目：\n- item_id=1: API文档示例\n- item_id=2: 数据字典示例\n```",
        "notes": ["无参数工具", "只读、幂等，可随意调用"],
    },
    "showdoc_get_item_tree": {
        "intro": "获取项目完整的目录树与页面清单，树中标注每个目录的 cat_id、排序号 s_number，以及每个页面的 page_id。**所有 cat_id / page_id 都从这里查**。",
        "returns": "树状文本（目录嵌套 + 页面明细 + 页面/目录计数）。页面超过 400 个时省略页面明细，只保留目录结构。",
        "example": "调用：`item_id=1`（省略则用默认项目）\n\n```\n项目 1「API文档示例」\n共 4 个目录 / 8 个页面\n[顶级页面] 说明 (page_id=1)\n├── 接口示例 (cat_id=1, s=1) [2 页面]\n│   · 用户注册 (page_id=4)\n│   · 用户登录 (page_id=5)\n└── 数据示例 (cat_id=3, s=2) [2 页面, 1 子目录]\n```",
        "notes": ["item_id 可省略（用 env 默认项目）", "只读、幂等"],
    },
    "showdoc_get_page": {
        "intro": "按 page_id 读取单个页面的完整内容（标题 + markdown 正文 + 元信息）。配合 `showdoc_search_pages` 或 `showdoc_get_item_tree` 拿到 page_id 后使用。",
        "returns": "页面标题、元信息行（page_id / cat_id / item_id / 作者）与 markdown 正文；空页面有占位提示。正文超过 30000 字符自动截断并注明总长。",
        "example": "调用：`page_id=12`\n\n```\n# 用户注册\n（page_id=12  cat_id=1  item_id=1  作者=admin）\n\n## 接口地址\nPOST /server/index.php?s=/api/user/register\n```",
        "notes": ["超长正文自动截断（3 万字符）", "只读、幂等"],
    },
    "showdoc_search_pages": {
        "intro": "按关键词搜索项目内的页面，**同时模糊匹配标题与正文**（不区分大小写）。找页面优先用这个，比翻目录树快得多；拿到 page_id 后用 `showdoc_get_page` 读正文。",
        "returns": "匹配页面的扁平清单（page_id / 所在目录 / 标题）；无匹配时提示更换关键词或改用 `showdoc_get_item_tree`。",
        "example": "调用：`keyword=部署`\n\n```\n项目 1 匹配「部署」的页面（2 个）：\n- page_id=14（cat_id=2）: 部署手册\n- page_id=23（顶级）: 运维杂记\n```",
        "notes": ["keyword 必填", "服务端 LIKE 匹配，不支持正则/分词", "只读、幂等"],
    },
    "showdoc_get_page_history": {
        "intro": "查某个页面的版本历史。ShowDoc 在**每次更新时自动保存一个历史版本**，每页最多保留最近 20 版（新版本会挤掉最老的）。适合回答「这页之前是什么内容 / 改了什么」。",
        "returns": "历史版本列表（page_history_id + 保存时间 + 内容预览前 120 字符），最新在前；页面从未更新过或不存在时给出提示。",
        "example": "调用：`page_id=12`\n\n```\n页面 page_id=12 的历史版本（2 个，最新在前）：\n- page_history_id=5  2026-10-17 02:12:49: # 用户注册 …接口地址 v2…\n- page_history_id=2  2026-10-16 18:11:02: # 用户注册 …接口地址 v1…\n```",
        "notes": ["历史从第一次更新才开始记录，初版不占历史", "只读、幂等"],
    },
    "showdoc_create_catalog": {
        "intro": "创建目录（`parent_cat_id=0` 为顶级，否则为子目录），可选排序号 s_number。**同名幂等**：同父目录下已存在同名目录时不重复创建，直接返回现有 cat_id。",
        "returns": "新目录的 cat_id；已存在时返回现有 cat_id 并注明「未重复创建」。",
        "example": "调用：`cat_name=部署指南, parent_cat_id=0, s_number=3`\n\n```\n已创建目录「部署指南」（顶级），cat_id=15\n```\n再次执行同样调用：\n```\n目录「部署指南」已存在 (cat_id=15)，未重复创建。\n```",
        "notes": ["幂等设计：批量建目录可安全重跑", "非破坏性（新增型）操作"],
    },
    "showdoc_update_catalog": {
        "intro": "编辑目录：改名和/或改排序号。cat_name / s_number 留空（0）表示不改对应项。cat_id 从 `showdoc_get_item_tree` 查。",
        "returns": "更新结果，列出实际变更项（旧名 → 新名 / 新 s_number）；无变更时注明。",
        "example": "调用：`cat_id=15, cat_name=部署与运维`\n\n```\n已更新目录 cat_id=15（名称「部署指南」→「部署与运维」）\n```",
        "notes": ["目录不存在时报错并提示先查目录树", "非破坏性、幂等"],
    },
    "showdoc_delete_catalog": {
        "intro": "删除目录。**非空目录（含子目录或页面）会拒绝执行**，防止页面被孤儿化——先删除其中内容，或改用 `showdoc_clear_item` 清空整个项目。",
        "returns": "删除成功确认；非空时报错并列出子目录/页面数量；不存在时提示核对。",
        "example": "调用：`cat_id=15`\n\n```\n已删除空目录「部署与运维」(cat_id=15)\n```\n目录非空时：\n```\n目录「部署与运维」(cat_id=15) 非空：0 个子目录、3 个页面。请先删除其中内容（showdoc_delete_page / showdoc_delete_catalog），或用 showdoc_clear_item 清空整个项目。\n```",
        "notes": ["破坏性操作（带 destructive 注解，客户端可能弹确认）", "空目录校验在删除前完成"],
    },
    "showdoc_save_page": {
        "intro": "创建或更新页面（markdown 正文）。`page_id=0`（默认）在指定目录下新建并回查返回新 page_id；传 page_id 则原地更新该页。**本项目最常用的写入工具**。",
        "returns": "新建：`已创建页面「标题」page_id=N（cat_id=X）`；更新：`已更新页面「标题」(page_id=N)`。回查失败时会提示用目录树确认。",
        "example": "调用：`page_title=部署手册, page_content=# 部署手册\\n\\n## Docker 方式\\n..., cat_id=15`\n\n```\n已创建页面「部署手册」page_id=23（cat_id=15）\n```\n更新已有页面：\n```\n已更新页面「部署手册」(page_id=23)\n```",
        "notes": ["幂等：同内容重复保存结果一致（注意每次更新都会生成一条历史版本）", "非破坏性（新增/覆盖型）", "更新时省略 cat_id 会保持页面原目录，显式传 cat_id 则移动页面"],
    },
    "showdoc_delete_page": {
        "intro": "删除页面。被删页面**进入 ShowDoc 回收站**（不是彻底删除），可在网页端恢复，更推荐用 `showdoc_recover_page` 直接恢复。",
        "returns": "删除确认（含页面标题与 page_id）。",
        "example": "调用：`page_id=23`\n\n```\n已删除页面「部署手册」(page_id=23)\n```",
        "notes": ["破坏性操作（destructive 注解）", "进回收站可恢复；彻底清除需在网页端清空回收站"],
    },
    "showdoc_list_recycle": {
        "intro": "列出项目回收站里的被删页面（page_id / 标题 / 删除时间，最新在前）。删除后的「后悔药」第一步，配合 `showdoc_recover_page` 使用。",
        "returns": "回收站清单；为空时明确提示。",
        "example": "调用：`item_id=1`（可省略）\n\n```\n项目 1 回收站（2 个页面，最新删除在前）：\n- page_id=23  2026-10-17 02:12:49: 部署手册\n- page_id=22  2026-10-17 02:09:50: 冒烟测试页面\n```",
        "notes": ["需要项目管理员权限（ShowDoc 侧限制）", "只读、幂等"],
    },
    "showdoc_recover_page": {
        "intro": "把页面从回收站恢复（撤销删除）。**注意 ShowDoc 的行为：恢复的页面落在顶级目录**，不会回到原来的目录——恢复后用 `showdoc_save_page`（带 cat_id）归位。",
        "returns": "恢复确认 + 归位提示；恢复后仍检测到页面在回收站时报错提示核对。",
        "example": "调用：`page_id=23`\n\n```\n已从回收站恢复页面 page_id=23（当前位于顶级目录，原目录不保留）。可用 showdoc_get_item_tree 确认位置，需要归位时用 showdoc_save_page 更新。\n```",
        "notes": ["恢复落顶级目录是 ShowDoc 服务端行为", "非破坏性、可重试"],
    },
    "showdoc_upload_markdown_dir": {
        "intro": "把本地 markdown 目录批量上传为 ShowDoc 目录树：一级子目录→顶级目录、二级子目录→子目录、根目录 .md→顶级页面。页面标题取正文首个 `# 标题`（无则用文件名），目录名数字前缀（如 `1快速开始`）作为排序号。",
        "returns": "统计（新建目录/复用目录/新建页面/更新页面）；dry-run 时输出完整执行计划。失败项单独列出，可重跑。",
        "example": "本地结构：\n```\ndocs/\n├── README.md\n├── 1快速开始/install.md\n└── 2进阶/部署.md\n```\n调用：`dir_path=/path/to/docs, dry_run=true`\n```\n[dry-run] 将上传 /path/to/docs：新建目录 2 个（已存在 0 个）、新建页面 3 个、更新页面 0 个。确认后用 dry_run=false 执行。\n  + 页面 [README]\n  + 目录 [0] 1快速开始\n  + 页面 [安装指南]\n  ...\n```",
        "notes": ["**幂等**：同名目录复用、同标题页面更新，中断后直接重跑", "仅支持两级目录，更深层级会报错", "建议先 dry_run 预览"],
    },
    "showdoc_clear_item": {
        "intro": "清空项目的全部页面与目录（**保留项目本身**）。默认 dry-run 只返回统计；必须显式 `confirm=true` 才真正删除。被删页面进回收站可恢复。",
        "returns": "dry-run：将删除的页面/目录数量统计；执行：删除进度（成功数/总数）与剩余数量，失败项列出。",
        "example": "调用：`item_id=1`（无 confirm）\n\n```\n[dry-run] 项目 1「API文档示例」将删除 8 个页面、4 个目录（项目本身保留）。\n确认无误请带 confirm=true 重新调用。\n```",
        "notes": ["**危险操作**（destructive 注解 + confirm 门闩双保险）", "删除顺序：先页面后目录，子目录优先", "执行前务必确认 item_id 正确"],
    },
    "showdoc_rename_numbered_catalogs": {
        "intro": "把顶级目录名「N名称」批量改为「N.名称」（数字前缀后加点，如 `1快速开始` → `1.快速开始`），统一排序命名风格。已符合格式的自动跳过。",
        "returns": "dry-run：重命名计划清单；执行：`旧名 -> 新名` 对照与成功数。",
        "example": "调用：`dry_run=true`\n\n```\n[dry-run] 将重命名 2 个顶级目录：\n  1快速开始 -> 1.快速开始\n  2进阶 -> 2.进阶\n```",
        "notes": ["只处理顶级目录，不动子目录", "幂等：已符合格式的跳过"],
    },
}

RISK_LABEL = {"read_only_hint": "R 只读", "destructive_hint": "D 破坏性", "idempotent_hint": "I 幂等"}


async def fetch_tool_specs() -> dict[str, dict]:
    async with Client(tools.mcp) as client:
        listing = await client.list_tools()
        return {t.name: t for t in listing.tools}


def param_table(spec) -> str:
    props = spec.input_schema.get("properties") or {}
    required = set(spec.input_schema.get("required") or [])
    if not props:
        return "无参数。"
    lines = ["| 参数 | 类型 | 必填 | 说明 |", "|---|---|---|---|"]
    for name, p in props.items():
        typ = (p.get("type") or "any") if isinstance(p, dict) else "any"
        lines.append(f"| `{name}` | {typ} | {'✓' if name in required else '—'} | {p.get('description', '')} |")
    return "\n".join(lines)


def risk_line(spec) -> str:
    ann = spec.annotations
    if ann is None:
        return ""
    marks = [label for key, label in RISK_LABEL.items() if getattr(ann, key, None)]
    closed = "闭环域（openWorldHint=false，仅访问配置的 ShowDoc 站点）" if ann.open_world_hint is False else ""
    parts = " · ".join(marks)
    return f"**风险注解**：{parts} · {closed}" if parts else closed


def tool_page_md(name: str, spec) -> str:
    cur = CURATED.get(name, {})
    lines = [f"# {name}", "", cur.get("intro", spec.description or ""), ""]
    risk = risk_line(spec)
    if risk:
        lines += [risk, ""]
    lines += ["## 参数", "", param_table(spec), ""]
    if cur.get("returns"):
        lines += ["## 返回", "", cur["returns"], ""]
    if cur.get("example"):
        lines += ["## 示例", "", cur["example"], ""]
    notes = cur.get("notes") or []
    if notes:
        lines += ["## 注意事项", ""] + [f"- {n}" for n in notes] + [""]
    lines += ["---", f"> 工具名：`{name}` · schema 由 publish_docs.py 从 MCPServer 在线生成，与代码同步"]
    return "\n".join(lines)


README_TEMPLATE = """# showdoc-mcp 项目概述

专属 ShowDoc 的 MCP 服务器（stdio）：让 AI agent（ZCode / Claude 等）直接读写 ShowDoc 的项目、目录与页面。

## 基本信息

- 版本：0.3.0 · Python 3.12+ · 官方 MCP SDK v2（双协议时代兼容：2026-07-28 与老式 initialize 握手均可）
- 传输：stdio；HTTP 层 httpx2；认证：`cookie_token` + `PHPSESSID`
- 工具总数：**15 个**（读取 5 / 写入 5 / 回收站 2 / 批量 3），全部带 `showdoc_` 前缀与风险注解

## 工具索引

> 风险标记：R=只读 · D=破坏性 · I=幂等；各工具详情见对应分类目录下的同名页面。

| 分类 | 工具（page_id） | 风险 | 一句话说明 |
|---|---|---|---|
{index_rows}

## 客户端注册（ZCode / Claude Desktop 等）

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

## 环境变量

| 变量 | 必填 | 说明 |
|---|---|---|
| `SHOWDOC_BASE_URL` | 是 | ShowDoc 站点根地址 |
| `SHOWDOC_COOKIE_TOKEN` | 是 | 登录 cookie（浏览器 F12 复制） |
| `SHOWDOC_PHPSESSID` | 是 | 登录会话 cookie |
| `SHOWDOC_DEFAULT_ITEM_ID` | 否 | 默认项目 id，工具省略 item_id 时使用 |
| `SHOWDOC_TRUST_ENV` | 否 | `1` 时走环境代理（默认内网直连） |

## 典型工作流

1. **找**：`showdoc_search_pages` 搜关键词（不知道项目先 `showdoc_list_items`）
2. **读**：`showdoc_get_page` 看正文；`showdoc_get_item_tree` 看结构
3. **写**：`showdoc_save_page` 更新 / 新建页面
4. **误删恢复**：`showdoc_list_recycle` → `showdoc_recover_page`
5. **批量迁移**：`showdoc_upload_markdown_dir`（先 dry_run 预览）

## 开发与测试

```bash
uv sync                                   # 安装依赖
uv run pytest tests/ -q                   # 单元测试（离线）
uv run python scripts/protocol_test.py    # 协议层自测（离线）
uv run python scripts/smoke_test.py       # 冒烟测试（真实环境）
uv run python scripts/publish_docs.py     # 重新生成本文档
```

> 本文档由 `scripts/publish_docs.py` 生成：工具参数 schema 从 MCPServer 在线读取，与代码保持同步。
"""

TOOL_BLURBS = {
    "showdoc_list_items": ("读取", "RI", "列出有权限的项目"),
    "showdoc_get_item_tree": ("读取", "RI", "项目目录树 + 页面清单"),
    "showdoc_get_page": ("读取", "RI", "按 page_id 读页面正文"),
    "showdoc_search_pages": ("读取", "RI", "关键词搜标题+正文"),
    "showdoc_get_page_history": ("读取", "RI", "页面版本历史"),
    "showdoc_create_catalog": ("写入", "I", "建目录（同名幂等）"),
    "showdoc_update_catalog": ("写入", "I", "目录改名/排序"),
    "showdoc_delete_catalog": ("写入", "D", "删目录（非空拒绝）"),
    "showdoc_save_page": ("写入", "I", "创建/更新页面"),
    "showdoc_delete_page": ("写入", "D", "删页面（进回收站）"),
    "showdoc_list_recycle": ("回收站", "RI", "回收站清单"),
    "showdoc_recover_page": ("回收站", "I", "从回收站恢复"),
    "showdoc_upload_markdown_dir": ("批量", "I", "本地 markdown 目录批量上传"),
    "showdoc_clear_item": ("批量", "D", "清空项目（需 confirm）"),
    "showdoc_rename_numbered_catalogs": ("批量", "I", "「N名称」批量加点"),
}


def readme_md(pids: dict[str, int]) -> str:
    rows = []
    for _, names in SECTIONS:
        for name in names:
            cat, risk, blurb = TOOL_BLURBS[name]
            rows.append(f"| {cat} | `{name}`（page_id={pids[name]}） | {risk} | {blurb} |")
    # 模板里含 JSON 配置块的花括号，不能用 str.format，用 replace
    return README_TEMPLATE.replace("{index_rows}", "\n".join(rows))


def page_map_by_cat(client) -> dict[tuple[str, str], int]:
    menu = (client.item_info(client.default_item_id) or {}).get("menu") or {}
    result: dict[tuple[str, str], int] = {}
    for p in menu.get("pages") or []:
        result[("0", p["page_title"])] = int(p["page_id"])
    stack = list(menu.get("catalogs") or [])
    while stack:
        node = stack.pop()
        for p in node.get("pages") or []:
            result[(str(node["cat_id"]), p["page_title"])] = int(p["page_id"])
        stack.extend(node.get("catalogs") or [])
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--item-id", type=int, default=0, help="目标项目 id；缺省时按名称查找/创建")
    args = parser.parse_args()

    client = get_client()
    specs = asyncio.run(fetch_tool_specs())
    missing = [n for _, names in SECTIONS for n in names if n not in specs]
    if missing:
        raise SystemExit(f"MCPServer 里没有这些工具: {missing}")

    item_id = args.item_id
    if not item_id:
        for it in client.my_items():
            if it.get("item_name") == DOCS_ITEM_NAME:
                item_id = int(it["item_id"])
                print(f"复用已有项目「{DOCS_ITEM_NAME}」 item_id={item_id}")
                break
    if not item_id:
        item_id = client.item_add(DOCS_ITEM_NAME, "showdoc-mcp MCP 服务器的工具文档（脚本自动生成）")
        print(f"已创建项目「{DOCS_ITEM_NAME}」 item_id={item_id}")
    client.default_item_id = item_id  # 后续工具省略 item_id 时用文档项目

    created_pages = 0
    pids: dict[str, int] = {}
    for section, names in SECTIONS:
        print(create_catalog(section, s_number=int(section[:2])))
        cat_tree = client.catalog_tree(item_id)
        from showdoc_mcp.client import catalog_id_map
        cat_id = int(catalog_id_map(cat_tree)[("0", section)])
        pmap = page_map_by_cat(client)
        for i, name in enumerate(names, 1):
            content = tool_page_md(name, specs[name])
            pid = pmap.get((str(cat_id), name), 0)
            print(save_page(name, content, cat_id=cat_id, page_id=pid, s_number=i))
            if not pid:
                pmap2 = page_map_by_cat(client)
                pid = pmap2.get((str(cat_id), name), 0)
            pids[name] = pid
            created_pages += 1

    # README 放顶级，工具索引带真实 page_id，同样幂等
    pmap = page_map_by_cat(client)
    pid = pmap.get(("0", "项目概述"), 0)
    print(save_page("项目概述", readme_md(pids), page_id=pid, s_number=0))
    created_pages += 1

    print(f"\n完成：{len(SECTIONS)} 个分类目录 + {created_pages} 个页面（项目 {item_id}）")
    print("查看：浏览器打开 ShowDoc → 项目「showdoc-mcp 工具文档」")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
