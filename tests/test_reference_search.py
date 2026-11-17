"""Reference pictures from the web: what may be fetched, what a fetched picture may become,
and how the sources' answers become results.

The filter is executed as Open WebUI executes it. Its two network seams, the name
resolution (``_resolve``) and one connection (``_request_once`` / ``_PinnedHTTPS``), are
replaced here; everything between them -- the URL checks, the address checks, the
redirects, the size cap, the type checks, the re-encoding -- is the shipped code.
"""

from __future__ import annotations

import io
import json
import os
import time

import pytest
from PIL import Image
from test_open_webui_image_filter import load_module

PUBLIC = "93.184.216.34"


@pytest.fixture
def ns(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    module = load_module()
    module["_resolve"] = lambda _host: [PUBLIC]
    return module


def picture(width=600, height=400, *, fmt="PNG", mode="RGB", **save) -> bytes:
    out = io.BytesIO()
    Image.new(mode, (width, height), (200, 30, 30) if mode == "RGB" else (200, 30, 30, 0)).save(
        out, fmt, **save
    )
    return out.getvalue()


# --- what may be fetched ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://upload.wikimedia.org/a.jpg",
        "file:///C:/Windows/win.ini",
        "ftp://host/a.jpg",
        "gopher://host/x",
        "javascript:alert(1)",
        "https://user:secret@host.example/a.jpg",
        "https://host.example:8443/a.jpg",
        "https:///a.jpg",
        "//host.example/a.jpg",
        "",
    ],
)
def test_only_plain_https_without_credentials_on_the_default_port_is_fetched(ns, url):
    with pytest.raises(ns["FetchError"]):
        ns["validate_url"](url)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.5",
        "172.16.4.4",
        "192.168.1.20",
        "169.254.169.254",
        "100.64.0.9",
        "0.0.0.0",
        "224.0.0.1",
        "::1",
        "fe80::1",
        "fc00::1",
        "::ffff:127.0.0.1",
        "::ffff:10.0.0.1",
    ],
)
def test_a_name_that_resolves_to_a_private_or_local_address_is_refused(ns, address):
    ns["_resolve"] = lambda _host: [address]
    with pytest.raises(ns["FetchError"], match="rede privada"):
        ns["public_addresses"]("tricky.example")


def test_one_private_address_among_public_ones_refuses_the_name(ns):
    ns["_resolve"] = lambda _host: [PUBLIC, "10.1.1.1"]
    with pytest.raises(ns["FetchError"]):
        ns["public_addresses"]("tricky.example")


def test_a_numeric_host_or_an_unresolvable_name_is_refused_and_a_public_one_passes(ns):
    for host in ("127.0.0.1", "93.184.216.34", "[::1]"):
        with pytest.raises(ns["FetchError"]):
            ns["public_addresses"](host.strip("[]"))
    ns["_resolve"] = lambda _host: (_ for _ in ()).throw(OSError("nope"))
    with pytest.raises(ns["FetchError"], match="resolver"):
        ns["public_addresses"]("missing.example")
    ns["_resolve"] = lambda _host: [PUBLIC, "2606:2800:220:1:248:1893:25c8:1946"]
    assert ns["public_addresses"]("ok.example") == [PUBLIC, "2606:2800:220:1:248:1893:25c8:1946"]


def hops(ns, script: dict):
    """Replace one connection with a script keyed by host: returns the calls it received."""
    calls = []

    def once(address, host, target, headers, limit):
        calls.append({"address": address, "host": host, "target": target, "headers": headers})
        answer = script[host]
        if isinstance(answer, Exception):
            raise answer
        return answer

    ns["_request_once"] = once
    return calls


def test_a_redirect_to_localhost_or_a_private_name_is_refused_before_anything_connects(ns):
    calls = hops(ns, {"cdn.example": (302, {"location": "https://127.0.0.1/admin"}, b"")})
    with pytest.raises(ns["FetchError"]):
        ns["safe_get"]("https://cdn.example/a.jpg")
    assert [call["host"] for call in calls] == ["cdn.example"], "the second hop never connected"

    ns["_resolve"] = lambda host: ["10.0.0.7"] if host == "internal.example" else [PUBLIC]
    calls = hops(ns, {"cdn.example": (301, {"location": "https://internal.example/x"}, b"")})
    with pytest.raises(ns["FetchError"], match="rede privada"):
        ns["safe_get"]("https://cdn.example/a.jpg")
    assert len(calls) == 1


