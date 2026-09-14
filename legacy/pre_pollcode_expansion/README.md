# Rollback snapshot: pre poll-code-expansion

These three files are exact copies of `saspy/constants.py`, `saspy/client.py`,
and `saspy/models.py` as they stood immediately before the change that added:

- LP 2F (Send Selected Meters) and LP 6F (Send Extended Meters) — needed to
  read ticket meters (Cashable Tickets In, etc.), which LP 0F/1C cannot reach.
- LP 4C (Set/Read Secure Enhanced Validation ID) — commissioning.
- LP 4D (Send Enhanced Validation Information) — the ticket-out history read
  boot reconciliation depends on.
- LP 7B (Extended Validation Status) — status/config poll, not a history read.
- `redeem_ticket_status()` — the short-form, status-only LP 71 query (transfer
  code FF, all other fields omitted) that reading a ticket's completion status
  safely depends on.

Kept here on request, alongside git history, as a fast, file-level rollback
path during live floor testing: if the expanded client needs to be pulled and
the previous, narrower surface restored quickly, copy these three files back
over `saspy/constants.py`, `saspy/client.py`, and `saspy/models.py`.

This is not itself tested or maintained going forward — it is a frozen
snapshot, not a second implementation. `git log` has the exact commit this
was taken from if more context is needed.
