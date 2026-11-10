"""The hand-transcribed MASTER ACCOUNTING pages, checked against themselves.

Covers the floor cabinets whose operator menu is Novomatic's
ACCOUNTING > MASTER ACCOUNTING -- cabinets 1 and 2 of the 2026-10-27 survey,
both of which answered NOTHING over SAS. Their photographs are the only
record of them, so there is no wire to check against and the arithmetic has
to carry the whole weight.

Cabinets 3 and 4 have different menus and did answer SAS; they are checked
against the wire in test_screen_vs_sas.py, which is a stronger test than
anything here.

A transcription from a photograph is the kind of thing that is wrong in one
digit and believed for a year. These tests ask whether the numbers written
down can all be true at once: two ledgers that must agree, breakdowns that
must sum to their totals, a yield that must follow from bet and won, and a
bill ladder that must reproduce the bill total.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from saspy.constants import meter_name  # noqa: E402

SURVEY = Path(__file__).resolve().parent.parent / "survey"


def _master_accounting_cabinets():
    """The ones with Novomatic's TOTAL IN-OUT line, which is the discriminator:
    no other menu in the survey reports it."""
    if not SURVEY.is_dir():
        return []
    out = []
    for path in sorted(SURVEY.glob("cabinet-*-screens-*.json")):
        doc = json.loads(path.read_text())
        if "total_in_minus_out" in doc.get("readings_credits", {}):
            out.append(pytest.param(doc, id=f"cabinet-{doc['survey_position']}"))
    return out


CABINETS = _master_accounting_cabinets()
skip_private = pytest.mark.skipif(
    not CABINETS, reason="survey/ is private-repo only")


@skip_private
@pytest.mark.parametrize("doc", CABINETS)
def test_the_two_ledgers_agree(doc):
    """Total bet minus total won must equal total in minus total out. They
    are different pages measuring different things, and a transcription slip
    in any of the four breaks it."""
    r = doc["readings_credits"]
    assert r["total_in"] - r["total_out"] == r["total_in_minus_out"]
    assert r["total_bet"] - r["total_won"] == r["total_in_minus_out"]


@skip_private
@pytest.mark.parametrize("doc", CABINETS)
def test_the_credit_meter_was_zero(doc):
    """in - out = bet - won + current credits. The two differences being
    equal is the same statement as the credit meter reading zero, which the
    survey rule requires -- a cabinet being played must not be surveyed."""
    r = doc["readings_credits"]
    implied = ((r["total_in"] - r["total_out"])
               - (r["total_bet"] - r["total_won"]))
    assert implied == 0
    assert doc["predicted_table_c7"]["000C"]["value"] == 0


@skip_private
@pytest.mark.parametrize("doc", CABINETS)
def test_the_in_and_out_breakdowns_sum_to_their_totals(doc):
    # A null reading means the line is not on any page of THIS cabinet's
    # menu -- asset 7's Impera pages carry no token row at all. Such a line
    # contributes nothing to the sum by definition, and the sum still has to
    # close, which is what makes treating it as nothing safe rather than
    # convenient: a transcription that dropped a line that IS on the page
    # would fail here exactly as before.
    r = {k: (0 if v is None else v) for k, v in doc["readings_credits"].items()}
    assert (r["coins_in"] + r["token_in"] + r["bill_in"] + r["remote_in"]
            + r["card_in"] + r["ticket_in"]) == r["total_in"]
    # ATTENDANT-PAID WIN IS AN OUTPUT, AND IT IS NOT handpay_out.
    # handpay_out is C-7 0x0004, total CANCELLED credits, which asset 7's
    # page 9/9 reports as 0.00 -- while its In-Out Summary lists 3,672.16
    # under Outputs as "Handpay". Those are different money: a cancelled
    # credit is a cashout of the credit meter, an attendant-paid win is a
    # jackpot. Both leave the machine, so both belong in total_out, and
    # folding the win into handpay_out to make the sum close would have
    # misreported a jackpot as a cashout on every cabinet after it.
    assert (r["coins_out"] + r["handpay_out"] + r["card_out"]
            + r["ticket_out"] + r["bill_out"]
            + r.get("win_to_handpay", 0)
            + r.get("win_to_handpay_progressive", 0)) == r["total_out"]


@skip_private
@pytest.mark.parametrize("doc", CABINETS)
def test_the_cashless_page_agrees_with_the_summary_page(doc):
    """Page 5 breaks the card totals into cashable, nonrestricted, restricted
    and debit. They must add back to page 1's CARD IN and CARD OUT."""
    r = doc["readings_credits"]
    assert (r["cashless_card_in_cashable"] + r["cashless_card_in_nonrestricted"]
            + r["cashless_card_in_restricted"] + r["cashless_card_in_debit"]
            ) == r["card_in"]
    assert (r["cashless_card_out_cashable"]
            + r["cashless_card_out_nonrestricted"]
            + r["cashless_card_out_restricted"]) == r["card_out"]


