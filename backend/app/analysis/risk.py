"""可解释的风险等级规则.

风险等级由确定性规则计算，每个等级都带原因说明；大模型不参与风险分计算。
阈值由分析任务启动时的规则快照提供（analysis_config["thresholds"]），缺失时回落
到部署配置的默认值，便于在「检测规则管理」中配置化调整。
"""

from app.analysis.thresholds import threshold
from app.models.enums import RiskLevel

_ORDER = {
    RiskLevel.NONE.value: 0,
    RiskLevel.LOW.value: 1,
    RiskLevel.MEDIUM.value: 2,
    RiskLevel.HIGH.value: 3,
}

_DEFAULTS = {
    "risk_text_high_count": 5,
    "risk_text_high_similarity": 0.95,
    "risk_text_medium_count": 2,
    "risk_text_medium_similarity": 0.85,
    "risk_personnel_high_count": 3,
    "risk_personnel_medium_count": 1,
}


def max_risk(*levels: str) -> str:
    return max(levels, key=lambda level: _ORDER.get(level, 0)) if levels else RiskLevel.NONE.value


def _rule(thresholds: dict | None, key: str):
    return threshold(thresholds, key, _DEFAULTS[key])


def text_risk(
    evidence_count: int,
    max_similarity: float,
    thresholds: dict | None = None,
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    value = RiskLevel.NONE.value
    high_count = _rule(thresholds, "risk_text_high_count")
    high_similarity = _rule(thresholds, "risk_text_high_similarity")
    medium_count = _rule(thresholds, "risk_text_medium_count")
    medium_similarity = _rule(thresholds, "risk_text_medium_similarity")
    if evidence_count >= high_count or max_similarity >= high_similarity:
        value = RiskLevel.HIGH.value
        reasons.append(
            f"文本相似证据 {evidence_count} 条（阈值 {high_count} 条），"
            f"最高相似度 {max_similarity:.0%}（阈值 {high_similarity:.0%}）"
        )
    elif evidence_count >= medium_count or max_similarity >= medium_similarity:
        value = RiskLevel.MEDIUM.value
        reasons.append(
            f"文本相似证据 {evidence_count} 条（阈值 {medium_count} 条），"
            f"最高相似度 {max_similarity:.0%}（阈值 {medium_similarity:.0%}）"
        )
    elif evidence_count >= 1:
        value = RiskLevel.LOW.value
        reasons.append(f"文本相似证据 {evidence_count} 条")
    return value, reasons


def personnel_risk(
    exact_count: int,
    similar_count: int,
    thresholds: dict | None = None,
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    high_count = _rule(thresholds, "risk_personnel_high_count")
    medium_count = _rule(thresholds, "risk_personnel_medium_count")
    if exact_count >= high_count:
        return RiskLevel.HIGH.value, [f"重复人员姓名 {exact_count} 人（阈值 {high_count} 人）"]
    if exact_count >= medium_count:
        return RiskLevel.MEDIUM.value, [f"重复人员姓名 {exact_count} 人（阈值 {medium_count} 人）"]
    if similar_count >= 1:
        return RiskLevel.LOW.value, [f"疑似相似姓名 {similar_count} 人"]
    return RiskLevel.NONE.value, reasons


def price_risk(severities: list[str]) -> tuple[str, list[str]]:
    level = max_risk(*severities) if severities else RiskLevel.NONE.value
    reasons = [] if level == RiskLevel.NONE.value else [f"涉及 {len(severities)} 条价格异常规则"]
    return level, reasons


def overall_risk(text: str, personnel: str, price: str) -> str:
    return max_risk(text, personnel, price)
