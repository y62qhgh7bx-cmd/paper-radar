#!/usr/bin/env python3
"""Sync starred/liked papers from marks.json into Zotero.

Reads marks.json (DOI -> {up, star, read, zotero_synced}) and docs/papers.json
(for matched keywords / title), classifies each un-synced up/star paper into a
Zotero collection using zotero_map.json, fetches full metadata from Crossref,
creates the item via the Zotero Web API, and marks it synced in marks.json.

Standard library only; no packages to install.
Usage: python3 sync_to_zotero.py
Required env vars: ZOTERO_API_KEY, ZOTERO_USER_ID
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
MARKS_PATH = ROOT / "marks.json"
PAPERS_PATH = ROOT / "docs" / "papers.json"
MAP_PATH = ROOT / "zotero_map.json"

ZOTERO_API_KEY = os.environ.get("ZOTERO_API_KEY", "").strip()
ZOTERO_USER_ID = os.environ.get("ZOTERO_USER_ID", "").strip()
ZOTERO_BASE = f"https://api.zotero.org/users/{ZOTERO_USER_ID}"


def load_json(path, default):
    if not path.exists():
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def http_json(url, method="GET", body=None, headers=None, retries=3):
    headers = headers or {}
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            if e.code in (409, 412) or attempt == retries - 1:
                return e.code, None
            time.sleep(2 * (attempt + 1))
        except Exception:
            if attempt == retries - 1:
                return None, None
            time.sleep(2 * (attempt + 1))


def crossref_metadata(doi):
    status, data = http_json(f"https://api.crossref.org/works/{doi}")
    if status != 200 or not data:
        return None
    m = data["message"]
    issued = m.get("issued", {}).get("date-parts", [[None]])[0]
    return {
        "title": (m.get("title") or [""])[0],
        "journal": (m.get("container-title") or [""])[0],
        "authors": [(a.get("given"), a.get("family")) for a in m.get("author", [])],
        "issued": issued,
        "ISSN": (m.get("ISSN") or [""])[0],
    }


def classify(paper_entry, rules, default_collection):
    """paper_entry: dict with 'title' and 'matched' (list of keywords). Returns
    (collection_key, tag) from the first matching rule, or the default."""
    title = (paper_entry.get("title") or "").lower()
    matched = set(k.lower() for k in paper_entry.get("matched", []))
    if any(w in title for w in ("review", "systematic review", "meta-analysis")):
        for rule in rules:
            if rule["name"] == "review":
                return rule["collection"], rule["tag"]
    for rule in rules:
        if rule["name"] in ("review",):
            continue
        if matched & set(k.lower() for k in rule["keywords"]):
            return rule["collection"], rule["tag"]
    return default_collection["collection"], default_collection["tag"]


def zotero_create_item(meta, doi, collection, tag):
    creators = [
        {"creatorType": "author", "firstName": fn or "", "lastName": ln or ""}
        for fn, ln in meta["authors"]
    ]
    item = {
        "itemType": "journalArticle",
        "title": meta["title"],
        "creators": creators,
        "publicationTitle": meta["journal"],
        "DOI": doi,
        "ISSN": meta["ISSN"],
        "url": f"https://doi.org/{doi}",
        "tags": [{"tag": tag}, {"tag": "paper-radar"}],
        "collections": [collection],
    }
    y = meta["issued"][0] if meta["issued"] else None
    if y:
        mo = meta["issued"][1] if len(meta["issued"]) > 1 else 1
        da = meta["issued"][2] if len(meta["issued"]) > 2 else 1
        item["date"] = f"{y}-{mo:02d}-{da:02d}"

    status, data = http_json(
        f"{ZOTERO_BASE}/items",
        method="POST",
        body=[item],
        headers={
            "Zotero-API-Key": ZOTERO_API_KEY,
            "Zotero-API-Version": "3",
            "Content-Type": "application/json",
        },
    )
    if status == 200 and data and data.get("successful"):
        return True
    print(f"  Zotero create failed: status={status} data={data}", file=sys.stderr)
    return False


def main():
    if not ZOTERO_API_KEY or not ZOTERO_USER_ID:
        print("ZOTERO_API_KEY / ZOTERO_USER_ID not set; skipping sync.", file=sys.stderr)
        return

    marks = load_json(MARKS_PATH, {})
    papers_data = load_json(PAPERS_PATH, {"papers": []})
    by_doi = {p["doi"]: p for p in papers_data.get("papers", [])}
    cfg = load_json(MAP_PATH, None)
    if cfg is None:
        print("zotero_map.json missing; aborting.", file=sys.stderr)
        return
    rules = cfg["rules"]
    default_collection = cfg["default"]

    pending = [
        doi for doi, m in marks.items()
        if (m.get("up") or m.get("star")) and not m.get("zotero_synced")
    ]
    print(f"{len(pending)} paper(s) pending Zotero sync")

    for doi in pending:
        paper_entry = by_doi.get(doi, {"title": "", "matched": []})
        meta = crossref_metadata(doi)
        if not meta:
            print(f"  skip {doi}: Crossref lookup failed")
            continue
        collection, tag = classify(paper_entry, rules, default_collection)
        ok = zotero_create_item(meta, doi, collection, tag)
        marks.setdefault(doi, {})["zotero_synced"] = ok
        print(f"  {doi}: {'synced -> ' + tag if ok else 'FAILED'}")
        time.sleep(1)  # be polite to both Crossref and Zotero

    save_json(MARKS_PATH, marks)
    print("Done.")


if __name__ == "__main__":
    main()
