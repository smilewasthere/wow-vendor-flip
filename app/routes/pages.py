"""HTML pages served from web/."""

import os

WEB_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "web",
)


def index():
    with open(os.path.join(WEB_ROOT, "index.html"), encoding="utf-8") as fh:
        return fh.read()
