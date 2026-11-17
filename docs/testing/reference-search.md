# Reference pictures from the web (Capella, filter 0.9.0)

The router action `image_search` finds pictures on the web **only when the user asks for
references** ("pesquise referências de uniformes soviéticos dos anos 1980"). Generating
"no seco" stays `new_image`; nothing is searched unless asked. Capella only, and only
when web search is on (same switch as text search).

## What happens

1. **Search**: Wikimedia Commons and Openverse are asked in parallel (no key). Unsplash and
   Pexels are asked only if the user saved a key in Hub **Configurações → Pesquisa web e
   referências** (or set `PEPITO_UNSPLASH_KEY` / `PEPITO_PEXELS_KEY`). The saved key is
   verified, sealed with Windows DPAPI and supplied only to the managed Open WebUI process
   after a service restart. Results are merged round-robin, without repeats; up to 6.
   Openverse wants every word to match, so a long query is retried with its first three
   words (measured 2026-10-14: "Soviet army uniform 1980s photograph" -> 0 results,
   "soldier uniform" -> 240).
2. **Show**: only thumbnails are downloaded (re-encoded as a small JPEG and hosted by Open
   WebUI as files on the answer). The text lists each result with author, licence and a
   link to the source page. A licence the source did not state is shown as
   "não verificada"; it is never guessed.
3. **Choose**: "use a 2 como pose", "roupa da 1 e iluminação da 3, e gere ...". The router
   returns `references: [{image, role}]` where web results are numbered after the chat's own
   pictures. Only the chosen pictures are downloaded in full, before the model is unloaded.
4. **Credits**: the answer ends with "Referências usadas" (role, title, author, licence,
   page) for every web picture used.

A failed download answers with the reason and generates nothing. Nothing is invented: no
result, no licence, no claim of a download that did not happen.

## What is fetched, and how

Every fetch (thumbnails, full pictures, the JSON APIs) goes through `safe_get`:

- https only, port 443, no credentials in the URL, no numeric host;
- the name is resolved once, **every** address must be public (loopback, private, link-local,
  CGNAT, multicast, and IPv4-mapped IPv6 are refused), and the connection is pinned to the
  checked address (no second resolution to rebind);
- redirects are followed up to 3 times and each hop is validated again; an `Authorization`
  header is sent only to the host it was meant for;
- 12 MB cap (declared or streamed), 60 s deadline, `Accept-Encoding: identity`;
- the bytes must really be JPEG/PNG/WebP/GIF, and agree with the declared type and the URL
  extension (an SVG or HTML renamed `.jpg` is refused); at most 50 Mpx and at least 256 px
  on the short side; the first frame only; alpha is flattened onto white; the picture is
  re-encoded (EXIF, GPS and text chunks are gone) and capped at 2048 px.

The last search of a chat is kept as text in `DATA_DIR/pepito-references` for 6 hours, with
the addresses of its thumbnails so that no later turn takes them for pictures of the chat.

## Limits, stated plainly

- Licences are what the source reports. The Hub does not verify them; the credits say so.
- Copyright control is by prompt only (the router is told not to name artists or copy an
  artist's style). It is a mitigation, not a guarantee.
- A web picture in an explicit turn has no extra restriction beyond the age safeguard and the
  one-time consent (the user's own term covers it).
- Openverse/Wikimedia can be slow or down; the answer names the source that failed.
- The edit workflow receives the downloaded picture as a `data:` URI. The test harness
  decodes it like Open WebUI's `image_edits` is expected to; **not yet confirmed against a
  running Open WebUI 0.12.0**. Checklist row 31 covers it.

## Measured (this machine, not the 5090)

Search of "Soviet army uniform 1980s photograph": 1.2 s, 4 Wikimedia results with author and
licence; full download of one (3 MB, 1534x1021): 1.5 s, EXIF gone; thumbnail 0.8 s, 35 KB.
`https://localhost`, `https://127.0.0.1`, `http://...` and `https://169.254.169.254` were
refused by the real resolver; no temporary file was left behind.

## Tests

`tests/test_reference_search.py` (URL and address checks, redirects, caps, type and pixel
checks, EXIF, parsers, saved searches) and the turn tests at the end of
`tests/test_image_edit_dispatch.py`. Network seams are replaced; the shipped code between
them is what runs.
