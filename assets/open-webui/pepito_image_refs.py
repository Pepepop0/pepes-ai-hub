"""
title: Buscar referências
author: Pepito's AI Hub
description: Switch for the Capella presets. On: when you ask for a new picture of something specific (a uniform, a car, a landmark, a place), the image chat searches the web for a licensed photo of that thing and builds the picture on it, crediting the author. Never for explicit pictures, and never a photo of a person as the person in your picture.
version: 0.1.0
"""

import base64

# This filter changes nothing itself. Open WebUI lists a filter with ``toggle`` in the
# composer's Integrations menu, and runs it only when the user switched it on; the image
# chat filter (pepito_image_chat) reads whether this one is on from the request's
# ``filter_ids`` and searches. See docs/testing/image-tool-chat.md.

# A picture under a magnifying glass, the reference search mark.
_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
    'stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">'
    '<rect x="3" y="4" width="12" height="10" rx="1.5"/>'
    '<path d="M3 12l3.5-3.5 3 3 2-2 3.5 3.5"/>'
    '<circle cx="16.5" cy="16.5" r="3.5"/><path d="M19 19l2.5 2.5"/></svg>'
)


class Filter:
    def __init__(self):
        self.toggle = True
        self.icon = "data:image/svg+xml;base64," + base64.b64encode(_SVG.encode()).decode()

    async def inlet(self, body: dict) -> dict:
        return body
