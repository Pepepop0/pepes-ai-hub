"""
title: Arte digital
author: Pepito's AI Hub
description: Switch for Capella. On: pictures are made as digital art and illustration (the style slider turned toward illustration, prompts that start with the medium). With neither this nor "Fotorrealista" on, or both, the style follows what you ask for.
version: 0.2.0
"""

import base64

# This filter changes nothing itself. Open WebUI lists a filter with ``toggle`` in the
# composer's Integrations menu, and runs it only when the user switched it on; the image
# chat filter (pepito_image_chat) reads whether this one is on from the request's
# ``filter_ids`` and sets the style. See docs/testing/image-tool-chat.md.

# A brush, the illustration mark.
_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
    'stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M18.4 2.6a2 2 0 0 1 2.9 2.9L11 15.8l-3.5.7.7-3.5L18.4 2.6z"/>'
    '<path d="M7.5 16.5c-2 0-3.5 1.5-3.5 3.5 0 .6-.4 1-1 1 1.2 1 2.6 1 4 1 2.2 0 4-1.8 4-4"/></svg>'
)


class Filter:
    def __init__(self):
        self.toggle = True
        self.icon = "data:image/svg+xml;base64," + base64.b64encode(_SVG.encode()).decode()

    async def inlet(self, body: dict) -> dict:
        return body
