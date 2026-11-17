"""Edit fidelity (Krea's ref_boost) and "Buscar referências" for new pictures."""

from __future__ import annotations

import pytest
from test_image_edit_dispatch import (
    capella_stacks,  # noqa: F401
    edit_config,  # noqa: F401
    new,
    preset,  # noqa: F401
    routed,  # noqa: F401
    settings,  # noqa: F401
    world,  # noqa: F401
)
from test_open_webui_image_filter import load_module

REFS = ("pepito_image_refs",)


def namespace(turn):
    return turn.filter.inlet.__func__.__globals__


def ref_boosts(comfyui) -> list:
    return [
        node["inputs"]["ref_boost"]
        for job in comfyui.jobs
        for node in job.values()
        if node.get("class_type") == "Krea2EditModelPatch"
    ]


# --- the rule ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "variations", "expected"),
    [
        ("subtle", 1, [4.0]),
        ("moderate", 1, [2.5]),
        ("strong", 1, [1.5]),
        # Two variations: one freer and one more faithful, inside the automatic range.
        ("strong", 2, [1.0, 2.25]),
        ("subtle", 2, [3.25, 4.75]),
    ],
)
def test_automatic_fidelity_follows_the_change_and_spreads_variations(change, variations, expected):
    assert load_module()["edit_strengths"](change, variations) == (expected, "auto")


def test_the_learned_bias_moves_the_automatic_value_within_its_range():
    strengths = load_module()["edit_strengths"]
    assert strengths("moderate", 1, bias=-0.75) == ([1.75], "auto")
    assert strengths("subtle", 1, bias=1.5) == ([5.0], "auto")
    assert strengths("strong", 1, bias=-1.5) == ([1.0], "auto")


def test_the_valves_win_over_the_router_the_bias_and_a_correction():
    strengths = load_module()["edit_strengths"]
    assert strengths("strong", 2, {"ref_boost_manual": 3.0}) == ([3.0, 3.0], "manual")
    assert strengths("strong", 1, {"ref_boost_manual": 9.0}) == ([5.0], "manual")
    assert strengths("strong", 1, {"fidelidade_edicao": "fiel"}, -1.0, retry_from=2.0) == (
        [4.0],
        "manual",
    )
    assert strengths("strong", 1, {"fidelidade_edicao": "outra"}) == ([1.5], "auto")


@pytest.mark.parametrize(
    ("previous", "variations", "expected"),
    [(2.5, 1, [1.5]), (1.5, 1, [1.0]), (3.0, 2, [1.25, 2.75])],
)
def test_a_correction_is_one_step_freer_than_the_last_try(previous, variations, expected):
    result = load_module()["edit_strengths"]("subtle", variations, retry_from=previous)
    assert result == (expected, "correction")


def ledger():
    import sys

    return sys.modules["open_webui.pepito_calibration"]


# --- "Buscar referências" -------------------------------------------------------------


UNIFORM = "faz um homem com a farda soviética dos anos 80 na neve"


def uniform_answer(**overrides) -> dict:
    return {
        **new("A photo of a man in his thirties in a 1980s Soviet army uniform in falling snow."),
        "ref_query": "Soviet army uniform 1980s",
        "ref_role": "C",
        **overrides,
    }


@pytest.mark.parametrize(
    ("query", "kept"),
    [
        ("Soviet army uniform 1980s", "Soviet army uniform 1980s"),
        ("Eiffel Tower at night", "Eiffel Tower at night"),
        ("woman in Soviet uniform", ""),
        ("celebrity red carpet dress", ""),
        ("nude beach", ""),
    ],
)
def test_a_reference_search_never_carries_a_person(query, kept):
    decision = load_module()["ref_decision"]({"ref_query": query, "ref_role": "B"})
    assert decision == {"ref_query": kept, "ref_role": "C"}
