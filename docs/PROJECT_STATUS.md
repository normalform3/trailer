# 徒步攻略 Agent 项目状态

## 功能概览

本项目是一个中国优先的徒步行前信息搜集 Agent 后端原型。当前 MVP 假设用户已经先在两步路等外部平台找好路线，并上传 KML 文件；系统优先围绕该轨迹做路线分析、风险识别、天气整理、住宿/交通/餐饮/补给信息搜集和待核实事项生成。

路线规划能力保留为兜底：当用户没有上传 KML 时，系统可以基于目的地生成规划路线候选，但第一阶段不把“从零自动设计路线”作为核心卖点。

核心流程由 LangGraph 编排：

1. `ingest_route`：优先解析用户上传的 KML。
2. `plan_route`：无可用 KML 时基于目的地生成规划路线候选。
3. `analyze_route`：计算距离、海拔、累计爬升/下降、预计耗时和风险因子。
4. `collect_research`：围绕路线整理天气、住宿、交通、餐饮、补给与应急、待核实事项。
5. `compose_guide`：通过百炼 `qwen3.7-plus` 生成中文攻略摘要和建议，失败时降级为模板生成。

## 已完成内容

- FastAPI 后端入口：
  - `GET /` 最简测试前端。
  - `POST /api/v1/hiking-guides`
  - `POST /api/v1/hiking-guides/upload`
  - `GET /health`
- 最简测试前端：
  - 支持目的地、出发城市、体能等级、偏好、路线描述输入。
  - 支持 KML 文件上传。
  - 支持调用 JSON API 或 KML 上传 API。
  - 展示摘要、路线候选、距离、耗时、海拔、风险因子、行前信息搜集、建议、告警和数据来源。
- 配置读取：
  - 支持在 `config/settings.toml` 中填写高德、OpenRouteService、百炼/DashScope、SerpApi API Key。
  - 支持系统环境变量，且系统环境变量优先于配置文件。
- LangGraph Agent 编排。
- KML 路线解析：
  - 支持 `Placemark`、`LineString`、`name`、`description`、三维坐标。
  - 支持多个 `Placemark`。
  - 无效 KML 自动降级到规划路线。
- 弹性路线来源优先级：
  - 用户 KML。
  - 用户路线文本 + 规划。
  - API/兜底规划。
- 行前信息搜集：
  - 天气信息会从路线分析阶段的天气快照中暴露给用户。
  - 住宿、交通、餐饮、补给/应急支持静态兜底清单。
  - 配置 `AMAP_API_KEY` 后，会优先尝试高德 POI 查询轨迹起点附近的住宿、交通、餐饮和补给点。
  - 配置 `SERPAPI_API_KEY` 后，会尝试通过 SerpApi Google Flights 查询出发城市到目的地周边机场的机票价格和耗时。
  - 输出 `travel_research.next_steps`，提醒用户核对两步路最新评论、景区公告、住宿余房、返程末班和天气预警。
- 路线分析：
  - 距离。
  - 预计耗时。
  - 最低/最高海拔。
  - 累计爬升/下降。
  - 高温、低温、降水、大风、高海拔、长距离、爬升较大等风险因子。
- 数据 provider：
  - 高德地点解析接口位。
  - SerpApi Google Flights 机票搜索接口位。
  - OpenRouteService 路线接口位。
  - OpenTopoData 海拔接口位。
  - Open-Meteo 天气接口位。
  - 静态/模板兜底实现。
- 百炼模型接入：
  - 默认模型：`qwen3.7-plus`。
  - 默认环境变量：`DASHSCOPE_API_KEY`。
  - 兼容环境变量：`BAILIAN_API_KEY`、`BAILIAN_MODEL`、`BAILIAN_BASE_URL`。
  - 默认 Base URL：`https://dashscope.aliyuncs.com/compatible-mode/v1`。
  - 模型不可用时不会中断流程，会降级为模板攻略。
- DashScope 多模态接入：
  - 通过官方 `dashscope.MultiModalConversation.call` 调用图片问答。
  - 默认 HTTP API URL：`https://dashscope.aliyuncs.com/api/v1`。
  - 冒烟脚本：`python scripts/test_dashscope_multimodal.py`。
- 测试覆盖：
  - KML 解析。
  - 路线优先级。
  - 路线分析。
  - 规划兜底。
  - 百炼响应解析与降级。
  - DashScope 多模态调用参数与响应解析。

## 未完成内容

- 尚未接入真实搜索 API 获取公开攻略网页。
- 尚未实现住宿、餐饮、补给点、交通 POI 的多点聚合和排序，目前只支持轨迹起点附近高德 POI + 静态兜底。
- 尚未实现真实 ORS 请求的集成测试，需要配置 `ORS_API_KEY` 后补充。
- 尚未实现公交/驾车交通方案、返程末班时间和天气多日预报兜底。
- 尚未实现 GPX、链接解析、截图/图片路线识别。
- 尚未实现数据库、缓存、任务队列和用户会话。
- 尚未实现前端地图可视化、轨迹折线预览和交互式路线编辑。
- 尚未做生产级限流、重试、日志脱敏和监控。

## 环境变量

```bash
export DASHSCOPE_API_KEY=...
export BAILIAN_MODEL=qwen3.7-plus
export BAILIAN_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
export DASHSCOPE_BASE_HTTP_API_URL=https://dashscope.aliyuncs.com/api/v1
export AMAP_API_KEY=...
export ORS_API_KEY=...
export SERPAPI_API_KEY=...
```

`DASHSCOPE_API_KEY` 是百炼官方文档推荐的环境变量。`BAILIAN_MODEL` 不配置时默认使用 `qwen3.7-plus`。

也可以在 `config/settings.toml` 中填写对应配置。读取优先级为：系统环境变量优先，其次配置文件。

## 当前边界

- 输出是行前信息搜集和规划建议，不是专业导航、景区公告或救援依据。
- API 规划路线会明确标注为规划路线，不会包装成社区热门路线。
- 不直接抓取两步路等非公开社区路线接口。
- 第一版路线文件只支持 KML。
