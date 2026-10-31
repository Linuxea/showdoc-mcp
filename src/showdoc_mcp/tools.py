"""showdoc MCP 工具集：读取 / 单条写入 / 批量操作。

所有静态配置来自环境变量（写在 MCP 客户端配置的 env 里）：
- SHOWDOC_BASE_URL           如 https://your-showdoc.example.com
- SHOWDOC_COOKIE_TOKEN       登录 cookie_token
- SHOWDOC_PHPSESSID          登录 PHPSESSID
- SHOWDOC_DEFAULT_ITEM_ID    缺省项目 id（工具的 item_id 参数可省略）

设计约定（对齐 MCP 2026 最佳实践）：
- 工具名带 showdoc_ 前缀，避免与其他 MCP 服务器撞名（list_items / get_page 太通用）；
- 每个工具声明 ToolAnnotations 风险注解（只读 / 破坏性 / 幂等 / 闭环域），
  供客户端做确认弹窗与重试策略；
- 参数用 Annotated 提供逐个 description，模型不用猜字段含义；
- 业务错误统一转 ToolError（isError 结果，消息可读可自纠），绝不抛协议错误。
"""

from __future__ import annotations

import functools
import logging
import os
import re
from pathlib import Path
from typing import Annotated, Any, Callable

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import __version__
from .client import (
    ShowDocClient,
    ShowDocError,
    catalog_id_map,
    find_catalog,
    flatten_catalogs,
)

logger = logging.getLogger("showdoc_mcp")

mcp = MCPServer(
    "showdoc",
    title="ShowDoc 文档服务器",
    instructions=(
        "ShowDoc 文档系统操作工具（项目/目录/页面的读取、写入、搜索与批量管理）。"
        "找页面优先用 showdoc_search_pages 搜索；不知道 item_id 时先用 showdoc_list_items；"
        "查目录结构用 showdoc_get_item_tree。误删页面可用 showdoc_list_recycle + showdoc_recover_page 恢复。"
        "批量操作 showdoc_upload_markdown_dir 建议先 dry_run=true 预览；"
        "showdoc_clear_item 是危险操作，必须显式 confirm=true 才会真正删除。"
    ),
    version=__version__,
)

_client: ShowDocClient | None = None


def get_client() -> ShowDocClient:
    global _client
    if _client is None:
        env = {
            "SHOWDOC_BASE_URL": os.environ.get("SHOWDOC_BASE_URL", ""),
            "SHOWDOC_COOKIE_TOKEN": os.environ.get("SHOWDOC_COOKIE_TOKEN", ""),
            "SHOWDOC_PHPSESSID": os.environ.get("SHOWDOC_PHPSESSID", ""),
        }
        missing = [k for k, v in env.items() if not v]
        if missing:
            raise ShowDocError(
                f"缺少环境变量: {missing}。请在 MCP 配置（如 ~/.zcode/cli/config.json 的 mcp.servers.showdoc.env）"
                "补齐后重启会话。"
            )
        _client = ShowDocClient(
            base_url=env["SHOWDOC_BASE_URL"],
            cookie_token=env["SHOWDOC_COOKIE_TOKEN"],
            phpsessid=env["SHOWDOC_PHPSESSID"],
            default_item_id=int(os.environ.get("SHOWDOC_DEFAULT_ITEM_ID") or 0),
            trust_env=os.environ.get("SHOWDOC_TRUST_ENV") == "1",
        )
    return _client


def _safe(fn: Callable) -> Callable:
    """把 ShowDocError 转成 ToolError（isError + 干净的中文提示，不带堆栈噪音）。"""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except ShowDocError as e:
            raise ToolError(str(e)) from None

    return wrapper


# --------------------------------------------------------------------- 元信息

# 闭环域注解：只和配置的那一个 ShowDoc 站点交互，openWorldHint 恒为 false。
# destructiveHint 默认是 true（协议悲观默认），除删除类外都显式压成 false。
_ANN_READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
_ANN_ADD = ToolAnnotations(destructive_hint=False, idempotent_hint=True, open_world_hint=False)
_ANN_DESTROY = ToolAnnotations(destructive_hint=True, open_world_hint=False)