@skip_private
@pytest.mark.parametrize("doc", CABINETS)
def test_the_win_breakdown_sums_to_total_won(doc):
    # win_to_handpay_progressive is optional and defaults to nothing: the
    # 2026-10-27 menus reported one attendant-paid figure, while asset 7's
    # Impera pages split it into paytable and progressive. Without the split
    # term the breakdown misses 337,901 credits on that cabinet -- which is
    # how the omission was found.
    r = {k: (0 if v is None else v) for k, v in doc["readings_credits"].items()}
    assert (r["game_win_to_credit"] + r["jp_win_to_credit"]
            + r["win_to_handpay"] + r.get("win_to_handpay_progressive", 0)
            + r["win_to_card"]
            + r["win_to_ticket"]) == r["total_won"]


@skip_private
@pytest.mark.parametrize("doc", CABINETS)
def test_the_machine_yield_is_truncated_not_rounded(doc):
    """Cabinet 1 reads 95,09 from 95.0982 and cabinet 2 reads 93,17 from
    93.1713. Both truncate. A test that rounded would fail against cabinet 1
    at 95,10 and look like a bad transcription."""
    if doc["machine_yield_percent"] is None:
        # Not every Novomatic menu carries a yield line. Asset 7's nine pages
        # do not, so the field is null -- and computing one to fill it would
        # be arithmetic presented as a reading, which is the exact failure
        # this whole file exists to prevent. Nothing to check.
        pytest.skip("this cabinet's pages show no machine yield")
    r = doc["readings_credits"]
    pct = 100 * r["total_won"] / r["total_bet"]
    truncated = int(pct * 100) / 100
    assert f"{truncated:.2f}".replace(".", ",") == doc["machine_yield_percent"]


@skip_private
@pytest.mark.parametrize("doc", CABINETS)
def test_the_bill_ladder_reproduces_bill_in(doc):
    """Page 7's per-channel counts must reproduce page 1's BILL IN and the
    page's own note total. On cabinet 2 that is 18 x $1 + 2 x $5 + 3 x $20 =
    $88.00 over 23 notes, which is a real check; on cabinet 1 every channel is
    zero, which is not."""
    r = doc["readings_credits"]
    channels = doc["bill_acceptor"]["channels"]
    money = sum(round(c["value"] * 100) * c["count"]
                for c in channels.values() if c["value"])
    assert money == r["bill_in"]
    assert sum(c["count"] for c in channels.values()) \
        == doc["counts"]["bill_notes_total"]


@skip_private
@pytest.mark.parametrize("doc", CABINETS)
def test_games_lost_is_derived_and_not_read(doc):
    """0007 is on none of the seven pages. It is games played minus games
    won, which is a prediction, and the generated block says so."""
    c = doc["counts"]
    p = doc["predicted_table_c7"]
    assert p["0007"]["value"] == c["total_games"] - c["total_games_won"]
    assert p["0007"]["from"].startswith("derived:")


@skip_private
@pytest.mark.parametrize("doc", CABINETS)
def test_every_prediction_is_named_and_sourced(doc):
    """A predicted value filed under the wrong name is worse than no
    prediction. Names are checked against Table C-7 rather than retyped from
    it; the generator takes them straight from METER_NAMES, so this guards
    against the generator being pointed at the wrong code."""
    for code, entry in doc["predicted_table_c7"].items():
        if code.startswith("_"):
            continue
        spec = meter_name(int(code, 16))
        assert spec, f"{code} is not a Table C-7 code"
        assert entry["name"] == spec, f"{code}: {entry['name']!r} != {spec!r}"
        assert entry.get("from", "").strip(), f"{code} has no provenance"


