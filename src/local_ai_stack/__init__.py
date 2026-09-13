"""Pepito's AI Hub: the one place the version is written down.

``__version__`` is what packaging metadata and the built executable carry;
``PRODUCT_NAME`` and :func:`display_version` are what a person sees on screen.
The two forms come from the same string so a release can never show one
number in the footer and another in the file properties.
"""

__version__ = "0.1.0"

PRODUCT_NAME = "Pepito's AI Hub"


def display_version(version: str = __version__) -> str:
    """``0.1.0`` on screen is ``v0.1``: major and minor, nothing more."""
    major, minor = version.split(".")[:2]
    return f"v{major}.{minor}"


def product_title(version: str = __version__) -> str:
    return f"{PRODUCT_NAME}  {display_version(version)}"
