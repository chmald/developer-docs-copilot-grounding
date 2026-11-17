"""Hybrid figure extraction, explicit model configuration and source provenance."""
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import hybrid_ingest as hybrid
import post_deploy_search as pds

IDS = {
    "searchEndpoint": "https://example.search.windows.net",
    "foundryOpenAIEndpoint": "https://example.openai.azure.com",
    "foundryResource": "example",
    "visionDeployment": "my-deployed-vision",
    "chatDeployment": "planner", "chatModel": "actual-planner-model",
}


def test_models_require_real_explicit_deployment_and_model_pairs():
    with pytest.raises(ValueError, match="Configure"):
        pds.query_model({})
    with pytest.raises(ValueError, match="queryModel"):
        pds.query_model({**IDS, "queryDeployment": "missing-model-name"})
    with pytest.raises(ValueError, match="visionDeployment"):
        hybrid._vision_skill({})
    assert hybrid._frontier_model(IDS) == ("planner", "actual-planner-model")
    assert pds.query_model({**IDS, "queryDeployment": "custom", "queryModel": "custom-model"}) == (
        "custom", "custom-model",
    )


def test_figure_prompt_contains_grounding_schema_uncertainty_and_safe_scope():
    skill = hybrid._vision_skill(IDS)
    inputs = {item["name"]: item["source"] for item in skill["inputs"]}
    assert "/my-deployed-vision/" in skill["uri"]
    prompt = inputs["userMessage"]
    for requirement in ("visible", "labels", "junction", "confidence", "uncertainties",
                        "relationships", "page numbers"):
        assert requirement in prompt
    assert "circuit validation" in inputs["systemMessage"]
    assert "safety procedures" in inputs["systemMessage"]
    assert skill["responseFormat"] == {"type": "text"}
    assert skill["extraParameters"]["max_completion_tokens"] == 1200
    assert "reasoning_effort" not in skill["extraParameters"]


def test_figure_config_can_retarget_without_weakening_fixed_system_scope():
    skill = hybrid._vision_skill({**IDS, "corpus": {
        "figureDescriptionPrompt": "Transcribe the specimen's visible labels.",
        "figureImageDetail": "low", "figureOutputFormat": "text",
        "figureSystemPrompt": "Use concise descriptions.",
    }, "visionMaxTokens": 600, "visionReasoningEffort": "low"})
    inputs = {item["name"]: item["source"] for item in skill["inputs"]}
    assert "specimen''s" in inputs["userMessage"]
    assert "bit field" not in inputs["userMessage"]
    assert inputs["imageDetail"] == "='low'"
    assert "circuit validation" in inputs["systemMessage"]
    assert "Use concise descriptions." in inputs["systemMessage"]
    assert skill["extraParameters"]["reasoning_effort"] == "low"
    assert skill["extraParameters"]["max_completion_tokens"] == 600


@patch("hybrid_ingest._req")
def test_cu_skillset_uses_the_supported_region_endpoint(request):
    request.return_value = MagicMock(status_code=201)
    endpoint = "https://cu-supported.services.ai.azure.com"
    hybrid.create_cu_skillset({**IDS, "contentUnderstandingSubdomainUrl": endpoint}, "key")
    body = request.call_args.args[4]
    assert body["cognitiveServices"]["subdomainUrl"] == endpoint


@pytest.mark.parametrize("key,value", [("figureImageDetail", "invalid"), ("figureOutputFormat", "xml")])
def test_bad_figure_options_rejected(key, value):
    with pytest.raises(ValueError):
        hybrid._vision_skill({**IDS, "corpus": {key: value}})


@patch("hybrid_ingest._req")
def test_each_tier_projects_real_figure_page_metadata(request):
    request.return_value = MagicMock(status_code=201)
    for build, page_field in (
        (hybrid.create_cu_skillset, "pageNumberFrom"),
        (hybrid.create_di_skillset, "pageNumber"),
    ):
        build(IDS, "key")
        body = request.call_args.args[4]
        image_projection = next(item for item in body["indexProjections"]["selectors"]
                                if item["sourceContext"] == "/document/normalized_images/*")
        mappings = {item["name"]: item["source"] for item in image_projection["mappings"]}
        assert mappings["pageNumberFrom"].endswith("/locationMetadata/" + page_field)
        assert mappings["content"].endswith("/imageDescription")
        assert "extractionTier" in mappings and "contentKind" in mappings
        if build == hybrid.create_di_skillset:
            layout = next(item for item in body["skills"] if item["name"] == "layout-figures")
            assert layout["outputFormat"] == "text"
            assert layout["extractionOptions"] == ["images", "locationMetadata"]
        else:
            assert mappings["pageNumberTo"].endswith("/locationMetadata/pageNumberTo")


def test_cu_can_be_disabled_before_routing(tmp_path):
    (tmp_path / "small.pdf").touch()
    with patch.object(hybrid, "page_count", return_value=2):
        assert hybrid.plan(str(tmp_path))[0]["tier"] == hybrid.TIER_CU
        row = hybrid.plan(str(tmp_path), content_understanding_enabled=False)[0]
    assert row["tier"] == hybrid.TIER_DI
    assert "disabled" in row["reason"]


@patch("hybrid_ingest._req")
def test_di_only_build_does_not_create_cu_indexer(request):
    request.return_value = MagicMock(status_code=201)
    hybrid.create_datasources_and_indexers(
        {**IDS, "hybridContentUnderstandingEnabled": False,
         "subscriptionId": "test", "resourceGroup": "test", "storageAccount": "test"}, "key",
    )
    paths = [call.args[1] for call in request.call_args_list]
    assert "/indexers('ixr-hybrid-di')" in paths
    assert not any("-cu" in path for path in paths)
