# 徒步攻略 Agent 项目状态

更新时间：2026-06-24

## 当前定位

Trailer 是一个中国优先的徒步行前决策 Agent。主链路仍然假设用户已经从两步路、奥维地图或其他外部平台拿到一条想走的路线，并上传 KML；系统围绕这条轨迹完成路线量化、天气筛选、周边 POI、交通方案、参考攻略补充和结构化攻略生成。

新增的“发现路线”模块用于前置辅助：当用户还没有明确路线时，系统可以先从本地路线知识库和公开网页中筛选有来源的候选路线名称。但它不生成可导航轨迹，也不把模型输出包装成真实热门路线；用户仍需要核对外部来源并获取 KML 后，进入主攻略链路。

## 当前运行链路

### 路线发现

1. `RouteRecommendationService` 解析用户自然语言需求，提取地区、天数、体能、距离、爬升、景观和交通偏好。
2. `SQLiteRouteKnowledgeRepository` 优先检索本地路线知识库，支持 FTS5、别名、标签、地区、编辑排序和状态过滤。
3. 候选不足时，`BailianRouteDiscoveryProvider` 通过 DashScope Responses API 的 `web_search` 补充公开网页中的真实路线名称。
4. `AmapRoutePlaceVerifier` 可用时做高德地点核验；未核验的联网候选至少需要两个独立网页来源。
5. 最终只返回达到证据门槛的候选路线名称、来源、匹配理由、未知字段、核验项和告警。

### 路线知识库管理

1. `/route-knowledge-manager` 复用单页前端，打开同窗口的路线库管理视图。
2. `/api/v1/route-knowledge` 支持查询、新增、编辑、归档和永久删除路线知识卡。
3. 知识卡包含地区、难度、距离、耗时、爬升、季节、标签、交通备注、风险提示、官方状态、编辑排序和来源。
4. `/api/v1/route-knowledge/import-jobs` 支持从公开 URL 或正文导入资料，抽取候选路线后由人工审核新增、合并或忽略。
5. 运行时默认数据库为 `data/route_knowledge.sqlite3`，可通过 `ROUTE_KNOWLEDGE_DB` 覆盖。

### KML 分析与攻略生成

1. `ingest_route`：优先解析用户上传的 KML。
2. `plan_route`：没有可用 KML 时才进入目的地规划兜底。
3. `collect_weather`：高德 + Open-Meteo 组合查询天气，按实际可用日期窗口补齐。
4. `analyze_route`：计算距离、海拔、累计爬升/下降、预计耗时和风险因子。
5. `collect_research`：整理住宿、交通、餐饮、补给、应急和待核实事项。
6. `plan_transport`：生成自驾、航班、铁路和接驳建议；在线数据不足时明确标注需人工核验。
7. `compose_guide`：通过百炼 `qwen3.7-plus` 生成中文攻略，失败时降级为模板生成。
8. `collect_reference`：读取用户提供的公开攻略链接或笔记，只作为经验补充和待核验项，不覆盖主攻略结论。

## 已完成内容

- FastAPI 后端入口：
  - `GET /` 单页前端。
  - `GET /route-knowledge-manager` 路线库管理深链。
  - `GET /health`
  - `GET /api/v1/config/map`
  - `GET /api/v1/llm/health`
  - `POST /api/v1/kml-preview`
  - `GET /api/v1/weather-forecast`
  - `POST /api/v1/hiking-guides`
  - `POST /api/v1/hiking-guides/upload`
  - `POST /api/v1/hiking-guides/upload/stream`
  - `POST /api/v1/route-recommendations`
  - `POST /api/v1/route-recommendations/stream`
  - `GET /api/v1/route-recommendations/featured`
  - `GET /api/v1/route-knowledge`
  - `POST /api/v1/route-knowledge`
  - `GET /api/v1/route-knowledge/{route_id}`
  - `PUT /api/v1/route-knowledge/{route_id}`
  - `DELETE /api/v1/route-knowledge/{route_id}`
  - `POST /api/v1/route-knowledge/import-jobs`
  - `GET /api/v1/route-knowledge/import-jobs/{job_id}`
  - `POST /api/v1/route-knowledge/import-jobs/{job_id}/apply`
- 单页前端：
  - 支持模块切换、路线发现、KML 上传、地图轨迹、海拔剖面、天气筛选、表单补充和攻略生成。
  - 支持 SSE 展示路线发现与攻略生成的执行阶段、节点状态、跳过原因和降级告警。
  - 支持左侧历史攻略恢复、收藏、置顶、折叠和清空。
  - 支持同窗口路线库管理视图、路线知识卡表格、编辑抽屉和导入审核。
