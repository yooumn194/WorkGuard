"""Extractor + Reflection tests (anti-hallucination layers)."""
from backend.agents.extractor import extract_facts, reflect_facts


class FakeLLM:
    available = True

    def __init__(self, *responses):
        self.responses = list(responses)

    def complete_json(self, system, user, purpose="unspecified"):
        return self.responses.pop(0) if self.responses else None


def _run(text: str):
    blocks = [{"location": "line_1", "text": text, "meta": {}}]
    facts = extract_facts(blocks)
    return reflect_facts(blocks, facts)


def test_change_sentence_extracts_new_value():
    kept, _ = _run("由于支付接口延期，Alpha V2.0 发布时间由 9 月 20 日调整至 9 月 27 日。")
    assert len(kept) == 1
    assert kept[0]["predicate"] == "release_date"
    assert kept[0]["value"] == "2026-09-27"
    assert kept[0]["change_from"] == "2026-09-20"


def test_plain_statement():
    kept, _ = _run("Alpha V2.0 计划于 9 月 20 日正式发布。")
    assert kept[0]["value"] == "2026-09-20"


def test_mixed_predicates_in_one_block():
    kept, _ = _run("Alpha V2.0 定于 9 月 20 日正式上线，灰度发布从 9 月 18 日开始。")
    got = {(f["predicate"], f["value"]) for f in kept}
    assert ("release_date", "2026-09-20") in got
    assert ("gray_release_date", "2026-09-18") in got


def test_hedged_sentence_downgraded():
    kept, dropped = _run("上线时间可能要延到 9 月 27 日，暂定。")
    # hedge -> confidence 0.5 -> reflection keeps it but it must be flagged uncertain
    assert all(f["confidence"] <= 0.5 and f["uncertain"] for f in kept) or dropped


def test_out_of_scope_predicates_ignored():
    kept, _ = _run("负责人是张伟，状态：测试中，版本 V2.0")
    assert kept == []


def test_relative_dates_not_hallucinated():
    kept, _ = _run("上线时间往后挪到下周三，具体日子等通知。")
    assert kept == []


def test_delayed_change_across_year_infers_next_year():
    kept, _ = _run("Alpha V2.0 上线时间由 2026 年 12 月 28 日延期至 1 月 4 日。")
    assert len(kept) == 1
    assert kept[0]["change_from"] == "2026-12-28"
    assert kept[0]["value"] == "2027-01-04"


def test_date_range_is_unverified_not_guessed_as_one_release_date():
    kept, _ = _run("Alpha V2.0 上线窗口为 9月20日至9月27日。")
    assert kept
    assert all(fact["uncertain"] and fact["confidence"] <= 0.5 for fact in kept)


def test_multiple_entities_in_one_line_keep_separate_evidence():
    kept, _ = _run("Alpha V2.0 上线日期为 9月20日，Beta V1.0 上线日期为 10月1日。")
    got = {(fact["entity_mention"], fact["value"]) for fact in kept}
    assert ("Alpha V2.0", "2026-09-20") in got
    assert ("Beta V1.0", "2026-10-01") in got


def test_document_year_is_used_for_short_dates_during_reflection():
    blocks = [
        {"location": "line_1", "text": "会议日期：2025-12-20", "meta": {}},
        {"location": "line_2", "text": "Alpha V2.0 上线日期：12月28日", "meta": {}},
    ]
    kept, _ = reflect_facts(blocks, extract_facts(blocks))
    release = next(fact for fact in kept if fact["predicate"] == "release_date")
    assert release["value"] == "2025-12-28"


def test_evidence_must_exist_in_source():
    """Fabricated evidence must be dropped by the deterministic review."""
    fabricated = {
        "entity_mention": "Alpha V2.0",
        "predicate": "release_date",
        "value": "2026-09-27",
        "surface_form": "9 月 27 日",
        "change_from": None,
        "location": "line_1",
        "evidence": "这句话原文里根本不存在",
        "confidence": 0.9,
        "uncertain": False,
        "extracted_by": "heuristic",
    }
    blocks = [{"location": "line_1", "text": "上线 9 月 20 日。", "meta": {}}]
    kept, dropped = reflect_facts(blocks, [dict(fabricated)])
    assert kept == []
    assert len(dropped) == 1
    assert "evidence" in dropped[0]["review_note"]


