"""Platform exceptions.

``ProhibitedCapability`` lives here, alone at the root, because it is raised
from three unrelated layers — the tool catalog at import, the registry
validator at load, the executor at dispatch — and a caller catching it should
not have to know which wall it hit.
"""


class KeelError(Exception):
    """Base class."""


class ProhibitedCapability(KeelError):
    """Something asked for a capability the platform does not implement.

    Not "you lack permission for this". There is no version of the system, no
    role, no override flag under which the request succeeds.
    """


class RegistryError(KeelError):
    """A spec failed validation."""


class ToolNotGranted(KeelError):
    """An agent reached for a tool outside its capability set."""


class ToolContractViolation(KeelError):
    """A call did not match a tool's declared parameters."""