@skip_private
@pytest.mark.parametrize("doc", CABINETS)
def test_the_declared_c7_identities_hold_on_the_predictions(doc):
    """If the predicted set is internally inconsistent it is wrong before a
    cable is ever plugged in."""
    v = {c: e["value"] for c, e in doc["predicted_table_c7"].items()
         if not c.startswith("_")}

    def identity(total, *parts):
        """Check only when every term was actually predicted.

        A screen that does not carry a line yields no prediction for it, and
        asserting on a code that is absent tests the menu's layout rather
        than the transcription. Asset 7 predicts no 0003 or 0023 because its
        pages show cancelled credits nowhere -- they are genuinely zero and
        reported as attendant-paid WIN instead."""
        if total not in v or any(part not in v for part in parts):
            return
        assert v[total] == sum(v[part] for part in parts), \
            f"{total} != {' + '.join(parts)}"

    identity("0022", "0001", "0002")
    identity("0023", "0003", "0002")
    # Machine-paid AND attendant-paid. The three-term version held on the
    # 2026-10-27 cabinets only because nothing was ever attendant-paid there;
    # on asset 7 it misses 367,216 credits.
    identity("0022", "001C", "001D", "001E", "001F", "0020", "0021")
    if "000B" in v and "0058" in v:
        assert v["000B"] == v["0058"], "bills accepted vs bills to drop"


@skip_private
@pytest.mark.parametrize("doc", CABINETS)
def test_the_period_is_flagged_as_not_comparable_to_a_meter(doc):
    """These pages are a period report; C-7 meters are lifetime since RAM
    clear. Comparing them without establishing that the period start IS the
    clear is the mistake most likely to be made with these files."""
    assert "PERIOD report" in doc["period"]["WARNING"]
    assert "NOT comparable" in doc["period"]["WARNING"]


@skip_private
@pytest.mark.parametrize("doc", CABINETS)
def test_the_sas_only_prediction_is_marked_untested(doc):
    """The interesting claim in these files -- that the SAS-only block reads
    zero while the general electronic meters carry the full amount -- cannot
    be checked until the cabinet answers something. It must not sit beside
    the readings as though it had been."""
    d = doc["the_discriminating_prediction"]
    assert "0080-0093" in d["claim"]
    status = d["status"]
    if status.startswith("CONFIRMED") or status.startswith("REFUTED"):
        # A prediction that has MET A MACHINE is better than an untested one,
        # and the point of the original assertion was that an untested claim
        # must not sit beside the readings as though it had been checked. A
        # settled one must name what settled it. Asset 7 was the first
        # cabinet able to test this at all; it answered SAS, so 0080-0093
        # could finally be read.
        assert d.get("evidence"), \
            "a settled prediction must cite the snapshot that settled it"
        return
    assert status.startswith("UNTESTED")


@skip_private
@pytest.mark.parametrize("doc", CABINETS)
def test_actual_yield_is_not_confused_with_007F(doc):
    note = " ".join(doc["do_not_confuse"])
    assert "007F" in note and "THEORETICAL" in note


@skip_private
def test_the_two_cabinets_run_different_firmware():
    """The operator called both of these the same generation of Novomatic.
    They format numbers differently and lay their pages out differently, so
    they do not run the same accounting build. Recorded because 'same
    vendor, same generation' is about to be used to explain why both are
    silent on SAS, and it is not a safe premise."""
    by_pos = {d["survey_position"]: d for d in (p.values[0] for p in CABINETS)}
    if set(by_pos) < {1, 2}:
        pytest.skip("both Novomatic transcriptions not present")
    assert by_pos[1]["number_format"]["style"] != by_pos[2]["number_format"]["style"]
    assert by_pos[1]["page_layout_notes"] != by_pos[2]["page_layout_notes"]
