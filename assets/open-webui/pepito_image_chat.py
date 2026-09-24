"""
title: Pepito Image Chat
author: Pepito's AI Hub
version: 0.5.1
required_open_webui_version: 0.11.3
description: Send every Image turn through Open WebUI's own image generation or editing, and decide which from the user's words.
"""

# This file is uploaded verbatim as an Open WebUI Filter Function. Keep it
# self-contained: the managed Open WebUI venv does not import the Hub package.
#
# Why this exists. With the Image feature on, Open WebUI 0.11.3 has two ways
# to make a picture:
#
# * "native" function calling (the UI's default): the builtin generate_image
#   tool is offered to the model, and the picture is made only if the model
#   answers with a structurally correct tool call. A small local model gets
#   this wrong -- it invents argument names, writes the call as prose, or says
#   nothing -- and then nothing reaches ComfyUI at all;
# * "legacy" function calling: Open WebUI itself turns the user's message
#   into an image prompt, submits the configured ComfyUI workflow, attaches
#   the result to the message, and only then asks the model to acknowledge.
#
# The second path does not depend on the model at all, so the Hub routes
# every Image turn through it. This filter runs at ``inlet``, before Open
# WebUI decides which path to take, and flips that one request's function
# calling mode. Ordinary chat turns, API calls without a UI session, and
# every other feature are left exactly as they were.
#
# The same legacy path edits instead of generating when the conversation
# already holds images and Open WebUI's image editing is enabled: it reads
# those images from the chat's own history (the message ``files``), uploads
# them to ComfyUI and runs the edit workflow. The model is asked afterwards
# only to acknowledge, and it is a text model: Open WebUI also copies every
# attached image into the request as ``image_url`` parts, which a model
# without vision rejects ("Multimodal data provided, but model does not
# support multimodal requests") -- the edit is made and the chat shows raw
# JSON instead of a reply. On an Image turn the model has nothing to look at,
# so those parts are removed from what it is sent; the ``files`` the image
# path reads are left exactly as they are.
#
# The model's acknowledgement is then the only text on the turn. It cannot
# add anything to the picture -- the picture is already attached -- but it can
# invent a link to it (a markdown image, or an HTML <img> tag), which the
# chat renders as a broken image or as stray markup. So the text
# of an Image turn is kept free of links and markdown images, and a turn that
# would otherwise say nothing says that the image was generated.
#
# One more thing the pinned path gets wrong: when ComfyUI runs the workflow
# and fails (an input it cannot decode, a node error, no memory), Open WebUI
# receives no picture, attaches nothing, and still tells the model "the
# requested image ... is now being shown to the user". The model then
# announces a picture that is not there. The ``request`` hook runs after
# Open WebUI's image step and before the model is asked; when that success
# context is present but the message holds no image, the context is replaced
# by a failure, the status says so, and the turn's text is the failure itself.
#
# And the decision the pinned handler does not make: with editing enabled, a
# chat that already holds an image edits it on *every* Image turn -- "Now an
# image of a necromancer" after a tank puts a necromancer into the tank. The
# handler reads the saved chat's history for that, not the request, so no
# body or metadata edit hides those images from it. So the filter decides
# first, from the user's words (``edit_intent``, biased towards a new image:
# an unwanted fresh picture is recoverable, a silently altered old one is
# not). An edit is left to Open WebUI exactly as before. A new image while the
# chat holds one is taken over: the handler is skipped for the turn
# (``features.image_generation`` off) and the ``request`` hook makes the
# picture through Open WebUI's own ``image_generations`` -- the same call the
# handler makes, with the same statuses, the same file attachment and the same
# context -- from the current message alone, so no earlier image is an input.
from __future__ import annotations

import re

