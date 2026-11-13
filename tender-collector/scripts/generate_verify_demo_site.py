#!/usr/bin/env python3
"""生成“中标候选人核对”离线示例站（纯静态 HTML，数据可复现）。

写入 demo_site_verify/，与采集服务示例站 (demo_site/) 相互独立，
docker compose 以同一 nginx 同时托管两个目录。

场景围绕原公告 SZ-2026-002 的 5 个标段展开，拟发中标公告
WIN-SZ-2026-002 发布于 2026-11-20 10:00（核对 cutoff）：

| 标段 | 场景 | 期望结论 |
| --- | --- | --- |
| 1 | 第 1 名被取消资格，第 2 名顺位且与中标人一致 | match |
| 2 | 无取消；含同名次同企业重复公示，应去重 | match |
| 3 | 第 1 名仍有效，中标人却写成第 2 名企业 | mismatch |
| 4 | 第 1 名并列两家不同企业（同名次不同企业） | undeterminable |
| 5 | 取消资格公示引用了不存在的候选人编号（引用失效） | undeterminable |

另含两条干扰数据，专门验证过滤逻辑：
- 取消资格公示 DQ-2026-006 发布于 cutoff 之后，必须被忽略；
- 候选人公示 HC-OTHER-1 引用其他原公告编号，必须被关联条件剔除。
"""
from __future__ import annotations

import html
from dataclasses import dataclass, field
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent.parent / "demo_site_verify"
PAGE_SIZE = 4
ORIGINAL_CODE = "SZ-2026-002"
CUTOFF = "2026-11-20 10:00"


@dataclass
class CandidateEntry:
    rank: int
    candidate_code: str
    enterprise: str


@dataclass
class CandidateDoc:
    code: str
    title: str
    published_at: str
    sections: dict[str, list[CandidateEntry]]  # 标段号 -> 候选人列表
    original_code: str = ORIGINAL_CODE
    filename: str = ""

    def __post_init__(self) -> None:
        self.filename = f"{self.code}.html"


@dataclass
class DisqualificationDoc:
    code: str
    title: str
    published_at: str
    # 标段号 -> 被取消者候选人编号（None 表示页面缺失，由解析层转引用失效）
    sections: dict[str, str | None]
    original_code: str = ORIGINAL_CODE

    @property
    def filename(self) -> str:
        return f"{self.code}.html"


@dataclass
class WinnerDoc:
    code: str
    title: str
    published_at: str
    sections: dict[str, str]  # 标段号 -> 拟发中标企业
    original_code: str = ORIGINAL_CODE

    @property
    def filename(self) -> str:
        return f"{self.code}.html"


# ---------------------------------------------------------------------------
# 候选人公示（8 份；含重复公示与跨原公告干扰项）
# ---------------------------------------------------------------------------
CANDIDATE_DOCS: list[CandidateDoc] = [
    CandidateDoc(
        code="HC-2026-001",
        title="城北污水处理厂扩建工程中标候选人公示（第一批）",
        published_at="2026-11-08 09:30",
        sections={
            "1": [
                CandidateEntry(1, "HC-001-1-01", "华城市政工程有限公司"),
                CandidateEntry(2, "HC-001-1-02", "远通路桥建设集团"),
                CandidateEntry(3, "HC-001-1-03", "恒泰建设股份有限公司"),
            ],
            "2": [
                CandidateEntry(1, "HC-001-2-01", "青云水利建设有限公司"),
                CandidateEntry(2, "HC-001-2-02", "广厦建工集团"),
            ],
        },
    ),
    CandidateDoc(
        code="HC-2026-002",
        title="城北污水处理厂扩建工程中标候选人公示（第二批）",
        published_at="2026-11-09 10:00",
        sections={
            "3": [
                CandidateEntry(1, "HC-001-3-01", "清源环境工程有限公司"),
                CandidateEntry(2, "HC-001-3-02", "碧水科技发展有限公司"),
            ],
            "4": [
                CandidateEntry(1, "HC-001-4-01", "中铁二十二局集团有限公司"),
                CandidateEntry(1, "HC-001-4-02", "中交水利水电建设有限公司"),
                CandidateEntry(2, "HC-001-4-03", "宏远市政工程公司"),
            ],
        },
    ),
    CandidateDoc(
        code="HC-2026-003",
        title="城北污水处理厂扩建工程中标候选人公示（第三批）",
        published_at="2026-11-10 11:15",
        sections={
            "5": [
                CandidateEntry(1, "HC-001-5-01", "润泽环保科技有限公司"),
                CandidateEntry(2, "HC-001-5-02", "安澜建设工程有限公司"),
            ],
        },
    ),
    # 标段 2 重复公示：同编号、同名次、同企业，核对层应按候选人编号去重
    CandidateDoc(
        code="HC-2026-004",
        title="城北污水处理厂扩建工程中标候选人公示（标段2复核公示）",
        published_at="2026-11-12 14:20",
        sections={
            "2": [
                CandidateEntry(1, "HC-001-2-01", "青云水利建设有限公司"),
                CandidateEntry(2, "HC-001-2-02", "广厦建工集团"),
            ],
        },
    ),
    # 干扰项：属于其他原公告，关联时必须剔除
    CandidateDoc(
        code="HC-OTHER-1",
        title="滨河路市政改造工程中标候选人公示",
        published_at="2026-11-11 09:00",
        original_code="SZ-2026-001",
        sections={
            "1": [
                CandidateEntry(1, "HC-900-1-01", "其他项目第一候选人公司"),
            ],
        },
    ),
]

