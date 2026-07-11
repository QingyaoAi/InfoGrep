"""``python -m infogrep`` — same entry point as the ``infogrep`` console script.

The standalone macOS app bundle has no console-script shims, so it launches the
backend with ``python -m infogrep serve …``.
"""

from .cli import app

if __name__ == "__main__":
    app()
