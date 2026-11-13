"""中标候选人核对 API 测试：纯逻辑单测 + 线程内静态服务器端到端测试。

示例站由 scripts/generate_verify_demo_site.py 生成到独立临时目录，
通过 stdlib http.server 托管，FastAPI TestClient 走完整核对流程。
"""
from __future__ import annotations

import http.server
import importlib.util
import sys
import threading
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


gen_verify = _load_module(
    "generate_verify_demo_site",
    ROOT / "scripts" / "generate_verify_demo_site.py",
)


def make_flaky_handler(directory: Path):
    """构造绑定静态目录、支持 404 故障注入的 handler 类。"""

    class FlakyHandler(http.server.SimpleHTTPRequestHandler):
        # 类属性：需要返回 404 的路径集合（如 /catalog_2.html）
        fail_paths: set[str] = set()

        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(directory), **kwargs)

        def do_GET(self):  # noqa: N802
            if self.path in self.fail_paths:
                self.send_error(404, "injected failure")
                return
            return super().do_GET()

        def log_message(self, *args):  # 静默
            pass

    return FlakyHandler


def start_server(directory: Path):
    handler_cls = make_flaky_handler(directory)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    return f"http://{host}:{port}", httpd, handler_cls


@pytest.fixture(scope="module")
def verify_server(tmp_path_factory):
    site_dir = tmp_path_factory.mktemp("verify_site")
    # 生成器的输出目录是模块级常量，临时改写到临时目录
    gen_verify.OUT_DIR = site_dir
    gen_verify.main()
    base, httpd, handler_cls = start_server(site_dir)
    try:
        yield base, httpd, handler_cls
    finally:
        httpd.shutdown()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TENDER_DB_PATH", str(tmp_path / "verify.db"))
    from app import config, database
    importlib.reload(config)
    import app.main as main
    importlib.reload(database)
    importlib.reload(main)
    database.init_db()

    # 写入核对所需的原公告锚点（模拟此前采集入库的结果）
    with database.get_conn() as conn:
        database.upsert_announcements(
            conn,
            [
                {
                    "code": gen_verify.ORIGINAL_CODE,
                    "title": "城北污水处理厂扩建工程招标公告",
                    "kind": "original",
                    "project_name": "城北污水处理厂扩建工程",
                    "published_at": "2026-10-12 10:30",
                    "deadline": "2026-11-10 09:00",
                    "corrects_code": None,
                    "detail_url": "http://demo-site/SZ-2026-002.html",
                }
            ],
        )
        conn.commit()

    with TestClient(main.app) as c:
        yield c, main, database


def _call(client, base, **overrides):
    payload = {
        "original_code": gen_verify.ORIGINAL_CODE,
        "catalog_url": f"{base}/catalog_1.html",
        "winner_url": f"{base}/{gen_verify.WINNER_DOC.filename}",
    }
    payload.update(overrides)
    return client.post("/api/verify-winner", json=payload)


def _section_map(payload):
    data = payload.json() if hasattr(payload, "json") else payload
    return {s["section_no"]: s for s in data["sections"]}