# 参数级说明：同一语义只写一份，模型在 inputSchema 里逐参数可见。
ItemId = Annotated[int, Field(description="ShowDoc 项目 id；省略(0)时用配置的默认项目，不知道 id 先调 showdoc_list_items")]
CatId = Annotated[int, Field(description="目录 cat_id，从 showdoc_get_item_tree 查询")]
PageId = Annotated[int, Field(description="页面 page_id，从 showdoc_get_item_tree 查询")]
DryRun = Annotated[bool, Field(description="true 只预览计划不写库，确认后再用 false 执行")]


# --------------------------------------------------------------------- helpers


def _natural_key(name: str) -> tuple[int, str]:
    m = re.match(r"^(\d+)", name)
    return (int(m.group(1)) if m else 99, name)


def _first_heading(content: str, fallback: str) -> str:
    for line in content.splitlines():
        s = line.strip()
        if s.startswith("# "):
            t = s[2:].strip()
            if t:
                return t
    return fallback


def _menu_pages_by_cat(item_info: dict) -> dict[tuple[str, str], str]:
    """(cat_id, page_title) -> page_id，来自 item/info 的菜单树。"""
    menu = item_info.get("menu") or {}
    result: dict[tuple[str, str], str] = {}
    for p in menu.get("pages") or []:
        result[("0", p["page_title"])] = str(p["page_id"])
    for node, _ in flatten_catalogs(menu.get("catalogs") or []):
        for p in node.get("pages") or []:
            result[(str(node["cat_id"]), p["page_title"])] = str(p["page_id"])
    return result


def _menu_counts(item_info: dict) -> tuple[int, int]:
    """(目录数, 页面数) 含顶级页面。"""
    menu = item_info.get("menu") or {}
    cats = sum(1 for _ in flatten_catalogs(menu.get("catalogs") or []))
    pages = len(menu.get("pages") or []) + sum(
        len(node.get("pages") or []) for node, _ in flatten_catalogs(menu.get("catalogs") or [])
    )
    return cats, pages


def _render_tree(item_info: dict, max_pages: int = 400) -> str:
    menu = item_info.get("menu") or {}
    cats = menu.get("catalogs") or []
    n_cats, n_pages = _menu_counts(item_info)
    show_pages = n_pages <= max_pages
    lines = [f'项目 {item_info.get("item_id")}「{item_info.get("item_name")}」',
             f"共 {n_cats} 个目录 / {n_pages} 个页面"]
    for p in menu.get("pages") or []:
        lines.append(f"[顶级页面] {p['page_title']} (page_id={p['page_id']})")

    def render(nodes: list[dict], prefix: str) -> None:
        for i, node in enumerate(nodes):
            last = i == len(nodes) - 1
            branch = "└── " if last else "├── "
            subs = node.get("catalogs") or []
            pages = node.get("pages") or []
            counts = f"[{len(pages)} 页面" + (f", {len(subs)} 子目录" if subs else "") + "]"
            lines.append(
                f"{prefix}{branch}{node['cat_name']} (cat_id={node['cat_id']}, s={node.get('s_number')}) {counts}"
            )
            child_prefix = prefix + ("    " if last else "│   ")
            if show_pages:
                for p in pages:
                    lines.append(f"{child_prefix}· {p['page_title']} (page_id={p['page_id']})")
            render(subs, child_prefix)

    render(cats, "")
    if not show_pages:
        lines.append(f"（页面超过 {max_pages} 个，已省略页面明细；目录结构完整）")
    return "\n".join(lines)


# ------------------------------------------------------------------------ 读


