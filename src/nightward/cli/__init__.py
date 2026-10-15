"""nightward CLI - init / run / review / doctor / approve / reject / gate / status.

Each command module registers its commands on `app` when imported, and
`nightward --help` lists them in that order: keep these imports in workflow
order, not alphabetical.
"""
# isort: off
from . import init_run  # noqa: F401  init, run, report
from . import review  # noqa: F401  review
from . import approve  # noqa: F401  approve, reject
from . import doctor  # noqa: F401  doctor
from . import gate  # noqa: F401  gate, view, status, mcp
# isort: on
from ._common import LEGACY_ENTRIES, TRANSIENT_ENTRIES, app
from .approve import _why_removed
from .doctor import _DOCTOR_MARKS

__all__ = ["app", "TRANSIENT_ENTRIES", "LEGACY_ENTRIES", "_why_removed", "_DOCTOR_MARKS"]

if __name__ == "__main__":
    app()
