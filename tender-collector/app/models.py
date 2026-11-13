from __future__ import annotations

from pydantic import BaseModel, Field


class CollectRequest(BaseModel):
    list_url: str = Field(
        ..., min_length=1, description="公告列表页地址（服务会自动跟随分页链接）"
    )


class ProjectOut(BaseModel):
    project_name: str
    current_deadline: str
    deadline_source_code: str
    deadline_source_url: str
    is_expired: bool


class CollectResponse(BaseModel):
    status: str
    source_url: str
    collected: int
    projects: list[ProjectOut]


class RunOut(BaseModel):
    id: int
    source_url: str
    status: str
    items_collected: int
    error: str | None
    started_at: str
    finished_at: str


class VerifyRequest(BaseModel):
    original_code: str = Field(
        ..., min_length=1, description="已采集的原公告编号（须存在于采集库中）"
    )
    catalog_url: str = Field(
        ..., min_length=1, description="同站候选人公示目录列表页地址（服务自动跟随分页）"
    )
    winner_url: str = Field(
        ..., min_length=1, description="拟发中标公告详情页地址"
    )


class SectionVerifyOut(BaseModel):
    section_no: str
    status: str = Field(
        ..., description="match=一致；mismatch=不一致；undeterminable=无法判定"
    )
    announced_winner: str | None
    expected_winner: str | None
    reason: str
    winner_source_url: str | None
    candidate_source_urls: list[str]
    disqualification_source_urls: list[str]


class VerifyResponse(BaseModel):
    status: str = Field(
        ..., description="match=全部一致；mismatch=含不一致标段；undeterminable=无法整体判定"
    )
    original_code: str
    winner_published_at: str
    catalog_url: str
    winner_url: str
    sections: list[SectionVerifyOut]
