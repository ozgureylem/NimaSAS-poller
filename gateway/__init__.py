"""The gateway-to-server link (migration spec §10).

Deliberately a separate package from `saspy`, which is the clean-room SAS
6.02 client and has `ozgureylem/saspy` frozen as its untouched reference.
The protocol in here is not SAS: it is how a NimaSAS gateway talks to the
CMS over the floor LAN, and mixing the two would blur a boundary this
project keeps on purpose.

NOTHING IN THE POLLER IMPORTS THIS. Operator, 2026-11-10: "the gateway I am
currently using for the lab should work the way it is. No HMAC, it is
dedicated for lab testing only. No server connection. Please keep it this
way." A test asserts that separation rather than trusting it.
"""
