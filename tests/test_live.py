"""基于真实 ShowDoc（Docker 容器）的集成测试。

与 test_tools.py 的 Fake 单测互补：Fake 驱动的单测快且离线，但保真度有限
（「更新时传 cat_id 会移动页面」这类真实行为曾被 Fake 漏掉导致线上 bug）；
这层直接打 Docker 里的真实 ShowDoc v2.6.7，验证工具行为与真实服务端语义一致。

环境完全自动管理：
- Docker 不可用 → 整个文件自动 skip（CI / 无 Docker 环境不受影响）；
- 容器 showdoc-test 不存在 → 自动创建、非交互安装、关注册验证码、注册测试账号；
- 容器已存在（含上次跑剩的）→ 直接复用，登录后清场；
- 每个测试 session 使用专属项目「pytest-live」（按名复用或新建），开始时
  clear_item 清空上次残留，用例间用唯一名字互不依赖。

运行方式与普通单测一致：uv run pytest tests/ -q
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
import uuid

import httpx2
import pytest

from showdoc_mcp import tools
from showdoc_mcp.client import ShowDocClient
from showdoc_mcp.tools import (
    clear_item,
    create_catalog,
    delete_page,
    get_item_tree,
    get_page,
    list_items,
    list_recycle,
    recover_page,
    save_page,
    search_pages,
    upload_markdown_dir,
)

pytestmark = pytest.mark.live

CONTAINER = "showdoc-test"
IMAGE = "star7th/showdoc:v2.6.7"
BASE_URL = "http://127.0.0.1:4999"
ITEM_NAME = "pytest-live"
USERNAME = "mcp_pytest"
PASSWORD = "pytest_live_2026"
API_CONF = "/var/www/html/server/Application/Api/Conf/config.php"


def _sh(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), capture_output=True, text=True, check=check)


def _wait_http(url: str, timeout: float = 120.0) -> None:
    """轮询直到 ShowDoc HTTP 就绪（首次启动 + 安装可能较慢）。忽略环境代理，直连 127.0.0.1。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx2.get(url, timeout=5, follow_redirects=True, trust_env=False).status_code == 200:
                return
        except httpx2.HTTPError:
            pass
        time.sleep(1)
    raise RuntimeError(f"{url} 在 {timeout}s 内未就绪")


def _login_cookies() -> dict[str, str]:
    """注册（已存在则忽略报错）+ 登录，返回 cookie_token / PHPSESSID。"""
    with httpx2.Client(timeout=30, trust_env=False) as http:
        http.post(
            f"{BASE_URL}/server/index.php?s=/api/user/register",
            data={"username": USERNAME, "password": PASSWORD, "confirm_password": PASSWORD},
            headers={"Referer": BASE_URL + "/web/"},
        )  # 已注册时返回错误码，忽略；以 login 结果为准
        resp = http.post(
            f"{BASE_URL}/server/index.php?s=/api/user/login",
            data={"username": USERNAME, "password": PASSWORD},
            headers={"Referer": BASE_URL + "/web/"},
        )
        body = resp.json()
        if body.get("error_code") != 0:
            raise RuntimeError(f"登录失败: {body}")
        return {k: http.cookies[k] for k in ("cookie_token", "PHPSESSID")}


@pytest.fixture(scope="session")
def live():
    # 1) Docker 可用性：不可用则整层 skip
    if shutil.which("docker") is None or _sh("docker", "info", check=False).returncode != 0:
        pytest.skip("Docker 不可用，跳过真实 ShowDoc 集成测试")

    # 2) 容器：不存在则创建（卷持久化，装一次后续复用）
    exists = _sh("docker", "ps", "-aq", "-f", f"name=^{CONTAINER}$", check=False).stdout.strip()
    if not exists:
        _sh(
            "docker", "run", "-d", "--name", CONTAINER,
            "-p", "127.0.0.1:4999:80", "-v", "showdoc-test-data:/var/www/html", IMAGE,
        )
    else:
        state = _sh("docker", "inspect", "-f", "{{.State.Running}}", CONTAINER).stdout.strip()
        if state != "true":
            _sh("docker", "start", CONTAINER)
    _wait_http(BASE_URL)

    # 3) 首次使用：非交互安装（HTTP，README 实测配方，重复调用幂等）+ 关注册验证码
    try:
        httpx2.get(f"{BASE_URL}/install/non_interactive.php", timeout=60, follow_redirects=True, trust_env=False)
    except httpx2.HTTPError:
        pass  # 已安装过的容器可能返回非 200，忽略
    has_close = _sh("docker", "exec", CONTAINER, "grep", "-c", "CloseVerify", API_CONF, check=False)
    if has_close.returncode != 0:  # grep 未命中才写入，避免重复插入
        _sh(
            "docker", "exec", CONTAINER, "sed", "-i",
            # 注意：传给 sed 的必须是字面 \n（sed 自行解释为换行），Python 里写 \\n
            "s/^return array(/return array(\\n    'CloseVerify' => true,/", API_CONF,
        )
        _sh("docker", "restart", CONTAINER)
        _wait_http(BASE_URL)

    # 4) 登录并替换 tools 层的 client 单例
    cookies = _login_cookies()
    client = ShowDocClient(
        base_url=BASE_URL,
        cookie_token=cookies["cookie_token"],
        phpsessid=cookies["PHPSESSID"],
    )
    tools._client = client

    # 5) 专属项目：按名复用或新建，清场上次残留
    item_id = next(
        (int(i["item_id"]) for i in client.my_items() if i.get("item_name") == ITEM_NAME), 0
    )
    if not item_id:
        item_id = client.item_add(ITEM_NAME, "pytest 集成测试专用项目，可随时删除")
    client.default_item_id = item_id
    clear_item(confirm=True)

    yield client

    tools._client = None