# ---------------------------------------------------------------------------
# 取消资格公示（4 份；1 份在 cutoff 之后，1 份引用失效）
# ---------------------------------------------------------------------------
DISQUALIFICATION_DOCS: list[DisqualificationDoc] = [
    # 标段 1：取消第 1 名，远通路桥（第 2 名）顺位为应中标人
    DisqualificationDoc(
        code="DQ-2026-001",
        title="城北污水处理厂扩建工程取消中标候选人资格公示（标段1）",
        published_at="2026-11-14 16:30",
        sections={"1": "HC-001-1-01"},
    ),
    # 标段 3：取消第 2 名（不影响第 1 名的首位地位）
    DisqualificationDoc(
        code="DQ-2026-002",
        title="城北污水处理厂扩建工程取消中标候选人资格公示（标段3）",
        published_at="2026-11-15 10:45",
        sections={"3": "HC-001-3-02"},
    ),
    # 标段 5：引用了该标段不存在的候选人编号 => 引用失效，无法判定
    DisqualificationDoc(
        code="DQ-2026-003",
        title="城北污水处理厂扩建工程取消中标候选人资格公示（标段5）",
        published_at="2026-11-16 15:00",
        sections={"5": "HC-999-5-99"},
    ),
    # 发布于拟发中标公告之后，必须被 cutoff 过滤掉
    DisqualificationDoc(
        code="DQ-2026-006",
        title="城北污水处理厂扩建工程取消中标候选人资格公示（滞后发布）",
        published_at="2026-11-22 09:10",
        sections={"2": "HC-001-2-01"},
    ),
]

# ---------------------------------------------------------------------------
# 拟发中标公告
# ---------------------------------------------------------------------------
WINNER_DOC = WinnerDoc(
    code="WIN-SZ-2026-002",
    title="城北污水处理厂扩建工程中标公告（拟发布）",
    published_at=CUTOFF,
    sections={
        "1": "远通路桥建设集团",      # 取消后顺位 => match
        "2": "青云水利建设有限公司",  # 第 1 名未变 => match
        "3": "碧水科技发展有限公司",  # 第 1 名清源仍有效 => mismatch
        "4": "中铁二十二局集团有限公司",  # 同名次不同企业 => undeterminable
        "5": "润泽环保科技有限公司",  # 取消引用失效 => undeterminable
    },
)


def _esc(value: str) -> str:
    return html.escape(str(value), quote=True)


