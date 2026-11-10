"""COMMANDS.md must stay true, because a stale commands list is worse than
none: it is read when someone is standing at a cabinet and does not want to
think.

Two things rot. Tools get renamed or removed, and -- the mistake that
prompted this file -- a command gets written with the wrong first argument.
sql_poll_logger.py is the only tool here that takes a config file; every
other tool that talks to the machine takes the device path. Both directions
of that confusion have cost real bench time, including once from a command
written into a chat message by Claude.

COMMANDS.md lives only in the private repository, so these tests skip
cleanly in the code-only mirror.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "COMMANDS.md"

# The single tool whose first positional argument is a config file.
CONFIG_TOOLS = {"sql_poll_logger.py"}
# Tools whose first argument is neither a port nor a config.
OTHER_TOOLS = {"capture_diff.py", "soak_report.py", "commission_gateway.py"}
# Tools that DISCOVER the SAS address instead of being told one. They take a
# device path, so they are not OTHER_TOOLS, but requiring them to assert an
# address inverts the rule below: address_scan.py has no --address flag at
# all, because finding the address is the entire tool. Exempt from the
# default-address rule, and from nothing else.
ADDRESS_DISCOVERING_TOOLS = {"address_scan.py"}

INVOCATION = re.compile(
    r"python3 examples/(?P<tool>[a-z_0-9]+\.py)(?P<rest>[^\n|`]*)")


def _invocations():
    if not DOC.exists():
        pytest.skip("COMMANDS.md is private-repo only")
    text = DOC.read_text()
    # Section 6 lists invocations that are known NOT to work, on purpose.
    good, _, _bad = text.partition("## 6. Invocations that did NOT work")
    return [(m.group("tool"), m.group("rest").strip()) for m in INVOCATION.finditer(good)]


# Every file somebody might copy a command out of. COMMANDS.md was the
# first, but the mistake is not specific to it: MANUAL.md and the examples'
# own docstrings carry far more invocations, and a wrong one there is copied
# just as readily.
COPYABLE = ("COMMANDS.md", "MANUAL.md", "POLL_TEST_TOOL_MANUAL.md",
            "README.md", "POLL_TEST_TOOL_WARNING.md",
            # Public-mirror-only documents. Absent here, so they skip in
            # the private tree and are checked in the mirror -- which is
            # the tree a developer copies commands out of.
            "INSTALL.md")


def _every_invocation():
    """(source, tool, first argument) for every command in every file a
    reader might copy from, including the examples' docstrings."""
    out = []
    sources = [ROOT / name for name in COPYABLE]
    sources += sorted((ROOT / "examples").glob("*.py"))
    for path in sources:
        if not path.is_file():
            continue
        text = path.read_text()
        # COMMANDS.md section 6 lists failing invocations on purpose.
        if path.name == "COMMANDS.md":
            text = text.partition("## 6. Invocations that did NOT work")[0]
        for m in INVOCATION.finditer(text):
            rest = m.group("rest").strip().split()
            out.append((path.name, m.group("tool"), rest[0] if rest else ""))
    return out


def test_no_document_anywhere_confuses_a_port_with_a_config():
    """The error that prompted this file, checked across every copyable
    source rather than just the one it happened in.
    """
    problems = []
    for source, tool, first in _every_invocation():
        if tool in OTHER_TOOLS:
            continue
        if tool in CONFIG_TOOLS and first.startswith("/dev/"):
            problems.append(f"{source}: {tool} given a device path ({first})")
        if tool not in CONFIG_TOOLS and first.endswith((".ini", ".cfg", ".conf")):
            problems.append(f"{source}: {tool} given a config file ({first})")
    assert not problems, "\n".join(problems)


