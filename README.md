# Trailer

一个面向中国徒步爱好者的 **AI 徒步行前攻略 Agent**。无论你是已经选好了路线，还是只确定了目的地，Trailer 都能帮你完成从路线分析到攻略生成的全流程。

## 两条路线，一个攻略

Trailer 提供两种使用方式，最终都汇聚到同一套攻略生成流程：

| | 路线 A：上传路线 | 路线 B：规划路线 |
|---|---|---|
| 适合场景 | 你已经有一条确定的徒步轨迹 | 你只确定了目的地，需要系统帮你规划路线 |
| 输入 | KML 轨迹文件（两步路、奥维等导出） | 目的地 + 出行偏好 |
| 路线来源 | 用户提供 | 系统智能规划 |
| 状态 | **已实现** | 规划中 |

两条路线在确定之后，都进入相同的攻略生成管线：路线分析 → 天气查询 → 交通规划 → 周边信息搜集 → AI 攻略生成。

## 核心能力

- **KML 路线解析** — 支持 `LineString` 和 `gx:Track`，自动计算距离、海拔、爬升/下降、预估耗时和风险等级
- **天气分析** — 默认评估未来 7 天天气；近 4 天优先使用高德天气，最长支持 16 天常规预报筛选
- **交通规划** — 根据出发城市和目的地，提供自驾/公共交通方案
- **行前信息搜集** — 自动查询目的地周边住宿、餐饮、补给点等信息
- **AI 攻略生成** — 基于百炼千问大模型，整合所有信息生成结构化攻略（行程日程、装备清单、安全提醒）；模型不可用时自动降级为模板生成
- **前端可视化** — KML 上传预览、高德地图轨迹渲染、海拔剖面图、天气展示

## 工作流

```
路线 A：用户上传 KML ──┐
                       ├──▶ 路线分析 ──▶ 天气/周边/交通 ──▶ AI 攻略生成
路线 B：用户填写目的地 ─┘       (规划路线)
```

工作流由 **LangGraph** 编排，节点化执行，每一步均有错误处理和降级方案。

## 技术栈

| 组件 | 技术 |
|------|------|
| 后端框架 | FastAPI + Uvicorn |
| AI 工作流 | LangGraph |
| 大模型 | 阿里云百炼 (DashScope) / 通义千问 |
| 地图服务 | 高德地图 API |
| 路线规划 | OpenRouteService (兜底) |
| 数据校验 | Pydantic v2 |
| HTTP 客户端 | httpx |

## 快速开始

### 环境要求

- Python >= 3.11

### 安装

```bash
git clone https://github.com/<your-username>/trailer.git
cd trailer
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 配置 API 密钥

通过环境变量配置（推荐）：

```bash
export AMAP_API_KEY="你的高德地图 Web 服务 Key"
export AMAP_WEB_KEY="你的高德地图 JS API Key"   # 前端地图渲染用
export DASHSCOPE_API_KEY="你的百炼 DashScope API Key"
export ORS_API_KEY="你的 OpenRouteService API Key"  # 可选，路线规划兜底
export SERPAPI_API_KEY="你的 SerpApi API Key"  # 可选，Google Flights 机票搜索
```

也可以直接编辑 `config/settings.toml`：

```toml
[api_keys]
amap_api_key = "你的高德地图 Key"
ors_api_key = "你的 ORS Key"
dashscope_api_key = "你的百炼 Key"
serpapi_api_key = "你的 SerpApi Key"
```

> **注意：** 请勿将真实 API 密钥提交到版本控制。生产环境建议使用环境变量或 `.env` 文件。

### 启动

```bash
uvicorn app.main:app --reload
```

访问 [http://localhost:8000](http://localhost:8000) 即可使用。

### 无 Key 模式

不配置任何 API 密钥也能启动运行：
- 天气、交通、高德相关功能不可用
- 路线规划使用静态兜底
- 攻略生成使用内置模板替代大模型

## 项目结构

```
trailer/
├── app/
│   ├── agents/          # LangGraph Agent（工作流编排）
│   ├── models/          # Pydantic 数据模型
│   ├── providers/       # 外部服务接入（天气、交通、LLM 等）
│   ├── services/        # 核心业务逻辑（路线解析、分析、规划）
│   ├── static/          # 前端页面
│   ├── config.py        # 配置加载
│   └── main.py          # FastAPI 应用入口
├── config/
│   └── settings.toml    # 配置文件模板
├── tests/               # 单元测试
└── scripts/             # 辅助脚本
```

## API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查 |
| GET | `/api/v1/config/map` | 获取前端地图配置 |
| POST | `/api/v1/kml-preview` | 上传 KML 文件并预览解析结果 |
| GET | `/api/v1/weather-forecast` | 查询指定坐标的天气预报 |
| POST | `/api/v1/hiking-guides` | 生成徒步攻略（JSON） |
| POST | `/api/v1/hiking-guides/upload` | 生成徒步攻略（含 KML 文件上传） |

## 运行测试

```bash
pytest
```

## License

MIT
