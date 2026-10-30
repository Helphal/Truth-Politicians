from __future__ import annotations

import html
import re
import unicodedata
from datetime import date
from html.parser import HTMLParser
from urllib.parse import urljoin


PARSER_VERSION = "senat-html-v2"
MONTHS = {
    "janvier": 1, "février": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
    "juillet": 7, "août": 8, "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12,
}


class _TextAndLinks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text = []
        self.links = []
        self._href = None
        self._link_text = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._link_text = []

    def handle_endtag(self, tag):
        if tag == "a" and self._href:
            self.links.append((" ".join(self._link_text).strip(), self._href))
            self._href = None
            self._link_text = []
        if tag in {"p", "li", "h1", "h2", "h3", "div", "tr"}:
            self.text.append("\n")

    def handle_data(self, data):
        cleaned = " ".join(data.split())
        if cleaned:
            self.text.append(cleaned + " ")
            if self._href:
                self._link_text.append(cleaned)


def _number(text: str, label_pattern: str) -> int | None:
    match = re.search(rf"(\d+)\s+{label_pattern}", text, re.IGNORECASE)
    return int(match.group(1)) if match else None


def _normalized_text(value: str) -> str:
    return " ".join(
        unicodedata.normalize("NFKC", value).replace("’", "'").split()
    ).casefold()


def extract_visible_text(data: bytes) -> str:
    decoded = data.decode("utf-8", errors="strict")
    parser = _TextAndLinks()
    parser.feed(decoded)
    return html.unescape(" ".join("".join(parser.text).split()))


def require_exact_quote(data: bytes, quote: str) -> None:
    """Refuse une citation qui n'est pas retrouvée dans le texte HTML capturé."""
    if not quote or _normalized_text(quote) not in _normalized_text(extract_visible_text(data)):
        raise ValueError("senat_exact_quote_not_found")


def find_nominative_vote(data: bytes, display_name: str) -> dict | None:
    """Lit les listes nominatives ``Pour/Contre/Abstention`` sans inférence."""
    decoded = data.decode("utf-8", errors="strict")
    wanted = _normalized_text(display_name)
    categories = {
        "pour": ("Pour", "for"),
        "contre": ("Contre", "against"),
        "abstention": ("Abstention", "abstention"),
        "abstentions": ("Abstentions", "abstention"),
        "n'a pas pris part au vote": ("N'a pas pris part au vote", "non_voting"),
        "n'ont pas pris part au vote": ("N'ont pas pris part au vote", "non_voting"),
    }
    found: set[tuple[str, str]] = set()
    for item in re.finditer(r"<li\b[^>]*>.*?</li>", decoded, re.IGNORECASE | re.DOTALL):
        fragment = item.group(0)
        label_match = re.search(r"<b\b[^>]*>(.*?)</b>", fragment, re.IGNORECASE | re.DOTALL)
        if not label_match:
            continue
        label = _normalized_text(html.unescape(re.sub(r"<[^>]+>", " ", label_match.group(1))))
        mapped = categories.get(label)
        if not mapped:
            continue
        fragment_parser = _TextAndLinks()
        fragment_parser.feed(fragment)
        visible = html.unescape(" ".join("".join(fragment_parser.text).split()))
        if wanted in _normalized_text(visible):
            found.add(mapped)
    if len(found) > 1:
        raise ValueError("senat_nominative_vote_ambiguous")
    if not found:
        return None
    source_category, normalized_category = found.pop()
    return {"source_category": source_category, "normalized_category": normalized_category}


def parse_scrutiny_html(data: bytes, public_url: str) -> dict:
    decoded = data.decode("utf-8", errors="strict")
    parser = _TextAndLinks()
    parser.feed(decoded)
    text = html.unescape(" ".join("".join(parser.text).split()))
    header = re.search(r"Scrutin\s+n[°o]\s*(\d+)\s*-\s*séance du\s+(\d{1,2})\s+([A-Za-zÀ-ÿ]+)\s+(\d{4})", text, re.IGNORECASE)
    if not header:
        raise ValueError("senat_scrutiny_header_not_found")
    month = MONTHS.get(header.group(3).lower())
    if not month:
        raise ValueError("senat_month_unknown")
    occurred = date(int(header.group(4)), month, int(header.group(2)))
    object_match = re.search(r'class="page-lead"[^>]*>(.*?)</p>', decoded, re.IGNORECASE | re.DOTALL)
    title = ""
    if object_match:
        title = html.unescape(re.sub(r"<[^>]+>", " ", object_match.group(1)))
        title = " ".join(title.split())
    result_block_match = re.search(r'R&eacute;sultat du scrutin(.*?)</section>', decoded, re.IGNORECASE | re.DOTALL)
    result_block = html.unescape(result_block_match.group(1)) if result_block_match else ""

    def result_number(label: str) -> int | None:
        match = re.search(rf'<strong[^>]*>(\d+)</strong>\s*{label}', result_block, re.IGNORECASE | re.DOTALL)
        return int(match.group(1)) if match else None

    abstention_match = re.search(r'Abstentions?\s*:\s*<span[^>]*>(\d+)</span>', result_block, re.IGNORECASE | re.DOTALL)
    official_text_url = ""
    for label, href in parser.links:
        if "dossier législatif" in label.lower() or "sur le texte" in label.lower():
            official_text_url = urljoin(public_url, href)
            break
    return {
        "source_identifier": header.group(1),
        "institution": "Sénat",
        "session": f"{occurred.year - 1}-{occurred.year}" if occurred.month < 10 else f"{occurred.year}-{occurred.year + 1}",
        "occurred_on": occurred.isoformat(),
        "official_title": title or "Objet non extrait",
        "official_vote_url": public_url,
        "official_text_url": official_text_url,
        "result_status": "adopted" if re.search(r"\bAdopté\b|Le Sénat a adopté", text, re.IGNORECASE) else "rejected",
        "registered": None,
        "voters": result_number("votants"),
        "expressed": result_number("suffrages exprimés"),
        "count_for": result_number("pour"),
        "count_against": result_number("contre"),
        "count_abstention": int(abstention_match.group(1)) if abstention_match else None,
        "nominative_status": "available" if "Analyse détaillée" in text else "partial",
        "parser_version": PARSER_VERSION,
    }