@pytest.mark.parametrize(
    "location", ["http://cdn.example/a.jpg", "file:///etc/passwd", "ftp://cdn.example/a"]
)
def test_a_redirect_that_leaves_https_is_refused(ns, location):
    hops(ns, {"cdn.example": (302, {"location": location}, b"")})
    with pytest.raises(ns["FetchError"]):
        ns["safe_get"]("https://cdn.example/a.jpg")


def test_redirects_are_followed_a_few_times_and_not_forever(ns):
    calls = hops(
        ns,
        {
            "a.example": (302, {"location": "https://b.example/x"}, b""),
            "b.example": (307, {"location": "/y?size=large"}, b""),
        },
    )
    with pytest.raises(ns["FetchError"], match="demais"):
        ns["safe_get"]("https://a.example/start")
    assert calls[1]["target"] == "/x" or calls[1]["host"] == "b.example"

    loop = {"a.example": (302, {"location": "https://a.example/again"}, b"")}
    calls = hops(ns, loop)
    with pytest.raises(ns["FetchError"], match="demais"):
        ns["safe_get"]("https://a.example/start")
    assert len(calls) == ns["MAX_REDIRECTS"] + 1


def test_a_credential_header_stays_on_the_host_it_was_meant_for(ns):
    calls = hops(
        ns,
        {
            "api.example": (302, {"location": "https://other.example/x"}, b""),
            "other.example": (200, {"content-type": "application/json"}, b"{}"),
        },
    )
    ns["safe_get"]("https://api.example/search", {"Authorization": "Client-ID secret", "X": "1"})
    assert calls[0]["headers"] == {"Authorization": "Client-ID secret", "X": "1"}
    assert calls[1]["headers"] == {"X": "1"}


def test_the_connection_is_made_to_the_address_that_was_checked_and_a_failure_is_plain(ns):
    ns["_resolve"] = lambda _host: [PUBLIC, "93.184.216.35"]
    calls = hops(ns, {"cdn.example": (200, {}, b"ok")})
    assert ns["safe_get"]("https://cdn.example/a")[0] == 200
    assert calls[0]["address"] == PUBLIC and calls[0]["host"] == "cdn.example"

    hops(ns, {"cdn.example": OSError("reset")})
    with pytest.raises(ns["FetchError"], match="conexão"):
        ns["safe_get"]("https://cdn.example/a")


class Wire:
    """One http.client connection as ``_request_once`` uses it: answers, then streams ``body``."""

    def __init__(self, status=200, headers=None, body=b""):
        self.status, self._headers, self._body = status, headers or {}, io.BytesIO(body)

    def __call__(self, host, address, **_kwargs):
        self.host, self.address = host, address
        return self

    def request(self, method, target, headers=None):
        self.sent = (method, target, headers)

    def getresponse(self):
        return self

    def getheaders(self):
        return list(self._headers.items())

    def read(self, size):
        return self._body.read(size)

    def close(self):
        pass


def test_a_response_over_the_cap_is_refused_declared_or_not_and_leaves_nothing_behind(ns, tmp_path):
    wire = Wire(headers={"Content-Length": str(ns["MAX_IMAGE_BYTES"] + 1)}, body=b"x")
    ns["_PinnedHTTPS"] = wire
    with pytest.raises(ns["FetchError"], match="grande demais"):
        ns["_request_once"](PUBLIC, "cdn.example", "/a", {}, ns["MAX_IMAGE_BYTES"])

    ns["_PinnedHTTPS"] = Wire(body=b"y" * 5000)  # no length declared: the stream is cut
    with pytest.raises(ns["FetchError"], match="grande demais"):
        ns["_request_once"](PUBLIC, "cdn.example", "/a", {}, 4096)

    ns["_PinnedHTTPS"] = Wire(headers={"Content-Type": "image/png"}, body=b"z" * 1000)
    status, headers, body = ns["_request_once"](PUBLIC, "cdn.example", "/a", {}, 4096)
    assert (status, body) == (200, b"z" * 1000) and headers["content-type"] == "image/png"
    spool = tmp_path / ns["REFERENCES_DIR"]
    assert spool.is_dir() and list(spool.iterdir()) == [], "the temporary file is gone"


