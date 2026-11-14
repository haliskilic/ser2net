"""ser2netweb — serial-to-network bridge with a web config UI."""

# Single source of truth for the version (ser2netweb.py and the web layer import this).
__version__ = "3.1.0"

# The product's name: up to 2.9.x it was ser2net (no relation to the C ser2net it can
# import the configuration of; NOTICE)
NAME = "ser2netweb"


def user_agent() -> str:
    """The User-Agent of the HTTP requests it makes: webhooks, OpenID Connect, cluster peers
    (no node reads a peer's: the version comes in the JSON answers)."""
    return f"{NAME}/{__version__}"


# Where this program's source code is offered: the web UI's footer links it (AGPL-3.0
# section 13). A modified copy that others use over a network names its own source in
# SER2NETWEB_SOURCE_URL (an http:// or https:// address), see ser2netweb/licences.py.
SOURCE_URL = "https://github.com/haliskilic/ser2netweb"