def _candidate_page(doc: CandidateDoc) -> str:
    section_blocks = []
    for section_no, entries in doc.sections.items():
        trs = "\n".join(
            '          <tr data-rank="{rank}">'
            '<td data-field="名次">{rank}</td>'
            '<td data-field="候选人编号">{code}</td>'
            '<td data-field="候选企业">{ent}</td></tr>'.format(
                rank=e.rank, code=_esc(e.candidate_code), ent=_esc(e.enterprise)
            )
            for e in entries
        )
        section_blocks.append(
            f'      <table class="candidate-table">\n'
            f'        <caption>标段 {_esc(section_no)} 中标候选人</caption>\n'
            f'        <tbody data-section-no="{_esc(section_no)}">\n'
            f"{trs}\n"
            f"        </tbody>\n"
            f"      </table>"
        )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>{_esc(doc.title)}</title>
</head>
<body>
  <article class="announcement-detail" data-code="{_esc(doc.code)}"
           data-kind="candidate"
           data-original-code="{_esc(doc.original_code)}"
           data-published-at="{_esc(doc.published_at)}">
    <h1 class="detail-title">{_esc(doc.title)}</h1>
    <table class="detail-table">
      <tr><th data-field="公告编号">公告编号</th><td data-value="公告编号">{_esc(doc.code)}</td></tr>
      <tr><th data-field="公告类型">公告类型</th><td data-value="公告类型">中标候选人公示</td></tr>
      <tr><th data-field="原公告编号">原公告编号</th><td data-value="原公告编号">{_esc(doc.original_code)}</td></tr>
      <tr><th data-field="发布时间">发布时间</th><td data-value="发布时间">{_esc(doc.published_at)}</td></tr>
    </table>
{chr(10).join(section_blocks)}
    <p><a href="catalog_1.html">返回候选人公示目录</a></p>
  </article>
</body>
</html>
"""


def _disqualification_page(doc: DisqualificationDoc) -> str:
    trs = []
    for section_no, ref in doc.sections.items():
        ref_cell = (
            f'<td data-field="被取消者候选人编号">{_esc(ref)}</td>'
            if ref is not None
            else '<td data-field="被取消者候选人编号"></td>'
        )
        trs.append(
            '          <tr>'
            f'<td data-field="标段号">{_esc(section_no)}</td>'
            f"{ref_cell}"
            '<td data-field="取消原因">投标文件提供虚假业绩材料</td></tr>'
        )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>{_esc(doc.title)}</title>
</head>
<body>
  <article class="announcement-detail" data-code="{_esc(doc.code)}"
           data-kind="disqualification"
           data-original-code="{_esc(doc.original_code)}"
           data-published-at="{_esc(doc.published_at)}">
    <h1 class="detail-title">{_esc(doc.title)}</h1>
    <table class="detail-table">
      <tr><th data-field="公告编号">公告编号</th><td data-value="公告编号">{_esc(doc.code)}</td></tr>
      <tr><th data-field="公告类型">公告类型</th><td data-value="公告类型">取消资格公示</td></tr>
      <tr><th data-field="原公告编号">原公告编号</th><td data-value="原公告编号">{_esc(doc.original_code)}</td></tr>
      <tr><th data-field="发布时间">发布时间</th><td data-value="发布时间">{_esc(doc.published_at)}</td></tr>
    </table>
    <table class="disqualification-table">
      <caption>取消中标候选人资格明细</caption>
      <tbody>
{chr(10).join(trs)}
      </tbody>
    </table>
    <p><a href="catalog_1.html">返回候选人公示目录</a></p>
  </article>
</body>
</html>
"""


def _winner_page(doc: WinnerDoc) -> str:
    trs = []
    for section_no, enterprise in doc.sections.items():
        trs.append(
            '          <tr>'
            f'<td data-field="标段号">{_esc(section_no)}</td>'
            f'<td data-field="中标企业">{_esc(enterprise)}</td></tr>'
        )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>{_esc(doc.title)}</title>
