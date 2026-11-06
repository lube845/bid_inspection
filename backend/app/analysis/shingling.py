"""Shingling、MinHash 与 LSH（方案第 8 章）.

- Shingle：字符级 N-gram（默认 3），使用稳定哈希，跨进程/跨任务结果可复现；
- MinHash：128 个独立置换，把 Shingle 集合压缩成固定长度签名；
- LSH：按 band 分桶召回候选，band/rows 由 num_perm 与召回阈值自动推导。

这里不依赖外部 MinHash 库，避免算法版本与第三方升级绑定；numpy 仅用于
向量化签名计算。
"""

from __future__ import annotations

import random
from dataclasses import dataclass

import numpy as np

from app.utils.text import stable_hash

# 小于 2^32 的最大素数，保证 (a*x+b) 在 uint64 内不溢出
_PRIME = 4294967291
_DEFAULT_SEED = 20240501


def shingle_set(text: str, size: int) -> set[int]:
    """字符级 N-gram 集合，自动去重。"""
    if not text:
        return set()
    if size <= 0:
        raise ValueError("shingle size 必须大于 0")
    if len(text) < size:
        return {stable_hash(text)}
    return {stable_hash(text[i : i + size]) for i in range(len(text) - size + 1)}


class MinHasher:
    """MinHash 签名生成器。

    signature[i] = min_{shingle} ((a_i * hash(shingle) + b_i) mod PRIME)
    """

    def __init__(self, num_perm: int = 128, seed: int = _DEFAULT_SEED):
        if num_perm <= 0:
            raise ValueError("num_perm 必须大于 0")
        self.num_perm = num_perm
        rng = random.Random(seed)
        self.a = np.array(
            [rng.randrange(1, _PRIME) for _ in range(num_perm)], dtype=np.uint64
        )
        self.b = np.array(
            [rng.randrange(0, _PRIME) for _ in range(num_perm)], dtype=np.uint64
        )

    def signature(self, hashes: set[int] | list[int]) -> np.ndarray:
        if not hashes:
            return np.zeros(self.num_perm, dtype=np.uint64)
        values = np.fromiter(hashes, dtype=np.uint64, count=len(hashes))
        permuted = (self.a[:, None] * values[None, :] + self.b[:, None]) % _PRIME
        return permuted.min(axis=1).astype(np.uint64)


def estimate_jaccard(signature_a: np.ndarray, signature_b: np.ndarray) -> float:
    if len(signature_a) == 0:
        return 0.0
    return float(np.mean(signature_a == signature_b))


def optimal_bands(num_perm: int, threshold: float) -> tuple[int, int]:
    """选择 bands/rows 组合。

    LSH 命中概率 P(s) = 1 - (1 - s^r)^b，b*r <= num_perm。
    不能只追求 P(threshold) 最大，否则会退化成 r=1、候选爆炸；这里同时考虑
    高相似度召回与低相似度误召回，并要求尽量用满签名位。
    """
    threshold = min(max(float(threshold), 0.05), 0.95)
    low = max(0.01, threshold - 0.3)
    high = min(0.99, threshold + 0.3)
    best = (1, num_perm)
    best_score = float("-inf")
    for bands in range(2, num_perm + 1):
        rows = num_perm // bands
        if rows <= 0:
            break
        coverage = (bands * rows) / num_perm
        p_threshold = 1 - (1 - threshold**rows) ** bands
        p_high = 1 - (1 - high**rows) ** bands
        p_low = 1 - (1 - low**rows) ** bands
        score = p_high - p_low + 0.25 * p_threshold + 0.05 * coverage
        if score > best_score + 1e-12:
            best_score = score
            best = (bands, rows)
    return best


@dataclass
class LSHParams:
    bands: int
    rows: int
    threshold: float
    num_perm: int

    @property
    def used_permutations(self) -> int:
        return self.bands * self.rows


class MinHashLSH:
    """基于 banding 的内存 LSH 索引。

    索引键为 ``(band_index, band_bytes)``；查询返回候选 id 以及命中的 band 数，
    命中 band 数可用于候选排序（不是最终相似度）。
    """

    def __init__(self, num_perm: int = 128, threshold: float = 0.5):
        self.params = LSHParams(
            *optimal_bands(num_perm, threshold), threshold=threshold, num_perm=num_perm
        )
        self.buckets: dict[tuple[int, bytes], list[str]] = {}

    def add(self, key: str, signature: np.ndarray) -> None:
        bands, rows = self.params.bands, self.params.rows
        for band in range(bands):
            chunk = signature[band * rows : (band + 1) * rows]
            bucket_key = (band, chunk.tobytes())
            self.buckets.setdefault(bucket_key, []).append(key)

    def query(self, signature: np.ndarray, exclude: str | None = None) -> dict[str, int]:
        bands, rows = self.params.bands, self.params.rows
        votes: dict[str, int] = {}
        for band in range(bands):
            chunk = signature[band * rows : (band + 1) * rows]
            for key in self.buckets.get((band, chunk.tobytes()), ()):  # 候选召回
                if key == exclude:
                    continue
                votes[key] = votes.get(key, 0) + 1
        return votes

    def __len__(self) -> int:
        return len({key for keys in self.buckets.values() for key in keys})
