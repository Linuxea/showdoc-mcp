"""冒烟测试：不经 MCP 协议直调各工具函数，打真实 ShowDoc。

写操作只碰「__mcp冒烟测试__」临时目录，跑完即删，不影响真实数据。
用法：SHOWDOC_* 环境变量就位后 `uv run python scripts/smoke_test.py`
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

from showdoc_mcp.tools import (
    clear_item,
    create_catalog,
    delete_catalog,
    delete_page,
    get_item_tree,
    get_page,
    get_page_history,
    list_items,
    list_recycle,
    recover_page,
    rename_numbered_catalogs,
    save_page,
    search_pages,
    update_catalog,
    upload_markdown_dir,
)

ITEM_ID = int(os.environ.get("SHOWDOC_DEFAULT_ITEM_ID") or 0)
if not ITEM_ID:
    sys.exit("请先设置 SHOWDOC_DEFAULT_ITEM_ID（测试目标项目 id）")
results: list[tuple[bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((cond, name))
    print(("PASS " if cond else "FAIL ") + name + (f"  | {detail[:200]}" if not cond else ""))


def tree_counts() -> tuple[int, int]:
    text = get_item_tree(ITEM_ID)
    m = re.search(r"共 (\d+) 个目录 / (\d+) 个页面", text)
    return int(m.group(1)), int(m.group(2))


def main() -> int:
    base_cats, base_pages = tree_counts()
    check("get_item_tree 读到目录树", base_cats > 0, f"{base_cats} catalogs")

    r = list_items()
    check(f"list_items 含项目 {ITEM_ID}", str(ITEM_ID) in r, r)

    cat_id = page_id = 0
    try:
        # 临时目录 + 页面全生命周期
        r = create_catalog("__mcp冒烟测试__", s_number=999)
        m = re.search(r"cat_id=(\d+)", r)
        check("create_catalog 创建", bool(m), r)
        cat_id = int(m.group(1)) if m else 0

        r2 = create_catalog("__mcp冒烟测试__")
        check("create_catalog 幂等（同名返回已存在）", "已存在" in r2, r2)

        r = save_page("冒烟测试页面", "# 冒烟测试页面\n\n正文 v1", cat_id=cat_id)
        m = re.search(r"page_id=(\d+)", r)
        check("save_page 新建", bool(m), r)
        page_id = int(m.group(1)) if m else 0

        r = get_page(page_id)
        check("get_page 读到新建内容", "正文 v1" in r, r)

        r = save_page("冒烟测试页面", "# 冒烟测试页面\n\n正文 v2", page_id=page_id)
        r = get_page(page_id)
        check("save_page 更新生效", "正文 v2" in r and "正文 v1" not in r, r)

        r = update_catalog(cat_id, cat_name="__mcp冒烟测试_改名__")
        check("update_catalog 改名", "已更新" in r, r)

        r = search_pages("冒烟测试页面")
        check("search_pages 搜到标题", "page_id=" in r and "冒烟测试页面" in r, r)
        r = search_pages("正文 v2")
        check("search_pages 搜到正文", "page_id=" in r, r)
        r = get_page_history(page_id)
        check("get_page_history 有历史且含旧版", "1 个" in r and "正文 v1" in r, r)

        r = delete_page(page_id)
        check("delete_page", "已删除" in r, r)

        # 回收站 → 恢复 → 再删除（保持基线）
        r = list_recycle()
        check("list_recycle 含刚删页面", f"page_id={page_id}" in r, r)
        r = recover_page(page_id)
        check("recover_page 恢复成功", "已从回收站恢复" in r, r)
        r = get_page(page_id)
        check("恢复后可读取", "正文 v2" in r, r)
        r = delete_page(page_id)
        check("delete_page 二次删除", "已删除" in r, r)
        r = delete_catalog(cat_id)
        check("delete_catalog 删除空目录", "已删除" in r, r)
        cat_id = page_id = 0
    finally:
        if page_id:
            delete_page(page_id)
        if cat_id:
            delete_catalog(cat_id)

    cats, pages = tree_counts()
    check("数据还原（目录/页面数与基线一致）", (cats, pages) == (base_cats, base_pages), f"{base_cats}/{base_pages} -> {cats}/{pages}")

    # 非空目录删除保护
    r = create_catalog("__mcp冒烟测试__")
    cat_id = int(re.search(r"cat_id=(\d+)", r).group(1))
    save_page("冒烟测试页面", "# 冒烟测试页面", cat_id=cat_id)
    try:
        delete_catalog(cat_id)
        check("delete_catalog 拒绝非空目录", False, "居然删成功了")
    except Exception as e:
        check("delete_catalog 拒绝非空目录", "非空" in str(e), str(e))
    # 清理
    tree_text = get_item_tree(ITEM_ID)
    m = re.search(r"冒烟测试页面 \(page_id=(\d+)\)", tree_text)
    if m:
        delete_page(int(m.group(1)))
    delete_catalog(cat_id)

    # 批量工具 dry-run（不写库）
    tmp = Path(tempfile.mkdtemp(prefix="mcp_smoke_docs_"))
    try:
        (tmp / "1冒烟分类" / "子分类").mkdir(parents=True)
        (tmp / "README.md").write_text("# 冒烟说明\n\n临时", encoding="utf-8")
        (tmp / "1冒烟分类" / "子分类" / "demo.md").write_text("# 冒烟接口\n\n内容", encoding="utf-8")
        r = upload_markdown_dir(str(tmp), dry_run=True)
        check("upload_markdown_dir dry_run 出计划", "dry-run" in r and "冒烟接口" in r, r)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    r = clear_item(confirm=False)
    check("clear_item 默认 dry-run", "dry-run" in r, r)
    r = rename_numbered_catalogs(dry_run=True)
    check("rename_numbered_catalogs dry_run", "dry-run" in r or "没有需要处理" in r, r)

    failed = [n for ok, n in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