def test_the_request_asks_for_an_identity_encoding_and_names_its_client(ns):
    wire = Wire(body=b"ok")
    ns["_PinnedHTTPS"] = wire
    ns["_request_once"](PUBLIC, "cdn.example", "/a?b=1", {"X-Test": "1"}, 100)
    method, target, headers = wire.sent
    assert (method, target) == ("GET", "/a?b=1")
    assert headers["Accept-Encoding"] == "identity" and headers["Host"] == "cdn.example"
    assert headers["User-Agent"].startswith("PepitosAIHub/") and headers["X-Test"] == "1"


# --- what a fetched picture may become --------------------------------------------------------


def test_a_downloaded_picture_is_encoded_afresh_without_exif_gps_or_text_chunks(ns):
    from PIL.PngImagePlugin import PngInfo

    exif = Image.Exif()
    exif[0x010F] = "SecretCamera"
    exif[0x8825] = {1: "N", 2: (10.0, 0.0, 0.0)}  # a GPS block
    source = picture(fmt="JPEG", exif=exif.tobytes())
    assert b"SecretCamera" in source
    cleaned, kind = ns["clean_image"](source, declared="image/jpeg", url="https://x.example/a.jpg")
    assert kind == "image/png" and cleaned[:8] == b"\x89PNG\r\n\x1a\n"
    assert b"SecretCamera" not in cleaned and b"Exif" not in cleaned
    result = Image.open(io.BytesIO(cleaned))
    assert len(result.getexif()) == 0 and "exif" not in result.info

    info = PngInfo()
    info.add_text("Author", "Someone Private")
    tagged = io.BytesIO()
    Image.new("RGB", (400, 400)).save(tagged, "PNG", pnginfo=info)
    assert b"Someone Private" in tagged.getvalue()
    again, _ = ns["clean_image"](tagged.getvalue(), declared="image/png")
    assert b"Someone Private" not in again and not Image.open(io.BytesIO(again)).text


def test_transparency_goes_onto_white_and_an_animation_keeps_its_first_frame(ns):
    clear = picture(500, 500, mode="RGBA")
    flat, _ = ns["clean_image"](clear)
    assert Image.open(io.BytesIO(flat)).convert("RGB").getpixel((10, 10)) == (255, 255, 255)

    frames = [Image.new("RGB", (300, 300), color) for color in ("red", "blue")]
    gif = io.BytesIO()
    frames[0].save(gif, "GIF", save_all=True, append_images=frames[1:])
    first, _ = ns["clean_image"](gif.getvalue(), declared="image/gif")
    assert Image.open(io.BytesIO(first)).convert("RGB").getpixel((5, 5))[0] > 200


def test_the_longest_side_is_brought_to_the_limit_and_a_thumbnail_is_a_small_jpeg(ns):
    big, _ = ns["clean_image"](picture(3000, 1500))
    assert Image.open(io.BytesIO(big)).size == (2048, 1024)
    thumb, kind = ns["clean_image"](picture(1200, 800), side=384, minimum=1, jpeg=True)
    assert kind == "image/jpeg" and thumb[:3] == b"\xff\xd8\xff"
    assert Image.open(io.BytesIO(thumb)).size == (384, 256)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (
            b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
            "não é uma imagem",
        ),
        (b"<html><body>captcha</body></html>", "não é uma imagem"),
        (b"%PDF-1.7 ...", "não é uma imagem"),
        (b"MZ\x90\x00" + b"\x00" * 200, "não é uma imagem"),
        (b"", "não é uma imagem"),
    ],
)
def test_bytes_that_are_not_a_jpeg_png_webp_or_gif_are_refused_whatever_they_claim(
    ns, data, message
):
    with pytest.raises(ns["FetchError"], match=message):
        ns["clean_image"](data, declared="image/jpeg", url="https://x.example/a.jpg")


