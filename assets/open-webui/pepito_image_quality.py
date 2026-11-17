"""
title: Alta qualidade
author: Pepito's AI Hub
description: Switch for Capella. Uses the higher-resolution generation workflow. The requested image count stays unchanged; local edits preserve original pixels outside their selected area.
version: 0.3.0
"""

import base64

# The image-chat filter reads this switch from filter_ids; this toggle never generates.
_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
    'stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M12 3l1.5 4.5L18 9l-4.5 1.5L12 15l-1.5-4.5L6 9l4.5-1.5L12 3z"/>'
    '<path d="M19 15l.7 2.3L22 18l-2.3.7L19 21l-.7-2.3L16 18l2.3-.7L19 15z"/></svg>'
)


class Filter:
    def __init__(self):
        self.toggle = True
        self.icon = "data:image/svg+xml;base64," + base64.b64encode(_SVG.encode()).decode()

    async def inlet(self, body: dict) -> dict:
        return body
