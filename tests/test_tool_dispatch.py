"""A generic tool selected in the composer must reach its executor, and only it.

What the tool-calling audit found on the Pepe root (2026-09-14, Open WebUI
0.11.3, Ollama 0.33.2, hermes3:8b, UI default sampling): every native UI turn
offers the model Open WebUI's 35 builtin tools next to the one the user
selected; Ollama's 4,096-token window cannot hold that prompt and cuts it to
its second half, which removes the tool-calling instructions and the selected
tool's schema. The model then answered with nothing, or called a builtin tool
(``create_tasks``, ``create_automation``) with the user's arguments. The Hub's
fix is Open WebUI's own switch: a model record with ``builtin_tools`` off
(``open_webui_models.py``), after which the same turns executed the selected
tool 50 times out of 50.

Open WebUI cannot be imported here (it lives in the managed venv), so
:class:`Turn` is a literal model of the pinned request path, with the lines it
mirrors::

    utils/middleware.py process_chat_payload
      2769  use_builtin_tools = session and not legacy
            and model.info.meta.capabilities.builtin_tools (default True)
      2984  builtin tools injected (35 on the Pepe root with the UI defaults)
      3032  native: every tool's spec goes to the model
    llama_server.go:317  a prompt over num_ctx is cut to keep + (num_ctx - keep) / 2
            tokens, taken from its end ("truncating input prompt" limit=2050)
    utils/middleware.py native loop
      5655  parse_tool_params: JSON, then ast.literal_eval, else no parameters
      5677  unknown name -> 'Error: Tool "<name>" not found.'
      5682  arguments filtered to the spec's parameters
      5706  an exception -> {"error": str(e)}
      5733  unparsable arguments -> 'Error: Tool call arguments could not be parsed...'
      5825  results reinjected as function_call_output items, never as text
    utils/tools.py 202 coerce_kwargs: a string for an int parameter goes through int()

The model's answers are shapes captured from the root. The probe executor is
the same deterministic receipt used there: a server-side nonce the model never
sees, so a receipt in the transcript proves a real execution.
"""

from __future__ import annotations

import ast
import asyncio
import json
import secrets

import pytest
from test_image_dispatch import HERMES_SILENCE, ComfyUI, Composer, load_filter

from local_ai_stack.open_webui_filters import filter_source
from local_ai_stack.open_webui_models import chat_model_record

PROBE = "pepito_probe_tool"
PROBE_SPEC = {
    "name": PROBE,
    "description": "Repeat a short message a number of times through the Pepito probe.",
    "parameters": {
        "type": "object",
        "properties": {"message": {"type": "string"}, "count": {"type": "integer"}},
        "required": ["message", "count"],
    },
}
# Prompt sizes measured on the root, in tokens: header + probe spec + a short
# user message, and the 35 builtin schemas the UI defaults add to every turn.
HEADER_AND_PROBE_TOKENS = 350
BUILTIN_TOOLS = 35
BUILTIN_TOKENS = 4_490
OLLAMA_NUM_CTX = 4_096
OLLAMA_KEEP = 4


def call(name: str, arguments: str, call_id: str = "call-1") -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


class Probe:
    """The temporary probe tool as Open WebUI executes it: coerced, then called."""

    def __init__(self) -> None:
        self.nonce = secrets.token_hex(6)
        self.invocations: list[tuple[str, int]] = []

    async def __call__(self, message: str, count: int) -> str:
        # utils/tools.py coerce_kwargs, for the probe's (str, int) signature.
        if isinstance(count, str):
            count = int(count)
        if isinstance(message, (int, float)) and not isinstance(message, bool):
            message = str(message)
        if not isinstance(count, int) or isinstance(count, bool):
            raise TypeError("count must be an integer")
        self.invocations.append((message, count))
        return f"PEPITO_TOOL_OK:{self.nonce}:{message}:{count}"