# ---------------------------------------------------------------------------
# 端到端：核对 API
# ---------------------------------------------------------------------------
def test_verify_all_sections(client, verify_server):
    c, _, _ = client
    base, _, _ = verify_server
    resp = _call(c, base)
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["status"] == "mismatch"
    assert payload["winner_published_at"] == gen_verify.CUTOFF
    sections = _section_map(payload)
    assert set(sections) == {"1", "2", "3", "4", "5"}

    # 标段 1：第 1 名被取消，第 2 名顺位，与拟发中标人一致
    s1 = sections["1"]
    assert s1["status"] == "match"
    assert s1["expected_winner"] == "远通路桥建设集团"
    assert s1["announced_winner"] == "远通路桥建设集团"
    assert any(u.endswith("DQ-2026-001.html") for u in s1["disqualification_source_urls"])
    assert any(u.endswith("HC-2026-001.html") for u in s1["candidate_source_urls"])
    assert s1["winner_source_url"].endswith("WIN-SZ-2026-002.html")

    # 标段 2：重复公示去重后第 1 名不变，且 cutoff 之后的 DQ-2026-006 被忽略
    s2 = sections["2"]
    assert s2["status"] == "match"
    assert s2["expected_winner"] == "青云水利建设有限公司"
    assert all(
        not u.endswith("DQ-2026-006.html")
        for u in s2["disqualification_source_urls"]
    )

    # 标段 3：第 1 名仍有效，拟发中标人写成第 2 名企业 => mismatch
    s3 = sections["3"]
    assert s3["status"] == "mismatch"
    assert s3["expected_winner"] == "清源环境工程有限公司"
    assert s3["announced_winner"] == "碧水科技发展有限公司"

    # 标段 4：同名次不同企业 => 无法判定
    s4 = sections["4"]
    assert s4["status"] == "undeterminable"
    assert s4["expected_winner"] is None
    assert "第 1 名" in s4["reason"]

    # 标段 5：取消资格引用失效 => 无法判定
    s5 = sections["5"]
    assert s5["status"] == "undeterminable"
    assert "引用失效" in s5["reason"]
    assert "HC-999-5-99" in s5["reason"]


def test_verify_other_original_code_is_ignored(client, verify_server):
    """候选人公示目录中其他原公告的记录必须被关联条件剔除。"""
    c, _, _ = client
    base, _, _ = verify_server
    payload = _call(c, base).json()
    sections = _section_map(payload)
    for section in sections.values():
        assert all(
            "HC-OTHER-1" not in u for u in section["candidate_source_urls"]
        )


def test_catalog_page_failure_marks_all_sections(client, verify_server):
    c, _, _ = client
    base, _, handler_cls = verify_server
    handler_cls.fail_paths = {"/catalog_2.html"}
    try:
        resp = _call(c, base)
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["status"] == "undeterminable"
        assert all(s["status"] == "undeterminable" for s in payload["sections"])
        assert all("目录读取失败" in s["reason"] for s in payload["sections"])
        # 拟发中标人信息仍要带回
        assert all(s["announced_winner"] for s in payload["sections"])
    finally:
        handler_cls.fail_paths = set()


def test_single_detail_failure_with_section_only_affects_that_section(
    client, verify_server
):
    c, _, _ = client
    base, _, handler_cls = verify_server
    # HC-2026-002 覆盖标段 3、4，列表项 data-section-no 标为首个标段 3；
    # 故障只应波及该页涉及的标段，标段 1、2、5 的正常结论不受影响
    handler_cls.fail_paths = {"/HC-2026-002.html"}
    try:
        resp = _call(c, base)
        assert resp.status_code == 200
        sections = _section_map(resp.json())
        assert sections["3"]["status"] == "undeterminable"
        assert "读取失败" in sections["3"]["reason"]
        assert sections["4"]["status"] == "undeterminable"
        # 其余标段保持各自的正常结论
        assert sections["1"]["status"] == "match"
        assert sections["2"]["status"] == "match"
        # 标段 5 仍因取消引用失效而无法判定，原因不被故障覆盖
        assert sections["5"]["status"] == "undeterminable"
        assert "引用失效" in sections["5"]["reason"]
    finally:
        handler_cls.fail_paths = set()


def test_detail_failure_without_section_marks_all(client, tmp_path):
    """单条详情失败且列表项未标明标段 => 全部标段无法判定。"""
    site_dir = tmp_path / "site_no_section"
    gen_verify.OUT_DIR = site_dir
    gen_verify.main()
    # 去掉目录列表项上的 data-section-no 标记
    for catalog in site_dir.glob("catalog_*.html"):
        text = catalog.read_text(encoding="utf-8")
        for n in ("1", "2", "3", "4", "5"):
            text = text.replace(f' data-section-no="{n}"', "")
        catalog.write_text(text, encoding="utf-8")

    base, httpd, handler_cls = start_server(site_dir)
    handler_cls.fail_paths = {"/HC-2026-002.html"}
    try:
        c, _, _ = client
        resp = _call(c, base)
        assert resp.status_code == 200
        payload = resp.json()
        assert all(s["status"] == "undeterminable" for s in payload["sections"])
        assert all("未标明所属标段" in s["reason"] for s in payload["sections"])
    finally:
        handler_cls.fail_paths = set()
        httpd.shutdown()