def test_no_document_anywhere_names_a_tool_that_is_gone():
    """Stale documentation sends somebody after a file that is not there.

    THE PUBLIC MIRROR IS EXEMPT, and deliberately. Since 2026-11-10 the
    mirror drops the 28 tools tool_tree.py classifies lab-only, so MANUAL.md
    names three of them -- meter_fingerprint, ticket_meter_identities,
    enabled_features_probe -- that the public tree does not carry. Those
    mentions sit inside prose sections about what the machines settled, not
    in per-tool sections, so filtering them out would delete the finding to
    remove the footnote.

    The guard keeps its full force where documentation is actually edited.
    tool_tree.py is itself lab-only, so its absence IS the mirror.
    """
    if not (ROOT / "deploy" / "tool_tree.py").is_file():
        pytest.skip("public mirror: lab-only tools are absent by design")
    missing = {f"{source}: {tool}" for source, tool, _ in _every_invocation()
               if not (ROOT / "examples" / tool).is_file()}
    assert not missing, missing


def test_every_tool_named_actually_exists():
    for tool, _rest in _invocations():
        assert (ROOT / "examples" / tool).is_file(), f"COMMANDS.md names a missing tool: {tool}"


def test_no_command_passes_a_config_where_a_port_belongs():
    """The mistake this file exists to stop."""
    for tool, rest in _invocations():
        if tool in CONFIG_TOOLS or tool in OTHER_TOOLS:
            continue
        first = rest.split()[0] if rest.split() else ""
        assert not first.endswith((".ini", ".cfg", ".conf")), (
            f"COMMANDS.md gives {tool} a config file ({first}); it takes a device path")


def test_no_command_passes_a_port_where_a_config_belongs():
    """And the opposite, which is the direction that wasted a session."""
    for tool, rest in _invocations():
        if tool not in CONFIG_TOOLS:
            continue
        first = rest.split()[0] if rest.split() else ""
        assert not first.startswith("/dev/"), (
            f"COMMANDS.md gives {tool} a device path ({first}); it reads a config file")


def test_port_taking_tools_do_not_rely_on_the_default_address():
    """The default is 1 and this cabinet is 1, so omitting it works here and
    silently will not elsewhere.

    --scan satisfies this too, and more strongly: it finds the address by
    polling rather than asserting one. The floor survey defaulted to address
    1 for weeks and reported every cabinet that was not on it as a dead
    cable (HANDOVER open item 11), which is the failure this rule exists to
    prevent -- so a tool that discovers the address is not an exception to
    the rule, it is the rule done properly.

    ADDRESS_DISCOVERING_TOOLS is that same reasoning applied to a tool whose
    whole job is the discovery. address_scan.py defines no --address flag, so
    the only way to satisfy this check literally would be to write a flag
    that does not exist. The rule asks "could this silently use the default
    address?", and a scan over an explicit --range cannot.
    """
    for tool, rest in _invocations():
        if tool in CONFIG_TOOLS or tool in OTHER_TOOLS:
            continue
        if tool in ADDRESS_DISCOVERING_TOOLS:
            continue
        if not rest.startswith("/dev/"):
            continue  # a placeholder like <port>, fine
        assert "--address" in rest or "--scan" in rest, \
            f"COMMANDS.md runs {tool} with neither --address nor --scan"


def test_no_real_validation_numbers_are_committed_here():
    """lab_tickets.json is gitignored because validation numbers identify
    tickets a real property issued. COMMANDS.md must reference the file, not
    inline its contents -- an 18-digit run of numbers here would defeat that.
    """
    if not DOC.exists():
        pytest.skip("COMMANDS.md is private-repo only")
    text = DOC.read_text()
    assert not re.search(r"\b\d{2}-\d{4}-\d{4}-\d{4}-\d{4}\b", text)
    assert not re.search(r"\b\d{18}\b", text)
    assert "lab_tickets.json" in text, "it should still point at the file"


def test_the_flag_names_it_teaches_are_real():
    """The restricted/nonrestricted inversion is the easiest thing to get
    wrong, so the flags COMMANDS.md teaches must be the flags that exist.
    """
    if not DOC.exists():
        pytest.skip("COMMANDS.md is private-repo only")
    text = DOC.read_text()
    push = (ROOT / "examples" / "aft_push_credits.py").read_text()
    for flag in ("--amount", "--restricted", "--nonrestricted"):
        assert flag in text
        assert f'"{flag}"' in push, f"COMMANDS.md teaches {flag}, which aft_push_credits does not define"
    cashout = (ROOT / "examples" / "aft_cashout.py").read_text()
    assert '"--cashable-only"' in cashout