FEATURE = "image_generation"
FORCED_MODE = "legacy"
STATE_KEY = "pepito_image_chat"
GENERATED = "Image generated."
NOT_CREATED = "The image could not be created: the image engine finished without a picture."
NOT_CREATED_STATUS = "The image could not be created"
NOT_CREATED_CONTEXT = (
    "<context>An image was requested, but the image engine finished without returning "
    "a picture, so nothing is shown. Tell the user that the image could not be created "
    "and that they can try again.</context>"
)
# Open WebUI 0.11.3 chat_image_generation_handler, both success contexts.
SUCCESS_MARK = "is now being shown to the user"
# The handler's own words for a generated picture, and for a failed attempt.
GENERATED_CONTEXT = (
    "<context>The requested image has been created by the system successfully and is now "
    "being shown to the user. Let the user know that the image they requested has been "
    "generated and is now shown in the chat.</context>"
)
FAILED_CONTEXT = (
    "<context>Image generation was attempted but failed because of an error. The system is "
    "currently unable to generate the image. Tell the user that the following error occurred: "
    "{error}</context>"
)
DISABLED_STATUS = "Image generation is disabled"
DISABLED_CONTEXT = (
    "<context>Image generation was requested but the feature is currently disabled by the "
    "administrator, so no image was created. Let the user know that image generation is "
    "currently unavailable.</context>"
)
# What the model is told once a picture really is attached to its reply. Open
# WebUI's own sentence ("...is now being shown to the user. Let the user know
# ...") is enough for an 8B model; a 3B one answers it with "I cannot create
# images" under the very picture it was asked about (measured on the 8 GB
# profile: hermes3:3b, llama3.2:3b, qwen3:4b; docs/testing/profile-8gb.md).
# Said this way, the same models acknowledge the picture. The success mark
# stays inside so nothing downstream reads it differently.
SHOWN_CONTEXT = (
    "<context>The requested image has been {verb} by the system successfully and is now "
    "being shown to the user: it is attached to this reply, in the chat, where the user can "
    "see it. You do not see the picture yourself and you do not need to. Tell the user "
    "briefly that the image they requested has been {verb} and is now shown in the chat. "
    "Do not say that you cannot create or display images, and do not describe the picture "
    "as if you had drawn it.</context>"
)
MAX_TEXT = 65536
# open_webui/utils/chat_id.py: the chats Open WebUI does not persist, whose
# messages therefore travel in the request.
NOT_SAVED_CHAT_PREFIXES = ("temporary:", "local:", "channel:")

_SUCCESS_CONTEXT = re.compile(r"<context>[^<]*" + re.escape(SUCCESS_MARK) + r"[^<]*</context>")

