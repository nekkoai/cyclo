import json
from types import SimpleNamespace

import pytest

from cyclo.component import component_sources_root
from cyclo.errors import CycloError
from cyclo.pi_runtime import inference_format, model_incompatibility
from cyclo.project_run import validate_pi_team_models


MODEL_ID_CASES = json.loads(
    (
        component_sources_root()
        / "protocol"
        / "provider"
        / "test"
        / "model-id-cases.json"
    ).read_text(encoding="utf-8")
)


def model(identifier: str = "provider/model") -> dict[str, object]:
    return {
        "id": identifier,
        "displayName": "Model",
        "capabilities": {
            "inputModalities": ["MODALITY_TEXT", "MODALITY_IMAGE"],
            "outputModalities": ["MODALITY_TEXT"],
        },
        "contextWindowTokens": "128000",
        "maxOutputTokens": "4096",
        "inferenceFormat": inference_format(),
    }


def test_current_provider_model_is_usable_by_the_pi_team_runtime() -> None:
    assert model_incompatibility(model()) == ""


@pytest.mark.parametrize(
    "case",
    MODEL_ID_CASES,
    ids=[case["name"] for case in MODEL_ID_CASES],
)
def test_pi_route_id_semantics_match_the_node_adapter(
    case: dict[str, object],
) -> None:
    identifier = (
        str(case["prefix"])
        + str(case["unit"]) * int(case["repeat"])
        + str(case["suffix"])
    )
    incompatibility = model_incompatibility(model(identifier))

    assert (incompatibility == "") is bool(case["valid"])
    if not case["valid"]:
        assert incompatibility == "invalid provider/model route"


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"inferenceFormat": "future-runtime@1"}, "inference format"),
        ({"maxOutputTokens": "0"}, "output-token limit"),
        ({"maxOutputTokens": "9" * 10_000}, "output-token limit"),
        ({"extensions": [{"typeUrl": "future"}]}, "extensions"),
        (
            {
                "capabilities": {
                    "inputModalities": [{}],
                    "outputModalities": ["MODALITY_TEXT"],
                }
            },
            "input modalities",
        ),
    ],
)
def test_pi_runtime_rejects_incompatible_typed_metadata(
    change: dict[str, object],
    reason: str,
) -> None:
    assert reason in model_incompatibility({**model(), **change})


def test_team_preflight_rejects_a_model_for_another_runtime() -> None:
    team = SimpleNamespace(
        agents=(
            SimpleNamespace(
                name="planner",
                engine="pi",
                model="future/model",
            ),
        )
    )
    catalogue = {
        "models": [
            {
                "id": "future/model",
                "inferenceFormat": "future-runtime@1",
            }
        ]
    }

    with pytest.raises(CycloError, match="incompatible with the pi runtime"):
        validate_pi_team_models(team, catalogue)  # type: ignore[arg-type]