class Turn:
    """One composer turn with the probe selected, as 0.11.3 routes it."""

    def __init__(self, probe: Probe, *, record: dict | None) -> None:
        self.probe = probe
        self.record = record
        self.output: list[dict] = []
        self.shown: list[str] = []
        self.side_effects: list[str] = []

    # -- what the model is offered ------------------------------------------------

    @property
    def use_builtin_tools(self) -> bool:
        capabilities = ((self.record or {}).get("meta") or {}).get("capabilities") or {}
        return capabilities.get("builtin_tools", True)

    @property
    def tools(self) -> dict:
        tools = {PROBE: {"callable": self.probe, "spec": PROBE_SPEC}}
        if self.use_builtin_tools:
            for name in ("create_tasks", "create_automation", "add_memory"):
                tools[name] = {
                    "callable": self.builtin(name),
                    "spec": {"parameters": {"properties": {}}},
                }
        return tools

    def builtin(self, name: str):
        async def run(**_):
            self.side_effects.append(name)
            return json.dumps({"status": "success"})

        return run

    @property
    def prompt_tokens(self) -> int:
        return HEADER_AND_PROBE_TOKENS + (BUILTIN_TOKENS if self.use_builtin_tools else 0)

    @property
    def selected_tool_reaches_the_model(self) -> bool:
        """After Ollama's cut, is the probe's schema (the first thing in the prompt) still there?"""
        if self.prompt_tokens <= OLLAMA_NUM_CTX:
            return True
        kept = OLLAMA_KEEP + (OLLAMA_NUM_CTX - OLLAMA_KEEP) // 2
        return self.prompt_tokens - kept < OLLAMA_KEEP

    # -- the native loop ----------------------------------------------------------

    @staticmethod
    def parse_tool_params(arguments: str) -> dict | None:
        if not arguments or not arguments.strip():
            return {}
        try:
            return json.loads(arguments)
        except ValueError:
            try:
                return ast.literal_eval(arguments)
            except (ValueError, SyntaxError):
                return None

    async def execute(self, tool_call: dict) -> str:
        name = tool_call["function"]["name"]
        params = self.parse_tool_params(tool_call["function"]["arguments"])
        if params is None:
            return (
                "Error: Tool call arguments could not be parsed. The model generated "
                f"malformed or incomplete JSON for `{name}`. Please try again."
            )
        tool = self.tools.get(name)
        if not tool:
            return f'Error: Tool "{name}" not found.'
        allowed = tool["spec"]["parameters"]["properties"].keys()
        params = {key: value for key, value in params.items() if key in allowed}
        try:
            result = await tool["callable"](**params)
        except Exception as exc:  # noqa: BLE001 - the pinned executor catches everything
            result = {"error": str(exc)}
        return result if isinstance(result, str) else json.dumps(result)

    async def answer(self, content: str, tool_calls: list[dict], continuation: str = "") -> None:
        """The model's first answer; tool results are reinjected and it answers again."""
        if not tool_calls:
            if content:
                self.shown.append(content)
            return
        for tool_call in tool_calls:
            self.output.append(
                {"type": "function_call", "call_id": tool_call["id"], **tool_call["function"]}
            )
            result = await self.execute(tool_call)
            self.output.append(
                {
                    "type": "function_call_output",
                    "call_id": tool_call["id"],
                    "output": [{"type": "input_text", "text": result}],
                    "status": "failed"
                    if result.startswith("Error") or '"error"' in result
                    else "completed",
                }
            )
        if continuation:
            self.shown.append(continuation)

    @property
    def reinjected(self) -> list[str]:
        return [
            part["text"]
            for item in self.output
            if item["type"] == "function_call_output"
            for part in item["output"]
        ]

    @property
    def text(self) -> str:
        return "".join(self.shown)


def run(coroutine):
    return asyncio.run(coroutine)


@pytest.fixture
def probe() -> Probe:
    return Probe()


@pytest.fixture
def turn(probe: Probe) -> Turn:
    return Turn(probe, record=chat_model_record("hermes3:8b"))


# --- the root cause, and what the record changes ----------------------------------------


def test_without_the_record_the_builtin_tools_push_the_selected_tool_out_of_the_prompt(probe):
    default = Turn(probe, record=None)
    assert default.use_builtin_tools and len(default.tools) > 1
    assert default.prompt_tokens > OLLAMA_NUM_CTX
    assert not default.selected_tool_reaches_the_model


def test_with_the_record_the_model_is_offered_exactly_the_selected_tool(turn):
    assert not turn.use_builtin_tools
    assert list(turn.tools) == [PROBE]
    assert turn.prompt_tokens < OLLAMA_NUM_CTX
    assert turn.selected_tool_reaches_the_model


# --- A-K ----------------------------------------------------------------------------------


def test_a_structured_call_executes_the_selected_tool(turn, probe):
    run(turn.answer("", [call(PROBE, '{"message": "banana", "count": 3}')]))
    assert probe.invocations == [("banana", 3)]