def test_a_declared_type_or_an_extension_that_lies_is_refused(ns):
    jpeg = picture(fmt="JPEG")
    with pytest.raises(ns["FetchError"], match="tipo informado"):
        ns["clean_image"](jpeg, declared="image/png")
    with pytest.raises(ns["FetchError"], match="extensão"):
        ns["clean_image"](jpeg, declared="image/jpeg", url="https://x.example/photo.png?v=2")
    with pytest.raises(ns["FetchError"], match="tipo informado"):
        ns["clean_image"](jpeg, declared="text/html; charset=utf-8")
    # The usual spellings still pass: image/jpg, a charset, an extension in capitals, none at all.
    for declared, url in [
        ("image/jpg", "https://x.example/a.jpg"),
        ("image/jpeg; charset=binary", "https://x.example/A.JPEG"),
        ("", "https://x.example/render?id=7"),
    ]:
        assert ns["clean_image"](jpeg, declared=declared, url=url)[1] == "image/png"


def test_a_picture_with_too_many_pixels_or_too_small_a_side_is_refused(ns):
    ns["MAX_IMAGE_PIXELS"] = 100_000
    with pytest.raises(ns["FetchError"], match="pixels"):
        ns["clean_image"](picture(600, 400))
    ns["MAX_IMAGE_PIXELS"] = 50_000_000
    with pytest.raises(ns["FetchError"], match="pequena"):
        ns["clean_image"](picture(120, 90))


def test_a_corrupt_picture_is_a_refusal_not_a_crash(ns):
    broken = picture(fmt="JPEG")[:400]
    with pytest.raises(ns["FetchError"], match="decodificada"):
        ns["clean_image"](broken, declared="image/jpeg")


# --- the sources' answers -------------------------------------------------------------------------

WIKIMEDIA = {
    "query": {
        "pages": {
            "11": {
                "index": 2,
                "title": "File:Soldier in winter parade.jpg",
                "imageinfo": [
                    {
                        "url": "https://upload.wikimedia.org/wikipedia/commons/a/a1/Soldier.jpg",
                        "thumburl": "https://upload.wikimedia.org/thumb/Soldier.jpg/384px-Soldier.jpg",
                        "descriptionshorturl": "https://commons.wikimedia.org/w/index.php?curid=11",
                        "mime": "image/jpeg",
                        "extmetadata": {
                            "Artist": {"value": '<a href="//x">Ivan Petrov</a> &amp; Co'},
                            "LicenseShortName": {"value": "CC BY-SA 4.0"},
                            "LicenseUrl": {
                                "value": "https://creativecommons.org/licenses/by-sa/4.0"
                            },
                        },
                    }
                ],
            },
            "12": {
                "index": 1,
                "title": "File:Drawing.svg",
                "imageinfo": [
                    {"url": "https://upload.wikimedia.org/Drawing.svg", "mime": "image/svg+xml"}
                ],
            },
            "13": {
                "index": 3,
                "title": "File:No licence.png",
                "imageinfo": [
                    {
                        "url": "https://upload.wikimedia.org/No.png",
                        "mime": "image/png",
                        "extmetadata": {},
                    }
                ],
            },
        }
    }
}
OPENVERSE = {
    "results": [
        {
            "title": "Parade uniform",
            "url": "https://live.staticflickr.com/1/2.jpg",
            "thumbnail": "https://api.openverse.org/v1/images/abc/thumb/",
            "foreign_landing_url": "https://www.flickr.com/photos/1/2",
            "creator": "Maria",
            "license": "by",
            "license_version": "2.0",
            "license_url": "https://creativecommons.org/licenses/by/2.0/",
            "source": "flickr",
        },
        {"title": "CC0 one", "url": "https://cdn.example/c.jpg", "license": "cc0"},
        {"title": "no licence at all", "url": "https://cdn.example/n.jpg"},
        {"title": "no url"},
    ]
}


