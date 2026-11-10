"""Shared fixtures and helpers."""


# --- lab-only apparatus, absent from the public mirror ---------------------
#
# tool_tree.py sorts every tool into tiers and one is "LAB ONLY -- NOT
# MIRRORED". Since 2026-11-10 mirror_to_public.py honours that, so the
# public checkout the dev team clones genuinely does not contain soak
# scripts, survey apparatus or ticket_lab_authority.
#
# A test that READS one of those files therefore cannot run there. It must
# SKIP, not pass quietly: a test that goes green because its subject is
# missing is the "absence read as a measurement" shape this project has hit
# five times, and it would report the public suite as fully passing while
# silently testing less.
import pytest as _pytest


def requires_lab_file(path):
    """Skip unless `path` (a Path) is in this checkout. Returns it."""
    if not path.exists():
        _pytest.skip(f"{path.name} is lab-only and not in this checkout")
    return path
