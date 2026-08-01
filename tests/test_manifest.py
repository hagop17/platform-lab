"""The committed source snapshot must match its manifest.

Cheap (milliseconds, no network) and runs under pre-commit and CI, so a
truncated file or a hand-edited XML can't reach the index unnoticed.
"""

import hashlib
import json

import pytest

from rag.fetch_sources import MANIFEST_PATH, SOURCES_DIR

_MANIFEST = json.loads(MANIFEST_PATH.read_text())
_FILES = _MANIFEST["files"]


@pytest.mark.parametrize("filename", sorted(_FILES), ids=sorted(_FILES))
def test_source_file_matches_manifest(filename: str):
    recorded = _FILES[filename]
    body = (SOURCES_DIR / filename).read_bytes()

    assert len(body) == recorded["bytes"]
    assert hashlib.sha256(body).hexdigest() == recorded["sha256"]


def test_manifest_covers_every_committed_source_file():
    """Guards the other direction: a file added to the directory without being
    re-manifested would otherwise be silently ingested with no provenance."""
    on_disk = {p.name for p in SOURCES_DIR.iterdir() if p.name != MANIFEST_PATH.name}

    assert on_disk == set(_FILES)


def test_manifest_records_the_ecfr_issue_date():
    assert _MANIFEST["ecfr"]["ecfr_issue_date"]
    assert _MANIFEST["pulled_on"]


def test_irs_faq_is_labelled_as_weaker_provenance():
    """The FAQ has no issue date and no API. That's stated plainly, not glossed."""
    faq = _FILES["irs-faq.html"]

    assert faq["source"] == "IRS"
    assert "unversioned" in faq["provenance_note"]
