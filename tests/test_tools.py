"""showdoc_mcp 工具集单元测试：不出网络，用有状态的 FakeShowDocClient 驱动全部 15 个工具。

Fake 模拟 ShowDoc v2.6.7 的关键行为：
- 目录树（catListGroup 嵌套 sub / item_info menu 嵌套 catalogs）；
- 页面更新时写历史（page_history）；
- 删除页面进回收站（is_del=1 + recycle 表），恢复时落顶级目录（cat_id=0）；
- item/info 的 keyword 搜索（标题+正文，忽略大小写）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError

from showdoc_mcp import tools
from showdoc_mcp.client import ShowDocError
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


class FakeShowDocClient:
    """实现 tools.py 用到的 ShowDocClient 接口子集，带内存状态。"""

    def __init__(self, default_item_id: int = 1) -> None:
        self.default_item_id = default_item_id
        self.items = [{"item_id": 1, "item_name": "测试项目", "item_description": ""}]
        self.catalogs: list[dict] = []  # {cat_id, cat_name, parent_cat_id, s_number}
        self.pages: dict[int, dict] = {}  # page_id -> {page_title, page_content, cat_id, is_del}
        self.recycle: list[dict] = []  # {page_id, page_title, del_time}
        self.history: dict[int, list[dict]] = {}
        self._next_cat = 100
        self._next_page = 200
        self._next_hist = 1
        self.fail_recover = False

    # -------------------------------------------------------------- 基础

    def resolve_item(self, item_id):
        item_id = int(item_id) if item_id else self.default_item_id
        if not item_id:
            raise ShowDocError("未指定 item_id 且未配置默认项目。")
        return item_id

    def my_items(self):
        return self.items

    # -------------------------------------------------------------- 树构建

    def _menu_node(self, cat: dict) -> dict:
        cid = cat["cat_id"]
        return {
            "cat_id": cid,
            "cat_name": cat["cat_name"],
            "s_number": cat.get("s_number"),
            "pages": [
                {"page_id": pid, "page_title": p["page_title"], "s_number": None}
                for pid, p in self.pages.items()
                if p["cat_id"] == cid and not p["is_del"]
            ],
            "catalogs": [
                self._menu_node(c) for c in self.catalogs if c["parent_cat_id"] == cid
            ],
        }

    def item_info(self, item_id: int) -> dict:
        return {
            "item_id": item_id,
            "item_name": "测试项目",
            "menu": {
                "pages": [
                    {"page_id": pid, "page_title": p["page_title"], "s_number": None}
                    for pid, p in self.pages.items()
                    if p["cat_id"] == 0 and not p["is_del"]
                ],
                "catalogs": [self._menu_node(c) for c in self.catalogs if c["parent_cat_id"] == 0],
            },
        }

    def catalog_tree(self, item_id: int) -> list[dict]:
        def node(cat: dict) -> dict:
            return {
                "cat_id": cat["cat_id"],
                "cat_name": cat["cat_name"],
                "parent_cat_id": cat["parent_cat_id"],
                "s_number": cat.get("s_number"),
                "sub": [node(c) for c in self.catalogs if c["parent_cat_id"] == cat["cat_id"]],
            }

        return [node(c) for c in self.catalogs if c["parent_cat_id"] == 0]

    def page_info(self, page_id: int) -> dict:
        p = self.pages.get(int(page_id))
        if not p or p["is_del"]:
            raise ShowDocError(f"页面 page_id={page_id} 不存在")
        return {"page_id": page_id, "page_title": p["page_title"], "page_content": p["page_content"],
                "cat_id": p["cat_id"], "item_id": 1, "author_username": "tester"}

    # -------------------------------------------------------------- 搜索/历史/回收

    def search_pages(self, item_id: int, keyword: str) -> list[dict]:
        kw = keyword.lower()
        return [
            {"page_id": pid, "cat_id": p["cat_id"], "page_title": p["page_title"]}
            for pid, p in self.pages.items()
            if not p["is_del"] and (kw in p["page_title"].lower() or kw in p["page_content"].lower())
        ]

    def page_history(self, page_id: int) -> list[dict]:
        return list(reversed(self.history.get(int(page_id), [])))

    def recycle_list(self, item_id: int) -> list[dict]:
        return list(self.recycle)

    def recycle_recover(self, item_id: int, page_id: int) -> None:
        if self.fail_recover:
            return  # 模拟恢复失败：页面仍留在回收站
        p = self.pages.get(int(page_id))
        if p:
            p["is_del"] = False
            p["cat_id"] = 0  # ShowDoc 行为：恢复后落顶级目录
        self.recycle = [e for e in self.recycle if int(e["page_id"]) != int(page_id)]

    # -------------------------------------------------------------- 写操作

    def catalog_save(self, item_id, cat_name, cat_id="undefined", parent_cat_id=None, s_number=None):
        if cat_id not in ("undefined", 0, "0"):
            for c in self.catalogs:
                if c["cat_id"] == int(cat_id):
                    c["cat_name"] = cat_name
                    return {}
            raise ShowDocError(f"目录 cat_id={cat_id} 不存在")
        new = {"cat_id": self._next_cat, "cat_name": cat_name,
               "parent_cat_id": int(parent_cat_id or 0), "s_number": s_number}
        self._next_cat += 1
        self.catalogs.append(new)
        return {}

    def catalog_delete(self, item_id: int, cat_id: int):
        self.catalogs = [c for c in self.catalogs if c["cat_id"] != int(cat_id)]

    def page_save(self, item_id, page_title, page_content, cat_id=0, page_id=0, s_number=None):
        if page_id:
            pid = int(page_id)
            p = self.pages[pid]
            self.history.setdefault(pid, []).append({
                "page_history_id": self._next_hist,
                "addtime": f"2026-01-0{self._next_hist} 10:00:00",
                "page_content": p["page_content"],  # 历史存旧内容
            })
            self._next_hist += 1
            p["page_title"] = page_title
            p["page_content"] = page_content
            return {"page_id": pid}
        pid = self._next_page
        self._next_page += 1
        self.pages[pid] = {"page_title": page_title, "page_content": page_content,
                           "cat_id": int(cat_id or 0), "is_del": False}
        return {"page_id": pid}

    def page_delete(self, page_id: int):
        p = self.pages[int(page_id)]
        p["is_del"] = True
        self.recycle.insert(0, {"page_id": int(page_id), "page_title": p["page_title"],
                                "del_time": "2026-01-01 12:00:00"})


# --------------------------------------------------------------------- fixtures


@pytest.fixture()
def fake():
    f = FakeShowDocClient()
    tools._client = f
    yield f
    tools._client = None


@pytest.fixture()
def no_default_item(fake):
    fake.default_item_id = 0
    return fake


# --------------------------------------------------------------------- 注册与配置


def test_registered_15_tools_with_prefix_and_annotations():
    """工具注册面：15 个、全带 showdoc_ 前缀、全有注解、搜索参数必填。"""

    async def run():
        async with Client(tools.mcp) as client:
            listing = await client.list_tools()
            return {t.name: t for t in listing.tools}

    by_name = asyncio.run(run())
    assert len(by_name) == 15
    assert all(n.startswith("showdoc_") for n in by_name)
    assert all(t.annotations is not None for t in by_name.values())
    assert by_name["showdoc_search_pages"].input_schema["required"] == ["keyword"]


def test_get_client_missing_env(monkeypatch):
    for key in ("SHOWDOC_BASE_URL", "SHOWDOC_COOKIE_TOKEN", "SHOWDOC_PHPSESSID"):
        monkeypatch.delenv(key, raising=False)
    tools._client = None
    with pytest.raises(ToolError, match="SHOWDOC_BASE_URL"):
        list_items()
    tools._client = None


# --------------------------------------------------------------------- 读


def test_list_items(fake):
    r = list_items()
    assert "item_id=1: 测试项目" in r


def test_list_items_empty(fake):
    fake.items = []
    assert "没有可见项目" in list_items()


def test_get_item_tree(fake):
    fake.catalog_save(1, "指南")
    fake.catalog_save(1, "进阶", parent_cat_id=100)
    fake.page_save(1, "首页", "# 首页")
    r = get_item_tree()
    assert "共 2 个目录 / 1 个页面" in r
    assert "cat_id=100" in r and "page_id=200" in r


def test_get_item_tree_requires_item(no_default_item):
    with pytest.raises(ToolError, match="未指定 item_id"):
        get_item_tree(0)


def test_get_page(fake):
    fake.page_save(1, "使用说明", "# 使用说明\n正文")
    r = get_page(200)
    assert "# 使用说明" in r and "正文" in r and "page_id=200" in r


def test_get_page_truncates(fake):
    fake.page_save(1, "长文", "x" * 40000)
    r = get_page(200)
    assert "已截断" in r and len(r) < 35000


def test_get_page_missing(fake):
    with pytest.raises(ToolError, match="不存在"):
        get_page(999)


def test_search_pages_title_and_content(fake):
    fake.page_save(1, "部署手册", "常规内容")
    fake.page_save(1, "杂记", "这里提到 K8s 部署")
    r = search_pages("部署")
    assert "page_id=200" in r and "page_id=201" in r and "2 个" in r


def test_search_pages_no_match(fake):
    fake.page_save(1, "部署手册", "内容")
    assert "没有匹配" in search_pages("不存在的关键词xyz")


def test_get_page_history(fake):
    fake.page_save(1, "页面", "v1")
    save_page("页面", "v2", page_id=200)
    save_page("页面", "v3", page_id=200)
    r = get_page_history(200)
    assert "2 个" in r and "v2" in r and "v1" in r  # v2、v1 依次为历史（当前是 v3）


def test_get_page_history_never_updated(fake):
    fake.page_save(1, "页面", "只有初版")
    assert "没有历史版本" in get_page_history(200)


# --------------------------------------------------------------------- 单条写


def test_create_catalog_idempotent(fake):
    r1 = create_catalog("文档")
    r2 = create_catalog("文档")
    assert "已创建目录「文档」" in r1 and "cat_id=" in r1
    assert "已存在" in r2
    assert len(fake.catalogs) == 1


def test_update_catalog_rename(fake):
    create_catalog("旧名")
    r = update_catalog(100, cat_name="新名")
    assert "旧名」→「新名」" in r
    assert fake.catalogs[0]["cat_name"] == "新名"


def test_update_catalog_missing(fake):
    with pytest.raises(ToolError, match="不存在"):
        update_catalog(999, cat_name="x")


def test_delete_catalog_empty(fake):
    create_catalog("空目录")
    r = delete_catalog(100)
    assert "已删除空目录" in r and not fake.catalogs


def test_delete_catalog_rejects_non_empty(fake):
    create_catalog("目录")
    fake.page_save(1, "页面", "内容", cat_id=100)
    with pytest.raises(ToolError, match="非空"):
        delete_catalog(100)
    assert len(fake.catalogs) == 1  # 未删除


def test_save_page_create_then_update(fake):
    r = save_page("新页", "# 新页\n内容")
    assert "page_id=200" in r
    r2 = save_page("新页", "# 新页\n内容v2", page_id=200)
    assert "已更新页面" in r2
    assert fake.pages[200]["page_content"].endswith("v2")


def test_delete_page_goes_to_recycle(fake):
    fake.page_save(1, "待删", "内容")
    r = delete_page(200)
    assert "已删除页面「待删」" in r
    assert fake.pages[200]["is_del"] and fake.recycle[0]["page_id"] == 200


def test_list_recycle(fake):
    assert "回收站为空" in list_recycle()
    fake.page_save(1, "页A", "内容")
    delete_page(200)
    r = list_recycle()
    assert "page_id=200" in r and "页A" in r


def test_recover_page(fake):
    fake.page_save(1, "页A", "内容", cat_id=7)  # 原目录 7
    delete_page(200)
    r = recover_page(200)
    assert "已从回收站恢复" in r and "顶级目录" in r
    assert not fake.pages[200]["is_del"]
    assert fake.pages[200]["cat_id"] == 0  # ShowDoc 行为：恢复落顶级
    assert not fake.recycle


def test_recover_page_failure_raises(fake):
    fake.page_save(1, "页A", "内容")
    delete_page(200)
    fake.fail_recover = True
    with pytest.raises(ToolError, match="仍在回收站"):
        recover_page(200)


# --------------------------------------------------------------------- 批量


def _make_upload_dir(tmp: Path) -> Path:
    (tmp / "1快速开始").mkdir(parents=True)
    (tmp / "1快速开始" / "install.md").write_text("# 安装指南\n先装依赖", encoding="utf-8")
    (tmp / "README.md").write_text("# 项目说明\n这是一个说明", encoding="utf-8")
    return tmp


def test_upload_markdown_dir_dry_run(fake, tmp_path):
    root = _make_upload_dir(tmp_path)
    r = upload_markdown_dir(str(root), dry_run=True)
    assert "[dry-run]" in r and "安装指南" in r and "新建目录 1 个" in r
    assert not fake.catalogs and not fake.pages  # 未写库


def test_upload_markdown_dir_real_and_rerun(fake, tmp_path):
    root = _make_upload_dir(tmp_path)
    r = upload_markdown_dir(str(root))
    assert "上传完成" in r and "新建页面 2 个" in r
    assert fake.catalogs[0]["cat_name"] == "1快速开始"
    # 重跑幂等：同标题页面更新而非重建
    r2 = upload_markdown_dir(str(root))
    assert "更新页面 2 个" in r2 and "新建页面 0 个" in r2
    assert len(fake.pages) == 2


def test_upload_markdown_dir_rejects_deep(fake, tmp_path):
    (tmp_path / "a" / "b" / "c").mkdir(parents=True)
    with pytest.raises(ToolError, match="两级"):
        upload_markdown_dir(str(tmp_path))


def test_upload_markdown_dir_missing(fake):
    with pytest.raises(ToolError, match="目录不存在"):
        upload_markdown_dir("/nonexistent/path")


def test_clear_item_dry_run_default(fake):
    create_catalog("目录")
    fake.page_save(1, "页", "内容", cat_id=100)
    r = clear_item(confirm=False)
    assert "[dry-run]" in r and "1 个页面、1 个目录" in r
    assert len(fake.pages) == 1  # 未删


def test_clear_item_confirmed(fake):
    create_catalog("父")
    fake.catalog_save(1, "子", parent_cat_id=100)
    fake.page_save(1, "页", "内容", cat_id=101)
    fake.page_save(1, "顶级页", "内容")
    r = clear_item(confirm=True)
    assert "清空完成：删除页面 2/2，目录 2/2" in r
    assert not fake.catalogs and all(p["is_del"] for p in fake.pages.values())


def test_rename_numbered_catalogs(fake):
    for name in ("1基础", "2进阶", "3.已规范"):
        fake.catalog_save(1, name)
    r = rename_numbered_catalogs(dry_run=True)
    assert "1基础 -> 1.基础" in r and "2 个" in r and "3.已规范" not in r
    r2 = rename_numbered_catalogs()
    assert {c["cat_name"] for c in fake.catalogs} == {"1.基础", "2.进阶", "3.已规范"}


def test_rename_numbered_catalogs_nothing_to_do(fake):
    assert "没有需要处理" in rename_numbered_catalogs()
