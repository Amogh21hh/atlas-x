"""Dashboard server with rotating logs, bound to localhost only.

    python -m atlas.serve            # http://127.0.0.1:8600
"""
from __future__ import annotations

import os

import uvicorn

from .logsetup import setup


def main() -> None:
    setup("dashboard")
    port = int(os.environ.get("ATLAS_PORT", "8600"))
    # 127.0.0.1 only: the dashboard controls a brokerage connection and must not be exposed on the LAN.
    uvicorn.run("atlas.api:app", host="127.0.0.1", port=port, log_config=None)


if __name__ == "__main__":
    main()
