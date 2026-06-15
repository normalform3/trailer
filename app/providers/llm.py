from __future__ import annotations

import json
from typing import Any, Protocol

from app.config import get_settings
from app.models import (
    GearCategory,
    GearList,
    HikingGuideRequest,
    Itinerary,
    RiskPoint,
    RouteCandidate,
    SafetyGuide,
    TransportPlan,
    TravelResearch,
)


class GuideDraft:
    def __init__(
        self,
        summary: str,
        recommendations: list[str],
        source: str,
        itinerary: Itinerary | None = None,
        gear_list: GearList | None = None,
        safety_guide: SafetyGuide | None = None,
    ) -> None:
        self.summary = summary
        self.recommendations = recommendations
        self.source = source
        self.itinerary = itinerary
        self.gear_list = gear_list
        self.safety_guide = safety_guide


class GuideLLMProvider(Protocol):
    def generate_guide(
        self,
        request: HikingGuideRequest,
        candidates: list[RouteCandidate],
        warnings: list[str],
        data_sources: list[str],
        travel_research: TravelResearch | None = None,
        transport_plan: TransportPlan | None = None,
    ) -> GuideDraft:
        raise NotImplementedError


class TemplateGuideProvider:
    def generate_guide(
        self,
        request: HikingGuideRequest,
        candidates: list[RouteCandidate],
        warnings: list[str],
        data_sources: list[str],
        travel_research: TravelResearch | None = None,
        transport_plan: TransportPlan | None = None,
    ) -> GuideDraft:
        if not candidates:
            return GuideDraft(
                summary=f"未能为 {request.destination} 生成可用路线，请补充 KML 或更具体的路线描述。",
                recommendations=["补充 KML 路线文件或明确起终点后重新生成攻略。"],
                source="template",
            )

        best = candidates[0]
        summary = (
            f"已为 {request.destination} 生成 {len(candidates)} 条路线方案。"
            f"首选路线为「{best.route.name}」，来源为{best.label}，"
            f"约 {best.analysis.distance_km} km，预计 {best.analysis.estimated_duration_hours} 小时，"
            f"风险等级 {best.analysis.risk_level}。"
        )
        recommendations = [
            "出发前再次核对天气预警、景区开放状态、交通末班时间和补给点营业情况。",
            "API 规划路线仅代表可行路径建议，不应包装为社区热门路线或真实用户轨迹。",
        ]
        if travel_research:
            recommendations.extend(travel_research.next_steps[:3])
            if travel_research.lodging:
                recommendations.append(f"住宿优先核对：{travel_research.lodging[0].title}。")
            if travel_research.transport:
                recommendations.append(f"交通优先核对：{travel_research.transport[0].title}。")
        if any(candidate.route.source == "user_kml" for candidate in candidates):
            recommendations.insert(0, "已优先使用用户提供的 KML 轨迹，并在此基础上补充距离、海拔和风险分析。")
        if any(candidate.analysis.risk_level == "high" for candidate in candidates):
            recommendations.append("存在高风险路线，建议降低强度、缩短行程或选择更稳定天气窗口。")

        # 生成静态行程日程
        itinerary = self._template_itinerary(request, candidates)
        # 生成静态装备清单
        gear_list = self._template_gear_list(request, candidates)
        # 生成静态安全提醒
        safety_guide = self._template_safety_guide(request, candidates, warnings)

        return GuideDraft(
            summary=summary,
            recommendations=recommendations,
            source="template",
            itinerary=itinerary,
            gear_list=gear_list,
            safety_guide=safety_guide,
        )

    def _template_itinerary(self, request: HikingGuideRequest, candidates: list[RouteCandidate]) -> Itinerary | None:
        best = candidates[0] if candidates else None
        if not best:
            return None
        duration = best.analysis.estimated_duration_hours
        is_multi = duration > 8.0
        total_days = max(1, round(duration / 8.0)) if is_multi else 1
        distance = best.analysis.distance_km
        elev = best.analysis.elevation

        days: list[dict] = []
        if not is_multi:
            days.append({
                "day_number": 1,
                "title": f"单日徒步：{best.route.name}",
                "distance_km": round(distance, 1),
                "elevation_gain_m": elev.ascent_m,
                "key_segments": [f"全程 {round(distance, 1)} km，预计 {round(duration, 1)} 小时"],
                "lodging_suggestion": None,
                "notes": ["请合理安排出发时间，预留充足返程时间"],
            })
        else:
            daily_dist = round(distance / total_days, 1)
            daily_ascent = round((elev.ascent_m or 0) / total_days, 0) if elev.ascent_m else None
            for day_num in range(1, total_days + 1):
                is_last = day_num == total_days
                days.append({
                    "day_number": day_num,
                    "title": f"Day {day_num}：{'下山段' if is_last else '上山段'}",
                    "distance_km": daily_dist,
                    "elevation_gain_m": daily_ascent if not is_last else None,
                    "key_segments": [f"预计行进 {daily_dist} km"],
                    "lodging_suggestion": "需提前确认沿途住宿" if not is_last else None,
                    "notes": ["注意分配体力，留足余量"],
                })

        from app.models import DayPlan
        return Itinerary(
            is_multi_day=is_multi,
            total_days=total_days,
            days=[DayPlan(**d) for d in days],
            notes=["行程为模板建议，请根据实际路线和体力调整"],
        )

    def _template_gear_list(self, request: HikingGuideRequest, candidates: list[RouteCandidate]) -> GearList | None:
        best = candidates[0] if candidates else None
        if not best:
            return None

        categories: list[GearCategory] = []
        categories.append(GearCategory(category="基础装备", items=["登山杖", "登山鞋/徒步鞋", "双肩背包(30-40L)", "头灯/手电筒"]))

        clothing = ["速干衣裤", "防风外套", "雨衣/防水外套"]
        if best.analysis.elevation.max_m is not None and best.analysis.elevation.max_m >= 3000:
            clothing.extend(["抓绒衣/羽绒内胆", "保暖帽", "防风手套"])
        categories.append(GearCategory(category="衣物防护", items=clothing))

        food_items = ["充足饮水(2-3L)", "高热量路粮(能量棒/坚果)", "电解质饮料"]
        if best.analysis.distance_km > 20:
            food_items.append("午餐便当/压缩饼干")
        categories.append(GearCategory(category="饮食补给", items=food_items))

        safety_items = ["急救包(创可贴/绷带/碘伏)", "求生哨", "防晒霜/墨镜"]
        if best.analysis.risk_level in ("medium", "high"):
            safety_items.extend(["保温毯", "备用手机电池/充电宝"])
        if request.fitness_level == "beginner":
            safety_items.append("护膝")
        categories.append(GearCategory(category="安全应急", items=safety_items))

        categories.append(GearCategory(category="电子导航", items=["手机(离线地图)", "充电宝", "运动手表/GPS"]))

        notes = []
        if best.analysis.elevation.max_m is not None and best.analysis.elevation.max_m >= 3000:
            notes.append("高海拔路线，注意防寒和高原反应")
        return GearList(categories=categories, notes=notes)

    def _template_safety_guide(self, request: HikingGuideRequest, candidates: list[RouteCandidate], warnings: list[str]) -> SafetyGuide | None:
        best = candidates[0] if candidates else None
        if not best:
            return None

        general_warnings = [
            "出发前告知家人/朋友行程路线和预计返回时间",
            "不要独自走未开发的野路",
            "遇到恶劣天气立即下撤，不要冒险继续",
        ]
        if best.analysis.elevation.max_m is not None and best.analysis.elevation.max_m >= 3000:
            general_warnings.append("高海拔地区注意高原反应，出现症状立即下撤")

        risk_points: list[RiskPoint] = []
        for factor in best.analysis.risk_factors:
            severity = "high" if best.analysis.risk_level == "high" else "medium"
            mitigation = "提高警觉，必要时下撤"
            if "高海拔" in factor:
                mitigation = "缓慢行进，注意呼吸节奏，出现不适立即下撤"
            elif "降水" in factor:
                mitigation = "携带雨具，避免涉水路段，注意路面湿滑"
            elif "大风" in factor:
                mitigation = "避免山脊暴露路段，注意保暖"
            elif "高温" in factor:
                mitigation = "避开正午时段，增加饮水频率"
            elif "低温" in factor:
                mitigation = "增加保暖层，注意防风防冻"
            risk_points.append(RiskPoint(
                location_description="路线全程",
                risk_type=factor,
                severity=severity,
                mitigation=mitigation,
            ))

        return SafetyGuide(
            general_warnings=general_warnings,
            risk_points=risk_points,
            emergency_contacts=["景区救援电话（出发前查询确认）", "当地卫生院/医院电话"],
            emergency_measures=[
                "迷路时原地等待救援，不要盲目移动",
                "受伤时先做简单包扎固定，再寻求救援",
                "拨打 110/119 求助时报告准确位置和伤情",
            ],
            seasonal_notes=["请根据实际出行季节关注对应安全事项"],
        )


