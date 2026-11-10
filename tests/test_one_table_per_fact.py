"""No module may keep its own copy of a SAS table.

WHY. `examples/ticket_in_watch.py` carried a private REDEEM_STATUS dict
whose values disagreed with Table 15.12c in `saspy/constants.py`, and
disagreed in the worst direction: it called 0x01 and 0x02 REJECTIONS
("not a valid ticket", "ticket expired") where the real table has them as
SUCCESSES -- restricted and nonrestricted promotional ticket redeemed.

On 2026-11-08 the lab cabinet returned 0x02 for a ticket it had just
credited. That tool would have reported it expired.

It also lacked 0x84, "transfer amount exceeded the gaming machine credit
limit" -- the status the cabinet used to explain a refusal that amendment
A16 was written as though the machine never explained at all.

`saspy/constants.py` already records this exact failure once, about 0x82
drifting out of the table it belonged to: "A constant and the table it
belongs to drifted apart because nothing checked they agreed; the test
now does." That test compared constants.py against itself and could never
have seen a second copy in another file. This one can.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from saspy.constants import (  # noqa: E402
    EXCEPTION_CLARIFICATIONS, TICKET_REDEMPTION_STATUS,
)

# The canonical tables, by the value that identifies them: a dict literal
# whose keys look like one of these must not be redefined anywhere else.
CANONICAL = {
    "TICKET_REDEMPTION_STATUS": TICKET_REDEMPTION_STATUS,
    "EXCEPTION_CLARIFICATIONS": EXCEPTION_CLARIFICATIONS,
}

FILES = [p for p in (ROOT / "examples").rglob("*.py")
         if "__pycache__" not in str(p)] + \
        [p for p in (ROOT / "deploy").rglob("*.py")] + \
        [p for p in (ROOT / "saspy").rglob("*.py")
         if p.name != "constants.py"]


def _int_keyed_dicts(path: Path):
    """Every module-level dict literal with integer keys, by name."""
    tree = ast.parse(path.read_text(), filename=str(path))
    out = {}
    for node in tree.body:
        target = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            target, value = node.targets[0].id, node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target, value = node.target.id, node.value
        if target is None or not isinstance(value, ast.Dict):
            continue
        keys = [k.value for k in value.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, int)]
        if keys:
            out[target] = set(keys)
    return out


@pytest.mark.parametrize("path", FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_module_redefines_a_canonical_sas_table(path: Path):
    for name, keys in _int_keyed_dicts(path).items():
        for canon_name, canon in CANONICAL.items():
            overlap = keys & set(canon)
            # A handful of shared small integers is coincidence. Most of a
            # table's keys is a copy.
            if len(overlap) >= 4 and len(overlap) >= len(keys) * 0.6:
                pytest.fail(
                    f"{path.relative_to(ROOT)} defines {name!r}, which looks "
                    f"like a private copy of {canon_name} "
                    f"({len(overlap)} of its {len(keys)} keys overlap). "
                    f"Import the canonical table instead -- a second copy "
                    f"cannot be kept in step, and the last one disagreed "
                    f"about whether a redeemed ticket had been rejected.")


def test_the_status_that_explains_a_CREDIT_LIMIT_refusal_is_in_the_table():
    """0x84 is what the lab cabinet returned for the ticket it refused on
    2026-11-08, and what it did NOT return for the banknote it refused
    seventeen minutes later. Amendment A16 rests on it."""
    assert TICKET_REDEMPTION_STATUS[0x84] == (
        "transfer amount exceeded the gaming machine credit limit")


def test_the_three_SUCCESS_codes_are_not_described_as_failures():
    """0x00, 0x01 and 0x02 are all redemptions. The private copy called two
    of them rejections, and the cabinet uses 0x02 routinely."""
    for code in (0x00, 0x01, 0x02):
        assert "redeemed" in TICKET_REDEMPTION_STATUS[code]
        assert "reject" not in TICKET_REDEMPTION_STATUS[code]