@mcp.tool(name="showdoc_list_items", title="列出项目", annotations=_ANN_READ)
@_safe
def list_items() -> str:
    """列出当前账号有权限的 ShowDoc 项目（item_id + 名称）。不确定 item_id 时先调这个。"""
    items = get_client().my_items()
    if not items:
        return "（该账号下没有可见项目）"
    lines = []
    for it in items:
        desc = it.get("item_description") or ""
        lines.append(f'- item_id={it["item_id"]}: {it["item_name"]}' + (f"（{desc}）" if desc else ""))
    tail = "\n（已达单页上限 100 个，若有更多项目需在网页端确认）" if len(items) >= 100 else ""
    return "有权限的项目：\n" + "\n".join(lines) + tail


@mcp.tool(name="showdoc_get_item_tree", title="项目目录树", annotations=_ANN_READ)
@_safe
def get_item_tree(item_id: ItemId = 0) -> str:
    """获取项目完整目录树与页面清单（cat_id / page_id / s_number 都在这里查）。item_id 省略用默认项目。"""
    client = get_client()
    item_id = client.resolve_item(item_id)
    info = client.item_info(item_id)
    return _render_tree(info)


@mcp.tool(name="showdoc_get_page", title="读取页面", annotations=_ANN_READ)
@_safe
def get_page(page_id: PageId) -> str:
    """按 page_id 获取页面内容（标题 + markdown 正文，可截断）。page_id 从 showdoc_get_item_tree 查。"""
    info = get_client().page_info(page_id)
    content = info.get("page_content") or "（空页面）"
    if len(content) > 30000:
        content = content[:30000] + f"\n\n...[正文共 {len(info['page_content'])} 字符，已截断]"
    meta = (
        f"page_id={info.get('page_id')}  cat_id={info.get('cat_id')}  "
        f"item_id={info.get('item_id')}  作者={info.get('author_username')}"
    )
    return f"# {info.get('page_title')}\n（{meta}）\n\n{content}"


@mcp.tool(name="showdoc_search_pages", title="搜索页面", annotations=_ANN_READ)
@_safe
def search_pages(
    keyword: Annotated[str, Field(description="关键词，模糊匹配页面标题与正文（不区分大小写）")],
    item_id: ItemId = 0,
) -> str:
    """按关键词搜索项目内页面（标题+正文模糊匹配），返回 page_id/cat_id/标题 的扁平清单。
    找页面优先用这个，比翻目录树快；拿到 page_id 后用 showdoc_get_page 读正文。"""
    client = get_client()
    item_id = client.resolve_item(item_id)
    pages = client.search_pages(item_id, keyword)
    if not pages:
        return f"项目 {item_id} 内没有匹配「{keyword}」的页面。可更换关键词，或用 showdoc_get_item_tree 浏览目录。"
    lines = [f"项目 {item_id} 匹配「{keyword}」的页面（{len(pages)} 个）："]
    for p in pages:
        where = f"cat_id={p.get('cat_id')}" if str(p.get("cat_id") or "0") != "0" else "顶级"
        lines.append(f"- page_id={p['page_id']}（{where}）: {p['page_title']}")
    return "\n".join(lines)


@mcp.tool(name="showdoc_get_page_history", title="页面版本历史", annotations=_ANN_READ)
@_safe
def get_page_history(page_id: PageId) -> str:
    """查页面版本历史（每次更新自动存一版，最多保留最近 20 版，按时间倒序）。
    返回 page_history_id / 保存时间 / 内容预览。页面从未更新过则没有历史。"""
    client = get_client()
    versions = client.page_history(page_id)
    if not versions:
        return f"页面 page_id={page_id} 没有历史版本（创建后从未更新过，或页面不存在——可先用 showdoc_get_page 确认）。"
    lines = [f"页面 page_id={page_id} 的历史版本（{len(versions)} 个，最新在前）："]
    for v in versions:
        content = (v.get("page_content") or "").strip().replace("\n", " ")
        preview = content[:120] + ("…" if len(content) > 120 else "")
        lines.append(f"- page_history_id={v.get('page_history_id')}  {v.get('addtime')}: {preview or '（空内容）'}")
    return "\n".join(lines)


