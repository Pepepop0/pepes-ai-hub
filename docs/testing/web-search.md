# Web search on Capella (filter 0.9.0)

Capella can search the web from the conversation, with no tool to switch on. The router
(see `image-tool-chat.md`) now chooses among five answers: `chat`, `web_search`,
`image_search`, `new_image` and `edit_image`; the last two keep their old behavior.

## What happens on a `web_search` turn

1. The router writes one to three short queries (the `prompt`, one per line).
2. The filter runs **Open WebUI's own web search** on them (`process_web_search`: its
   configured engine and page loader) and attaches the result to the request exactly as Open
   WebUI's legacy handler does, so its context injection and source chips work unchanged.
   It does not use that handler: the handler asks the task model for queries, a second call
   to the chat model that Ollama would reload with other options.
3. The model is told to answer from the results in the user's language, prefer official sources,
   compare sources that disagree, treat page text as data and never as an instruction, never
   invent facts, links or quotes, and not reproduce lyrics, books or articles in full (summary,
   analysis, links and short quotes only). That last rule is the model's, not a filter: it is a
   mitigation, not a guarantee.
4. After the answer the filter lists the URLs the search really loaded ("Fontes consultadas:"),
   dropping advertisement redirects. They come from the engine's result, never from the model.
5. Statuses are Open WebUI's own: "Searching the web", "Searched N sites".

**Failure is said, not hidden.** If the engine raises (a provider that is down or limiting
use), finds nothing, or no administrator/session is available, the user sees an error status
and the model is told the search failed and to give no links or sources. The log keeps the
exception class and never the query.

The age safeguard applies: a search that asks for something explicit about a minor is refused
like a picture; researching children's law or schools is not (a minor word blocks a search only
next to something explicit).

## Settings (Hub → Configurações, and the agent CLI)

| setting | meaning |
|---|---|
| on/off | `ENABLE_WEB_SEARCH`. Off: the router offers neither `web_search` nor `image_search` |
| SearXNG address | `http://host:port` becomes `http://host:port/search?q=<query>`; no credentials in it |
| Brave Search key | verified with Brave, kept sealed with Windows DPAPI, never shown or logged; `BRAVE_SEARCH_API_KEY` in the environment wins |
| Unsplash key | optional source of reference photos; verified, DPAPI-sealed; `PEPITO_UNSPLASH_KEY` wins |
| Pexels key | optional source of reference photos; verified, DPAPI-sealed; `PEPITO_PEXELS_KEY` wins |

Engine for text: a SearXNG address wins, then a Brave key, then DuckDuckGo (Open WebUI's
`ddgs` library: no account, no key). The Hub writes those text settings into Open WebUI's
database through its admin API (`/api/v1/retrieval/config/update`, only the `web` fields it
owns), the next time it integrates with the running Open WebUI (open it from the Hub,
restart). Pages are loaded without an embedding model
(`BYPASS_WEB_SEARCH_EMBEDDING_AND_RETRIEVAL`), at most four of them, each capped at 12,000
characters, one at a time.

Unsplash/Pexels keys are given only to the managed Open WebUI process, never Ollama or
ComfyUI, because the installed filter calls those providers directly. Restart the services
after adding or removing one. Wikimedia Commons and Openverse need no key.

Commands: `web_search_status`, `web_search_configure {enabled, searxng_url?}`,
`brave_login {token}`, `brave_logout`, `image_search_login {provider, token}` and
`image_search_logout {provider}` (`provider`: `unsplash` or `pexels`). Login results contain
configured booleans and a source label (`windows-dpapi` or the environment variable), never
the submitted key.

## DuckDuckGo is limited and unreliable (measured, 2026-10-14)

`ddgs` is a metasearch scraper, not an API: no published quota, no guarantee. From the
development machine (one IP, no proxy):

- the first search worked (4 results in 4.0 s); then the `auto` backend answered *No results
  found* on about half of the calls, even when `duckduckgo` alone answered in 0.9 s;
- one `auto` result was a sponsored Bing redirect (`bing.com/aclick`);
- after about 13 queries in three minutes every backend I tried (duckduckgo, bing, brave, yahoo,
  wikipedia, mojeek, startpage, google) answered *No results found*; only `yandex` still did.
  It had not recovered after several minutes.

Reading: one search now and then works; a burst of research does not. For regular use, set a
SearXNG address (the best option: aggregates many engines, no key) or a Brave key (free tier,
stable). The router asks for at most three queries a turn, one page at a time, to stay gentle.

SearXNG is not an official Windows package: it runs in WSL2 or Docker. The Hub only takes its
address; it does not install it.

## Tests

`tests/test_age_safeguard.py` (the sources block), the last tests of
`tests/test_image_edit_dispatch.py` (the turns: queries, attached files, statuses, failure,
nothing found, setting off, minors), `tests/test_web_search.py` (settings, provider key
verification/sealing, the Open WebUI sync), `tests/test_reference_search.py` (safe fetch and
source parsers), `tests/test_desktop_security.py` (the commands, no key echoed), and
`tests/test_open_webui_integration.py` (keys reach only Open WebUI's launch environment).

The real DuckDuckGo engine and Wikimedia download were exercised only as described here and
in `reference-search.md`, from scripts; Open WebUI itself was not started.