# --- edit or new image ------------------------------------------------------------
#
# Words that only make sense about a picture already in the chat, and words
# that announce a new one; Portuguese and English. The order of the rules is
# the policy: an explicit reference to the earlier picture, then a request
# that opens with an editing verb, then a request phrased as a new picture,
# then an editing verb anywhere, then a verb that only edits when it points
# at something ("make it blue", "faça o fundo preto"). Anything undecided is
# a new picture.
_W = r"(?<![\w])(?:{})(?![\w])"
_THE_IMAGE = re.compile(
    _W.format(
        r"(?:this|that|the (?:previous|last|current|above|same)|previous|last|current|above|same)"
        r" (?:image|picture|photo|drawing|render|pic|one)"
        r"|(?:nessa|nesta|essa|esta|dessa|desta) (?:imagem|foto|figura|ilustra[cç][aã]o)"
        r"|(?:imagem|foto|figura) (?:anterior|acima|atual|de cima)"
    ),
    re.IGNORECASE,
)
_NEW_IMAGE = re.compile(
    _W.format(
        r"(?:an?|another|one more|new) (?:\w+ ){0,2}?"
        r"(?:image|picture|photo|drawing|illustration|render|painting|portrait|sketch)"
        r"|(?:image|picture|photo|drawing|illustration|render|painting|sketch) of"
        r"|(?:uma?|outra?|mais uma?|nova?) (?:\w+ ){0,2}?"
        r"(?:imagem|foto|desenho|ilustra[cç][aã]o|pintura|retrato|figura)"
        r"|(?:imagem|foto|desenho|ilustra[cç][aã]o|pintura|retrato) de"
    ),
    re.IGNORECASE,
)
_EDIT_VERB = re.compile(
    _W.format(
        r"change|edit|modify|alter|adjust|remove|erase|delete|replace|swap|recolou?r|repaint|"
        r"keep|retain|crop|resize|rotate|flip|blur|brighten|darken|add|insert|put|place|"
        r"mude|muda|mudar|altere|altera|alterar|edite|edita|editar|modifique|modifica|"
        r"remova|remove|remover|tire|tira|tirar|apague|apaga|troque|troca|trocar|substitua|"
        r"substitui|mantenha|mant[eé]m|manter|ajuste|ajusta|recorte|corte|adicione|adiciona|"
        r"coloque|coloca|ponha|p[oõ]e|insira|insere|bote|bota|deixe|deixa|transforme|transforma"
    ),
    re.IGNORECASE,
)
# Verbs that edit only when they point at something already there. "a" is an
# English article and a Portuguese pointer, so each language keeps its own.
_POINTING = (
    (
        re.compile(_W.format(r"make|turn|give|paint|colou?r|set|fill|leave"), re.IGNORECASE),
        re.compile(_W.format(r"it|its|this|that|these|those|them|the"), re.IGNORECASE),
    ),
    (
        re.compile(_W.format(r"fa[cç]a|faz|fazer|pinte|pinta|pintar|d[eê]"), re.IGNORECASE),
        re.compile(
            _W.format(
                r"ele|ela|eles|elas|isso|isto|aquilo|nele|nela|neles|nelas|nisso|nisto|"
                r"o|a|os|as|no|na|nos|nas|do|da|dos|das|ao|à|esse|essa|este|esta|desse|dessa"
            ),
            re.IGNORECASE,
        ),
    ),
)
# Someone already in the picture: "make her blonde", "turn him into a knight".
# Only right after the verb -- elsewhere "her" is a possessive in a new
# picture's description ("make a girl hugging her cat").
_POINTED_PERSON = re.compile(
    _W.format(r"(?:make|turn|give|paint|colou?r|set|fill|leave) (?:her|him)"), re.IGNORECASE
)
_LEADING_FILLER = re.compile(
    r"^(?:(?:now|then|next|ok|okay|please|and|also|so|well|hmm|agora|ent[aã]o|depois|"
    r"por favor|e|tamb[eé]m|bem)[,.!:]?\s+)+",
    re.IGNORECASE,
)
_FIRST_WORD = re.compile(r"^\W*([\w\-]+)")


def edit_intent(text: str) -> bool:
    """Whether the user's words ask to change the picture already in the chat.

    Only positive evidence says edit; everything else is a new picture.
    """
    words = _LEADING_FILLER.sub("", (text or "").strip())
    if not words:
        return False
    if _THE_IMAGE.search(words):
        return True
    first = _FIRST_WORD.match(words)
    if first and _EDIT_VERB.fullmatch(first.group(1)):
        return True
    if _NEW_IMAGE.search(words):
        return False
    if _EDIT_VERB.search(words) or _POINTED_PERSON.search(words):
        return True
    return any(verb.search(words) and pointer.search(words) for verb, pointer in _POINTING)


# The model's acknowledgement, without any picture it made up.
_HTML_IMAGE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_MARKDOWN_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MARKDOWN_LINK = re.compile(r"\[([^\]]*)\]\((?:https?:|data:|file:|/)[^)]*\)")
_BARE_URL = re.compile(r"(?:https?://|www\.)\S+")
_BLANK_LINES = re.compile(r"\n{3,}")


def text_only(messages: list) -> None:
    """Drop the image parts of every message; keep its words and its files."""
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        parts = [
            part
            for part in content
            if not (isinstance(part, dict) and part.get("type") == "image_url")
        ]
        if len(parts) == len(content):
            continue
        if all(isinstance(part, dict) and part.get("type") == "text" for part in parts):
            message["content"] = "\n".join(str(part.get("text") or "") for part in parts)
        else:
            message["content"] = parts


