"""Clean-room SAS 6.02 host client.

See README.md for the coverage map (what's implemented here vs. what
remains in legacy/sas.py as unverified reference material vs. what's not
started at all).
"""

from .client import SASClient
from .exceptions import (
    SASAddressMismatchError,
    SASChecksumError,
    SASEncodingError,
    SASError,
    SASTimeoutError,
)
from .transport import SASTransport

__all__ = [
    "SASClient",
    "SASTransport",
    "SASError",
    "SASTimeoutError",
    "SASChecksumError",
    "SASAddressMismatchError",
    "SASEncodingError",
]