class BailianQwenGuideProvider:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_http_api_url: str | None = None,
        timeout_s: float = 60.0,
    ) -> None:
        settings = get_settings()
        self.api_key = (
            api_key
            or settings.api_keys.dashscope_api_key
            or settings.api_keys.bailian_api_key
        )
        self.model = model or settings.bailian.model
        self.base_http_api_url = (base_http_api_url or settings.dashscope.base_http_api_url).rstrip("/")
        self.timeout_s = timeout_s

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def generate_guide(
        self,
        request: HikingGuideRequest,
        candidates: list[RouteCandidate],
        warnings: list[str],
        data_sources: list[str],
        travel_research: TravelResearch | None = None,
        transport_plan: TransportPlan | None = None,
    ) -> GuideDraft:
        if not self.api_key:
            raise RuntimeError("DASHSCOPE_API_KEY or BAILIAN_API_KEY is not configured")

        try:
            import dashscope
        except ModuleNotFoundError as exc:
            raise RuntimeError("dashscope package is not installed; run `pip install -e .`") from exc

        dashscope.base_http_api_url = self.base_http_api_url
        response = dashscope.MultiModalConversation.call(
            api_key=self.api_key,
            model=self.model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "text": (
                                "你是一个严谨的中文徒步攻略规划助手。你只能基于输入的结构化数据生成攻略内容，"
                                "不得编造热门程度、社区评价、未提供的交通班次、余房或住宿价格。\n\n"
                                "输出必须是 JSON 对象，包含以下字段：\n"
                                "1. \"summary\": 综合概述（2-4句，包含路线特征、难度评估、行程天数判断）\n"
                                "2. \"recommendations\": 建议列表（6-10条，按重要性排序，涵盖出发前准备、行程注意事项、天气关注点）\n"
                                "3. \"itinerary\": 行程日程规划，包含：\n"
                                "   - \"is_multi_day\": boolean，是否多日行程（超过8小时或超过20km且有住宿需求则视为多日）\n"
                                "   - \"total_days\": 总天数\n"
                                "   - \"days\": 数组，每个包含 day_number(int), title(str), distance_km(float), elevation_gain_m(float|null), "
                                "key_segments(str[]), lodging_suggestion(str|null), notes(str[])\n"
                                "4. \"gear_list\": 装备物资清单，包含：\n"
                                "   - \"categories\": 数组，每个包含 category(str: 基础装备|衣物防护|饮食补给|安全应急|电子导航|其他), items(str[])\n"
                                "   - \"notes\": 装备补充说明(str[])\n"
                                "   生成规则：基于 distance_km、elevation、risk_level、weather、fitness_level 决定。"
                                "高海拔(>3000m)增加防寒层/防晒/电解质；长距离(>20km)增加备用鞋/路粮；"
                                "降水概率高增加防水外套/防水袋；初学者增加护膝/登山杖\n"
                                "5. \"safety_guide\": 安全提醒，包含：\n"
                                "   - \"general_warnings\": 一般安全警告(str[], 3-5条)\n"
                                "   - \"risk_points\": 路线风险点(str[]，每个含 location_description, risk_type, severity(low|medium|high), mitigation)\n"
                                "   - \"emergency_contacts\": 应急联系建议(str[])\n"
                                "   - \"emergency_measures\": 应急措施(str[], 3-5条)\n"
                                "   - \"seasonal_notes\": 季节性注意事项(str[])\n\n"
                                f"输入 JSON：{self._build_prompt_payload(request, candidates, warnings, data_sources, travel_research, transport_plan)}"
                            )
                        }
                    ],
                }
            ],
        )
        self._raise_for_dashscope_error(response)
        content = self._extract_text(response)
        parsed = self._parse_json_content(content)
        summary = str(parsed.get("summary") or "").strip()
        recommendations = parsed.get("recommendations") or []
        if not summary:
            raise RuntimeError("LLM response did not include summary")
        if not isinstance(recommendations, list):
            raise RuntimeError("LLM response recommendations must be a list")

        # 解析可选字段，容错处理
        itinerary = None
        try:
            itinerary_data = parsed.get("itinerary")
            if itinerary_data and isinstance(itinerary_data, dict):
                itinerary = Itinerary(**itinerary_data)
        except Exception:  # noqa: BLE001
            pass

        gear_list = None
        try:
            gear_data = parsed.get("gear_list")
            if gear_data and isinstance(gear_data, dict):
                gear_list = GearList(**gear_data)
        except Exception:  # noqa: BLE001
            pass

        safety_guide = None
        try:
            safety_data = parsed.get("safety_guide")
            if safety_data and isinstance(safety_data, dict):
                safety_guide = SafetyGuide(**safety_data)
        except Exception:  # noqa: BLE001
            pass

        return GuideDraft(
            summary=summary,
            recommendations=[str(item).strip() for item in recommendations if str(item).strip()],
            source=f"bailian:{self.model}",
            itinerary=itinerary,
            gear_list=gear_list,
            safety_guide=safety_guide,
        )

    def _build_prompt_payload(
        self,
        request: HikingGuideRequest,
        candidates: list[RouteCandidate],
        warnings: list[str],
        data_sources: list[str],
        travel_research: TravelResearch | None,
        transport_plan: TransportPlan | None,
    ) -> str:
        return json.dumps(
            {
                "destination": request.destination,
                "start_city": request.start_city,
                "date_range": [d.isoformat() for d in request.date_range]
                if request.date_range
                else None,
                "fitness_level": request.fitness_level,
                "preferences": request.preferences,
                "route_text": request.route_text,
                "route_candidates": [
                    {
                        "name": candidate.route.name,
                        "label": candidate.label,
                        "source": candidate.route.source,
                        "confidence": candidate.route.confidence,
                        "distance_km": candidate.analysis.distance_km,
                        "estimated_duration_hours": candidate.analysis.estimated_duration_hours,
                        "elevation": candidate.analysis.elevation.model_dump(),
                        "risk_level": candidate.analysis.risk_level,
                        "risk_factors": candidate.analysis.risk_factors,
                        "warnings": candidate.analysis.warnings,
                    }
                    for candidate in candidates
                ],
                "warnings": warnings,
                "data_sources": data_sources,
                "travel_research": travel_research.model_dump() if travel_research else None,
                "transport_plan": transport_plan.model_dump() if transport_plan else None,
            },
            ensure_ascii=False,
        )

    def _raise_for_dashscope_error(self, response: Any) -> None:
        status_code = self._read(response, "status_code", default=None)
        if status_code is None or int(status_code) == 200:
            return
        code = self._read(response, "code", default="unknown")
        message = self._read(response, "message", default="DashScope request failed")
        raise RuntimeError(f"DashScope request failed ({status_code}, {code}): {message}")

    def _extract_text(self, response: Any) -> object:
        try:
            output = self._read(response, "output")
            choices = self._read(output, "choices")
            message = self._read(choices[0], "message")
            content = self._read(message, "content")
            if isinstance(content, str):
                return content
            return self._read(content[0], "text")
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise RuntimeError("DashScope response did not include text content") from exc

    def _read(self, value: Any, key: str, default: Any = ...) -> Any:
        try:
            if isinstance(value, dict):
                return value[key]
            return getattr(value, key)
        except (KeyError, AttributeError):
            if default is ...:
                raise
            return default

    def _parse_json_content(self, content: object) -> dict[str, object]:
        """解析模型返回的 content 字段，接受多种格式并返回 dict。

        支持的输入类型：
        - 已经是 dict：直接返回
        - 字符串：可能为纯 JSON、被 ```json ``` 包裹，或以 `json` 前缀开始
        - 列表：如果是单元素字符串列表，会尝试解析该字符串
        解析失败时抛出含义明确的 RuntimeError 以便上层捕获。
        """
        # 已经是 dict，直接返回
        if isinstance(content, dict):
            parsed = content
            if not isinstance(parsed, dict):
                raise RuntimeError("LLM response must be a JSON object")
            return parsed

        # 列表情况（例如某些实现返回 parts 列表）
        if isinstance(content, list):
            if len(content) == 1 and isinstance(content[0], str):
                content_str = content[0].strip()
            else:
                raise RuntimeError("LLM response must be a JSON object")
        elif isinstance(content, str):
            content_str = content.strip()
        else:
            raise RuntimeError("LLM response content has unsupported type")

        # 尝试提取被三重反引号包裹的内容 (```json ... ```)，支持大小写
        import re

        m = re.search(r"```(?:json)?\s*(.*?)\s*```", content_str, re.DOTALL | re.IGNORECASE)
        if m:
            content_str = m.group(1)

        # 有些模型会在字符串前加上 `json` 标识，去掉它
        if content_str.lower().startswith("json"):
            content_str = content_str[4:].strip()

        try:
            parsed = json.loads(content_str)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Failed to parse LLM JSON content: {e.msg}") from e

        if not isinstance(parsed, dict):
            raise RuntimeError("LLM response must be a JSON object")

        return parsed
