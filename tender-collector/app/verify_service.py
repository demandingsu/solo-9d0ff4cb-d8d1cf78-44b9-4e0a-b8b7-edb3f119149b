"""中标核对编排服务。

流程：
1. 校验原公告编号确为采集库中的原公告（保证按原公告编号关联有锚点）；
2. 抓取拟发中标公告详情，得到发布时间 cutoff 与各标段中标人，
   并核对其原公告编号与输入一致；
3. 沿分页遍历候选人公示目录，再逐条抓取详情：
   - 目录分页失败 => 所有标段无法判定；
   - 单条详情失败且列表项标明标段 => 仅该标段无法判定，否则全部标段无法判定；
4. 只保留 cutoff 之前已发布的记录，调用纯逻辑层按标段核对。

核对过程只读采集库、不写入任何数据。
"""
from __future__ import annotations

from . import database
from .scraper import CollectionError
from .verify import (
    STATUS_UNDETERMINABLE,
    SectionResult,
    overall_status,
    parse_published_at,
    verify_sections,
)
from .verify_scraper import (
    KIND_CANDIDATE,
    KIND_DISQUALIFICATION,
    FailedDetail,
    crawl_catalog,
    fetch_catalog_details,
    fetch_winner_detail,
)


class VerifyServiceError(Exception):
    """核对请求本身无法成立（原公告缺失、拟发详情不可读等）。"""


def _mark_undeterminable(
    section_nos: list[str],
    reason: str,
    winner_map: dict[str, dict],
    failures: list[FailedDetail] | None = None,
) -> list[SectionResult]:
    failure_urls = sorted({f.detail_url for f in (failures or [])})
    results: list[SectionResult] = []
    for section_no in section_nos:
        winner = winner_map.get(section_no)
        results.append(
            SectionResult(
                section_no=section_no,
                status=STATUS_UNDETERMINABLE,
                announced_winner=winner.get("enterprise") if winner else None,
                expected_winner=None,
                reason=reason,
                winner_source_url=winner.get("detail_url") if winner else None,
                candidate_source_urls=failure_urls,
                disqualification_source_urls=[],
            )
        )
    return results


def run_verification(
    original_code: str, catalog_url: str, winner_url: str
) -> dict:
    # 1) 原公告必须已采集且确为原公告
    with database.get_conn() as conn:
        row = conn.execute(
            "SELECT code, kind FROM announcements WHERE code = ?", (original_code,)
        ).fetchone()
    if row is None:
        raise VerifyServiceError(f"原公告编号 {original_code} 未在采集库中找到，请先采集")
    if row["kind"] != "original":
        raise VerifyServiceError(f"{original_code} 不是原公告编号，无法作为核对锚点")

    # 2) 拟发中标公告详情（不可读 => 整体 422）
    try:
        winner_detail = fetch_winner_detail(winner_url)
    except CollectionError as exc:
        raise VerifyServiceError(str(exc)) from exc
    if winner_detail["original_code"] != original_code:
        raise VerifyServiceError(
            f"拟发中标公告的原公告编号 {winner_detail['original_code']} "
            f"与输入的 {original_code} 不一致"
        )
    cutoff = parse_published_at(winner_detail["published_at"])
    winners = [
        {**w, "detail_url": winner_url} for w in winner_detail["winners"]
    ]
    winner_map = {str(w["section_no"]): w for w in winners}
    section_nos = sorted(
        winner_map,
        key=lambda s: (0, int(s)) if s.isdigit() else (1, s),
    )

    # 3) 目录分页遍历：失败 => 所有标段无法判定（仍返回 200 结构化结论）
    try:
        links = crawl_catalog(catalog_url)
        candidates, disqualifications, failures = fetch_catalog_details(links)
    except CollectionError as exc:
        sections = _mark_undeterminable(
            section_nos, f"候选人公示目录读取失败：{exc}", winner_map
        )
        return _build_payload(
            original_code, catalog_url, winner_url,
            winner_detail["published_at"], sections,
        )

    # 4) 单条详情失败的影响范围隔离
    if failures:
        unsectioned = [f for f in failures if not f.section_no]
        if unsectioned:
            detail = unsectioned[0]
            sections = _mark_undeterminable(
                section_nos,
                f"目录详情 {detail.detail_url} 读取失败且未标明所属标段，全部标段无法判定："
                f"{detail.error}",
                winner_map,
                failures=unsectioned,
            )
            return _build_payload(
                original_code, catalog_url, winner_url,
                winner_detail["published_at"], sections,
            )

        failed_sections = {f.section_no for f in failures}
        failure_by_section: dict[str, list[FailedDetail]] = {}
        for f in failures:
            failure_by_section.setdefault(f.section_no, []).append(f)
    else:
        failed_sections = set()
        failure_by_section = {}

    # 5) 纯逻辑核对（只统计 cutoff 之前已发布的记录）
    results = verify_sections(
        original_code, candidates, disqualifications, winners, cutoff
    )

    # 用详情失败结论覆盖对应标段；其余标段保留正常核对结果
    final: list[SectionResult] = []
    result_by_section = {r.section_no: r for r in results}
    for section_no in section_nos:
        if section_no in failed_sections:
            fs = failure_by_section[section_no]
            urls = sorted({f.detail_url for f in fs})
            final.append(
                SectionResult(
                    section_no=section_no,
                    status=STATUS_UNDETERMINABLE,
                    announced_winner=winner_map[section_no]["enterprise"],
                    expected_winner=None,
                    reason=(
                        f"标段 {section_no} 的目录详情读取失败，仅本标段无法判定："
                        + "；".join(f.error for f in fs)
                    ),
                    winner_source_url=winner_url,
                    candidate_source_urls=urls,
                    disqualification_source_urls=[],
                )
            )
        elif section_no in result_by_section:
            final.append(result_by_section[section_no])
        else:  # 理论上不会发生（verify_sections 覆盖全部中标标段），兜底
            final.extend(
                _mark_undeterminable(
                    [section_no], f"标段 {section_no} 缺少候选人公示数据", winner_map
                )
            )

    return _build_payload(
        original_code, catalog_url, winner_url,
        winner_detail["published_at"], final,
    )


def _build_payload(
    original_code: str,
    catalog_url: str,
    winner_url: str,
    winner_published_at: str,
    sections: list[SectionResult],
) -> dict:
    return {
        "status": overall_status(sections),
        "original_code": original_code,
        "winner_published_at": winner_published_at,
        "catalog_url": catalog_url,
        "winner_url": winner_url,
        "sections": [
            {
                "section_no": r.section_no,
                "status": r.status,
                "announced_winner": r.announced_winner,
                "expected_winner": r.expected_winner,
                "reason": r.reason,
                "winner_source_url": r.winner_source_url,
                "candidate_source_urls": r.candidate_source_urls,
                "disqualification_source_urls": r.disqualification_source_urls,
            }
            for r in sections
        ],
    }


__all__ = [
    "KIND_CANDIDATE",
    "KIND_DISQUALIFICATION",
    "VerifyServiceError",
    "run_verification",
]
