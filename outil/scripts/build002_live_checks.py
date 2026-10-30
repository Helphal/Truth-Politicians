#!/usr/bin/env python3
"""Live checks bornés BUILD-002-R1 — pas de dump volumineux, pas de secret."""
from __future__ import annotations

import json
import os
import ssl
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# Endpoints publics à sonder (HEAD puis GET borné).
CHECKS = [
    ("AN_AMO10_XVII", "https://data.assemblee-nationale.fr/static/openData/repository/17/amo/deputes_actifs_mandats_actifs_organes/AMO10_deputes_actifs_mandats_actifs_organes.json.zip"),
    ("AN_VOTES_XVII", "https://data.assemblee-nationale.fr/static/openData/repository/17/loi/scrutins/Scrutins.json.zip"),
    ("AN_AMENDEMENTS_XVII", "https://data.assemblee-nationale.fr/static/openData/repository/17/loi/amendements_div_legis/Amendements.json.zip"),
    ("AN_DOSSIERS_XVII", "https://data.assemblee-nationale.fr/static/openData/repository/17/loi/dossiers_legislatifs/Dossiers_Legislatifs.json.zip"),
    ("AN_QUESTIONS_XVII", "https://data.assemblee-nationale.fr/static/openData/repository/17/questions/questions_ecrites/Questions_ecrites.json.zip"),
    ("AN_COMPTES_RENDUS_XVII", "https://data.assemblee-nationale.fr/static/openData/repository/17/vp/syceronbrut/syseron.xml.zip"),
    ("SENAT_SENATEURS", "https://data.senat.fr/data/senateurs/ODSEN_GENERAL.json"),
    ("SENAT_AKN_DEPOTS", "https://www.senat.fr/akomantoso/depots.xml"),
    ("SENAT_DOSLEG", "https://www.senat.fr/dossiers-legislatifs/textes-recents.html"),
    ("SENAT_SCRUTINS_PUBLICS", "https://www.senat.fr/scrutin-public/scr2025.html"),
    ("SENAT_COMPTES_RENDUS", "https://www.senat.fr/seances/seances.html"),
    ("SENAT_QUESTIONS_BASILE", "https://www.senat.fr/basile/baseRequete.do"),
    ("SENAT_AMELI", "https://data.senat.fr/category/ameli/"),
    ("ELYSEE_DISCOURS", "https://www.elysee.fr/"),
    ("GOUV_DISCOURS", "https://www.gouvernement.fr/"),
]


def probe(url: str, *, max_bytes: int = 4096) -> dict:
    req = Request(url, method="GET", headers={"User-Agent": "TruthPoliticians-livecheck/0.1", "Range": f"bytes=0-{max_bytes-1}"})
    ctx = ssl.create_default_context()
    try:
        with urlopen(req, timeout=25, context=ctx) as resp:
            raw = resp.read(max_bytes)
            ctype = resp.headers.get("Content-Type", "")
            return {
                "http_status": resp.status,
                "content_type": ctype.split(";")[0].strip(),
                "bytes_sampled": len(raw),
                "ok": resp.status in {200, 206},
                "format_hint": _hint(ctype, raw),
            }
    except HTTPError as exc:
        return {"http_status": exc.code, "ok": False, "error": "HTTPError", "content_type": exc.headers.get("Content-Type", "")}
    except URLError as exc:
        return {"http_status": None, "ok": False, "error": type(exc.reason).__name__ if exc.reason else "URLError", "message": str(exc.reason)[:200]}
    except Exception as exc:  # noqa: BLE001
        return {"http_status": None, "ok": False, "error": type(exc).__name__, "message": str(exc)[:200]}


def _hint(ctype: str, raw: bytes) -> str:
    c = (ctype or "").lower()
    if "json" in c or raw[:1] in (b"{", b"["):
        return "json"
    if "xml" in c or raw.lstrip()[:5] in (b"<?xml", b"<feed", b"<rss", b"<html"):
        if b"<html" in raw[:200].lower() or "html" in c:
            return "html"
        return "xml"
    if "zip" in c or raw[:2] == b"PK":
        return "zip"
    if "html" in c:
        return "html"
    return "opaque"


def main() -> int:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "runtime" / "source-checks"
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for feed_id, url in CHECKS:
        print(f"probe {feed_id} ...", flush=True)
        result = probe(url)
        rows.append({"feed_id": feed_id, "url": url, **result})
        print(f"  -> {result}", flush=True)
    payload = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "network_mode": "direct_urllib_bounded",
        "max_sample_bytes": 4096,
        "bulky_content_committed": False,
        "secrets_committed": False,
        "checks": rows,
        "summary": {
            "total": len(rows),
            "ok": sum(1 for r in rows if r.get("ok")),
            "failed": sum(1 for r in rows if not r.get("ok")),
        },
    }
    path = out_dir / "LIVE_CHECKS.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md = ["# Live checks BUILD-002-R1", "", "| feed | HTTP | format | ok |", "|---|---|---|---|"]
    for r in rows:
        md.append(f"| `{r['feed_id']}` | {r.get('http_status')} | {r.get('format_hint') or r.get('error')} | {r.get('ok')} |")
    (out_dir / "LIVE_CHECKS.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("wrote", path)
    return 0 if payload["summary"]["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
