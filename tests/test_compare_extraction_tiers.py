"""Guards for scripts/compare_extraction_tiers.py.

The comparison harness is only useful if it compares fairly. These tests pin the
methodology decisions -- fair scoping, correct response parsing, correct
knowledge-source routing -- because each one was a real bug during the
2026-08-20 build that produced plausible but wrong numbers.
"""
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import compare_extraction_tiers as cet  # noqa: E402

IDS = {
    "searchEndpoint": "https://srch-x.search.windows.net",
    "searchIndexName": "idx-documents",
    "searchSkillsetName": "skillset-documents",
    "searchDataSourceName": "ds-documents-blob",
    "searchIndexerName": "ixr-documents",
    "knowledgeSourceName": "ks-documents",
    "knowledgeBaseName": "kb-documents",
    "foundryOpenAIEndpoint": "https://aif-x.openai.azure.com",
    "foundryResource": "aif-x",
    "chatDeployment": "chat",
    "chatModel": "gpt-5-mini",
    "embeddingDeployment": "embedding",
    "embeddingModel": "text-embedding-3-large",
    "resourceGroup": "rg-x",
    "storageAccount": "stx",
    "subscriptionId": "00000000-0000-0000-0000-000000000000",
    "rawContainer": "raw",
    "documentIntelligenceEndpoint": "https://aif-x.cognitiveservices.azure.com/",
}


def test_tier_b_names_are_suffixed_and_never_collide_with_baseline():
    """Tier B must coexist with the baseline, never overwrite it."""
    n = cet.cu_names(IDS)
    assert n["index"] == "idx-documents-cu"
    assert n["knowledgeBase"] == "kb-documents-cu"
    for key, value in n.items():
        assert value.endswith("-cu"), key
        assert value != IDS.get("searchIndexName")


@patch("compare_extraction_tiers._request")
def test_comparison_uses_same_explicit_query_model_as_baseline(mock_req):
    mock_req.return_value = MagicMock(status_code=201)
    cet.create_cu_knowledge_base(
        {**IDS, "queryDeployment": "chosen-planner", "queryModel": "actual-model"}, "key",
    )
    parameters = mock_req.call_args.args[4]["models"][0]["azureOpenAIParameters"]
    assert parameters["deploymentId"] == "chosen-planner"
    assert parameters["modelName"] == "actual-model"


@patch("compare_extraction_tiers._request")
def test_cu_skillset_model_name_follows_the_actual_deployment(mock_req):
    """DEFECT: hardcoding the doc sample's 'gpt-4.1' against a gpt-5-mini
    deployment silently produced ZERO figure descriptions -- the skillset was
    accepted and the indexer reported success."""
    mock_req.return_value = MagicMock(status_code=201)
    cet.create_cu_skillset(IDS, "key")
    body = mock_req.call_args[0][4]
    skill = body["skills"][0]
    assert skill["modelName"] == "gpt-5-mini"
    assert skill["modelDeployment"] == "chat"


@patch("compare_extraction_tiers._request")
def test_cu_skillset_uses_the_supported_region_endpoint(mock_req):
    mock_req.return_value = MagicMock(status_code=201)
    endpoint = "https://cu-supported.services.ai.azure.com"
    cet.create_cu_skillset({**IDS, "contentUnderstandingSubdomainUrl": endpoint}, "key")
    body = mock_req.call_args[0][4]
    assert body["cognitiveServices"]["subdomainUrl"] == endpoint


@patch("compare_extraction_tiers._request")
def test_cu_skillset_replaces_split_skill_and_uses_semantic_chunking(mock_req):
    mock_req.return_value = MagicMock(status_code=201)
    cet.create_cu_skillset(IDS, "key")
    body = mock_req.call_args[0][4]
    kinds = [s["@odata.type"] for s in body["skills"]]
    assert "#Microsoft.Skills.Util.ContentUnderstandingSkill" in kinds
    # CU does both extraction and chunking -- a Split skill here would be a bug.
    assert not any("SplitSkill" in k for k in kinds)
    chunking = body["skills"][0]["chunkingProperties"]
    assert chunking["method"] == "semantic"
    assert chunking["unit"] == "tokens"          # semantic requires tokens
    assert "overlapLength" not in chunking       # forbidden with semantic