</head>
<body>
  <article class="announcement-detail" data-code="{_esc(doc.code)}"
           data-kind="winner"
           data-original-code="{_esc(doc.original_code)}"
           data-published-at="{_esc(doc.published_at)}">
    <h1 class="detail-title">{_esc(doc.title)}</h1>
    <table class="detail-table">
      <tr><th data-field="公告编号">公告编号</th><td data-value="公告编号">{_esc(doc.code)}</td></tr>
      <tr><th data-field="公告类型">公告类型</th><td data-value="公告类型">中标公告</td></tr>
      <tr><th data-field="原公告编号">原公告编号</th><td data-value="原公告编号">{_esc(doc.original_code)}</td></tr>
      <tr><th data-field="发布时间">发布时间</th><td data-value="发布时间">{_esc(doc.published_at)}</td></tr>
    </table>
    <table class="winner-table">
      <caption>各标段中标人</caption>
      <tbody>
{chr(10).join(trs)}
      </tbody>
    </table>
  </article>
</body>
</html>
"""


def _catalog_page(items: list[tuple[str, str, str, str, str | None]], page: int,
                  total_pages: int) -> str:
    """items: (filename, kind, title, published_at, section_no 标记)。"""
    lis = []
    for filename, kind, title, published_at, section_no in items:
        section_attr = (
            f' data-section-no="{_esc(section_no)}"' if section_no is not None else ""
        )
        kind_label = "中标候选人公示" if kind == "candidate" else "取消资格公示"
        lis.append(
            f'      <li class="announcement-item" data-kind="{kind}"{section_attr}>\n'
            f'        <a class="detail-link" href="{_esc(filename)}">{_esc(title)}</a>\n'
            f'        <span class="announcement-kind">{kind_label}</span>\n'
            f'        <span class="published-at">{_esc(published_at)}</span>\n'
            f"      </li>"
        )
    nav_parts = []
    for p in range(1, total_pages + 1):
        if p == page:
            nav_parts.append(f'<span class="page-current">{p}</span>')
        else:
            nav_parts.append(f'<a class="page-link" href="catalog_{p}.html">{p}</a>')
    next_link = (
        f'<a class="page-next" href="catalog_{page + 1}.html">下一页</a>'
        if page < total_pages
        else '<span class="page-next-disabled">下一页</span>'
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>中标候选人公示目录 - 第 {page} 页</title>
</head>
<body>
  <h1>中标候选人 / 取消资格公示目录（第 {page}/{total_pages} 页）</h1>
  <ul class="announcement-list" data-page="{page}">
{chr(10).join(lis)}
  </ul>
  <nav class="pagination">
    {''.join(nav_parts)}
    {next_link}
  </nav>
</body>
</html>
"""


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    for doc in CANDIDATE_DOCS:
        (OUT_DIR / doc.filename).write_text(_candidate_page(doc), encoding="utf-8")
    for doc in DISQUALIFICATION_DOCS:
        (OUT_DIR / doc.filename).write_text(
            _disqualification_page(doc), encoding="utf-8"
        )
    (OUT_DIR / WINNER_DOC.filename).write_text(
        _winner_page(WINNER_DOC), encoding="utf-8"
    )

    # 目录项：按发布时间倒序制造跨页乱序；section 标记取文档涉及的第一个标段
    items: list[tuple[str, str, str, str, str | None]] = []
    for doc in CANDIDATE_DOCS:
        first_section = next(iter(doc.sections))
        items.append(
            (doc.filename, "candidate", doc.title, doc.published_at, first_section)
        )
    for doc in DISQUALIFICATION_DOCS:
        first_section = next(iter(doc.sections))
        items.append(
            (doc.filename, "disqualification", doc.title, doc.published_at, first_section)
        )
    items.sort(key=lambda it: it[3], reverse=True)

    total_pages = (len(items) + PAGE_SIZE - 1) // PAGE_SIZE
    for page in range(1, total_pages + 1):
        chunk = items[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
        (OUT_DIR / f"catalog_{page}.html").write_text(
            _catalog_page(chunk, page, total_pages), encoding="utf-8"
        )

    (OUT_DIR / "index.html").write_text(
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        '<meta http-equiv="refresh" content="0; url=catalog_1.html">'
        "<title>候选人公示目录</title></head><body>跳转中…</body></html>\n",
        encoding="utf-8",
    )
    print(
        f"generated {len(CANDIDATE_DOCS)} candidate pages, "
        f"{len(DISQUALIFICATION_DOCS)} disqualification pages, "
        f"1 winner page, {total_pages} catalog pages in {OUT_DIR}"
    )


if __name__ == "__main__":
    main()
