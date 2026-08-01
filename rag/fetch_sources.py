"""Fetch phase for the tangible-property RAG corpus.

The ONLY module in this repo permitted to reach the network for RAG data.
A human runs it occasionally; it rewrites docs/tpr-sources/ and the manifest,
which are committed to git. rag/ingest.py then builds the index offline from
those committed files. The governing analogy is uv.lock: `uv lock` is a
deliberate human act, the build runs `uv sync --frozen` against the result.

Usage:
    uv run python -m rag.fetch_sources
"""

import hashlib
import json
from datetime import date
from pathlib import Path
from urllib.parse import quote

import httpx

API_BASE = "https://www.ecfr.gov/api/versioner/v1"
TITLE = 26
PART = "1"

# docs/tpr-sources/ lives in the repo (unlike the ChromaDB index, which is
# derived and stays outside it) — these files ARE the reviewable source text.
SOURCES_DIR = Path(__file__).resolve().parent.parent / "docs" / "tpr-sources"
MANIFEST_PATH = SOURCES_DIR / "_manifest.json"

# Whole sections, no subsection filtering. 1.168(i)-8 and 1.168(a)-1 are
# deliberately excluded: retrieval returns a fixed k=6, so depreciation and
# partial-disposition text would compete with repair-vs-capitalize text for
# those slots. More corpus is not monotonically better.
CFR_SECTIONS = {
    "1.263(a)-1": {
        "title": "Capital expenditures; in general",
        "covers": "General rule for capital expenditures and the de minimis safe harbor",
    },
    "1.263(a)-2": {
        "title": "Amounts paid to acquire or produce tangible property",
        "covers": "Acquisition/production costs — supports separate-asset questions",
    },
    "1.263(a)-3": {
        "title": "Amounts paid to improve tangible property",
        "covers": "Core betterment/adaptation/restoration test, unit of property, "
        "routine maintenance safe harbor",
    },
    "1.162-4": {
        "title": "Repairs",
        "covers": "The deduction side — counterpart to capitalization",
    },
}

IRS_FAQ_URL = (
    "https://www.irs.gov/businesses/small-businesses-self-employed/"
    "tangible-property-final-regulations"
)
IRS_FAQ_FILENAME = "irs-faq.html"

TIMEOUT = 60.0
# The versioner API serves plain clients fine, but the ecfr.gov *website* does
# not — send a normal UA so a future redirect to a bot-checked host is at least
# no worse off, and so irs.gov doesn't 403 us.
HEADERS = {"User-Agent": "Mozilla/5.0 (platform-lab rag/fetch_sources.py)"}

# If a CDN interstitial ever replaces a real response, it arrives as HTTP 200
# with plausible headers. Fail on the body, not just the status line.
BOT_CHECK_MARKERS = (
    b"Just a moment",
    b"cf-browser-verification",
    b"challenge-platform",
    b"Enable JavaScript and cookies to continue",
    b"captcha",
)


class FetchError(RuntimeError):
    """A source could not be retrieved, or came back looking wrong.

    Raised before anything is written to disk: a partial refresh is strictly
    worse than keeping the last-good snapshot intact.
    """


def _check(resp: httpx.Response, *, expect_content_type: str, must_contain: bytes) -> bytes:
    """Validate one response and return its raw bytes.

    Raw bytes, deliberately — not a parse-and-reserialize — so the recorded
    sha256 describes exactly what the API returned.
    """
    if resp.status_code != 200:
        raise FetchError(f"{resp.request.url}: expected HTTP 200, got {resp.status_code}")

    content_type = resp.headers.get("content-type", "").split(";")[0].strip()
    if content_type != expect_content_type:
        raise FetchError(
            f"{resp.request.url}: expected content-type {expect_content_type!r}, "
            f"got {content_type!r} — likely an error or interstitial page"
        )

    body = resp.content
    for marker in BOT_CHECK_MARKERS:
        if marker.lower() in body.lower():
            raise FetchError(
                f"{resp.request.url}: response body contains bot-check marker "
                f"{marker!r} — refusing to save an interstitial page as source text"
            )

    if must_contain not in body:
        raise FetchError(
            f"{resp.request.url}: response body is missing expected marker "
            f"{must_contain!r} — the endpoint's format may have changed"
        )

    return body


def fetch_title_dates(client: httpx.Client) -> tuple[str, str]:
    """Return (latest_issue_date, up_to_date_as_of) for Title 26.

    The issue date must be discovered, not guessed: the /full/ endpoint 404s on
    any date that isn't an actual issue date.
    """
    resp = client.get(f"{API_BASE}/titles.json")
    _check(resp, expect_content_type="application/json", must_contain=b"latest_issue_date")
    titles = resp.json()["titles"]
    for entry in titles:
        if entry["number"] == TITLE:
            return entry["latest_issue_date"], entry["up_to_date_as_of"]
    raise FetchError(f"Title {TITLE} not present in {API_BASE}/titles.json")


def section_url(issue_date: str, section: str) -> str:
    """The request URL, recorded in the manifest so a human can re-fetch by hand.

    Section labels contain parentheses ("1.263(a)-3"), so quote them.
    """
    return (
        f"{API_BASE}/full/{issue_date}/title-{TITLE}.xml"
        f"?part={PART}&section={quote(section, safe='')}"
    )