def _uid() -> str:
    return uuid.uuid4().hex[:6]


def _cat_id(result: str) -> int:
    m = re.search(r"cat_id=(\d+)", result)
    assert m, result
    return int(m.group(1))


def _page_id(result: str) -> int:
    m = re.search(r"page_id=(\d+)", result)
    assert m, result
    return int(m.group(1))


# ---------------------------------------------------------------- 读取基础


def test_list_items_contains_project(live):
    assert ITEM_NAME in list_items()


def test_get_item_tree_format(live):
    r = get_item_tree()
    assert re.search(r"共 \d+ 个目录 / \d+ 个页面", r), r


# ------------------------------------------------- save_page 目录归属（重点）


def test_save_page_create_in_catalog(live):
    """新建页面落在指定目录（真实服务端验证归属，不是 Fake 的内存状态）。"""
    cat = _cat_id(create_catalog(f"归属-{_uid()}"))
    pid = _page_id(save_page(f"页面-{_uid()}", "正文 v1", cat_id=cat))
    assert int(live.page_info(pid)["cat_id"]) == cat


def test_save_page_update_keeps_catalog(live):
    """核心回归（真实环境版）：编辑页面省略 cat_id 时必须保持原目录，不许掉到顶级。"""
    cat = _cat_id(create_catalog(f"编辑-{_uid()}"))
    pid = _page_id(save_page(f"编辑页-{_uid()}", "正文 v1", cat_id=cat))
    assert int(live.page_info(pid)["cat_id"]) == cat
    r = save_page("编辑后标题", "正文 v2", page_id=pid)  # 模型最常见的忘传 cat_id 场景
    assert "已更新页面" in r
    assert int(live.page_info(pid)["cat_id"]) == cat, "更新后页面掉出原目录！"


def test_save_page_update_moves_when_cat_given(live):
    """更新时显式传 cat_id → 移动页面（回收站恢复后归位依赖此行为）。"""
    cat = _cat_id(create_catalog(f"归位-{_uid()}"))
    pid = _page_id(save_page(f"归位页-{_uid()}", "正文", cat_id=0))  # 顶级
    save_page("归位页", "正文 v2", page_id=pid, cat_id=cat)
    assert int(live.page_info(pid)["cat_id"]) == cat


# ------------------------------------------------- 搜索 / 历史 / 回收站


def test_search_and_history(live):
    tag = _uid()
    pid = _page_id(save_page(f"搜索页-{tag}", f"# 搜索页\n\n唯一标记 {tag}"))
    assert f"搜索页-{tag}" in search_pages(f"标记 {tag}")
    assert "没有匹配" in search_pages(f"不存在-{_uid()}")
    assert live.page_history(pid) == []  # ShowDoc 真实行为：新建无历史
    save_page(f"搜索页-{tag}", "内容 v2", page_id=pid)
    save_page(f"搜索页-{tag}", "内容 v3", page_id=pid)
    hist = live.page_history(pid)
    assert len(hist) == 2, "每次更新存一版（旧内容）"


def test_get_page_roundtrip(live):
    tag = _uid()
    title = f"往返-{tag}"
    pid = _page_id(save_page(title, f"# {title}\n\n正文含 markdown 表格 `|a|b|`"))
    text = get_page(pid)
    assert title in text and "正文含 markdown 表格" in text


def test_delete_recycle_recover(live):
    """真实验证：删除进回收站、恢复后落顶级目录（ShowDoc 真实行为，Fake 曾靠模拟）。"""
    cat = _cat_id(create_catalog(f"回收站-{_uid()}"))
    pid = _page_id(save_page(f"待删-{_uid()}", "内容", cat_id=cat))
    delete_page(pid)
    assert f"page_id={pid}" in list_recycle() or "待删" in list_recycle()
    r = recover_page(pid)
    assert "已从回收站恢复" in r and "顶级" in r
    assert int(live.page_info(pid)["cat_id"]) == 0  # ShowDoc 真实行为：恢复落顶级
    # 归位（用 save_page 显式 cat_id 移动能力）
    save_page(live.page_info(pid)["page_title"], "内容", page_id=pid, cat_id=cat)
    assert int(live.page_info(pid)["cat_id"]) == cat


# ------------------------------------------------- 批量上传


def test_upload_markdown_dir(live, tmp_path):
    tag = _uid()
    d1 = tmp_path / f"01 指南-{tag}"
    d1.mkdir()
    (d1 / f"{d1.name}.md").write_text(f"# {d1.name}\n\n指南内容", encoding="utf-8")
    d2 = d1 / "进阶"
    d2.mkdir()
    (d2 / "进阶篇.md").write_text("# 进阶篇\n\n进阶内容", encoding="utf-8")

    dry = upload_markdown_dir(str(tmp_path), dry_run=True)
    assert "dry" in dry and f"指南-{tag}" in dry

    r = upload_markdown_dir(str(tmp_path))
    assert "上传完成" in r and "新建目录 2 个" in r
    tree = get_item_tree()
    assert f"指南-{tag}" in tree and "进阶篇" in tree

    r2 = upload_markdown_dir(str(tmp_path))  # 幂等重跑：不重复创建
    tree2 = get_item_tree()
    assert tree2.count("进阶篇") == tree.count("进阶篇") == 1