# ------------------------------------------------------------------- 单条写


@mcp.tool(name="showdoc_create_catalog", title="创建目录", annotations=_ANN_ADD)
@_safe
def create_catalog(
    cat_name: Annotated[str, Field(description="目录名，同父目录下唯一")],
    parent_cat_id: Annotated[int, Field(description="父目录 cat_id，0 表示顶级目录")] = 0,
    s_number: Annotated[int, Field(description="排序号，0 表示不设置")] = 0,
    item_id: ItemId = 0,
) -> str:
    """创建目录。parent_cat_id=0 为顶级目录，否则为子目录；s_number 控制排序。同名目录已存在时直接返回现有 cat_id（幂等）。"""
    client = get_client()
    item_id = client.resolve_item(item_id)
    parent = str(parent_cat_id or "0")
    id_map = catalog_id_map(client.catalog_tree(item_id))
    key = (parent, cat_name)
    if key in id_map:
        return f"目录「{cat_name}」已存在 (cat_id={id_map[key]})，未重复创建。"
    client.catalog_save(item_id, cat_name, parent_cat_id=parent_cat_id or None, s_number=s_number or None)
    new_map = catalog_id_map(client.catalog_tree(item_id))
    new_id = new_map.get(key)
    if not new_id:
        raise ShowDocError(f"目录「{cat_name}」创建请求成功但未在目录树中找到，请用 showdoc_get_item_tree 核对。")
    where = f"父目录 cat_id={parent_cat_id} 下" if parent_cat_id else "顶级"
    logger.info("created catalog %s cat_id=%s", cat_name, new_id)
    return f"已创建目录「{cat_name}」（{where}），cat_id={new_id}"


@mcp.tool(name="showdoc_update_catalog", title="编辑目录", annotations=_ANN_ADD)
@_safe
def update_catalog(
    cat_id: CatId,
    cat_name: Annotated[str, Field(description="新目录名，留空表示不改名")] = "",
    s_number: Annotated[int, Field(description="新排序号，0 表示不改")] = 0,
    item_id: ItemId = 0,
) -> str:
    """编辑目录：改名和/或改排序 s_number。cat_name/s_number 留空表示不改。cat_id 从 showdoc_get_item_tree 查。"""
    client = get_client()
    item_id = client.resolve_item(item_id)
    tree = client.catalog_tree(item_id)
    found = find_catalog(tree, cat_id)
    if not found:
        raise ShowDocError(f"目录 cat_id={cat_id} 不存在，请用 showdoc_get_item_tree 查询确认。")
    node, _ = found
    new_name = cat_name or node["cat_name"]
    parent = str(node.get("parent_cat_id") or "0")
    client.catalog_save(
        item_id,
        new_name,
        cat_id=cat_id,
        parent_cat_id=parent if parent != "0" else None,
        s_number=s_number or None,
    )
    changes = []
    if new_name != node["cat_name"]:
        changes.append(f"名称「{node['cat_name']}」→「{new_name}」")
    if s_number:
        changes.append(f"s_number={s_number}")
    logger.info("updated catalog %s: %s", cat_id, "; ".join(changes))
    return f"已更新目录 cat_id={cat_id}（{'；'.join(changes) if changes else '无变更'}）"


@mcp.tool(name="showdoc_delete_catalog", title="删除目录", annotations=_ANN_DESTROY)
@_safe
def delete_catalog(cat_id: CatId, item_id: ItemId = 0) -> str:
    """删除目录。目录下有子目录或页面时拒绝执行（防止内容被孤儿化），先清理内容或用 showdoc_clear_item。"""
    client = get_client()
    item_id = client.resolve_item(item_id)
    menu = (client.item_info(item_id).get("menu")) or {}
    found = find_catalog(menu.get("catalogs") or [], cat_id)
    if not found:
        raise ShowDocError(f"目录 cat_id={cat_id} 不存在，请用 showdoc_get_item_tree 查询确认。")
    node, _ = found
    subs = node.get("catalogs") or []
    pages = node.get("pages") or []
    if subs or pages:
        raise ShowDocError(
            f"目录「{node['cat_name']}」(cat_id={cat_id}) 非空：{len(subs)} 个子目录、{len(pages)} 个页面。"
            "请先删除其中内容（showdoc_delete_page / showdoc_delete_catalog），或用 showdoc_clear_item 清空整个项目。"
        )
    client.catalog_delete(item_id, cat_id)
    logger.info("deleted catalog %s %s", cat_id, node["cat_name"])
    return f"已删除空目录「{node['cat_name']}」(cat_id={cat_id})"


