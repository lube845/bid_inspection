"""文本相似度引擎测试（方案第 6、8～10 章）。

覆盖：结构化分块、字符级 Shingle、MinHash/LSH 召回、Jaccard 精确计算、
招标文件基准排除、局部对齐和证据定位。
"""

from app.analysis.baseline import TenderBaseline
from app.analysis.chunking import build_chunks
from app.utils.text import stable_hash
from app.analysis.shingling import (
    MinHashLSH,
    MinHasher,
    optimal_bands,
    shingle_set,
)
from app.analysis.sources import BlockRef
from app.analysis.text_similarity import (
    TextIndex,
    build_text_evidences,
    find_text_matches,
    match_chunks,
)
from app.analysis.thresholds import text_thresholds


def make_block(
    text: str,
    company_id: str = "c1",
    company_name: str = "甲公司",
    block_id: str | None = None,
    page: int = 1,
    file_id: str = "f1",
    block_type: str = "paragraph",
) -> BlockRef:
    return BlockRef(
        company_id=company_id,
        company_name=company_name,
        file_id=file_id,
        file_name=f"{company_name}.pdf",
        file_type="pdf",
        file_version=1,
        page_index=page,
        printed_page_number=None,
        block_id=block_id or f"b{stable_hash(company_id + text)}",
        block_index=0,
        content=text,
        normalized=text,
        block_type=block_type,
    )


def paragraph(prefix: str, length: int = 180) -> str:
    sentence = f"{prefix}应当结合项目实际情况，科学组织施工，合理配置人员设备材料，确保工程按期保质完成。"
    return (sentence * (length // len(sentence) + 1))[:length]


def test_chunking_respects_chapter_and_heading():
    blocks = [
        make_block("第一章 总体方案", block_id="h1"),
        make_block("1.1 项目概况", block_id="h2"),
        make_block(paragraph("项目概况"), block_id="p1"),
        make_block("第二章 实施计划", block_id="h3"),
        make_block(paragraph("实施计划"), block_id="p2", page=2),
    ]
    chunks = build_chunks(blocks, text_thresholds(None))

    assert chunks
    assert {chunk.chapter for chunk in chunks} == {"第一章 总体方案", "第二章 实施计划"}
    # 标题只作为结构元数据，不进入正文
    assert all("第一章" not in chunk.normalized for chunk in chunks)
    first = next(chunk for chunk in chunks if chunk.chapter == "第一章 总体方案")
    assert first.section == "1.1 项目概况"
    assert first.page_start == 1


def test_chunking_splits_long_paragraph_at_sentence_boundary():
    text = "。".join(f"第{i}条措施要求严格落实安全生产责任制，确保施工安全" for i in range(80)) + "。"
    chunks = build_chunks([make_block(text)], text_thresholds(None))
    rules = text_thresholds(None)
    assert len(chunks) > 1
    assert all(len(chunk.normalized) <= rules["text_chunk_max_chars"] for chunk in chunks)
    assert sum(len(chunk.normalized) for chunk in chunks) > 0


def test_minhash_estimate_tracks_jaccard_and_lsh_recalls():
    rules = text_thresholds(None)
    base = paragraph("项目实施方案", 300)
    similar = base[:240] + "补充一段不同的技术说明" + base[240:]
    different = paragraph("企业财务状况说明", 300)

    hasher = MinHasher(rules["text_minhash_num_perm"])
    sig_base = hasher.signature(shingle_set(base, rules["text_shingle_size"]))
    sig_similar = hasher.signature(shingle_set(similar, rules["text_shingle_size"]))
    sig_diff = hasher.signature(shingle_set(different, rules["text_shingle_size"]))

    exact = len(shingle_set(base, 3) & shingle_set(similar, 3)) / len(
        shingle_set(base, 3) | shingle_set(similar, 3)
    )
    estimate = float((sig_base == sig_similar).mean())
    assert abs(estimate - exact) < 0.2

    bands, rows = optimal_bands(rules["text_minhash_num_perm"], 0.5)
    assert bands * rows <= rules["text_minhash_num_perm"]
    index = MinHashLSH(rules["text_minhash_num_perm"], 0.5)
    index.add("base", sig_base)
    index.add("different", sig_diff)
    assert "base" in index.query(sig_similar)


def test_tender_baseline_marks_common_content_and_effective_similarity():
    tender_text = paragraph("招标文件统一要求", 200)
    unique_a = ("甲公司根据本项目特点自主编制施工组织设计，采用分段流水作业组织施工" * 10)[:220]
    unique_b = ("乙公司结合自身管理经验制定质量保证措施，实行全过程质量控制和隐患排查" * 10)[:220]

    blocks_a = [make_block(tender_text + unique_a, company_id="c1", company_name="甲公司")]
    blocks_b = [make_block(tender_text + unique_b, company_id="c2", company_name="乙公司")]
    rules = text_thresholds(None)

    chunks_a = build_chunks(blocks_a, rules)
    chunks_b = build_chunks(blocks_b, rules)
    index = TextIndex(rules)
    index.add_chunks(chunks_a + chunks_b)
    tender = TenderBaseline.from_chunks(
        build_chunks([make_block(tender_text)], rules),
        rules["text_shingle_size"],
    )

    # 这里直接注入候选对，验证 Jaccard/基准排除本身；
    # LSH 召回能力由 test_minhash_estimate_tracks_jaccard_and_lsh_recalls 覆盖。
    matches, stats = match_chunks([(chunks_a[0], chunks_b[0], 1)], tender, thresholds=None)
    assert stats["candidate_count"] == 1
    assert matches
    match = matches[0]
    assert match.baseline_ratio > 0
    assert match.effective_similarity < match.similarity

    evidence = build_text_evidences(matches, tender)[0]
    assert evidence.evidence_data["baseline_match"] is True
    assert evidence.evidence_data["effective_similarity"] < evidence.evidence_data["similarity"]
    assert evidence.evidence_data["excluded_ranges_a"]
    assert evidence.evidence_data["baseline_source"]["page_index"] == 1


def test_evidence_keeps_original_file_page_and_offsets():
    text = paragraph("项目实施方案", 260)
    blocks_a = [make_block(text, company_id="c1", company_name="甲公司", page=3)]
    blocks_b = [make_block(text, company_id="c2", company_name="乙公司", page=7, file_id="f2")]

    matches = find_text_matches(blocks_a, blocks_b)
    assert matches
    evidence = build_text_evidences(matches, None)[0]
    data = evidence.evidence_data

    assert evidence.source_a["file_id"] == "f1"
    assert evidence.source_a["page_index"] == 3
    assert evidence.source_b["file_id"] == "f2"
    assert evidence.source_b["page_index"] == 7
    assert evidence.source_a["start_offset"] >= 0
    assert evidence.source_a["end_offset"] > evidence.source_a["start_offset"]
    assert evidence.source_a["ranges"]
    assert evidence.source_a["ranges"][0]["block_id"]
    assert data["matched_chars"] > 0
    assert data["localization_status"] == "local"
    assert data["chunk_a_id"]
    assert data["algorithm"] == "minhash_lsh+jaccard+sequence_matcher"
    assert data["shingle_size"] == 3
    assert data["minhash_num_perm"] == 128
    assert data["lsh_bands"] * data["lsh_rows"] <= data["minhash_num_perm"]
