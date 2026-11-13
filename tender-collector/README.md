# 市政工程投标截止时间采集 / 中标候选人核对服务

面向招投标专员的公告采集与核对 API：

- **采集**：提交市政工程公告站的**列表页地址**，服务自动遍历所有分页与详情页，
  将**更正公告按其引用的原公告编号归并**到同一项目，并**按发布时间**（而非页面
  排列顺序）确定当前生效的投标截止时间，标记是否已过期。
- **中标核对**：发布中标公告前，输入已采集的原公告编号、同站候选人公示目录 URL
  与拟发中标公告详情 URL，沿分页读取各标段候选人名次与引用候选人编号的取消资格
  公示，剔除被取消者后以最小名次核对中标人是否仍是首位有效候选人。

## 核心规则

1. **分页遍历**：从提交的列表页出发，沿 `nav.pagination` 链接抓取全部列表页
   （去重、防环、限量），收集所有详情页链接后逐页抓取。
2. **更正归并**：更正公告通过"原公告编号"归并到对应原公告所在项目；引用悬空
   （原公告不存在/不是原公告）视为数据错误，本次采集整体失败。
3. **生效截止时间**：同一项目内按**发布时间**排序，取发布时间最新且声明了
   截止时间的公告；发布时间相同时按公告编号确定次序，结果与页面顺序完全无关。
   若最新更正公告未给出截止时间（例如只改联系人），沿用上一份带截止时间的公告。
4. **过期判定**：`is_expired = 截止时间 <= 当前时间`（截止时间按服务器本地时区
   的 naive 时间比较，与公告页 `YYYY-MM-DD HH:MM` 格式一致）。
5. **幂等采集**：公告以编号为唯一键 UPSERT，重复采集不产生重复记录。
6. **失败保留**：任一列表页/详情页读取失败（网络错误、非 2xx、字段缺失）或
   归并校验失败时，本次采集不写入任何公告数据，**完整保留上次成功采集结果**；
   失败原因记入 `collection_runs` 表并通过 HTTP 422 返回。

### 中标核对规则（`POST /api/verify-winner`）

1. **分页遍历**：沿候选人公示目录的 `nav.pagination` 抓取全部分页，收集
   候选人公示与取消资格公示详情链接（列表项 `data-kind` 区分类型）。
2. **关联口径**：按**原公告编号 + 标段号**关联；只统计**拟发中标公告发布时间
   之前**已发布的公示记录（严格小于，同时刻不计入），并剔除其他原公告的记录。
3. **取消资格**：取消资格公示按被取消者**候选人编号**匹配；引用的编号在该标段
   候选人中不存在（引用失效）时，该标段**无法判定**。
4. **首位有效候选人**：剔除被取消者后按**最小名次**取应中标企业；同一最小名次
   出现多家不同企业（同名次不同企业）时该标段**无法判定**；同一候选人编号的
   重复公示自动去重。
5. **核对结论**：拟发中标人与应中标企业一致为 `match`，不一致为 `mismatch`，
   信息不足/引用失效/同名次冲突为 `undeterminable`；每个标段都返回两侧来源链接
   （候选人公示、取消资格公示、拟发中标公告）。
6. **失败隔离**：目录分页读取失败时**所有标段无法判定**；单条详情读取失败且
   列表项标明所属标段时**只影响对应标段**，未标明标段时影响全部标段；
   拟发中标公告详情本身不可读时整体返回 422。核对过程只读不写采集库。

## 目录结构

```
tender-collector/
├── app/                       # FastAPI 应用
│   ├── main.py                # 路由：/api/collect、/api/verify-winner、/api/projects、/api/collections
│   ├── service.py             # 采集编排 + 事务边界 + 过期判定
│   ├── verify_service.py      # 中标核对编排（失败隔离、cutoff 过滤、只读不写库）
│   ├── verify.py              # 取消资格剔除与最小名次核对（纯函数）
│   ├── verify_scraper.py      # 候选人目录/取消资格/拟发中标详情抓取与解析
│   ├── scraper.py             # httpx + BeautifulSoup 抓取/解析
│   ├── merge.py               # 更正归并与生效截止时间判定（纯函数）
│   ├── database.py            # SQLite 连接、建表、UPSERT
│   ├── models.py              # Pydantic 模型
│   └── config.py              # 环境变量配置
├── demo_site/                 # 采集示例站（5 项目、10 公告、3 列表页）
├── demo_site_verify/          # 中标核对示例站（5 标段、候选人/取消资格/拟发中标）
├── scripts/generate_demo_site.py         # 采集示例站生成器（数据可复现）
├── scripts/generate_verify_demo_site.py  # 核对示例站生成器（数据可复现）
├── tests/test_collection.py              # 采集端到端 + 归并单元测试
├── tests/test_verify.py                  # 核对端到端 + 核对规则单元测试
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 快速开始（Docker Compose）

```bash
# 1. 生成两个示例公告站静态文件（已随仓库提供，可重复生成）
python3 scripts/generate_demo_site.py
python3 scripts/generate_verify_demo_site.py