@mcp.tool(name="showdoc_save_page", title="创建/更新页面", annotations=_ANN_ADD)
@_safe
def save_page(
    page_title: Annotated[str, Field(description="页面标题")],
    page_content: Annotated[str, Field(description="markdown 正文")],
    cat_id: Annotated[int, Field(description="归属目录 cat_id，0 为顶级。新建时指定归属目录；更新时可省略（=0），自动保持页面原目录")] = 0,
    page_id: Annotated[int, Field(description="要更新的页面 id，0 表示新建")] = 0,
    s_number: Annotated[int, Field(description="排序号，0 表示不设置；新建时生效")] = 0,
    item_id: ItemId = 0,
) -> str:
    """创建或更新页面（markdown 正文）。page_id=0 新建（cat_id 指定归属目录，0 为顶级）并回查返回 page_id；
    否则更新该页，此时省略 cat_id 会保持原目录（不会掉到顶级），显式传 cat_id 则移动页面。"""
    client = get_client()
    item_id = client.resolve_item(item_id)
    if page_id:
        try:
            old = client.page_info(page_id)
        except ShowDocError:
            old = {}
        old_title = old.get("page_title")
        if cat_id:
            real_cat = cat_id  # 显式传了 cat_id：移动页面（如回收站恢复后归位）
        elif old:
            real_cat = int(old.get("cat_id") or 0)  # 回查原目录，防止更新掉到顶级
        else:
            real_cat = None  # 回查失败：不传该字段，让服务端保持原状
        client.page_save(item_id, page_title, page_content, cat_id=real_cat, page_id=page_id)
        logger.info("updated page %s", page_id)
        return f"已更新页面「{old_title or page_title}」(page_id={page_id})"
    client.page_save(item_id, page_title, page_content, cat_id=cat_id, s_number=s_number or None)
    # 新建后回查菜单拿 page_id，方便后续引用
    page_map = _menu_pages_by_cat(client.item_info(item_id))
    new_id = page_map.get((str(cat_id or "0"), page_title))
    if new_id:
        logger.info("created page %s %s", new_id, page_title)
        return f"已创建页面「{page_title}」page_id={new_id}（cat_id={cat_id or '顶级'}）"
    return f"已创建页面「{page_title}」（cat_id={cat_id or '顶级'}）；未能自动回查到 page_id，可用 showdoc_get_item_tree 确认。"


@mcp.tool(name="showdoc_delete_page", title="删除页面", annotations=_ANN_DESTROY)
@_safe
def delete_page(page_id: PageId, item_id: ItemId = 0) -> str:
    """删除页面（进入 ShowDoc 回收站，可在网页端恢复）。page_id 从 showdoc_get_item_tree 查。"""
    client = get_client()
    client.resolve_item(item_id)
    try:
        title = client.page_info(page_id).get("page_title")
    except ShowDocError:
        title = None
    client.page_delete(page_id)
    logger.info("deleted page %s %s", page_id, title)
    return f"已删除页面「{title or page_id}」(page_id={page_id})"


