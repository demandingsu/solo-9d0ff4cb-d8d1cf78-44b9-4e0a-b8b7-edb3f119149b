"""中标候选人核对纯逻辑：剔除被取消资格者后，以最小名次核对拟发中标人。

输入均为已结构化的记录（与抓取层解耦，便于离线单测）：

- candidate_rows：候选人公示记录，每条含原公告编号、标段号、候选人名次、
  候选人编号、候选企业、公示发布时间与详情链接；
- disqualification_rows：取消资格公示记录，每条引用被取消者的候选人编号，
  并标注原公告编号、标段号与发布时间；
- winners：拟发中标公告中的各标段中标企业；
- cutoff：拟发中标公告的发布时间，只统计严格早于该时间发布的记录。

核对规则（不依赖任何输入顺序）：

1. 按 (原公告编号, 标段号) 关联，只保留目标原公告下的记录；
2. 取消资格公示引用的候选人编号在该标段候选人中不存在（引用失效）时，
   该标段无法判定；
3. 剔除被取消者后，剩余候选人按最小名次取“应中标企业”；
   同一最小名次出现多家不同企业（同名次不同企业）时该标段无法判定；
4. 拟发中标人与应中标企业一致 => match，否则 mismatch；
   缺少候选人/中标人等信息不足 => undeterminable。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

TIME_FORMAT = "%Y-%m-%d %H:%M"

STATUS_MATCH = "match"
STATUS_MISMATCH = "mismatch"
STATUS_UNDETERMINABLE = "undeterminable"

VALID_STATUSES = (STATUS_MATCH, STATUS_MISMATCH, STATUS_UNDETERMINABLE)


class VerifyError(ValueError):
    """核对输入本身无法成立（如缺少拟发公告发布时间）。"""


@dataclass(frozen=True)
class SectionResult:
    section_no: str
    status: str
    announced_winner: str | None
    expected_winner: str | None
    reason: str
    winner_source_url: str | None
    candidate_source_urls: list[str] = field(default_factory=list)
    disqualification_source_urls: list[str] = field(default_factory=list)


def parse_published_at(text: str) -> datetime:
    """解析公告发布时间，兼容 'YYYY-MM-DD HH:MM' 与 ISO 形式。"""
    try:
        return datetime.strptime(text.strip(), TIME_FORMAT)
    except (ValueError, AttributeError) as exc:
        try:
            return datetime.fromisoformat(text.strip())
        except (ValueError, AttributeError):
            raise VerifyError(f"无法解析发布时间: {text!r}") from exc


def _section_sort_key(section_no: str) -> tuple[int, Any]:
    """标段号排序：纯数字按数值，其余按字符串兜底，保证结果确定。"""
    s = str(section_no)
    return (0, int(s)) if s.isdigit() else (1, s)


def _published_before(row: dict[str, Any], cutoff: datetime) -> bool:
    return parse_published_at(row["published_at"]) < cutoff


def verify_sections(
    original_code: str,
    candidate_rows: Iterable[dict[str, Any]],
    disqualification_rows: Iterable[dict[str, Any]],
    winners: Iterable[dict[str, Any]],
    cutoff: datetime,
) -> list[SectionResult]:
    """按标段返回核对结论。仅统计 cutoff 之前已发布的公示记录。"""
    candidates: dict[str, list[dict[str, Any]]] = {}
    for row in candidate_rows:
        if row.get("original_code") != original_code:
            continue
        if not _published_before(row, cutoff):
            continue
        candidates.setdefault(str(row["section_no"]), []).append(row)

    disqualifications: dict[str, list[dict[str, Any]]] = {}
    for row in disqualification_rows:
        if row.get("original_code") != original_code:
            continue
        if not _published_before(row, cutoff):
            continue
        disqualifications.setdefault(str(row["section_no"]), []).append(row)

    winner_map: dict[str, dict[str, Any]] = {}
    duplicated_winner_sections: set[str] = set()
    for row in winners:
        section_no = str(row["section_no"])
        if section_no in winner_map:
            duplicated_winner_sections.add(section_no)
        else:
            winner_map[section_no] = row

    section_nos = set(candidates) | set(winner_map)
    results: list[SectionResult] = []
    for section_no in sorted(section_nos, key=_section_sort_key):
        results.append(
            _verify_one(
                original_code,
                section_no,
                candidates.get(section_no, []),
                disqualifications.get(section_no, []),
                winner_map.get(section_no),
                duplicated=section_no in duplicated_winner_sections,
            )
        )
    return results


def _verify_one(
    original_code: str,
    section_no: str,
    rows: list[dict[str, Any]],
    dq_rows: list[dict[str, Any]],
    winner: dict[str, Any] | None,
    *,
    duplicated: bool,
) -> SectionResult:
    winner_enterprise = winner.get("enterprise") if winner else None
    winner_url = winner.get("detail_url") if winner else None
    dq_urls = sorted({r["detail_url"] for r in dq_rows})

    def undeterminable(reason: str, cand_urls: list[str] | None = None) -> SectionResult:
        return SectionResult(
            section_no=section_no,
            status=STATUS_UNDETERMINABLE,
            announced_winner=winner_enterprise,
            expected_winner=None,
            reason=reason,
            winner_source_url=winner_url,
            candidate_source_urls=cand_urls
            if cand_urls is not None
            else sorted({r["detail_url"] for r in rows}),
            disqualification_source_urls=dq_urls,
        )

    if duplicated:
        return undeterminable(
            f"拟发中标公告中段号 {section_no} 重复出现，中标人无法唯一确定"
        )
    if winner is None:
        return undeterminable(f"拟发中标公告缺少标段 {section_no} 的中标人")
    if not rows:
        return undeterminable(
            f"原公告 {original_code} 标段 {section_no} 在拟发公告发布前无候选人公示"
        )

    # 同一候选人编号去重（同名次同企业的重复公示不影响结论），保留首次出现的记录
    by_code: dict[str, dict[str, Any]] = {}
    for row in rows:
        by_code.setdefault(row["candidate_code"], row)

    # 取消资格：引用的候选人编号必须真实存在于该标段，否则引用失效 => 无法判定
    disqualified: set[str] = set()
    for dq in dq_rows:
        ref = dq.get("disqualified_candidate_code")
        if not ref or ref not in by_code:
            return undeterminable(
                f"取消资格公示 {dq.get('code', '')} 引用的候选人编号 "
                f"{ref or '（空）'} 在标段 {section_no} 候选人中不存在，引用失效"
            )
        disqualified.add(ref)

    surviving = [r for code, r in by_code.items() if code not in disqualified]
    if not surviving:
        return undeterminable(f"标段 {section_no} 候选人被全部取消资格，无有效候选人")

    min_rank = min(int(r["rank"]) for r in surviving)
    top = [r for r in surviving if int(r["rank"]) == min_rank]
    top_urls = sorted({r["detail_url"] for r in top})
    top_enterprises = {r["enterprise"] for r in top}
    if len(top_enterprises) > 1:
        return SectionResult(
            section_no=section_no,
            status=STATUS_UNDETERMINABLE,
            announced_winner=winner_enterprise,
            expected_winner=None,
            reason=(
                f"标段 {section_no} 最小名次（第 {min_rank} 名）存在不同企业："
                + "、".join(sorted(top_enterprises))
            ),
            winner_source_url=winner_url,
            candidate_source_urls=top_urls,
            disqualification_source_urls=dq_urls,
        )

    expected = top[0]["enterprise"]
    if winner_enterprise == expected:
        return SectionResult(
            section_no=section_no,
            status=STATUS_MATCH,
            announced_winner=winner_enterprise,
            expected_winner=expected,
            reason=(
                f"剔除 {len(disqualified)} 名被取消资格者后，最小名次第 {min_rank} 名"
                f"候选人 {expected} 与拟发中标人一致"
            ),
            winner_source_url=winner_url,
            candidate_source_urls=top_urls,
            disqualification_source_urls=dq_urls,
        )

    return SectionResult(
        section_no=section_no,
        status=STATUS_MISMATCH,
        announced_winner=winner_enterprise,
        expected_winner=expected,
        reason=(
            f"剔除被取消资格者后最小名次第 {min_rank} 名候选人为 {expected}，"
            f"与拟发中标人 {winner_enterprise} 不一致"
        ),
        winner_source_url=winner_url,
        candidate_source_urls=top_urls,
        disqualification_source_urls=dq_urls,
    )


def overall_status(results: Iterable[SectionResult]) -> str:
    """汇总标段结论：全部 match => match；含 mismatch => mismatch；否则 undeterminable。"""
    statuses = {r.status for r in results}
    if not statuses:
        return STATUS_UNDETERMINABLE
    if statuses == {STATUS_MATCH}:
        return STATUS_MATCH
    if STATUS_MISMATCH in statuses:
        return STATUS_MISMATCH
    return STATUS_UNDETERMINABLE