def test_winner_detail_failure_returns_422(client, verify_server):
    c, _, _ = client
    base, _, handler_cls = verify_server
    handler_cls.fail_paths = {f"/{gen_verify.WINNER_DOC.filename}"}
    try:
        resp = _call(c, base)
        assert resp.status_code == 422
        assert "HTTP 404" in resp.json()["detail"]
    finally:
        handler_cls.fail_paths = set()


def test_unknown_original_code_returns_422(client, verify_server):
    c, _, _ = client
    base, _, _ = verify_server
    resp = _call(c, base, original_code="SZ-2026-999")
    assert resp.status_code == 422
    assert "未在采集库" in resp.json()["detail"]


def test_winner_original_code_mismatch_returns_422(client, tmp_path):
    site_dir = tmp_path / "site_wrong_ref"
    gen_verify.OUT_DIR = site_dir
    gen_verify.main()
    winner = site_dir / gen_verify.WINNER_DOC.filename
    winner.write_text(
        winner.read_text(encoding="utf-8").replace(
            gen_verify.ORIGINAL_CODE, "SZ-2026-001"
        ),
        encoding="utf-8",
    )
    base, httpd, _ = start_server(site_dir)
    try:
        c, _, _ = client
        resp = _call(c, base)
        assert resp.status_code == 422
        assert "不一致" in resp.json()["detail"]
    finally:
        httpd.shutdown()


def test_verification_does_not_write_database(client, verify_server):
    c, _, database = client
    base, _, _ = verify_server
    with database.get_conn() as conn:
        before = conn.execute("SELECT COUNT(*) FROM announcements").fetchone()[0]
    assert _call(c, base).status_code == 200
    with database.get_conn() as conn:
        after = conn.execute("SELECT COUNT(*) FROM announcements").fetchone()[0]
        runs = conn.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0]
    assert before == after == 1
    assert runs == 0


# ---------------------------------------------------------------------------
# 纯逻辑单测：app.verify
# ---------------------------------------------------------------------------
def _cand(section, rank, code, ent, published="2026-11-10 09:00"):
    return {
        "original_code": "T-1",
        "section_no": section,
        "rank": rank,
        "candidate_code": code,
        "enterprise": ent,
        "published_at": published,
        "detail_url": f"http://x/{code}.html",
    }


def _dq(section, ref, published="2026-11-15 09:00", code="DQ-1"):
    return {
        "code": code,
        "original_code": "T-1",
        "section_no": section,
        "disqualified_candidate_code": ref,
        "published_at": published,
        "detail_url": f"http://x/{code}.html",
    }


def _winner(section, ent):
    return {"section_no": section, "enterprise": ent, "detail_url": "http://x/w.html"}