def test_b_the_arguments_reach_the_executor_as_typed(turn, probe):
    run(turn.answer("", [call(PROBE, '{"count": 2, "message": "hello"}')]))
    assert probe.invocations == [("hello", 2)]
    assert isinstance(probe.invocations[0][1], int)


def test_c_the_real_result_is_reinjected_for_the_next_model_call(turn, probe):
    run(
        turn.answer(
            "",
            [call(PROBE, '{"message": "kiwi", "count": 5}')],
            continuation="Here is the receipt: PEPITO_TOOL_OK:{nonce}:kiwi:5",
        )
    )
    assert turn.reinjected == [f"PEPITO_TOOL_OK:{probe.nonce}:kiwi:5"]
    assert turn.output[-1]["status"] == "completed"


def test_d_a_textual_tool_call_is_not_a_silent_success(turn, probe):
    # Captured raw from Ollama: the wrapper is there, the name is cut short.
    textual = '<tool_call>\n{"arguments": {"message": "banana", "count": 3}, "name": "pepito_probe"}\n</tool_call>'
    run(turn.answer(textual, []))
    assert probe.invocations == []
    assert "PEPITO_TOOL_OK" not in turn.text


def test_e_an_unknown_tool_is_a_controlled_error_and_no_builtin_runs(turn, probe):
    # Captured on the root without the record: the user's arguments on a builtin's name.
    run(turn.answer("", [call("create_tasks", '{"count": 3, "content": "Good morning"}')]))
    assert probe.invocations == [] and turn.side_effects == []
    assert turn.reinjected == ['Error: Tool "create_tasks" not found.']
    assert turn.output[-1]["status"] == "failed"


def test_f_malformed_json_arguments_are_a_controlled_error(turn, probe):
    run(turn.answer("", [call(PROBE, '{"message": "banana", "count": 3')]))
    assert probe.invocations == []
    assert turn.reinjected[0].startswith("Error: Tool call arguments could not be parsed")
    assert "Traceback" not in turn.reinjected[0]


def test_g_a_wrong_argument_type_is_a_controlled_error_not_a_default(turn, probe):
    run(turn.answer("", [call(PROBE, '{"message": "banana", "count": "three"}')]))
    assert probe.invocations == []
    assert json.loads(turn.reinjected[0]) == {
        "error": "invalid literal for int() with base 10: 'three'"
    }
    # A numeric string is coerced by the pinned executor and does execute.
    run(turn.answer("", [call(PROBE, '{"message": "banana", "count": "3"}', "call-2")]))
    assert probe.invocations == [("banana", 3)]


def test_h_plain_chat_makes_zero_calls(turn, probe):
    run(turn.answer("Hello! How can I assist you today?", []))
    assert probe.invocations == [] and turn.output == []
    assert turn.text == "Hello! How can I assist you today?"


def test_i_two_sequential_calls_are_two_executions_with_two_receipts(probe):
    first, second = Turn(probe, record=chat_model_record("hermes3:8b")), None
    run(first.answer("", [call(PROBE, '{"message": "alpha", "count": 1}')]))
    second = Turn(probe, record=chat_model_record("hermes3:8b"))
    run(second.answer("", [call(PROBE, '{"message": "beta", "count": 2}', "call-2")]))
    assert probe.invocations == [("alpha", 1), ("beta", 2)]
    assert first.reinjected != second.reinjected


def test_j_the_raw_payload_is_never_the_text_the_user_reads(turn, probe):
    run(
        turn.answer(
            "",
            [call(PROBE, '{"message": "banana", "count": 3}')],
            continuation="The probe repeated 'banana' 3 times.",
        )
    )
    assert turn.text == "The probe repeated 'banana' 3 times."
    assert "function_call" not in turn.text and "<tool_call>" not in turn.text
    assert all(item["type"] != "message" for item in turn.output)


def test_k_an_image_turn_still_becomes_a_comfyui_job_with_the_record_in_place():
    capabilities = chat_model_record("hermes3:8b")["meta"]["capabilities"]
    assert capabilities["image_generation"] is True
    comfyui = ComfyUI()
    composer = Composer(load_filter(filter_source()), comfyui, HERMES_SILENCE)
    run(composer.turn("Generate an image of a red apple on a plain white background."))
    assert len(comfyui.jobs) == 1
