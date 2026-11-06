"""检测规则版本管理。

一条记录 = 某个检测维度的某一版参数。修改规则时不覆盖旧记录，而是新建一条版本；
同一维度同时只有一条 active 版本，旧版本转 archived 保留，保证历史分析任务仍能
追溯到启动时使用的规则版本（PRD 4.13.3 / 5.4）。

规则参数保存在数据库里，部署配置（core/config.py）只在首次初始化时作为种子。
"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import DetectionRuleVersion, User
from app.models.base import utcnow
from app.models.enums import RuleStatus


class RuleError(Exception):
    """检测规则相关的业务校验错误。"""


# 各维度的参数目录。键名刻意与 core/config.py 中的设置同名，使解析出的阈值
# 结构与历史分析任务 analysis_config["thresholds"] 保持一致。
TEXT_PARAMS = {
    "text_similarity": 0.6,
    "text_min_match_chars": 40,
    "text_min_block_chars": 50,
    "text_chunk_target_min": 300,
    "text_chunk_target_max": 500,
    "text_chunk_max_chars": 800,
    "text_chunk_min_chars": 50,
    # 字符级 Shingle，不依赖中文分词；3-gram 对少量标点/空格变化有容忍度
    "text_shingle_size": 3,
    "text_minhash_num_perm": 128,
    "text_lsh_threshold": 0.5,
    "max_candidate_pairs": 20000,
    "max_matches_per_pair": 200,
    # 招标文件公共内容排除：命中招标文件的指纹占比阈值
    "tender_exclusion_ratio": 0.9,
    # 风险等级分级阈值（PRD 4.10.8：等级必须可解释、可配置）
    "risk_text_high_count": 5,
    "risk_text_high_similarity": 0.95,
    "risk_text_medium_count": 2,
    "risk_text_medium_similarity": 0.85,
}

PERSONNEL_PARAMS = {
    "similar_name_ratio": 0.85,
    "name_match_modes": ["exact", "similar"],
    "key_positions": [
        "项目负责人",
        "项目经理",
        "技术负责人",
        "授权代表",
        "造价人员",
        "施工人员",
        "设计负责人",
    ],
    "risk_personnel_high_count": 3,
    "risk_personnel_medium_count": 1,
}

PRICE_PARAMS = {
    "price_cv": 0.05,
    "price_close_deviation": 0.01,
    "price_sequence_tolerance": 0.005,
    "special_tail": ["888", "999", "998"],
    "tail_concentration_min_count": 3,
}

DIMENSION_PARAMS: dict[str, dict] = {
    "text": TEXT_PARAMS,
    "personnel": PERSONNEL_PARAMS,
    "price": PRICE_PARAMS,
}

DIMENSION_LABELS = {
    "text": "文本相似",
    "personnel": "人员重复",
    "price": "价格异常",
}

# 这些参数在 V1.0 只存储与展示，尚未接入检测引擎。
NOT_ENFORCED_PARAMS = {
    "personnel": {"key_positions"},
    "price": {"amount_case_consistency"},
}


def default_params(dimension: str) -> dict:
    """该维度的出厂参数（以部署配置为种子）。"""
    if dimension == "text":
        return {
            "text_similarity": settings.text_similarity_threshold,
            "text_min_match_chars": settings.text_min_match_chars,
            "text_min_block_chars": settings.text_min_block_chars,
            "text_chunk_target_min": settings.text_chunk_target_min,
            "text_chunk_target_max": settings.text_chunk_target_max,
            "text_chunk_max_chars": settings.text_chunk_max_chars,
            "text_chunk_min_chars": settings.text_chunk_min_chars,
            "text_shingle_size": settings.text_shingle_size,
            "text_minhash_num_perm": settings.text_minhash_num_perm,
            "text_lsh_threshold": settings.text_lsh_threshold,
            "max_candidate_pairs": settings.max_candidate_pairs,
            "max_matches_per_pair": settings.max_matches_per_pair,
            "tender_exclusion_ratio": 0.9,
            "risk_text_high_count": 5,
            "risk_text_high_similarity": 0.95,
            "risk_text_medium_count": 2,
            "risk_text_medium_similarity": 0.85,
        }
    if dimension == "price":
        return {
            "price_cv": settings.price_cv_threshold,
            "price_close_deviation": settings.price_close_deviation,
            "price_sequence_tolerance": settings.price_sequence_tolerance,
            "special_tail": ["888", "999", "998"],
            "tail_concentration_min_count": 3,
        }
    return dict(PERSONNEL_PARAMS)


# 算法级参数：升级检测引擎时随全局配置一起切换
ENGINE_PARAM_KEYS = {
    "text_min_block_chars",
    "text_chunk_target_min",
    "text_chunk_target_max",
    "text_chunk_max_chars",
    "text_chunk_min_chars",
    "text_shingle_size",
    "text_minhash_num_perm",
    "text_lsh_threshold",
}


def ensure_engine_version(db: Session, actor: User | None = None) -> DetectionRuleVersion | None:
    """把旧版文本规则升级到当前检测引擎配置。

    判定依据是旧版参数里没有 MinHash 相关键；升级时新建一个规则版本并生效，
    旧版本保留为历史版本。管理员之后调整参数不会再触发自动升级。
    """
    active = active_version(db, "text")
    if active is None:
        return None
    params = dict(active.params or {})
    if "text_minhash_num_perm" in params:
        return None
    factory = default_params("text")
    merged = dict(params)
    merged.update({key: factory[key] for key in ENGINE_PARAM_KEYS if key in factory})
    record = create_version(
        db,
        "text",
        merged,
        "文本相似度引擎升级：字符级 3-gram + MinHash/LSH",
        actor,
        activate=True,
    )
    return record


def list_versions(db: Session, dimension: str | None = None) -> list[DetectionRuleVersion]:
    query = select(DetectionRuleVersion)
    if dimension:
        query = query.where(DetectionRuleVersion.dimension == dimension)
    return list(
        db.scalars(
            query.order_by(
                DetectionRuleVersion.dimension,
                DetectionRuleVersion.version_no.desc(),
            )
        )
    )


def active_version(db: Session, dimension: str) -> DetectionRuleVersion | None:
    return db.scalars(
        select(DetectionRuleVersion)
        .where(
            DetectionRuleVersion.dimension == dimension,
            DetectionRuleVersion.status == RuleStatus.ACTIVE.value,
        )
        .order_by(DetectionRuleVersion.version_no.desc())
        .limit(1)
    ).first()


def ensure_initial_version(db: Session, actor: User | None = None) -> list[DetectionRuleVersion]:
    """为每个维度创建出厂版本（幂等）。"""
    created: list[DetectionRuleVersion] = []
    for dimension in DIMENSION_PARAMS:
        if active_version(db, dimension) is not None:
            continue
        params = default_params(dimension)
        version_no = _next_version_no(db, dimension)
        record = DetectionRuleVersion(
            dimension=dimension,
            version_no=version_no,
            batch_no=_next_batch_no(db),
            status=RuleStatus.ACTIVE.value,
            params=params,
            previous_params={},
            change_reason="出厂初始版本",
            created_by=actor.id if actor else None,
            created_by_name=(actor.display_name or actor.username) if actor else "system",
            activated_at=utcnow(),
        )
        db.add(record)
        created.append(record)
    return created


def _next_version_no(db: Session, dimension: str) -> int:
    current = db.scalar(
        select(func.max(DetectionRuleVersion.version_no)).where(
            DetectionRuleVersion.dimension == dimension
        )
    )
    return int(current or 0) + 1


def _next_batch_no(db: Session) -> int:
    current = db.scalar(select(func.max(DetectionRuleVersion.batch_no)))
    return int(current or 0) + 1


def _validate_text_params(params: dict) -> None:
    """算法参数合法性校验，避免管理员误填导致分析任务运行中断。"""
    try:
        shingle = int(params.get("text_shingle_size", 3))
        num_perm = int(params.get("text_minhash_num_perm", 128))
        threshold = float(params.get("text_lsh_threshold", 0.5))
        target_min = int(params.get("text_chunk_target_min", 300))
        target_max = int(params.get("text_chunk_target_max", 500))
        max_chars = int(params.get("text_chunk_max_chars", 800))
        min_chars = int(params.get("text_chunk_min_chars", 50))
    except (TypeError, ValueError) as exc:
        raise RuleError("文本算法参数必须是数值") from exc
    if not 1 <= shingle <= 8:
        raise RuleError("Shingle 长度必须在 1～8 之间")
    if not 8 <= num_perm <= 4096:
        raise RuleError("MinHash 签名数必须在 8～4096 之间")
    if not 0 < threshold < 1:
        raise RuleError("LSH 召回阈值必须在 0～1 之间")
    if min_chars > target_min:
        raise RuleError("最小有效长度不能大于文本块目标长度下限")
    if not target_min <= target_max <= max_chars:
        raise RuleError("文本块长度需满足 目标下限 ≤ 目标上限 ≤ 最大长度")


def create_version(
    db: Session,
    dimension: str,
    params: dict,
    change_reason: str,
    actor: User | None = None,
    activate: bool = False,
) -> DetectionRuleVersion:
    """新建一个规则版本。默认状态为 draft；activate=True 时立即取代当前生效版本。"""
    if dimension not in DIMENSION_PARAMS:
        raise RuleError(f"未知的检测维度：{dimension}")
    if not change_reason or not change_reason.strip():
        raise RuleError("必须填写修改原因")
    if not isinstance(params, dict):
        raise RuleError("规则参数格式不正确")

    current = active_version(db, dimension)
    merged = {**default_params(dimension), **params}
    if dimension == "text":
        _validate_text_params(merged)
    record = DetectionRuleVersion(
        dimension=dimension,
        version_no=_next_version_no(db, dimension),
        batch_no=_next_batch_no(db),
        status=RuleStatus.DRAFT.value,
        params=merged,
        previous_params=dict(current.params) if current else {},
        change_reason=change_reason.strip(),
        supersedes_id=current.id if current else None,
        created_by=actor.id if actor else None,
        created_by_name=(actor.display_name or actor.username) if actor else "system",
    )
    db.add(record)
    db.flush()
    if activate:
        _apply_activation(db, record)
    return record


def activate(db: Session, version_id: str, actor: User | None = None) -> DetectionRuleVersion:
    """生效确认：把指定版本置为 active，同维度旧版本转 archived。"""
    record = db.get(DetectionRuleVersion, version_id)
    if not record:
        raise RuleError("规则版本不存在")
    if record.status == RuleStatus.ACTIVE.value:
        return record
    _apply_activation(db, record)
    return record


def _apply_activation(db: Session, record: DetectionRuleVersion) -> None:
    for other in db.scalars(
        select(DetectionRuleVersion).where(
            DetectionRuleVersion.dimension == record.dimension,
            DetectionRuleVersion.id != record.id,
            DetectionRuleVersion.status == RuleStatus.ACTIVE.value,
        )
    ):
        other.status = RuleStatus.ARCHIVED.value
    record.status = RuleStatus.ACTIVE.value
    record.activated_at = utcnow()


def active_snapshot(db: Session) -> dict[str, dict]:
    """当前生效的各维度规则版本快照。"""
    snapshot: dict[str, dict] = {}
    for dimension in DIMENSION_PARAMS:
        record = active_version(db, dimension)
        if not record:
            continue
        snapshot[dimension] = {
            "version_id": record.id,
            "version_no": record.version_no,
            "batch_no": record.batch_no,
            "status": record.status,
            "params": dict(record.params or {}),
        }
    return snapshot


def resolve_thresholds(snapshot: dict[str, dict]) -> dict:
    """把各维度的规则快照压平成分析任务使用的扁平阈值字典。

    输出的键名与历史 analysis_config["thresholds"] 完全一致，因此既有消费方无需
    改动；缺失的键回落到部署配置的默认值，保证与旧任务兼容。
    """
    thresholds = {
        "text_similarity": settings.text_similarity_threshold,
        "text_min_match_chars": settings.text_min_match_chars,
        "text_min_block_chars": settings.text_min_block_chars,
        "text_chunk_target_min": settings.text_chunk_target_min,
        "text_chunk_target_max": settings.text_chunk_target_max,
        "text_chunk_max_chars": settings.text_chunk_max_chars,
        "text_chunk_min_chars": settings.text_chunk_min_chars,
        "text_shingle_size": settings.text_shingle_size,
        "text_minhash_num_perm": settings.text_minhash_num_perm,
        "text_lsh_threshold": settings.text_lsh_threshold,
        "max_candidate_pairs": settings.max_candidate_pairs,
        "max_matches_per_pair": settings.max_matches_per_pair,
        "tender_exclusion_ratio": 0.9,
        "price_cv": settings.price_cv_threshold,
        "price_close_deviation": settings.price_close_deviation,
        "price_sequence_tolerance": settings.price_sequence_tolerance,
        "risk_text_high_count": 5,
        "risk_text_high_similarity": 0.95,
        "risk_text_medium_count": 2,
        "risk_text_medium_similarity": 0.85,
        "risk_personnel_high_count": 3,
        "risk_personnel_medium_count": 1,
    }
    for entry in (snapshot or {}).values():
        for key, value in (entry.get("params") or {}).items():
            if key in thresholds and isinstance(thresholds[key], (int, float)):
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    thresholds[key] = value
    return thresholds


def version_payload(record: DetectionRuleVersion) -> dict:
    """规则版本的对外结构。"""
    dimension = record.dimension
    changed = []
    previous = record.previous_params or {}
    for key, value in (record.params or {}).items():
        if previous.get(key) != value:
            changed.append({"key": key, "before": previous.get(key), "after": value})
    return {
        "id": record.id,
        "dimension": dimension,
        "dimension_label": DIMENSION_LABELS.get(dimension, dimension),
        "version_no": record.version_no,
        "batch_no": record.batch_no,
        "status": record.status,
        "status_label": {
            RuleStatus.DRAFT.value: "待生效",
            RuleStatus.ACTIVE.value: "已生效",
            RuleStatus.ARCHIVED.value: "历史版本",
        }.get(record.status, record.status),
        "params": record.params or {},
        "previous_params": record.previous_params or {},
        "changed": changed,
        "change_reason": record.change_reason,
        "created_by_name": record.created_by_name,
        "created_at": record.created_at,
        "activated_at": record.activated_at,
        "not_enforced_params": sorted(NOT_ENFORCED_PARAMS.get(dimension, set())),
    }
