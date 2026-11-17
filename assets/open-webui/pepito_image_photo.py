"""
title: Fotorrealista
author: Pepito's AI Hub
description: Switch for Capella. On: pictures are made as photographs (the style slider turned toward photographs). With neither this nor "Arte digital" on, or both, the style follows what you ask for.
version: 0.1.0
"""

import base64

# This filter changes nothing itself. Open WebUI lists a filter with ``toggle`` in the
# composer's Integrations menu, and runs it only when the user switched it on; the image
# chat filter (pepito_image_chat) reads whether this one is on from the request's
# ``filter_ids`` and sets the style. See docs/testing/image-tool-chat.md.

# A camera, the photograph mark.
_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
    'stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M3 8a2 2 0 0 1 2-2h2.5l1.5-2h6l1.5 2H19a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5'
    'a2 2 0 0 1-2-2V8z"/>'
    '<circle cx="12" cy="13" r="3.5"/></svg>'
)


class Filter:
    def __init__(self):
        self.toggle = True
        self.icon = "data:image/svg+xml;base64," + base64.b64encode(_SVG.encode()).decode()

    async def inlet(self, body: dict) -> dict:
        return body