@mcp.tool(name="showdoc_list_recycle", title="回收站列表", annotations=_ANN_READ)
@_safe
def list_recycle(item_id: ItemId = 0) -> str:
    """列出项目回收站内被删除的页面（page_id / 标题 / 删除时间）。恢复用 showdoc_recover_page。"""
    client = get_client()
    item_id = client.resolve_item(item_id)
    entries = client.recycle_list(item_id)
    if not entries:
        return f"项目 {item_id} 回收站为空。"
    lines = [f"项目 {item_id} 回收站（{len(entries)} 个页面，最新删除在前）："]
    for e in entries:
        lines.append(f"- page_id={e.get('page_id')}  {e.get('del_time')}: {e.get('page_title')}")
    return "\n".join(lines)


@mcp.tool(name="showdoc_recover_page", title="从回收站恢复页面", annotations=_ANN_ADD)
@_safe
def recover_page(page_id: PageId, item_id: ItemId = 0) -> str:
    """把页面从回收站恢复（撤销删除）。注意：ShowDoc 会把恢复的页面放回顶级目录而不是原目录，
    恢复后如需归位，用 showdoc_get_page 确认内容后再 showdoc_save_page 调整。"""
    client = get_client()
    item_id = client.resolve_item(item_id)
    client.recycle_recover(item_id, page_id)
    left = [e for e in client.recycle_list(item_id) if str(e.get("page_id")) == str(page_id)]
    if left:
        raise ShowDocError(
            f"恢复请求已发送，但 page_id={page_id} 仍在回收站列表中，请在网页端核对或稍后重试。"
        )
    logger.info("recovered page %s item=%s", page_id, item_id)
    return (
        f"已从回收站恢复页面 page_id={page_id}（当前位于顶级目录，原目录不保留）。"
        "可用 showdoc_get_item_tree 确认位置，需要归位时用 showdoc_save_page 更新。"
    )


# --------------------------------------------------------------------- 批量