def test_wikimedia_results_carry_author_licence_and_page_and_skip_what_cannot_be_used(ns):
    ns["fetch_json"] = lambda url, headers=None: WIKIMEDIA
    found = ns["search_wikimedia"]("soviet uniform", 4)
    assert [item["title"] for item in found] == ["Soldier in winter parade.jpg", "No licence.png"]
    first = found[0]
    assert first["author"] == "Ivan Petrov & Co" and first["license"] == "CC BY-SA 4.0"
    assert first["page_url"].endswith("curid=11") and first["provider"] == "Wikimedia Commons"
    assert first["image_url"].startswith("https://upload.wikimedia.org/")
    assert found[1]["license"] == "não verificada" and found[1]["author"] == ""


def test_openverse_results_name_their_licence_and_an_unstated_one_is_not_verified(ns):
    ns["fetch_json"] = lambda url, headers=None: OPENVERSE
    found = ns["search_openverse"]("uniform", 4)
    assert [item["license"] for item in found] == ["CC BY 2.0", "CC0 1.0", "não verificada"]
    assert found[0]["provider"] == "Openverse (flickr)" and found[0]["author"] == "Maria"
    assert found[0]["page_url"] == "https://www.flickr.com/photos/1/2"


def test_the_keyed_sources_are_asked_only_with_the_users_own_key_and_send_it_to_their_host(
    ns, monkeypatch
):
    assert [name for name, _ in ns["search_providers"]()] == ["Wikimedia Commons", "Openverse"]
    monkeypatch.setenv("PEPITO_UNSPLASH_KEY", "u-key")
    monkeypatch.setenv("PEPITO_PEXELS_KEY", "p-key")
    assert [name for name, _ in ns["search_providers"]()][2:] == ["Unsplash", "Pexels"]

    seen = []

    def fetch(url, headers=None):
        seen.append((url.split("/")[2], headers))
        if "unsplash" in url:
            return {
                "results": [
                    {
                        "description": "Snow soldier",
                        "urls": {
                            "regular": "https://images.unsplash.com/r",
                            "thumb": "https://images.unsplash.com/t",
                        },
                        "links": {
                            "html": "https://unsplash.com/photos/1",
                            "download_location": "https://api.unsplash.com/photos/1/download",
                        },
                        "user": {"name": "Ana"},
                    }
                ]
            }
        return {
            "photos": [
                {
                    "alt": "Winter coat",
                    "src": {
                        "large2x": "https://images.pexels.com/l",
                        "medium": "https://images.pexels.com/m",
                    },
                    "url": "https://www.pexels.com/photo/1",
                    "photographer": "Bob",
                }
            ]
        }

    ns["fetch_json"] = fetch
    unsplash, pexels = ns["search_unsplash"]("x", 4), ns["search_pexels"]("x", 4)
    assert seen == [
        ("api.unsplash.com", {"Authorization": "Client-ID u-key"}),
        ("api.pexels.com", {"Authorization": "p-key"}),
    ]
    assert unsplash[0]["author"] == "Ana" and unsplash[0]["license"] == "Licença Unsplash"
    assert pexels[0]["author"] == "Bob" and pexels[0]["license_url"].endswith("/license/")


def test_the_results_are_interleaved_without_repeats_and_a_failed_source_is_named(ns):
    def listing(provider: str, *names: str) -> list:
        return [
            {"provider": provider, "title": name, "image_url": f"https://x.example/{name}"}
            for name in names
        ]

    wiki = listing("Wikimedia Commons", "w1", "w2", "w3", "dup")
    verse = listing("Openverse", "o1", "dup", "o3")
    ns["search_providers"] = lambda: [
        ("Wikimedia Commons", lambda q, n: wiki),
        ("Openverse", lambda q, n: verse),
        ("Unsplash", lambda q, n: (_ for _ in ()).throw(RuntimeError("quota for secret query"))),
    ]
    results, failed = asyncio_run(ns["search_references"]("q"))
    assert [item["title"] for item in results] == ["w1", "o1", "w2", "dup", "w3", "o3"]
    assert failed == ["Unsplash"]

    ns["search_providers"] = lambda: [
        ("Wikimedia Commons", lambda q, n: (_ for _ in ()).throw(OSError("down"))),
    ]
    assert asyncio_run(ns["search_references"]("q")) == ([], ["Wikimedia Commons"])


