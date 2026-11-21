"""HTTP API for Astronomo.

The pipeline is headless.  This package is the supported way for a frontend (the in-repo React
client, or any other client) to read telemetry and to plan, cancel, rehearse or start.  The API
does not talk to the telescope; a confirmed start runs ``python main.py --from-plan``.

Run ``python -m dashboard.api`` from the project root.  The contract is ``docs/api.md``.
"""

from dashboard.api.app import create_app, main

__all__ = ["create_app", "main"]