# 2. 启动：API 在 :8000；采集示例站在 :8001/，核对示例站在 :8001/verify/
docker compose up --build -d

# 3. 提交列表页地址触发采集
curl -X POST http://localhost:8000/api/collect \
  -H 'Content-Type: application/json' \
  -d '{"list_url": "http://demo-site/list_1.html"}'

# 4. 查询项目当前截止时间
curl http://localhost:8000/api/projects

# 5. 发布中标公告前核对中标人（原公告 SZ-2026-002 须已采集入库）
curl -X POST http://localhost:8000/api/verify-winner \
  -H 'Content-Type: application/json' \
  -d '{
    "original_code": "SZ-2026-002",
    "catalog_url": "http://demo-site/verify/catalog_1.html",
    "winner_url": "http://demo-site/verify/WIN-SZ-2026-002.html"
  }'
```

容器内 API 通过 compose 网络访问示例站（主机名 `demo-site`）；从宿主机浏览
示例站则打开 <http://localhost:8001/list_1.html> 与
<http://localhost:8001/verify/catalog_1.html>。SQLite 数据存于命名卷
`tender-data`（容器内 `/data/tenders.db`）。

停止与清理：

```bash
docker compose down          # 停止（保留数据卷）
docker compose down -v       # 停止并删除采集数据
```

## 本地开发运行（不使用 Docker）

```bash
python3 -m pip install -r requirements-dev.txt
python3 scripts/generate_demo_site.py
python3 scripts/generate_verify_demo_site.py

