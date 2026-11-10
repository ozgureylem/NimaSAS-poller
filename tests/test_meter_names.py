"""Table C-7's meter names, and the arithmetic that checks they are right.

A transcription from a PDF is only as good as what cross-checks it, so none
of these tests take the table on trust. Two independent checks run against
it: the sizes that came out of the same table rows must agree with
METER_CODE_SIZES_BCD, which was typed in by hand long before and separately;
and the names must predict arithmetic that holds on real cabinets.

The cabinet data is the 2026-10-27 floor survey, committed under survey/ in
the private repository. Where it is absent -- the public mirror excludes it
-- those tests skip rather than pass quietly.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from saspy.constants import (  # noqa: E402
    METER_CODE_SIZES_BCD,
    METER_NAMES,
    METER_NOTES,
    METER_RELATIONS,
    meter_name,
)

SURVEY = Path(__file__).resolve().parent.parent / "survey"

# One snapshot per cabinet from the floor survey, plus the lab cabinet.
CABINETS = {
    "lab 246": "2026-10-27T21-57-08Z-asset246.json",
    "IGT asset 7": "2026-10-27T22-22-43Z-asset7.json",
    "zoom roulette asset 8": "2026-10-27T22-40-53Z-asset8.json",
    "rosetta asset 5": "2026-10-27T22-49-31Z-asset5.json",
    "novomatic new asset 1": "2026-10-27T22-55-42Z-asset1.json",
}


def _meters(filename):
    path = SURVEY / filename
    if not path.is_file():
        pytest.skip("survey/ is private-repo only")
    return json.loads(path.read_text())["meters_raw"]


def test_every_named_code_has_a_size_and_the_reverse():
    """The two tables came from the same rows by different routes, years
    apart. Disagreement means one of them was edited without the other."""
    assert set(METER_NAMES) == set(METER_CODE_SIZES_BCD)
    assert len(METER_NAMES) == 162


def test_no_name_is_empty_or_leaks_a_table_column():
    for code, name in METER_NAMES.items():
        assert name.strip(), f"{code:04X} has no name"
        # The support columns are single X's and the size column ends in BCD
        # or binary. Either appearing in a name means the row was split wrong.
        assert " BCD" not in name, f"{code:04X} kept its size column: {name!r}"
        assert not name.rstrip().endswith(" X"), f"{code:04X} kept a support column"
        assert "Reserved for future use" not in name


# C-7 declares a relation in two different notations, and a test that knows
# only one of them will fail on the other. Most totals carry a bracketed
# "[same as meter 0080 + 0084]" naming the component CODES. 0022 and 0023
# instead say it in prose, naming the component METERS: "Total won credits
# (sum of total coin out and total jackpot)". Both are declarations; neither
# is an inference; and METER_RELATIONS carries both as declared=True.
PROSE_DECLARED = {
    0x0022: "sum of total coin out and total jackpot",
    0x0023: "sum of total hand paid cancelled credits and total jackpot",
}


def test_the_declared_relations_are_quoted_in_the_names():
    """METER_RELATIONS was built by reading C-7's declarations. Those
    declarations are in the names now, so the two can be checked against
    each other rather than both believed."""
    for total, components, declared in METER_RELATIONS:
        if not declared:
            continue  # the SAS-only twins are an argument, not C-7's words
        name = meter_name(total)
        if total in PROSE_DECLARED:
            assert PROSE_DECLARED[total] in name, f"{total:04X}: {name!r}"
            continue
        assert "[same as meter" in name, f"{total:04X}: {name!r}"
        for c in components:
            assert f"{c:04X}" in name, f"{total:04X} does not name {c:04X}"


def test_every_bracketed_relation_in_a_name_is_in_METER_RELATIONS():
    """The other direction: a '[same as meter ...]' that nothing encodes is
    a cross-check the project is not making."""
    import re
    encoded = {t for t, _c, dec in METER_RELATIONS if dec}
    for code, name in METER_NAMES.items():
        if "[same as meter" not in name:
            continue
        assert code in encoded, f"{code:04X} declares a relation nothing uses"
        quoted = {int(x, 16) for x in re.findall(r"\b([0-9A-F]{4})\b",
                                                name.split("[same as meter")[1])}
        declared_components = {c for t, cs, _d in METER_RELATIONS if t == code
                               for c in cs}
        assert quoted == declared_components, (
            f"{code:04X}: name says {sorted(quoted)}, "
            f"METER_RELATIONS says {sorted(declared_components)}")


def test_the_sas_only_note_is_recorded():
    assert 0x007F in METER_NOTES
    assert "Section 7.24.1" in METER_NOTES[0x007F]


@pytest.mark.parametrize("cabinet,filename", sorted(CABINETS.items()))
def test_bills_accepted_equals_the_denomination_breakdown(cabinet, filename):
    """0040 onwards name a dollar value each. Multiply by the counts and the
    result must be 000B, 'total credits from bills accepted'.

    This is the test that would have caught a misread denomination: get
    0043 wrong by one row and the sum stops matching on every cabinet that
    ever took a $10 bill. It holds on all five, from a single $1 note in the
    lab to 25,492.00 on the floor.
    """
    meters = _meters(filename)
    denominations = {}
    for code, name in METER_NAMES.items():
        # "Total number of $20.00 bills accepted", and NOT 000B's "Total
        # credits from bills accepted", which has no denomination in it. An
        # earlier version matched on "bills accepted" alone and tried to
        # split 000B on a "$" that is not there.
        if not name.startswith("Total number of $") or "bills accepted" not in name:
            continue
        value = name.split("$", 1)[1].split()[0].rstrip(".,")
        denominations[f"{code:04X}"] = int(float(value.replace(",", "")) * 100)
    assert len(denominations) == 24, "C-7 lists 24 bill denominations"

    total = sum(denominations[c] * meters[c]
                for c in denominations if meters.get(c))
    accepted = meters.get("000B")
    if accepted is None:
        pytest.skip(f"{cabinet} does not answer 000B")
    assert total == accepted, (
        f"{cabinet}: denominations sum to {total}, 000B says {accepted}")


@pytest.mark.parametrize("cabinet,filename", sorted(CABINETS.items()))
def test_total_won_decomposes_into_its_three_named_parts(cabinet, filename):
    """0022 is 'total won'. 001C, 001D and 001E name machine-paid paytable
    win EXCLUDING progressive and external bonus, progressive win, and
    external bonus win. If those names are right the three must add to 0022.

    They do on four cabinets of five, including one carrying 7,721,743.35 in
    total win with a real 34,830.90 progressive share -- which is the part
    that makes this a test of the names rather than of nothing.

    Rosetta is the exception and is excluded by name, not by a loosened
    assertion: it answers 0022 with 3,385,693 and all three components with
    zero, which cannot be true at once. See test_a_cabinet_may_answer_zero
    _rather_than_decline below -- it is why its zeros are not readings.
    """
    if cabinet == "rosetta asset 5":
        pytest.skip("rosetta pads unimplemented meters with zero; see below")
    meters = _meters(filename)
    parts = [meters.get(c) for c in ("001C", "001D", "001E")]
    won = meters.get("0022")
    if won is None or any(p is None for p in parts):
        pytest.skip(f"{cabinet} does not answer all four meters")
    assert won == sum(parts), (
        f"{cabinet}: 0022={won} but 001C+001D+001E={sum(parts)}")


def test_a_cabinet_may_answer_zero_rather_than_decline():
    """Rosetta answered all 162 codes and declined none -- the only cabinet
    of the seven to do so -- and 139 of those answers were zero.

    On its own that is ambiguous: a cabinet may genuinely implement every
    meter and have most of them at zero. The names settle it. 0022 'total
    won' reads 3,385,693 while the three meters that by name must sum to it
    all read zero. A machine cannot have won that much through no category
    at all, so at least three of those zeros are not readings.

    §4.4 says an unsupported long poll is to be ignored. Rosetta answers
    instead, which means on that cabinet 'answered' carries no information
    and a zero must not be reported as a meter reading of zero. Recorded
    here so the conclusion survives the conversation it came from.
    """
    meters = _meters(CABINETS["rosetta asset 5"])
    assert meters.get("0022") == 3385693
    assert [meters.get(c) for c in ("001C", "001D", "001E")] == [0, 0, 0]
