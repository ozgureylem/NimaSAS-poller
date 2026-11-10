# `meter_snapshot-2026-11-08-pre-probe-table.py`

The floor-survey snapshot tool exactly as it stood on 2026-11-08, before the
probe table was added. Kept at the operator's request — *"for safety; keep
the old survey python inside legacy/"* — because every survey taken between
2026-10-27 and 2026-11-05 was taken with THIS file, and a snapshot can only
be read against the tool that wrote it.

**It is frozen. Do not fix bugs here.** If one of those snapshots turns out
to be wrong, the finding belongs in a note beside it; editing this file
would make the surveys unreproducible, which is the one thing it exists to
prevent.

What it polled: LP `0x54` version/serial, `0x1F` machine id, `0x74`
lock/status (query only), `0x7E` date/time, `0x51` games implemented, the
extended-meter sweep, and `0xB5` per game. What it did **not** poll is the
reason it was replaced: `0xA0` enabled features, `0x4D` validation history,
`0x1B` handpay, `0x2D`, `0x1E`, `0x48`, `0x56`, and the rest of the
read-only surface — so a survey could not say which long polls a cabinet
answers, only which meters it returned.