@mcp.tool(name="showdoc_upload_markdown_dir", title="批量上传 markdown 目录", annotations=_ANN_ADD)
@_safe
def upload_markdown_dir(
    dir_path: Annotated[str, Field(description="本地 markdown 根目录的绝对路径")],
    item_id: ItemId = 0,
    dry_run: DryRun = False,
) -> str:
    """把本地 markdown 目录批量上传为项目目录树：一级子目录→顶级目录，二级子目录→子目录，根 README.md→顶级页面；
    页面标题取正文首个 `# 标题`；目录名前缀数字作为 s_number。同名目录/同标题页面自动跳过或更新，可安全重跑。
    dry_run=true 只输出计划不写库。目录层级最多两级。"""
    root = Path(dir_path).expanduser().resolve()
    if not root.is_dir():
        raise ShowDocError(f"目录不存在: {dir_path}")
    client = get_client()
    item_id = client.resolve_item(item_id)

    l1_dirs = sorted([d for d in root.iterdir() if d.is_dir()], key=lambda p: _natural_key(p.name))
    root_mds = sorted([f for f in root.iterdir() if f.is_file() and f.suffix.lower() == ".md"])
    if not l1_dirs and not root_mds:
        raise ShowDocError(f"{root} 下没有子目录或 .md 文件，无可上传内容。")

    # 深层目录不支持，先检查
    for d1 in l1_dirs:
        for d2 in [x for x in d1.iterdir() if x.is_dir()]:
            deep = [x for x in d2.iterdir() if x.is_dir()]
            if deep:
                raise ShowDocError(f"仅支持两级目录，发现更深层级: {deep[0]}")

    tree = client.catalog_tree(item_id)
    cat_ids: dict[tuple[str, str], str] = catalog_id_map(tree)
    page_map = _menu_pages_by_cat(client.item_info(item_id))

    stats = {"cat_created": 0, "cat_exists": 0, "page_created": 0, "page_updated": 0}
    fails: list[str] = []
    plan_lines: list[str] = []

    def ensure_dir(name: str, parent: str) -> str | None:
        """返回 cat_id；dry_run 时返回 None 并记计划。"""
        key = (parent, name)
        if key in cat_ids:
            stats["cat_exists"] += 1
            return cat_ids[key]
        if dry_run:
            plan_lines.append(f"  + 目录 [{parent}] {name}")
            stats["cat_created"] += 1
            cat_ids[key] = "dry"
            return "dry"
        client.catalog_save(item_id, name, parent_cat_id=parent if parent != "0" else None,
                            s_number=_natural_key(name)[0] if _natural_key(name)[0] != 99 else None)
        fresh = catalog_id_map(client.catalog_tree(item_id))
        if key not in fresh:
            raise ShowDocError(f"目录「{name}」创建后未在目录树中找到")
        cat_ids[key] = fresh[key]
        stats["cat_created"] += 1
        return cat_ids[key]

    def ensure_page(path: Path, cat_id: str, fallback_title: str, index: int) -> None:
        content = path.read_text(encoding="utf-8")
        title = _first_heading(content, fallback_title)
        if cat_id == "dry":
            plan_lines.append(f"  + 页面 [{title}]")
            stats["page_created"] += 1
            return
        existing = page_map.get((cat_id, title))
        if existing:
            if dry_run:
                plan_lines.append(f"  ~ 更新 [{title}] (page_id={existing})")
                stats["page_updated"] += 1
                return
            client.page_save(item_id, title, content, cat_id=int(cat_id), page_id=int(existing))
            stats["page_updated"] += 1
        else:
            if dry_run:
                plan_lines.append(f"  + 页面 [{title}]")
                stats["page_created"] += 1
                return
            client.page_save(item_id, title, content, cat_id=int(cat_id), s_number=index)
            stats["page_created"] += 1

    # 根级 md → 顶级页面
    for i, f in enumerate(root_mds, 1):
        try:
            ensure_page(f, "0", f.stem if f.stem.lower() != "readme" else "README", i)
        except ShowDocError as e:
            fails.append(f"{f.name}: {e}")

    for d1 in l1_dirs:
        try:
            cat1 = ensure_dir(d1.name, "0")
        except ShowDocError as e:
            fails.append(f"{d1.name}/: {e}")
            continue
        for i, f in enumerate(sorted([x for x in d1.iterdir() if x.is_file() and x.suffix.lower() == ".md"]), 1):
            try:
                if cat1:
                    ensure_page(f, cat1, f.stem, i)
            except ShowDocError as e:
                fails.append(f"{d1.name}/{f.name}: {e}")
        for d2 in sorted([x for x in d1.iterdir() if x.is_dir()], key=lambda p: _natural_key(p.name)):
            try:
                cat2 = ensure_dir(d2.name, cat1 or "0")
            except ShowDocError as e:
                fails.append(f"{d1.name}/{d2.name}/: {e}")
                continue
            for i, f in enumerate(sorted([x for x in d2.iterdir() if x.is_file() and x.suffix.lower() == ".md"]), 1):
                try:
                    fallback = f.stem if f.stem.lower() != "readme" else d2.name
                    if cat2:
                        ensure_page(f, cat2, fallback, i)
                except ShowDocError as e:
                    fails.append(f"{d1.name}/{d2.name}/{f.name}: {e}")

    if dry_run:
        head = f"[dry-run] 将上传 {root}：新建目录 {stats['cat_created']} 个（已存在 {stats['cat_exists']} 个）、"
        f"新建页面 {stats['page_created']} 个、更新页面 {stats['page_updated']} 个。确认后用 dry_run=false 执行。"
        detail = "\n".join(plan_lines[:200])
        return head + ("\n" + detail if detail else "")

    logger.info("upload %s: %s", root, stats)
    result = (
        f"上传完成 {root}：新建目录 {stats['cat_created']} 个（复用已存在 {stats['cat_exists']} 个）、"
        f"新建页面 {stats['page_created']} 个、更新页面 {stats['page_updated']} 个"
    )
    if fails:
        result += f"；失败 {len(fails)} 项（可重跑，已完成部分会自动跳过/更新）：\n" + "\n".join(fails[:50])
    return result