def clean_acknowledgement(text: str) -> str:
    """The model's words about the picture, without any picture it made up."""
    cleaned = _HTML_IMAGE.sub("", text)
    cleaned = _MARKDOWN_IMAGE.sub("", cleaned)
    cleaned = _MARKDOWN_LINK.sub(r"\1", cleaned)
    cleaned = _BARE_URL.sub("", cleaned)
    cleaned = _BLANK_LINES.sub("\n\n", cleaned).strip()
    return cleaned or GENERATED


async def attached_images(metadata: dict) -> list | None:
    """The images Open WebUI attached to this turn's message, or None when unknowable.

    Only a saved chat keeps them; a temporary chat, or any lookup that fails,
    gives no answer rather than a guess.
    """
    chat_id, message_id = metadata.get("chat_id"), metadata.get("message_id")
    if not (isinstance(chat_id, str) and chat_id and message_id):
        return None
    try:
        from open_webui.models.chats import Chats
        from open_webui.utils.chat_id import is_saved_chat_id

        if not is_saved_chat_id(chat_id):
            return None
        files = await Chats.get_message_metadata(chat_id, message_id, "files")
    except Exception:  # noqa: BLE001 - no answer is better than breaking the turn
        return None
    return [
        item
        for item in files or []
        if isinstance(item, dict)
        and (
            item.get("type") == "image" or str(item.get("content_type") or "").startswith("image/")
        )
    ]


def shown_context(claimed: str) -> str:
    """Open WebUI's success context, said so that a small model believes it.

    Keeps what the claim said happened: an edit stays an edit, anything else
    was created.
    """
    return SHOWN_CONTEXT.format(verb="edited" if "edited" in claimed else "created")


def claimed_success(messages: list) -> dict | None:
    """The system message in which Open WebUI said a picture is being shown, if any."""
    for message in messages:
        if (
            isinstance(message, dict)
            and message.get("role") == "system"
            and isinstance(message.get("content"), str)
            and _SUCCESS_CONTEXT.search(message["content"])
        ):
            return message
    return None


def last_user_text(messages: list) -> str:
    """The words of the last user message, whatever shape Open WebUI gave it."""
    for message in reversed(messages):
        if not (isinstance(message, dict) and message.get("role") == "user"):
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
        return ""
    return ""


def images_in_messages(messages: list) -> bool:
    """Open WebUI's get_images_from_messages (utils/middleware.py 1673): any message
    whose ``files`` hold an image, by type or by content type."""
    for message in messages:
        if not isinstance(message, dict):
            continue
        for item in message.get("files") or []:
            if isinstance(item, dict) and (
                item.get("type") == "image"
                or str(item.get("content_type") or "").startswith("image/")
            ):
                return True
    return False


async def chat_holds_images(metadata: dict, user_id: str | None, messages: list) -> bool:
    """Whether Open WebUI's image handler would find an image to edit on this turn.

    The handler (utils/middleware.py chat_image_generation_handler) has two
    sources, and this asks the same one it would: a saved chat's history up to
    the current message, read from the database; a temporary or channel chat's
    messages as sent in the request. A saved chat that cannot be read -> False,
    which leaves the turn to Open WebUI.
    """
    chat_id = metadata.get("chat_id")
    if not (isinstance(chat_id, str) and chat_id):
        return False
    if chat_id.startswith(NOT_SAVED_CHAT_PREFIXES):
        return images_in_messages(messages)
    if not user_id:
        return False
    try:
        from open_webui.models.chats import Chats
        from open_webui.utils.middleware import get_images_from_messages
        from open_webui.utils.misc import get_message_list

        chat = await Chats.get_chat_by_id_and_user_id(chat_id, user_id)
        history = (chat.chat if chat else {}).get("history") or {}
        chain = get_message_list(history.get("messages") or {}, history.get("currentId"))
        return bool(get_images_from_messages(chain or []))
    except Exception:  # noqa: BLE001 - no answer is better than breaking the turn
        return False


