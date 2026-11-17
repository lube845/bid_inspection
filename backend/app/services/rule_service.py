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
from app.services.ocr import BAIDU_LANGUAGE_TYPES, MAX_RAW_FILE_MB


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
    # 批次共同模板：至少几家企业共现、信息量上限、长段豁免长度
    "template_min_companies": 3,
    "template_max_informativeness": 0.12,
    "template_max_chars_guard": 200,
    # 跨章节类型命中（技术标 vs 商务标等）需达到的长度才成证据
    "text_cross_type_guard_chars": 120,
    # 结构指纹：至少几个非公共标题按同序命中、占各自标题的比例、表格门槛
    "structure_min_titles": 3,
    "structure_title_ratio": 0.5,
    "structure_table_min": 3,
    # 风险等级分级阈值（PRD 4.10.8：等级必须可解释、可配置）
    "risk_text_high_count": 5,
    "risk_text_high_similarity": 0.95,
    "risk_text_medium_count": 2,
    "risk_text_medium_similarity": 0.85,
}

PERSONNEL_PARAMS = {
    "similar_name_ratio": 0.85,
    "name_match_modes": ["exact", "similar"],
    # 关键岗位范围：空列表 = 不按岗位筛选（全部岗位参与比对）。
    # 填写后只比对岗位命中这些关键词的人员；改动会在下一次分析任务中生效。
    "key_positions": [],
    "risk_personnel_high_count": 3,
    "risk_personnel_medium_count": 1,
}

PRICE_PARAMS = {
    "price_cv": 0.05,
    "price_close_deviation": 0.01,
    "price_sequence_tolerance": 0.005,
    "special_tail": ["888", "999", "998"],
    "tail_concentration_min_count": 3,
    # 整数万元/整数百元这类取整尾数在采购中很常见，默认不作为「尾数集中」信号
    "common_tail_values": ["000", "500"],
    # 逐项单价雷同（清单明细）：三条门槛同时满足才报
    "price_line_item_min_shared": 5,
    "price_line_item_min_identical": 4,
    "price_line_item_min_ratio": 0.6,
    # 单家大小写金额一致性（质量项）
    "price_amount_case_consistency": True,
}

# OCR 识别属于解析期设置，不参与比对判定，但同样走规则版本管理：
# 开关、调用方式与凭据都有版本记录，改动可追溯。
OCR_PARAMS = {
    # 是否启用 OCR：只影响没有原生文本层的扫描页
    "ocr_enabled": False,
    # 调用方式，V1 只提供百度 OCR
    "ocr_provider": "baidu",
    # 百度智能云 OCR 凭据（API Key / Secret Key），对外展示时脱敏
    "baidu_api_key": "",
    "baidu_secret_key": "",
    "baidu_language_type": "CHN_ENG",
    "baidu_detect_direction": True,
    "baidu_probability": False,
    "ocr_timeout_seconds": 60,
    # 单次 OCR 上传的原始文件大小上限（MB），超过时 PDF 自动拆分成多个文件
    "ocr_max_file_mb": 6,
}

DIMENSION_PARAMS: dict[str, dict] = {
    "text": TEXT_PARAMS,
    "personnel": PERSONNEL_PARAMS,
    "price": PRICE_PARAMS,
    "ocr": OCR_PARAMS,
}

DIMENSION_LABELS = {
    "text": "文本相似",
    "personnel": "人员重复",
    "price": "价格异常",
    "ocr": "OCR 识别",
}

# 这些参数在 V1.0 只存储与展示，尚未接入检测引擎。
NOT_ENFORCED_PARAMS: dict[str, set[str]] = {}

# 敏感参数：对外只返回脱敏占位符，只有管理员能读写真实值
SENSITIVE_PARAMS = {"baidu_api_key", "baidu_secret_key"}
SECRET_MASK = "********"