def test_sources_parse_from_references_shape():
    """`/retrieve` returns prose + a top-level `references` array."""
    payload = {
        "response": [{"content": [{"text": "The dmcontrol register ... [ref_id:5]"}]}],
        "references": [
            {"title": "riscv-debug-specification.pdf"},
            {"title": "riscv-debug-specification.pdf"},
            {"title": "riscv-spec.pdf"},
        ],
    }
    assert cet._sources_from_payload(payload) == [
        "riscv-debug-specification.pdf", "riscv-spec.pdf",
    ]


def test_sources_prefer_indexed_source_document_over_stale_reference_title():
    payload = {"references": [
        {"title": "old-title.pdf", "sourceData": {"sourceDocument": "manual.pdf"}},
        {"sourceData": {"sourceDocument": "manual.pdf"}},
        {"sourceData": {"sourceDocument": "other.pdf"}},
    ]}
    assert cet._sources_from_payload(payload) == ["manual.pdf", "other.pdf"]


def test_sources_parse_from_extractive_json_shape():
    """The MCP/extractiveData surface returns a JSON array inside `text`."""
    payload = {"response": [{"content": [{"text": '[{"ref_id":0,"title":"a.pdf"},{"ref_id":1,"title":"b.pdf"}]'}]}]}
    assert cet._sources_from_payload(payload) == ["a.pdf", "b.pdf"]


def test_sources_parse_returns_empty_rather_than_raising():
    assert cet._sources_from_payload({"response": [{"content": [{"text": "not json"}]}]}) == []
    assert cet._sources_from_payload({}) == []


def test_image_description_markers_detect_verbalized_bitfield_diagram():
    """CU emits no <figure> tag -- counting tags alone scores it 0 when it is
    actually doing more. Detect the prose description instead."""
    verbalized = ('- The image shows a single horizontal axis with boxes containing the labels: '
                  '"sbversion", "sbbusyerror", "sbbusy"')
    assert cet.IMAGE_DESCRIPTION_MARKERS.search(verbalized)
    # A mere textual mention of a figure is NOT a description.
    assert not cet.IMAGE_DESCRIPTION_MARKERS.search(
        "Figure 2 shows a conceptual view of the states passed through by a hart.")


@patch("compare_extraction_tiers._fetch_all")
def test_audit_counts_split_tables_via_unbalanced_tags(mock_fetch):
    mock_fetch.return_value = [
        {"content": "<table><tr><td>ok</td></tr></table>", "sourceDocument": "d.pdf",
         "pageNumberFrom": 1, "pageNumberTo": 1},
        {"content": "<td>orphaned cell</td></tr></table>", "sourceDocument": "d.pdf",
         "pageNumberFrom": 2, "pageNumberTo": 2},
    ]
    out = cet.audit_index(IDS, "key", "idx", "tier", has_headings=False)
    # Both chunks are table-involved; the orphaned tail -- the one that lost its
    # header row -- must be counted, not filtered out for lacking a <table> tag.
    assert out["tables"]["chunks_with_table"] == 2
    assert out["tables"]["split_across_chunks"] == 1
    assert out["tables"]["intact_pct"] == 50.0


@patch("compare_extraction_tiers._fetch_all")
def test_audit_flags_wrong_parent_headings(mock_fetch):
    """Siblings presented as parent->child must be counted as wrong, not valid."""
    mock_fetch.return_value = [
        {"content": "x", "sourceDocument": "d.pdf",
         "sectionH1": "5. Triggers", "sectionH2": "5.7.1. Trigger Select", "sectionH3": "5.7.2. Trigger Data 1"},
        {"content": "y", "sourceDocument": "d.pdf",
         "sectionH1": "5. Triggers", "sectionH2": "5.7. Triggers", "sectionH3": "5.7.1. Trigger Select"},
    ]
    out = cet.audit_index(IDS, "key", "idx", "tier", has_headings=True)
    assert out["citation"]["wrong_parent"] == 1
    assert out["citation"]["valid_ancestor"] == 1


@patch("compare_extraction_tiers._fetch_all")
def test_page_range_citation_is_never_structurally_misattributable(mock_fetch):
    mock_fetch.return_value = [
        {"content": "x", "sourceDocument": "d.pdf", "pageNumberFrom": 27, "pageNumberTo": 28},
    ]
    out = cet.audit_index(IDS, "key", "idx", "tier", has_headings=False)
    assert out["citation"]["structurally_misattributable"] is False
    assert out["citation"]["chunks_spanning_pages"] == 1