def with_system_context(messages: list, content: str) -> list:
    """Open WebUI's add_or_update_system_message: one system message, first."""
    if messages and isinstance(messages[0], dict) and messages[0].get("role") == "system":
        existing = messages[0].get("content")
        messages[0]["content"] = (
            f"{content}\n\n{existing}" if isinstance(existing, str) else content
        )
        return messages
    return [{"role": "system", "content": content}, *messages]


async def generate_new_image(request, prompt: str, metadata: dict, user, emit) -> str | None:
    """Make a picture from ``prompt`` alone through Open WebUI's own generation.

    The same calls as the pinned handler's generation branch, in the same
    order: the task model may refine the prompt (from this message only, so
    no earlier picture leaks in through the words either), image_generations
    submits the configured text-to-image workflow and attaches the result to
    this message, and the statuses and the context the model is given are the
    handler's own. Returns that context.
    """
    from open_webui.models.config import Config
    from open_webui.routers.images import CreateImageForm, image_generations
    from open_webui.utils.access_control import has_permission

    # The gates the handler and the /images routes apply, in the same order:
    # the user's permission (an administrator always passes) and the
    # administrator's global switch. The internal image_generations checks
    # neither, so they are checked here, before anything reaches ComfyUI.
    if getattr(user, "role", None) != "admin" and not await has_permission(
        getattr(user, "id", ""), "features.image_generation", await Config.get("user.permissions")
    ):
        return None
    if not await Config.get("image_generation.enable"):
        await emit({"type": "status", "data": {"description": DISABLED_STATUS, "done": True}})
        return DISABLED_CONTEXT
    image_metadata = {"chat_id": metadata.get("chat_id"), "message_id": metadata.get("message_id")}
    await emit({"type": "status", "data": {"description": "Creating image", "done": False}})
    refined = prompt
    try:
        if await Config.get("image_generation.prompt.enable"):
            from open_webui.routers.tasks import generate_image_prompt
            from open_webui.utils.json_codec import JSONCodec

            answer = await generate_image_prompt(
                request,
                {
                    "model": metadata.get("model_id"),
                    "messages": [{"role": "user", "content": prompt}],
                    "chat_id": metadata.get("chat_id"),
                },
                user,
            )
            text = answer["choices"][0]["message"]["content"]
            candidate = JSONCodec.loads(text[text.rfind("{") : text.rfind("}") + 1]).get("prompt")
            if isinstance(candidate, str) and candidate.strip():
                refined = candidate
    except Exception:  # noqa: BLE001 - the handler falls back to the user's words too
        refined = prompt
    try:
        images = await image_generations(
            request=request,
            form_data=CreateImageForm(prompt=refined),
            metadata=image_metadata,
            user=user,
        )
    except Exception as exc:  # noqa: BLE001 - reported to the user, like the handler
        detail = getattr(exc, "detail", None)
        error = (
            detail.get("message", str(detail)) if isinstance(detail, dict) else str(detail or exc)
        )
        await emit(
            {
                "type": "status",
                "data": {
                    "description": "An error occurred while generating an image",
                    "done": True,
                },
            }
        )
        return FAILED_CONTEXT.format(error=error)
    await emit({"type": "status", "data": {"description": "Image created", "done": True}})
    await emit(
        {"type": "files", "data": {"files": [{"type": "image", **image} for image in images]}}
    )
    return GENERATED_CONTEXT