@mcp.tool(name="showdoc_clear_item", title="清空项目（危险）", annotations=_ANN_DESTROY)
@_safe
def clear_item(item_id: ItemId = 0, confirm: Annotated[bool, Field(description="必须显式传 true 才真正删除，默认只预览")] = False) -> str:
    """清空项目全部页面与目录（保留项目本身）。默认 dry-run 只返回统计；confirm=true 才真正删除。
    被删页面进入 ShowDoc 回收站，可在网页端恢复。危险操作，请确认 item_id 正确。"""
    client = get_client()
    item_id = client.resolve_item(item_id)
    info = client.item_info(item_id)
    menu = info.get("menu") or {}
    pages = [(p["page_id"], p["page_title"]) for p in menu.get("pages") or []]
    cats: list[tuple[int, str, int]] = []
    for node, depth in flatten_catalogs(menu.get("catalogs") or []):
        pages += [(p["page_id"], p["page_title"]) for p in node.get("pages") or []]
        cats.append((int(node["cat_id"]), node["cat_name"], depth))
    name = info.get("item_name")
    if not confirm:
        return (
            f"[dry-run] 项目 {item_id}「{name}」将删除 {len(pages)} 个页面、{len(cats)} 个目录（项目本身保留）。\n"
            "确认无误请带 confirm=true 重新调用。"
        )
    page_fails, cat_fails = [], []
    for pid, title in pages:
        try:
            client.page_delete(pid)
        except ShowDocError as e:
            page_fails.append(f"page {pid} {title}: {e}")
    for cid, cname, _ in sorted(cats, key=lambda x: -x[2]):  # 子目录优先
        try:
            client.catalog_delete(item_id, cid)
        except ShowDocError as e:
            cat_fails.append(f"cat {cid} {cname}: {e}")
    left_cats, left_pages = _menu_counts(client.item_info(item_id))
    logger.info("clear item %s: pages=%d cats=%d remain=%d/%d", item_id, len(pages), len(cats), left_pages, left_cats)
    result = (
        f"清空完成：删除页面 {len(pages) - len(page_fails)}/{len(pages)}，目录 {len(cats) - len(cat_fails)}/{len(cats)}；"
        f"剩余 {left_pages} 页面 / {left_cats} 目录"
    )
    if page_fails or cat_fails:
        result += "。失败项：\n" + "\n".join((page_fails + cat_fails)[:30])
    return result


@mcp.tool(name="showdoc_rename_numbered_catalogs", title="目录名加点的批量重命名", annotations=_ANN_ADD)
@_safe
def rename_numbered_catalogs(item_id: ItemId = 0, dry_run: DryRun = False) -> str:
    """把顶级目录名「N名称」批量改为「N.名称」（数字前缀后加点）。已符合格式的跳过。dry_run=true 只看计划。"""
    client = get_client()
    item_id = client.resolve_item(item_id)
    tree = client.catalog_tree(item_id)
    renames = []
    for node in tree or []:
        m = re.match(r"^(\d+)(?!\.)(.*)$", node["cat_name"])
        if m:
            renames.append((node["cat_id"], node["cat_name"], f"{m.group(1)}.{m.group(2)}"))
    if not renames:
        return "没有需要处理的目录（顶级目录均已符合 N.名称 格式或无数字前缀）。"
    if dry_run:
        lines = [f"  {old} -> {new}" for _, old, new in renames]
        return f"[dry-run] 将重命名 {len(renames)} 个顶级目录：\n" + "\n".join(lines)
    fails = []
    for cid, old, new in renames:
        try:
            client.catalog_save(item_id, new, cat_id=cid, parent_cat_id=None)
        except ShowDocError as e:
            fails.append(f"{old}: {e}")
    logger.info("rename numbered catalogs item=%s done=%d", item_id, len(renames) - len(fails))
    result = f"重命名完成 {len(renames) - len(fails)}/{len(renames)}：\n" + "\n".join(
        f"  {old} -> {new}" for cid, old, new in renames
    )
    if fails:
        result += "\n失败项：\n" + "\n".join(fails)
    return result