- 路线发现：
  - 本地路线知识库优先，百炼联网搜索补充。
  - 高德地点核验、独立来源门槛、硬条件冲突过滤和证据质量排序。
  - 全国精选和区域精选从同一份知识库生成。
- KML 路线解析：
  - 支持 `Placemark`、`LineString`、`gx:Track`、`name`、`description`、三维坐标和多段路线。
  - 无效 KML 会返回明确错误或在 Agent 主链路中降级到规划路线。
- 路线分析：
  - 距离、预计耗时、最低/最高海拔、累计爬升/下降。
  - 通过海拔反转阈值降低 GPS 抖动对累计爬升的影响。
  - 输出高温、低温、降水、大风、高海拔、长距离、爬升较大等风险因子。
- 行前信息搜集：
  - 天气：高德近期预报 + Open-Meteo 多日补齐。
  - POI：围绕路线采样点查询住宿、餐饮和补给，外部服务不可用时给出静态核验清单。
  - 交通：高德驾车、SerpApi 航班、聚合 MCP 火车/航班只读查询和静态兜底建议。
  - 参考攻略：公开链接和用户笔记进入独立补充区。
- 百炼模型接入：
  - 默认模型：`qwen3.7-plus`。
  - 支持 `DASHSCOPE_API_KEY`、`BAILIAN_API_KEY`、`BAILIAN_MODEL`、`BAILIAN_BASE_URL`。
  - 模型不可用时不阻断主流程，会降级为模板攻略或返回稳定的公开错误。
- DashScope 多模态接入：
  - 已封装 `dashscope.MultiModalConversation.call` 图片问答 Provider。
  - 默认 HTTP API URL：`https://dashscope.aliyuncs.com/api/v1`。
  - 冒烟脚本：`python scripts/test_dashscope_multimodal.py`。
- 测试覆盖：
  - 当前 `pytest -q` 结果：99 passed，1 warning。
  - 覆盖 KML 解析、路线优先级、路线分析、天气 Provider、路线发现、路线知识库 CRUD、导入审核、Agent 分支、LLM 解析与降级、交通 Provider、流式 API 和配置读取。

## 未完成或当前边界

- 发现路线不生成 KML、GPX 或可导航轨迹，只返回可核验的路线名称和来源。
- 路线知识库是本地 SQLite 治理层，尚未引入用户账户、权限、跨设备同步、任务队列或服务端攻略历史。
- POI 排序仍偏行前辅助，尚未做到生产级全轨迹聚合、营业状态核验和实时余量判断。
- 交通输出仍是行前建议，不做订票、付款、座位库存保证或实时导航。
- OpenTopoData 海拔补全 Provider 已封装，但未默认接入主流程。
- GPX、路线链接解析、截图/图片路线识别仍未接入主链路。
- 尚未完成生产级限流、集中日志、指标监控、Docker Compose、CI/CD 和密钥轮换。

## 环境变量

```bash
export DASHSCOPE_API_KEY=...
export BAILIAN_MODEL=qwen3.7-plus
export BAILIAN_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
export DASHSCOPE_BASE_HTTP_API_URL=https://dashscope.aliyuncs.com/api/v1
export AMAP_API_KEY=...
export AMAP_WEB_KEY=...
export ORS_API_KEY=...
export SERPAPI_API_KEY=...
export JUHE_MCP_TOKEN=...
export ROUTE_KNOWLEDGE_DB=data/route_knowledge.sqlite3
```

`DASHSCOPE_API_KEY` 是百炼官方文档推荐的环境变量。`BAILIAN_MODEL` 不配置时默认使用 `qwen3.7-plus`。`JUHE_MCP_TOKEN` 只从环境变量读取，当前仅允许只读查询。也可以在 `config/settings.toml` 中填写对应配置；读取优先级为系统环境变量优先，其次配置文件。

## 当前边界

- Trailer 输出的是行前信息搜集和规划建议，不是专业导航、景区公告、天气预警或救援依据。
- 用户提供 KML 时，KML 轨迹始终是主事实来源。
- API 规划路线和发现路线都会明确标注为候选或待核验内容，不包装成真实轨迹或官方推荐。
- 用户提供的攻略链接和笔记是补充材料，不改写主攻略结论。
- 不直接抓取两步路等非公开社区路线接口。