# OCR 属于解析期设置：不进入分析任务的规则快照，
# 既避免把解析参数混进分析配置，也避免密钥被写进任务记录。
SNAPSHOT_EXCLUDED_DIMENSIONS = {"ocr"}


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
            "template_min_companies": 3,
            "template_max_informativeness": 0.12,
            "template_max_chars_guard": 200,
            "text_cross_type_guard_chars": 120,
            # 结构指纹：仅标题一致不成立证据，表格/图片信号才计入
            "structure_min_titles": 3,
            "structure_title_ratio": 0.5,
            "structure_table_min": 3,
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
            "price_line_item_min_shared": 5,
            "price_line_item_min_identical": 4,
            "price_line_item_min_ratio": 0.6,
            "price_amount_case_consistency": True,
        }
    if dimension == "ocr":
        return {
            "ocr_enabled": settings.ocr_enabled,
            "ocr_provider": settings.ocr_provider,
            "baidu_api_key": settings.baidu_api_key,
            "baidu_secret_key": settings.baidu_secret_key,
            "baidu_language_type": settings.baidu_language_type,
            "baidu_detect_direction": settings.baidu_detect_direction,
            "baidu_probability": settings.baidu_probability,
            "ocr_timeout_seconds": settings.ocr_timeout_seconds,
            "ocr_max_file_mb": settings.ocr_max_file_mb,
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


def ensure_missing_text_params(db: Session, actor: User | None = None) -> DetectionRuleVersion | None:
    """给生效中的文本规则补齐新引入的参数键（幂等）。

    检测引擎新增参数（例如批次共同模板的三个阈值）后，旧的生效版本里没有这些键：
    阈值回落机制会让它们按默认值生效，但管理员在规则页看不到、也改不了。
    这里补一个版本把缺的键带上并生效，**已存在的键保持原值**——不覆盖管理员
    调过的参数。缺省值取自 ``default_params``，与回落默认值同源。
    """
    active = active_version(db, "text")
    if active is None:
        return None
    params = dict(active.params or {})
    factory = default_params("text")
    missing = {
        key: factory[key]
        for key in factory
        if key not in params
    }
    if not missing:
        return None
    merged = dict(params)
    merged.update(missing)
    return create_version(
        db,
        "text",
        merged,
        "补齐文本相似新增参数：" + "、".join(sorted(missing)),
        actor,
        activate=True,
    )


def _migrate_legacy_key_positions(
    db: Session, actor: User | None = None
) -> list[DetectionRuleVersion]:
    """把历史版本里「只展示不生效」的关键岗位范围迁成空列表（不筛选）。

    只在参数恰好等于旧出厂默认值时迁移；管理员自己填过的值保持不动。
    """
    legacy_default = [
        "项目负责人",
        "项目经理",
        "技术负责人",
        "授权代表",
        "造价人员",
        "施工人员",
        "设计负责人",
    ]
    active = active_version(db, "personnel")
    if active is None:
        return []
    params = dict(active.params or {})
    if params.get("key_positions") != legacy_default:
        return []
    merged = dict(params)
    merged["key_positions"] = []
    return [
        create_version(
            db,
            "personnel",
            merged,
            "关键岗位范围改为可配置筛选：历史默认值迁移为不筛选，"
            "如需只比对关键岗位请在规则页填写岗位关键词",
            actor,
            activate=True,
        )
    ]


def ensure_missing_params(
    db: Session, actor: User | None = None
) -> list[DetectionRuleVersion]:
    """给所有生效维度补齐新引入的参数键（幂等）。

    检测引擎新增参数（批次共同模板、逐项单价门槛等）后，旧的生效版本里没有
    这些键：阈值回落机制会让它们按默认值生效，但管理员在规则页看不到、也改不
    了。这里逐个维度补一个版本把缺的键带上并生效，**已存在的键保持原值**——
    不覆盖管理员调过的参数。

    敏感参数（OCR 密钥）不参与补齐：空值不是「缺键」，补进去反而会清掉真实凭据。
    """
    records: list[DetectionRuleVersion] = []
    # 关键岗位范围历史上只记录不生效，出厂值是 7 个岗位；它一旦真正接入引擎，
    # 旧版本里的这份列表会立刻开始过滤人员。这里先把它迁成「不筛选」，由管理员
    # 在规则页按需填写，而不是让升级本身悄悄改变人员比对的覆盖范围。
    records.extend(_migrate_legacy_key_positions(db, actor))
    for dimension in DIMENSION_PARAMS:
        active = active_version(db, dimension)
        if active is None:
            continue
        params = dict(active.params or {})
        factory = {
            key: value
            for key, value in default_params(dimension).items()
            if key not in SENSITIVE_PARAMS
        }
        missing = {key: value for key, value in factory.items() if key not in params}
        if not missing:
            continue
        merged = dict(params)
        merged.update(missing)
        records.append(
            create_version(
                db,
                dimension,
                merged,
                f"{DIMENSION_LABELS.get(dimension, dimension)}补齐新增参数："
                + "、".join(sorted(missing)),
                actor,
                activate=True,
            )
        )
    return records


def ensure_missing_text_params(db: Session, actor: User | None = None) -> DetectionRuleVersion | None:
    """兼容旧调用：只补齐文本维度。"""
    active = active_version(db, "text")
    if active is None:
        return None
    params = dict(active.params or {})
    factory = default_params("text")
    missing = {key: value for key, value in factory.items() if key not in params}
    if not missing:
        return None
    merged = dict(params)
    merged.update(missing)
    return create_version(
        db,
        "text",
        merged,
        "补齐文本相似新增参数：" + "、".join(sorted(missing)),
        actor,
        activate=True,
    )


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


def _normalize_ocr_params(params: dict) -> None:
    """把 OCR 参数收敛到接口允许的范围内。

    百度接口对 base64 + urlencode 后的请求体限制 8MB，换算成原始文件约 6MB；
    超出的一律按 6MB 处理，解析时才不会因为体积被拒。
    """
    try:
        max_mb = int(params.get("ocr_max_file_mb") or MAX_RAW_FILE_MB)
    except (TypeError, ValueError):
        max_mb = MAX_RAW_FILE_MB
    params["ocr_max_file_mb"] = max(1, min(max_mb, MAX_RAW_FILE_MB))


def _validate_ocr_params(params: dict) -> None:
    """OCR 参数校验，避免管理员误填导致解析阶段整批失败。"""
    provider = str(params.get("ocr_provider") or "").strip().lower()
    if provider not in {"baidu"}:
        raise RuleError("OCR 调用方式必须是 baidu")
    language = str(params.get("baidu_language_type") or "CHN_ENG").strip().upper()
    if language not in BAIDU_LANGUAGE_TYPES:
        raise RuleError(
            "OCR 识别语言必须是 " + "、".join(BAIDU_LANGUAGE_TYPES) + " 之一"
        )
    try:
        timeout = int(params.get("ocr_timeout_seconds") or 60)
        max_mb = int(params.get("ocr_max_file_mb") or 8)
    except (TypeError, ValueError) as exc:
        raise RuleError("OCR 超时时间与文件体积上限必须是数值") from exc
    if not 5 <= timeout <= 600:
        raise RuleError("OCR 超时时间必须在 5～600 秒之间")
    if not 1 <= max_mb <= MAX_RAW_FILE_MB:
        raise RuleError(
            f"OCR 单次文件大小上限必须在 1～{MAX_RAW_FILE_MB}MB 之间"
        )
    if params.get("ocr_enabled") and provider == "baidu":
        if not str(params.get("baidu_api_key") or "").strip():
            raise RuleError("启用 OCR 前必须填写百度 API Key")
        if not str(params.get("baidu_secret_key") or "").strip():
            raise RuleError("启用 OCR 前必须填写百度 Secret Key")


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
    # 前端回传的是脱敏占位符时，保留当前生效版本里的真实值
    for key in SENSITIVE_PARAMS:
        if str(merged.get(key) or "").strip() == SECRET_MASK:
            fallback = (current.params or {}).get(key) if current else None
            merged[key] = (
                fallback
                if fallback is not None
                else default_params(dimension).get(key, "")
            )
    if dimension == "text":
        _validate_text_params(merged)
    if dimension == "ocr":
        _normalize_ocr_params(merged)
        _validate_ocr_params(merged)
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
        if dimension in SNAPSHOT_EXCLUDED_DIMENSIONS:
            continue
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


def active_ocr_config(db: Session) -> dict:
    """当前生效的 OCR 配置（含真实凭据，仅供解析流程内部使用）。"""
    record = active_version(db, "ocr")
    config = default_params("ocr")
    if record:
        config.update(record.params or {})
    return config


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
        "template_min_companies": 3,
        "template_max_informativeness": 0.12,
        "template_max_chars_guard": 200,
        "price_cv": settings.price_cv_threshold,
        "price_close_deviation": settings.price_close_deviation,
        "price_sequence_tolerance": settings.price_sequence_tolerance,
        "price_line_item_min_shared": 5,
        "price_line_item_min_identical": 4,
        "price_line_item_min_ratio": 0.6,
        "special_tail": ["888", "999", "998"],
        "tail_concentration_min_count": 3,
        "common_tail_values": ["000", "500"],
        "price_amount_case_consistency": True,
        "structure_min_titles": 3,
        "structure_title_ratio": 0.5,
        "structure_table_min": 3,
        "risk_text_high_count": 5,
        "risk_text_high_similarity": 0.95,
        "risk_text_medium_count": 2,
        "risk_text_medium_similarity": 0.85,
        "risk_personnel_high_count": 3,
        "risk_personnel_medium_count": 1,
        "key_positions": [],
    }
    for entry in (snapshot or {}).values():
        for key, value in (entry.get("params") or {}).items():
            if key not in thresholds or value is None:
                continue
            default = thresholds[key]
            # 数值阈值按默认值的类型转换；列表（如 special_tail）与布尔值单独处理，
            # 否则会被「只覆盖数值键」的旧逻辑静默丢弃。
            if isinstance(default, bool):
                if isinstance(value, bool):
                    thresholds[key] = value
            elif isinstance(default, list):
                if isinstance(value, (list, tuple)):
                    thresholds[key] = [str(item) for item in value]
            elif isinstance(default, (int, float)):
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    thresholds[key] = value
    return thresholds