def web_view_url(section: str) -> str:
    return f"https://www.ecfr.gov/current/title-{TITLE}/section-{section}"


def fetch_section(client: httpx.Client, issue_date: str, section: str) -> bytes:
    resp = client.get(
        f"{API_BASE}/full/{issue_date}/title-{TITLE}.xml",
        params={"part": PART, "section": section},
    )
    return _check(resp, expect_content_type="text/xml", must_contain=b'TYPE="SECTION"')


def fetch_irs_faq(client: httpx.Client) -> bytes:
    resp = client.get(IRS_FAQ_URL)
    return _check(resp, expect_content_type="text/html", must_contain=b"<article")


def main() -> None:
    pulled_on = date.today().isoformat()

    # Fetch everything into memory and validate it all BEFORE touching disk.
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True, headers=HEADERS) as client:
        issue_date, up_to_date_as_of = fetch_title_dates(client)
        print(f"Title {TITLE} latest_issue_date={issue_date} up_to_date_as_of={up_to_date_as_of}")

        payloads: dict[str, bytes] = {}
        for section in CFR_SECTIONS:
            payloads[f"{section}.xml"] = fetch_section(client, issue_date, section)
            print(f"  fetched {section} ({len(payloads[f'{section}.xml']):,} bytes)")

        payloads[IRS_FAQ_FILENAME] = fetch_irs_faq(client)
        print(f"  fetched IRS FAQ ({len(payloads[IRS_FAQ_FILENAME]):,} bytes)")

    files: dict[str, dict] = {}
    for section, meta in CFR_SECTIONS.items():
        filename = f"{section}.xml"
        files[filename] = {
            "source": "eCFR",
            "citation": f"26 CFR {section}",
            "title": meta["title"],
            "covers": meta["covers"],
            "url": section_url(issue_date, section),
            "web_view": web_view_url(section),
            "bytes": len(payloads[filename]),
            "sha256": hashlib.sha256(payloads[filename]).hexdigest(),
        }
    files[IRS_FAQ_FILENAME] = {
        "source": "IRS",
        "citation": "IRS, Tangible Property Final Regulations (FAQ)",
        "title": "Tangible Property Final Regulations",
        "covers": "Plain-language election mechanics and procedures",
        "url": IRS_FAQ_URL,
        "web_view": IRS_FAQ_URL,
        "bytes": len(payloads[IRS_FAQ_FILENAME]),
        "sha256": hashlib.sha256(payloads[IRS_FAQ_FILENAME]).hexdigest(),
        "provenance_note": (
            "Weaker provenance than the XML: this page is unversioned. It has no "
            "issue date and no API, so 'pulled_on' is the only thing pinning it in "
            "time and its content can change silently between pulls."
        ),
    }

    manifest = {
        "description": (
            "Committed source text for the tangible-property RAG index. These files "
            "are BUILD INPUTS, not reference data: rag/ingest.py parses them to "
            "produce the ChromaDB index that ships in the Docker image and answers "
            "user queries. A refresh therefore propagates to retrieval results with "
            "no human in the loop."
        ),
        "integrity_note": (
            "Refreshing is not free. A re-pull can shift chunk boundaries, drop a "
            "subsection if eCFR restructures its markup, or — if a fetch silently "
            "returns an error page — yield an empty index that still answers HTTP 200 "
            "with no sources. Always review `git diff docs/tpr-sources/`, rebuild the "
            "index, and run the tests before committing a refresh."
        ),
        "verify_command": "uv run pytest tests/test_manifest.py",
        "generated_by": "uv run python -m rag.fetch_sources",
        "pulled_on": pulled_on,
        "ecfr": {
            "source": "eCFR — Office of the Federal Register / GPO",
            "api_base": API_BASE,
            "endpoint_pattern": "/full/{issue_date}/title-26.xml?part=1&section={section}",
            "ecfr_issue_date": issue_date,
            "ecfr_up_to_date_as_of": up_to_date_as_of,
        },
        "files": files,
        "reimport_notes": [
            "The issue date must be discovered from /titles.json, not guessed — the "
            "/full/ endpoint returns HTTP 404 for any date that is not a real issue date.",
            "The versioner API (ecfr.gov/api/versioner/v1/...) serves plain scripted "
            "clients. The ecfr.gov WEBSITE (ecfr.gov/current/...) returns a bot-check "
            "page. An earlier evaluation tested the website and wrongly concluded eCFR "
            "was unscriptable.",
            "Response bytes are written verbatim, never parsed and reserialized, so each "
            "sha256 describes exactly what the API returned.",
            "The fetch is atomic: every source is retrieved and validated in memory "
            "before any file is written, so a failure leaves the last-good snapshot intact.",
            "1.168(i)-8 (partial dispositions) and 1.168(a)-1 (MACRS) are excluded on "
            "purpose. Retrieval returns a fixed k=6 chunks; off-topic material would "
            "compete for those slots.",
        ],
    }

    SOURCES_DIR.mkdir(parents=True, exist_ok=True)
    for filename, body in payloads.items():
        (SOURCES_DIR / filename).write_bytes(body)
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")

    print(f"Wrote {len(payloads)} source files + manifest to {SOURCES_DIR}")


if __name__ == "__main__":
    main()