def asyncio_run(coroutine):
    import asyncio

    return asyncio.run(coroutine)


def test_a_failed_source_is_logged_by_name_and_class_and_never_by_query(ns, caplog):
    import logging

    ns["search_providers"] = lambda: [
        ("Openverse", lambda q, n: (_ for _ in ()).throw(RuntimeError("429 for my secret query"))),
    ]
    with caplog.at_level(logging.DEBUG):
        asyncio_run(ns["search_references"]("my secret query"))
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "Openverse" in logged and "RuntimeError" in logged and "secret" not in logged


# --- the last search of a chat ---------------------------------------------------------------------


def test_the_last_search_is_kept_per_chat_as_text_and_forgotten_after_a_while(ns, tmp_path):
    results = [{"title": "A", "image_url": "https://x.example/a", "license": "CC0 1.0"}]
    ns["save_results"]("chat-1", "soviet uniform", results)
    assert ns["load_results"]("chat-1") == results and ns["load_results"]("chat-2") == []
    folder = tmp_path / ns["REFERENCES_DIR"]
    (saved,) = folder.iterdir()
    assert json.loads(saved.read_text(encoding="utf-8"))["query"] == "soviet uniform"
    assert saved.read_bytes().count(b"\x89PNG") == 0, "text only, never a picture"

    old = time.time() - ns["RESULTS_TTL_SECONDS"] - 60
    os.utime(saved, (old, old))
    assert ns["load_results"]("chat-1") == [] and not saved.exists()

    ns["save_results"]("chat-3", "q", results)
    stale = folder / "search-stale.json"
    stale.write_text("{}", encoding="utf-8")
    os.utime(stale, (old, old))
    ns["purge_results"]()
    assert not stale.exists() and len(list(folder.iterdir())) == 1


def test_a_corrupt_or_foreign_saved_search_is_no_search(ns, tmp_path):
    ns["save_results"]("chat-1", "q", [{"image_url": "https://x.example/a", "title": "A"}])
    (path,) = (tmp_path / ns["REFERENCES_DIR"]).iterdir()
    path.write_text("not json", encoding="utf-8")
    assert ns["load_results"]("chat-1") == [] and ns["load_results"](None) == []
    path.write_text(json.dumps({"results": [1, "x", {"title": "no url"}]}), encoding="utf-8")
    assert ns["load_results"]("chat-1") == []


def test_the_listing_shows_every_credit_and_says_what_was_and_was_not_downloaded(ns):
    results = [
        {
            "title": "Soldier",
            "author": "Ivan",
            "license": "CC BY-SA 4.0",
            "provider": "Wikimedia Commons",
            "page_url": "https://commons.wikimedia.org/x",
            "image_url": "https://upload.wikimedia.org/a.jpg",
        },
        {
            "title": "Parade",
            "provider": "Openverse (flickr)",
            "image_url": "https://cdn.example/b.jpg",
        },
    ]
    text = ns["reference_listing"](results, ["Pexels"], "soviet uniform")
    assert (
        "1. **Soldier** — Ivan · CC BY-SA 4.0 · Wikimedia Commons — [página](https://commons.wikimedia.org/x)"
        in text
    )
    assert "2. **Parade** — autor não informado · não verificada · Openverse (flickr)" in text
    assert "Baixei só as miniaturas" in text and "use a 2 como pose" in text
    assert "Não consegui consultar: Pexels." in text


def test_the_credit_of_a_used_reference_names_its_role_author_licence_and_page(ns):
    slots = [
        {"roles": ["A"], "url": "data:image/png;base64,xx"},
        {
            "roles": ["B", "C"],
            "candidate": {
                "title": "Soldier",
                "author": "Ivan",
                "license": "CC BY-SA 4.0",
                "provider": "Wikimedia Commons",
                "page_url": "https://commons.wikimedia.org/x",
            },
        },
    ]
    block = ns["credits_block"](slots)
    assert block.startswith(f"\n\n{ns['CREDITS_HEADING']}")
    assert "- B+C: Soldier — Ivan · CC BY-SA 4.0 · https://commons.wikimedia.org/x" in block
    assert ns["credits_block"]([{"roles": ["A"], "url": "x"}]) == ""