def test_llm_mode_recovers_old_value_and_sanitizes_confidence():
    evidence = "Alpha V2.0 上线日期由 2026-09-20 调整至 2026-09-27。"
    blocks = [{"location": "line_1", "text": evidence, "meta": {}}]
    llm = FakeLLM({"facts": [{
        "entity_mention": "Alpha V2.0",
        "predicate": "release_date",
        "value": "2026-09-27",
        "surface_form": "2026-09-27",
        "location": "line_1",
        "evidence": evidence,
        "confidence": "not-a-number",
    }]})
    facts = extract_facts(blocks, llm)
    assert facts[0]["change_from"] == "2026-09-20"
    assert facts[0]["confidence"] == 0.5


def test_invalid_llm_payload_falls_back_to_heuristics():
    text = "Alpha V2.0 上线日期：2026-09-20。"
    blocks = [{"location": "line_1", "text": text, "meta": {}}]
    facts = extract_facts(blocks, FakeLLM({"facts": "not-a-list"}))
    assert len(facts) == 1
    assert facts[0]["extracted_by"] == "heuristic_fallback"
    assert facts[0]["status"] == "unverified"
    assert facts[0]["confidence"] < 0.6


def test_llm_rejecting_every_fact_uses_deterministic_fallback():
    text = "Alpha V2.0 上线日期：2026-09-20。"
    blocks = [{"location": "line_1", "text": text, "meta": {}}]
    extracted = [{
        "entity_mention": "Alpha V2.0", "predicate": "release_date",
        "value": "2026-09-20", "surface_form": "2026-09-20",
        "change_from": None, "location": "line_1", "evidence": text,
        "confidence": 0.9, "uncertain": False, "extracted_by": "llm",
    }]
    kept, _ = reflect_facts(
        blocks, extracted, FakeLLM({"facts": [{"supported": False}]})
    )
    assert len(kept) == 1
    assert kept[0]["extracted_by"] == "heuristic_fallback"
    assert kept[0]["status"] == "unverified"


def test_llm_unknown_location_cannot_reach_write_pipeline():
    text = "Alpha V2.0 上线日期：2026-09-20。"
    blocks = [{"location": "line_1", "text": text, "meta": {}}]
    extracted = [{
        "entity_mention": "Alpha V2.0", "predicate": "release_date",
        "value": "2026-09-20", "surface_form": "2026-09-20",
        "change_from": None, "location": "line_999", "evidence": text,
        "confidence": 0.9, "uncertain": False, "extracted_by": "llm",
    }]
    llm = FakeLLM({"facts": [{
        "predicate": "release_date", "value": "2026-09-20",
        "location": "line_999", "supported": True,
    }]})
    kept, dropped = reflect_facts(blocks, extracted, llm)
    assert any("unknown source location" in fact.get("review_note", "") for fact in dropped)
    assert kept[0]["location"] == "line_1"
    assert kept[0]["extracted_by"] == "heuristic_fallback"
    assert kept[0]["status"] == "unverified"


def test_llm_reflection_can_correct_value_without_losing_provenance():
    text = "Alpha V2.0 上线日期由 2026-09-20 调整至 2026-09-27。"
    blocks = [{"location": "line_1", "text": text, "meta": {}}]
    extracted = [{
        "entity_mention": "Alpha V2.0", "predicate": "release_date",
        "value": "2026-09-20", "surface_form": "2026-09-20",
        "change_from": None, "location": "line_1", "evidence": text,
        "confidence": 0.8, "uncertain": False, "extracted_by": "llm",
    }]
    reviewed = FakeLLM({"facts": [{
        "predicate": "release_date", "value": "2026-09-27",
        "location": "line_1", "confidence": 1.7, "supported": True,
    }]})
    kept, dropped = reflect_facts(blocks, extracted, reviewed)
    assert dropped == []
    assert kept[0]["entity_mention"] == "Alpha V2.0"
    assert kept[0]["value"] == "2026-09-27"
    assert kept[0]["change_from"] == "2026-09-20"
    assert kept[0]["confidence"] == 1.0