class TestVerifySections:
    def test_disqualified_first_promotes_second(self):
        from app.verify import verify_sections

        results = verify_sections(
            "T-1",
            [_cand("1", 1, "C1", "甲公司"), _cand("1", 2, "C2", "乙公司")],
            [_dq("1", "C1")],
            [_winner("1", "乙公司")],
            datetime(2026, 11, 20, 10, 0),
        )
        assert len(results) == 1
        r = results[0]
        assert r.status == "match"
        assert r.expected_winner == "乙公司"

    def test_mismatch_when_winner_is_not_top_valid(self):
        from app.verify import verify_sections

        results = verify_sections(
            "T-1",
            [_cand("1", 1, "C1", "甲公司"), _cand("1", 2, "C2", "乙公司")],
            [],
            [_winner("1", "乙公司")],
            datetime(2026, 11, 20, 10, 0),
        )
        assert results[0].status == "mismatch"
        assert results[0].expected_winner == "甲公司"

    def test_same_rank_different_enterprises_undeterminable(self):
        from app.verify import verify_sections

        results = verify_sections(
            "T-1",
            [_cand("1", 1, "C1", "甲公司"), _cand("1", 1, "C2", "乙公司")],
            [],
            [_winner("1", "甲公司")],
            datetime(2026, 11, 20, 10, 0),
        )
        assert results[0].status == "undeterminable"
        assert results[0].expected_winner is None

    def test_broken_dq_reference_undeterminable(self):
        from app.verify import verify_sections

        results = verify_sections(
            "T-1",
            [_cand("1", 1, "C1", "甲公司")],
            [_dq("1", "GHOST")],
            [_winner("1", "甲公司")],
            datetime(2026, 11, 20, 10, 0),
        )
        assert results[0].status == "undeterminable"
        assert "引用失效" in results[0].reason

    def test_records_after_cutoff_are_ignored(self):
        from app.verify import verify_sections

        cutoff = datetime(2026, 11, 20, 10, 0)
        # 取消公示晚于 cutoff：不应剔除第 1 名
        results = verify_sections(
            "T-1",
            [_cand("1", 1, "C1", "甲公司"), _cand("1", 2, "C2", "乙公司")],
            [_dq("1", "C1", published="2026-11-21 08:00")],
            [_winner("1", "甲公司")],
            cutoff,
        )
        assert results[0].status == "match"

        # 候选人公示晚于 cutoff：该标段无有效候选人
        results2 = verify_sections(
            "T-1",
            [_cand("2", 1, "C9", "丙公司", published="2026-11-21 08:00")],
            [],
            [_winner("2", "丙公司")],
            cutoff,
        )
        assert results2[0].status == "undeterminable"

    def test_other_original_code_excluded(self):
        from app.verify import verify_sections

        results = verify_sections(
            "T-1",
            [
                _cand("1", 1, "C1", "甲公司"),
                {**_cand("1", 1, "X1", "其他公司"), "original_code": "T-OTHER"},
            ],
            [],
            [_winner("1", "甲公司")],
            datetime(2026, 11, 20, 10, 0),
        )
        assert results[0].status == "match"
        assert results[0].expected_winner == "甲公司"

    def test_missing_winner_section_undeterminable(self):
        from app.verify import verify_sections

        results = verify_sections(
            "T-1",
            [_cand("1", 1, "C1", "甲公司"), _cand("2", 1, "C2", "乙公司")],
            [],
            [_winner("1", "甲公司")],
            datetime(2026, 11, 20, 10, 0),
        )
        by_section = {r.section_no: r for r in results}
        assert by_section["1"].status == "match"
        assert by_section["2"].status == "undeterminable"
        assert by_section["2"].announced_winner is None

    def test_duplicate_candidate_code_dedupes(self):
        from app.verify import verify_sections

        results = verify_sections(
            "T-1",
            [
                _cand("1", 1, "C1", "甲公司"),
                _cand("1", 1, "C1", "甲公司"),
                _cand("1", 2, "C2", "乙公司"),
            ],
            [],
            [_winner("1", "甲公司")],
            datetime(2026, 11, 20, 10, 0),
        )
        assert results[0].status == "match"

    def test_all_candidates_disqualified_undeterminable(self):
        from app.verify import verify_sections

        results = verify_sections(
            "T-1",
            [_cand("1", 1, "C1", "甲公司")],
            [_dq("1", "C1")],
            [_winner("1", "甲公司")],
            datetime(2026, 11, 20, 10, 0),
        )
        assert results[0].status == "undeterminable"

    def test_overall_status_aggregation(self):
        from app.verify import (
            STATUS_MATCH,
            STATUS_MISMATCH,
            STATUS_UNDETERMINABLE,
            SectionResult,
            overall_status,
        )

        def r(status):
            return SectionResult(
                section_no="1",
                status=status,
                announced_winner=None,
                expected_winner=None,
                reason="",
                winner_source_url=None,
            )

        assert overall_status([r(STATUS_MATCH), r(STATUS_MATCH)]) == STATUS_MATCH
        assert (
            overall_status([r(STATUS_MATCH), r(STATUS_MISMATCH)])
            == STATUS_MISMATCH
        )
        assert (
            overall_status([r(STATUS_MATCH), r(STATUS_UNDETERMINABLE)])
            == STATUS_UNDETERMINABLE
        )
        assert overall_status([]) == STATUS_UNDETERMINABLE