# 终端 A：示例公告站（采集站在根路径，核对站在 verify/ 子路径，与 compose 一致）
mkdir -p /tmp/tender-web/verify
ln -sf "$PWD"/demo_site/* /tmp/tender-web/
ln -sf "$PWD"/demo_site_verify/* /tmp/tender-web/verify/
python3 -m http.server 8001 --directory /tmp/tender-web

# 终端 B：API（数据库默认在 ./data/tenders.db）
uvicorn app.main:app --reload --port 8000

# 终端 C：测试
pytest -q

# 采集
curl -X POST http://localhost:8000/api/collect \
  -H 'Content-Type: application/json' \
  -d '{"list_url": "http://127.0.0.1:8001/list_1.html"}'

# 核对
curl -X POST http://localhost:8000/api/verify-winner \
  -H 'Content-Type: application/json' \
  -d '{
    "original_code": "SZ-2026-002",
    "catalog_url": "http://127.0.0.1:8001/verify/catalog_1.html",
    "winner_url": "http://127.0.0.1:8001/verify/WIN-SZ-2026-002.html"
  }'
```

交互式 API 文档（Swagger UI）：<http://localhost:8000/docs>

## API 说明

### `POST /api/collect` — 提交列表页，触发采集

请求：

```json
{ "list_url": "http://demo-site/list_1.html" }
```

成功响应（200）：

```json
{
  "status": "success",
  "source_url": "http://demo-site/list_1.html",
  "collected": 10,
  "projects": [
    {
      "project_name": "城北污水处理厂扩建工程",
      "current_deadline": "2026-11-30 10:00",
      "deadline_source_code": "SZ-2026-002-G2",
      "deadline_source_url": "http://demo-site/SZ-2026-002-G2.html",
      "is_expired": false
    }
  ]
}
```

失败响应（422，任一页面不可读/数据无法归并）：

```json
{ "detail": "读取页面失败 http://demo-site/SZ-2026-002-G2.html: HTTP 404" }
```

### `GET /api/projects` — 查询全部项目的当前截止时间

返回字段：项目名称、当前生效截止时间、截止时间来源公告编号、来源链接、是否过期。

### `GET /api/collections?limit=20` — 采集运行历史

包含每次采集的成功/失败状态、采集条数与错误信息。

### `POST /api/verify-winner` — 核对拟发中标人是否仍为首位有效候选人

请求（原公告编号必须已通过采集入库；目录 URL 与拟发详情 URL 须同源）：

```json
{
  "original_code": "SZ-2026-002",
  "catalog_url": "http://demo-site/verify/catalog_1.html",
  "winner_url": "http://demo-site/verify/WIN-SZ-2026-002.html"
}
```

成功响应（200，逐标段给出结论；整体 `status` 汇总各标段）：

```json
{
  "status": "mismatch",
  "original_code": "SZ-2026-002",
  "winner_published_at": "2026-11-20 10:00",
  "catalog_url": "http://demo-site/verify/catalog_1.html",
  "winner_url": "http://demo-site/verify/WIN-SZ-2026-002.html",
  "sections": [
    {
      "section_no": "1",
      "status": "match",
      "announced_winner": "远通路桥建设集团",
      "expected_winner": "远通路桥建设集团",
      "reason": "剔除 1 名被取消资格者后，最小名次第 2 名候选人 … 与拟发中标人一致",
      "winner_source_url": "http://demo-site/verify/WIN-SZ-2026-002.html",
      "candidate_source_urls": ["http://demo-site/verify/HC-2026-001.html"],
      "disqualification_source_urls": ["http://demo-site/verify/DQ-2026-001.html"]
    }
  ]
}
```

- 标段 `status`：`match`（一致）/ `mismatch`（不一致）/ `undeterminable`
  （无法判定：同名次不同企业、取消引用失效、信息不足或源站详情读取失败）；
- 整体 `status`：全部 match 才为 `match`；含 mismatch 为 `mismatch`；
  其余为 `undeterminable`；
- 目录分页失败时 HTTP 仍为 200，所有标段以 `undeterminable` 返回并保留
  拟发中标人信息；输入不成立（原公告未采集、拟发详情不可读、原公告编号不一致）
  返回 422：

```json
{ "detail": "原公告编号 SZ-2026-999 未在采集库中找到，请先采集" }
```

## 示例站数据设计

`demo_site/` 含 5 个项目、10 条公告、3 个列表页，列表按发布时间**倒序**排列
（更正公告排在原公告之前），专门用于验证归并不受页面顺序影响：

| 项目 | 场景 | 生效截止时间来源 |
| --- | --- | --- |
| 滨河路市政改造工程 | 一次更正延期 | 更正公告 SZ-2026-001-G1 |
| 城北污水处理厂扩建工程 | 两次更正，取最新一次 | 第二次更正 SZ-2026-002-G2 |
| 东风桥加固维修工程 | 无更正，已过期 | 原公告 SZ-2026-003 |
| 高新区雨污分流管网工程 | 更正不改截止时间 | 原公告 SZ-2026-004 |
| 湖滨公园配套市政道路工程 | 原公告在末页、更正在首页（跨页乱序） | 更正公告 SZ-2026-005-G1 |

### 中标核对示例站 `demo_site_verify/`

围绕原公告 `SZ-2026-002` 的 5 个标段构造，拟发中标公告 `WIN-SZ-2026-002`
发布于 `2026-11-20 10:00`（核对 cutoff），目录共 3 个分页、按发布时间倒序：

| 标段 | 场景 | 核对结论 |
| --- | --- | --- |
| 1 | 第 1 名被取消资格，第 2 名顺位且与拟发中标人一致 | `match` |
| 2 | 无取消；含同编号同名次同企业的重复公示（应去重）；cutoff 后的取消公示须被忽略 | `match` |
| 3 | 第 1 名仍有效，拟发中标人却写成第 2 名企业 | `mismatch` |
| 4 | 第 1 名并列两家不同企业（同名次不同企业） | `undeterminable` |
| 5 | 取消资格公示引用不存在的候选人编号（引用失效） | `undeterminable` |

另含候选人公示 `HC-OTHER-1`（引用其他原公告编号，须被关联条件剔除）与
取消资格公示 `DQ-2026-006`（发布于 cutoff 之后，须被时间条件忽略）两条干扰数据。

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `TENDER_DB_PATH` | 本地 `./data/tenders.db`；容器内 `/data/tenders.db` | SQLite 文件路径 |
| `TENDER_HTTP_TIMEOUT` | `15` | 单个页面请求超时（秒） |
| `TENDER_MAX_LIST_PAGES` | `200` | 单次采集最多遍历的列表页数（防环） |
| `TENDER_USER_AGENT` | `TenderCollector/1.0 ...` | 请求 UA |

## 解析约定（接入真实站点时）

采集解析器依赖以下稳定选择器，适配其他站点时调整 `app/scraper.py` 即可：

- 列表页：`ul.announcement-list a.detail-link` 为详情链接，
  `nav.pagination a` 为分页链接；
- 详情页：`article.announcement-detail` 容器，支持
  `data-code` / `data-kind` / `data-published-at` / `data-corrects` 属性，
  以及 `table.detail-table` 内 `th[data-field="字段名"] + td` 表格字段
  （公告编号、公告类型、项目名称、发布时间、投标截止时间、原公告编号）。

中标核对解析器（`app/verify_scraper.py`）在同一套约定上扩展：

- 目录列表项：`li.announcement-item` 的 `data-kind` 取
  `candidate`（候选人公示）或 `disqualification`（取消资格公示），
  可选 `data-section-no` 标明所属标段（用于单条详情失败时的影响范围隔离）；
- 候选人公示详情：`article` 的 `data-original-code` 与
  `data-published-at`，`table.candidate-table tbody[data-section-no]`
  内 `tr[data-rank]` 逐行给出名次、`td[data-field="候选人编号"]`、
  `td[data-field="候选企业"]`；
- 取消资格公示详情：`table.disqualification-table tbody tr` 内
  `td[data-field="标段号"]` 与 `td[data-field="被取消者候选人编号"]`；
- 拟发中标公告详情：`table.winner-table tbody tr` 内
  `td[data-field="标段号"]` 与 `td[data-field="中标企业"]`。
