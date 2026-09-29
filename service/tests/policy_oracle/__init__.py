"""The independent policy oracle for eligibility, display and disclosure (bead `1ir.1.12`).

A test-only, standard-library-only reference model of the shared eligibility ADR
(`docs/adr/shared-eligibility-display-disclosure.md`), read through its section 15.2 and the Workbench
threat model section 15. Import it as `service.tests.policy_oracle`, never as a bare `policy_oracle`
(a second copy of every class would break `isinstance`). Production code never imports it.

See `README.md` in this directory for what it is, how to use it and how to change it.
"""

from . import cases, fixtures, generate, machine, model, rules
from .cases import *  # noqa: F403
from .fixtures import *  # noqa: F403
from .generate import *  # noqa: F403
from .machine import *  # noqa: F403
from .model import *  # noqa: F403
from .rules import *  # noqa: F403

__all__ = sorted(
    {
        *cases.__all__,
        *fixtures.__all__,
        *generate.__all__,
        *machine.__all__,
        *model.__all__,
        *rules.__all__,
    }
)
