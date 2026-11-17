"""What a model's stack adds to the router: its poses and its own rules.

The filter has no poses and no extra rules of its own. ``stack["pose"]["adapters"]`` names the
adapters, ``stack["pose"]["prompts"]`` the sentence each one was trained on, and
``stack["router_rules"]`` is a list of sentences appended to the router's rules. A pose is
offered to the router only when it has both its adapter and its sentence.
"""

from __future__ import annotations

from test_open_webui_image_filter import load_module

STACK = {
    "pose": {
        "adapters": {"sit": "pose_sit.safetensors", "lean": "pose_lean.safetensors"},
        "prompts": {
            "sit": "change the woman pose to sitting on a chair.",
            "lean": "change the woman pose to leaning on a wall.",
        },
    },
    "router_rules": ["Pose meanings: sit = on a chair, lean = against a wall."],
}


def test_a_stack_without_poses_or_rules_offers_none():
    module = load_module()
    caps = module["router_capabilities"]({})
    assert caps.poses == () and caps.rules == ()
    assert "pose" not in module["router_schema"](False, caps)["properties"]
    system = module["router_system"](caps)
    assert system == module["router_system"]()
    assert "{names}" not in system and "Pose meanings" not in system


def test_a_pose_is_offered_only_with_its_adapter_and_its_sentence():
    module = load_module()
    stack = {
        "pose": {
            "adapters": {"sit": "a.safetensors", "lean": "b.safetensors", "wave": "c.safetensors"},
            "prompts": {"sit": "sit.", "lean": "lean.", "jump": "jump."},
        }
    }
    caps = module["router_capabilities"](stack)
    assert caps.poses == ("lean", "sit"), "no sentence for wave, no adapter for jump"
    schema = module["router_schema"](False, caps)
    assert schema["properties"]["pose"]["enum"] == ["", "lean", "sit"]
    system = module["router_system"](caps)
    assert '"lean", "sit"' in system
    assert (
        module["router_capabilities"]({"pose": {"adapters": {"sit": "a.safetensors"}}}).poses == ()
    )


def test_the_stack_rules_come_last_in_the_order_given():
    module = load_module()
    stack = {**STACK, "router_rules": ["First extra rule.", "", "  ", "Second extra rule."]}
    caps = module["router_capabilities"](stack, references=True)
    assert caps.rules == ("First extra rule.", "Second extra rule.")
    system = module["router_system"](caps)
    assert system.endswith("First extra rule.\nSecond extra rule.")
    assert system.index(module["ROUTER_PLAN_RULES"]) < system.index("First extra rule.")
    assert system.index(module["ROUTER_REFS_RULES"]) < system.index("First extra rule.")


def test_the_pose_rules_are_neutral_and_the_meanings_are_the_stacks():
    module = load_module()
    caps = module["router_capabilities"](STACK)
    system = module["router_system"](caps)
    assert "POSE_PROMPTS" not in module
    assert "Pose meanings: sit = on a chair, lean = against a wall." in system
    assert "explicit" not in module["ROUTER_POSE_RULES"]