@patch("compare_extraction_tiers._request")
def test_audit_scopes_to_one_source_document_when_asked(mock_req):
    """Comparing whole indexes when one tier rejected a document compares
    different corpora and confounds every metric."""
    mock_req.return_value = MagicMock(status_code=200, json=lambda: {"value": []})
    cet._fetch_all(IDS, "key", "idx", "content", only_source="riscv-debug-specification.pdf")
    body = mock_req.call_args[0][4]
    assert body["filter"] == "sourceDocument eq 'riscv-debug-specification.pdf'"


@patch("compare_extraction_tiers._request")
def test_golden_run_targets_the_knowledge_source_of_the_tier_under_test(mock_req):
    """DEFECT: passing the baseline knowledge source to Tier B's knowledge base
    returns HTTP 200 with no results, which looks like a retrieval failure."""
    mock_req.return_value = MagicMock(
        status_code=200,
        json=lambda: {"references": [{"title": "riscv-debug-specification.pdf"}]},
    )
    out = cet.golden_run(IDS, "key", "kb-documents-cu",
                         [{"q": "q?", "expect": "riscv-debug-specification.pdf"}],
                         knowledge_source="ks-documents-cu")
    body = mock_req.call_args[0][4]
    assert body["knowledgeSourceParams"][0]["knowledgeSourceName"] == "ks-documents-cu"
    assert out["passed"] == 1


def test_load_golden_set_for_another_corpus(tmp_path):
    path = tmp_path / "golden.json"
    path.write_text(json.dumps([{"q": " Which connector? ", "expect": " manual.pdf "}]), encoding="utf-8")
    assert cet.load_golden_set(str(path)) == [{"q": "Which connector?", "expect": "manual.pdf"}]


@pytest.mark.parametrize("data", [
    [],
    {},
    [{"q": "", "expect": "manual.pdf"}],
    [{"q": "Which connector?", "expect": 3}],
    [{"q": "Which connector?", "expect": "manual.pdf", "page": 2}],
    [{"q": "Same question?", "expect": "a.pdf"}, {"q": "Same question?", "expect": "b.pdf"}],
])
def test_load_golden_set_rejects_invalid_entries(tmp_path, data):
    path = tmp_path / "golden.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        cet.load_golden_set(str(path))


def test_golden_cli_uses_custom_questions_for_shared_sources(tmp_path, monkeypatch):
    path = tmp_path / "golden.json"
    questions = [
        {"q": "Which connector?", "expect": "manual.pdf"},
        {"q": "Which capacity?", "expect": "other.pdf"},
    ]
    path.write_text(json.dumps(questions), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["compare", "--ids-file", "ids.json", "--golden", "--golden-file", str(path)])
    monkeypatch.setattr(cet.pds, "load_ids", lambda _: IDS)
    monkeypatch.setattr(cet.pds, "get_admin_key", lambda _: "key")
    monkeypatch.setattr(cet, "common_sources", lambda *_: ["manual.pdf"])
    calls = []

    def run(_ids, _key, kb, golden, knowledge_source=None):
        calls.append((kb, golden, knowledge_source))
        return {"passed": 1, "total": 1}

    monkeypatch.setattr(cet, "golden_run", run)
    cet.main()
    assert len(calls) == 2
    assert all(call[1] == questions[:1] for call in calls)
    assert [call[2] for call in calls] == ["ks-documents", "ks-documents-cu"]


def test_invalid_golden_file_fails_before_cloud_access(tmp_path, monkeypatch):
    path = tmp_path / "golden.json"
    path.write_text("{", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["compare", "--ids-file", "ids.json", "--golden", "--golden-file", str(path)])
    monkeypatch.setattr(cet.pds, "load_ids", lambda _: pytest.fail("must validate before loading credentials"))
    with pytest.raises(SystemExit) as exc:
        cet.main()
    assert exc.value.code == 2


@pytest.mark.parametrize("shared", [[], ["unrelated.pdf"]])
def test_golden_cli_refuses_uncomparable_sets(monkeypatch, shared):
    monkeypatch.setattr(sys, "argv", ["compare", "--ids-file", "ids.json", "--golden"])
    monkeypatch.setattr(cet.pds, "load_ids", lambda _: IDS)
    monkeypatch.setattr(cet.pds, "get_admin_key", lambda _: "key")
    monkeypatch.setattr(cet, "common_sources", lambda *_: shared)
    monkeypatch.setattr(cet, "golden_run", lambda *args, **kwargs: pytest.fail("must not compare different sources"))
    with pytest.raises(SystemExit, match="no source documents|No golden questions"):
        cet.main()
