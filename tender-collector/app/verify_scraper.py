"""候选人公示目录 / 拟发中标公告详情的抓取与 HTML 解析（中标核对专用）。

与采集服务共用 httpx + BeautifulSoup 技术栈与解析约定：

- 目录列表页：`ul.announcement-list a.detail-link` 为详情链接，
  `nav.pagination a` 为分页链接；列表项通过 `data-kind` 区分
  候选人公示 (candidate) 与取消资格公示 (disqualification)，
  可带 `data-section-no` 标明所属标段；
- 候选人公示详情：`table.candidate-table` 内逐行给出
  名次 / 候选人编号 / 候选企业（可含多个标段分组，`data-section-no`）；
- 取消资格公示详情：表格给出原公告编号、标段号及被取消者候选人编号；
- 拟发中标公告详情：`table.winner-table` 内逐行给出标段号与中标企业。

失败语义区别于采集服务：目录分页读取失败属于整体失败（所有标段无法判定），
单条详情读取失败只标记该条；列表项标明了所属标段时只影响该标段，
否则影响本次核对的全部标段。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup, Tag

from .config import settings
from .scraper import CollectionError, _get, _same_origin  # 复用请求/同源工具

KIND_CANDIDATE = "candidate"
KIND_DISQUALIFICATION = "disqualification"


@dataclass(frozen=True)
class CatalogLink:
    detail_url: str
    kind: str
    section_no: str | None


@dataclass(frozen=True)
class FailedDetail:
    detail_url: str
    kind: str | None
    section_no: str | None
    error: str


def _parse_catalog_list(html_text: str, list_url: str) -> tuple[set[CatalogLink], set[str]]:
    soup = BeautifulSoup(html_text, "html.parser")
    links: set[CatalogLink] = set()
    page_urls: set[str] = set()

    for a in soup.select("ul.announcement-list a.detail-link[href]"):
        href = urljoin(list_url, a["href"])
        if not _same_origin(href, list_url):
            continue
        li = a.find_parent("li")
        kind = (li.get("data-kind") if isinstance(li, Tag) else None) or a.get("data-kind")
        section_no = (
            li.get("data-section-no") if isinstance(li, Tag) else None
        ) or a.get("data-section-no")
        if kind not in (KIND_CANDIDATE, KIND_DISQUALIFICATION):
            # 未知类型链接不参与核对，但不影响其他记录
            continue
        links.add(
            CatalogLink(
                detail_url=href,
                kind=kind,
                section_no=str(section_no).strip() if section_no else None,
            )
        )

    for a in soup.select("nav.pagination a[href]"):
        href = urljoin(list_url, a["href"])
        if _same_origin(href, list_url):
            page_urls.add(href)

    if not links:
        raise CollectionError(f"候选人公示目录 {list_url} 未解析到任何候选人/取消资格链接")
    return links, page_urls


def crawl_catalog(catalog_url: str) -> list[CatalogLink]:
    """沿分页遍历候选人公示目录，返回全部详情链接（去重、防环、限量）。"""
    headers = {"User-Agent": settings.user_agent}
    with httpx.Client(
        timeout=settings.request_timeout, follow_redirects=True, headers=headers
    ) as client:
        seen_pages: set[str] = set()
        queue: list[str] = [catalog_url]
        links: set[CatalogLink] = set()

        while queue:
            if len(seen_pages) >= settings.max_list_pages:
                raise CollectionError(
                    f"目录分页数量超过上限 {settings.max_list_pages}，疑似分页环"
                )
            page_url = queue.pop(0)
            if page_url in seen_pages:
                continue
            seen_pages.add(page_url)

            html_text = _get(client, page_url)  # 分页失败 => 抛出，整体无法判定
            page_links, more_pages = _parse_catalog_list(html_text, page_url)
            links.update(page_links)
            for u in more_pages:
                if u not in seen_pages and u not in queue:
                    queue.append(u)

    return sorted(links, key=lambda l: l.detail_url)


def _field(soup: BeautifulSoup, label: str) -> str | None:
    cell = soup.select_one(f'th[data-field="{label}"] + td')
    if cell is None:
        return None
    text = cell.get_text(strip=True)
    return text or None


def _article(soup: BeautifulSoup, detail_url: str) -> Tag:
    article = soup.select_one("article.announcement-detail")
    if article is None:
        raise CollectionError(f"详情页 {detail_url} 缺少 article.announcement-detail")
    return article


def _require(value: str | None, label: str, detail_url: str) -> str:
    if not value:
        raise CollectionError(f"详情页 {detail_url} 缺少必填字段: {label}")
    return value.strip()


def _parse_candidate_detail(
    html_text: str, detail_url: str
) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html_text, "html.parser")
    article = _article(soup, detail_url)
    original_code = _require(
        article.get("data-original-code") or _field(soup, "原公告编号"),
        "原公告编号",
        detail_url,
    )
    published_at = _require(
        article.get("data-published-at") or _field(soup, "发布时间"),
        "发布时间",
        detail_url,
    )

    rows: list[dict[str, Any]] = []
    groups = soup.select("table.candidate-table tbody[data-section-no]")
    if not groups:
        raise CollectionError(f"候选人公示 {detail_url} 缺少 table.candidate-table 标段分组")
    for group in groups:
        section_no = _require(group.get("data-section-no"), "标段号", detail_url)
        for tr in group.select("tr[data-rank]"):
            rank_text = _require(tr.get("data-rank"), "名次", detail_url)
            if not rank_text.isdigit():
                raise CollectionError(
                    f"候选人公示 {detail_url} 标段 {section_no} 名次非法: {rank_text!r}"
                )
            code_cell = tr.select_one('td[data-field="候选人编号"]')
            ent_cell = tr.select_one('td[data-field="候选企业"]')
            candidate_code = _require(
                code_cell.get_text(strip=True) if code_cell else None,
                "候选人编号",
                detail_url,
            )
            enterprise = _require(
                ent_cell.get_text(strip=True) if ent_cell else None,
                "候选企业",
                detail_url,
            )
            rows.append(
                {
                    "original_code": original_code,
                    "section_no": section_no,
                    "rank": int(rank_text),
                    "candidate_code": candidate_code,
                    "enterprise": enterprise,
                    "published_at": published_at.strip(),
                    "detail_url": detail_url,
                }
            )
    return rows


def _parse_disqualification_detail(
    html_text: str, detail_url: str
) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html_text, "html.parser")
    article = _article(soup, detail_url)
    original_code = _require(
        article.get("data-original-code") or _field(soup, "原公告编号"),
        "原公告编号",
        detail_url,
    )
    published_at = _require(
        article.get("data-published-at") or _field(soup, "发布时间"),
        "发布时间",
        detail_url,
    )
    code = _require(article.get("data-code") or _field(soup, "公告编号"), "公告编号", detail_url)

    rows: list[dict[str, Any]] = []
    for tr in soup.select("table.disqualification-table tbody tr"):
        sec_cell = tr.select_one('td[data-field="标段号"]')
        ref_cell = tr.select_one('td[data-field="被取消者候选人编号"]')
        section_no = _require(
            sec_cell.get_text(strip=True) if sec_cell else None, "标段号", detail_url
        )
        ref = (
            ref_cell.get_text(strip=True)
            if ref_cell is not None
            else None
        ) or None  # 引用编号缺失保留为 None，由核对层判定为引用失效
        rows.append(
            {
                "code": code,
                "original_code": original_code,
                "section_no": section_no,
                "disqualified_candidate_code": ref,
                "published_at": published_at.strip(),
                "detail_url": detail_url,
            }
        )
    if not rows:
        raise CollectionError(f"取消资格公示 {detail_url} 未列出任何被取消者")
    return rows


def parse_catalog_detail(
    html_text: str, detail_url: str, kind: str
) -> list[dict[str, Any]]:
    if kind == KIND_CANDIDATE:
        return _parse_candidate_detail(html_text, detail_url)
    if kind == KIND_DISQUALIFICATION:
        return _parse_disqualification_detail(html_text, detail_url)
    raise CollectionError(f"详情页 {detail_url} 类型未知: {kind!r}")


def fetch_catalog_details(
    links: list[CatalogLink],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[FailedDetail]]:
    """逐条抓取目录详情；单条失败转为 FailedDetail，不中断其余详情。"""
    candidates: list[dict[str, Any]] = []
    disqualifications: list[dict[str, Any]] = []
    failures: list[FailedDetail] = []

    headers = {"User-Agent": settings.user_agent}
    with httpx.Client(
        timeout=settings.request_timeout, follow_redirects=True, headers=headers
    ) as client:
        for link in links:
            try:
                html_text = _get(client, link.detail_url)
                rows = parse_catalog_detail(html_text, link.detail_url, link.kind)
            except CollectionError as exc:
                failures.append(
                    FailedDetail(
                        detail_url=link.detail_url,
                        kind=link.kind,
                        section_no=link.section_no,
                        error=str(exc),
                    )
                )
                continue
            if link.kind == KIND_CANDIDATE:
                candidates.extend(rows)
            else:
                disqualifications.extend(rows)

    return candidates, disqualifications, failures


def parse_winner_detail(html_text: str, detail_url: str) -> dict[str, Any]:
    """解析拟发中标公告详情：发布时间 + 各标段中标人。"""
    soup = BeautifulSoup(html_text, "html.parser")
    article = _article(soup, detail_url)
    original_code = _require(
        article.get("data-original-code") or _field(soup, "原公告编号"),
        "原公告编号",
        detail_url,
    )
    published_at = _require(
        article.get("data-published-at") or _field(soup, "发布时间"),
        "发布时间",
        detail_url,
    )

    winners: list[dict[str, Any]] = []
    for tr in soup.select("table.winner-table tbody tr"):
        sec_cell = tr.select_one('td[data-field="标段号"]')
        ent_cell = tr.select_one('td[data-field="中标企业"]')
        section_no = _require(
            sec_cell.get_text(strip=True) if sec_cell else None, "标段号", detail_url
        )
        enterprise = _require(
            ent_cell.get_text(strip=True) if ent_cell else None, "中标企业", detail_url
        )
        winners.append({"section_no": section_no, "enterprise": enterprise})
    if not winners:
        raise CollectionError(f"拟发中标公告 {detail_url} 未列出任何标段中标人")

    return {
        "original_code": original_code,
        "published_at": published_at.strip(),
        "winners": winners,
        "detail_url": detail_url,
    }


def fetch_winner_detail(winner_url: str) -> dict[str, Any]:
    headers = {"User-Agent": settings.user_agent}
    with httpx.Client(
        timeout=settings.request_timeout, follow_redirects=True, headers=headers
    ) as client:
        html_text = _get(client, winner_url)  # 拟发详情不可读 => 整体请求失败
    return parse_winner_detail(html_text, winner_url)