class Filter:
    async def inlet(self, body: dict, __metadata__: dict, __user__: dict | None = None) -> dict:
        metadata = __metadata__
        features = metadata.get("features") or {}
        params = metadata.get("params")
        if not (
            features.get(FEATURE)
            and metadata.get("session_id")  # a UI turn, which has the event channel
            and isinstance(params, dict)
        ):
            return body
        # Open WebUI reads this from the request metadata when it decides
        # whether to generate the image itself (see utils/middleware.py,
        # process_chat_payload). Only this turn is affected.
        params["function_calling"] = FORCED_MODE
        state: dict = {"text": ""}
        metadata[STATE_KEY] = state
        messages = body.get("messages")
        if not isinstance(messages, list):
            return body
        text_only(messages)
        prompt = last_user_text(messages)
        state["edit"] = edit_intent(prompt)
        if not state["edit"] and await chat_holds_images(
            metadata, (__user__ or {}).get("id"), messages
        ):
            # Open WebUI would edit the chat's latest picture. Keep it out of
            # this turn: the handler is skipped and ``request`` generates instead.
            body_features = body.get("features")
            for holder in (features, body_features if isinstance(body_features, dict) else None):
                if holder is not None:
                    holder[FEATURE] = False
            state["generate"] = {"prompt": prompt, "model": body.get("model")}
        return body

    async def request(
        self,
        body: dict,
        __metadata__: dict,
        __event_emitter__=None,
        __request__=None,
        __user__: dict | None = None,
    ) -> dict:
        state = __metadata__.get(STATE_KEY)
        messages = body.get("messages")
        if state is None or not isinstance(messages, list):
            return body
        generate = state.pop("generate", None)
        if generate is not None:
            context = await self._generate(
                generate, __metadata__, __request__, __user__, __event_emitter__
            )
            if context is not None:
                body["messages"] = messages = with_system_context(messages, context)
        system = claimed_success(messages)
        if system is None:
            return body
        images = await attached_images(__metadata__)
        if images is None:
            return body
        if images:
            # A picture is attached: tell the model so in words it will not argue with.
            system["content"] = _SUCCESS_CONTEXT.sub(
                lambda claim: shown_context(claim.group(0)), system["content"]
            )
            return body
        # Open WebUI says a picture is shown, and the message holds none.
        system["content"] = _SUCCESS_CONTEXT.sub(NOT_CREATED_CONTEXT, system["content"])
        state["failed"] = True
        if __event_emitter__ is not None:
            try:
                await __event_emitter__(
                    {"type": "status", "data": {"description": NOT_CREATED_STATUS, "done": True}}
                )
            except Exception:  # noqa: BLE001, S110 - the status is a courtesy; the text still says it
                pass
        return body

    async def _generate(
        self, generate: dict, metadata: dict, request, user: dict | None, emit
    ) -> str | None:
        """A new picture for this turn through Open WebUI, the handler's failure or
        disabled context, or None when the user may not generate images at all."""
        try:
            from open_webui.models.users import Users

            account = await Users.get_user_by_id((user or {}).get("id"))
            if request is None or account is None or emit is None:
                raise RuntimeError("the request, the user or the event channel is missing")
            return await generate_new_image(
                request,
                generate["prompt"],
                {**metadata, "model_id": generate.get("model")},
                account,
                emit,
            )
        except Exception as exc:  # noqa: BLE001 - never a silent turn; the model reports it
            return FAILED_CONTEXT.format(error=str(exc))

    async def stream(self, event: dict, __metadata__: dict) -> dict:
        state = __metadata__.get(STATE_KEY)
        if state is None:
            return event
        # Hold the prose until the turn ends, so a made-up link is never on
        # screen even for a moment; then release it cleaned, in one piece.
        for choice in event.get("choices", []):
            delta = choice.setdefault("delta", {})
            text = delta.pop("content", None)
            if isinstance(text, str) and len(state["text"]) + len(text) <= MAX_TEXT:
                state["text"] += text
            if choice.get("finish_reason"):
                delta["content"] = (
                    NOT_CREATED if state.get("failed") else clean_acknowledgement(state["text"])
                )
                state["text"] = ""
        return event

    async def outlet(self, body: dict, __metadata__: dict) -> dict:
        state = __metadata__.get(STATE_KEY)
        if state is None:
            return body
        assistants = [
            message
            for message in body.get("messages", [])
            if message.get("role") == "assistant" and isinstance(message.get("content"), str)
        ]
        for message in assistants:
            message["content"] = clean_acknowledgement(message["content"])
        if state.get("failed") and assistants:
            assistants[-1]["content"] = NOT_CREATED
        return body