def _mask_params(params: dict) -> dict:
    """敏感参数脱敏：只提示「已配置」，不泄露真实值。"""
    masked = {}
    for key, value in (params or {}).items():
        if key in SENSITIVE_PARAMS and str(value or "").strip():
            masked[key] = SECRET_MASK
        else:
            masked[key] = value
    return masked


def version_payload(record: DetectionRuleVersion, include_secrets: bool = False) -> dict:
    """规则版本的对外结构。

    ``include_secrets=False`` 时敏感参数会被脱敏，只有管理员编辑规则时才会
    拿到真实值（前端用占位符回传，服务端识别占位符后保留原值）。
    """
    dimension = record.dimension
    params = dict(record.params or {})
    previous = dict(record.previous_params or {})
    if not include_secrets:
        params = _mask_params(params)
        previous = _mask_params(previous)
    changed = []
    for key, value in params.items():
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
        "params": params,
        "previous_params": previous,
        "changed": changed,
        "change_reason": record.change_reason,
        "created_by_name": record.created_by_name,
        "created_at": record.created_at,
        "activated_at": record.activated_at,
        "not_enforced_params": sorted(NOT_ENFORCED_PARAMS.get(dimension, set())),
        "has_secrets": any(
            str((record.params or {}).get(key) or "").strip() for key in SENSITIVE_PARAMS
        ),
    }
