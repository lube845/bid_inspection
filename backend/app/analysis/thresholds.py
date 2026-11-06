"""分析阈值读取。

检测引擎的阈值优先取「分析任务启动时的规则快照」（analysis_config["thresholds"]），
缺失或非法时回落到部署配置（core/config.py）的默认值。这样历史任务即使在规则
被修改之后重跑，用的仍然是启动时的那一版规则。
"""

from app.core.config import settings


def threshold(thresholds: dict | None, key: str, default=None):
    """从阈值快照取一个数值；快照缺失、值为空或类型不符时返回 default。"""
    if thresholds:
        value = thresholds.get(key)
        if value is not None:
            try:
                if isinstance(default, bool):
                    return bool(value)
                if isinstance(default, float):
                    return float(value)
                if isinstance(default, int):
                    return int(value)
                return value
            except (TypeError, ValueError):
                return default
    return default


def text_thresholds(thresholds: dict | None) -> dict:
    """文本相似维度使用的阈值集合。"""
    return {
        "text_similarity": threshold(
            thresholds, "text_similarity", settings.text_similarity_threshold
        ),
        "text_min_match_chars": threshold(
            thresholds, "text_min_match_chars", settings.text_min_match_chars
        ),
        "text_min_block_chars": threshold(
            thresholds, "text_min_block_chars", settings.text_min_block_chars
        ),
        "text_chunk_target_min": threshold(
            thresholds, "text_chunk_target_min", settings.text_chunk_target_min
        ),
        "text_chunk_target_max": threshold(
            thresholds, "text_chunk_target_max", settings.text_chunk_target_max
        ),
        "text_chunk_max_chars": threshold(
            thresholds, "text_chunk_max_chars", settings.text_chunk_max_chars
        ),
        "text_chunk_min_chars": threshold(
            thresholds, "text_chunk_min_chars", settings.text_chunk_min_chars
        ),
        "text_shingle_size": threshold(
            thresholds, "text_shingle_size", settings.text_shingle_size
        ),
        "text_minhash_num_perm": threshold(
            thresholds, "text_minhash_num_perm", settings.text_minhash_num_perm
        ),
        "text_lsh_threshold": threshold(
            thresholds, "text_lsh_threshold", settings.text_lsh_threshold
        ),
        "max_candidate_pairs": threshold(
            thresholds, "max_candidate_pairs", settings.max_candidate_pairs
        ),
        "max_matches_per_pair": threshold(
            thresholds, "max_matches_per_pair", settings.max_matches_per_pair
        ),
        "tender_exclusion_ratio": threshold(thresholds, "tender_exclusion_ratio", 0.9),
    }


def price_thresholds(thresholds: dict | None) -> dict:
    """价格异常维度使用的阈值集合。"""
    return {
        "price_cv": threshold(thresholds, "price_cv", settings.price_cv_threshold),
        "price_close_deviation": threshold(
            thresholds, "price_close_deviation", settings.price_close_deviation
        ),
        "price_sequence_tolerance": threshold(
            thresholds, "price_sequence_tolerance", settings.price_sequence_tolerance
        ),
    }


def personnel_thresholds(thresholds: dict | None) -> dict:
    """人员重复维度使用的阈值集合。"""
    return {
        "similar_name_ratio": threshold(thresholds, "similar_name_ratio", 0.85),
    }
