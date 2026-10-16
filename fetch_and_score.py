#!/usr/bin/env python3
"""Paper Radar: fetch new journal articles from Crossref, score them against the
interest model, and write docs/papers.json.

Standard library only; no packages to install.
Usage: python3 fetch_and_score.py
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent
OUTPUT = ROOT / "docs" / "papers.json"

API = "https://api.crossref.org/journals/{issn}/works"

# Optional abstract translation via the Gemini API. If GEMINI_API_KEY is unset
# (e.g. local runs), translation is skipped and the site still works.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = "gemini-flash-lite-latest"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def http_get(url, retries=3):
    req = urllib.request.Request(url, headers={"User-Agent": "paper-radar/1.0"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except Exception as e:
            if attempt == retries - 1:
                print(f"  skipped ({e})", file=sys.stderr)
                return None
            time.sleep(2 * (attempt + 1))


def clean_abstract(raw):
    if not raw:
        return ""
    text = re.sub(r"<[^>]+>", " ", raw)          # strip JATS XML tags
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^(ABSTRACT|Abstract)\s*", "", text)
    return text


def reconstruct_abstract(inv):
    """Rebuild plain text from an OpenAlex abstract_inverted_index."""
    if not inv:
        return ""
    n = max(p for ps in inv.values() for p in ps) + 1
    words = [""] * n
    for w, positions in inv.items():
        for p in positions:
            if 0 <= p < n:
                words[p] = w
    return clean_abstract(" ".join(w for w in words if w))


def backfill_abstracts(papers, cfg):
    """Fill missing abstracts from OpenAlex (Taylor & Francis and some others
    deposit none to Crossref). Batched by DOI, mutates papers in place."""
    by_doi = {p["doi"].lower(): p for p in papers if not p["abstract"] and p.get("doi")}
    if not by_doi:
        return
    dois = list(by_doi)
    print(f"Backfilling {len(dois)} missing abstract(s) from OpenAlex ...")
    for i in range(0, len(dois), 40):
        chunk = dois[i:i + 40]
        params = {
            "filter": "doi:" + "|".join(chunk),
            "per-page": "40",
            "select": "doi,abstract_inverted_index",
            "mailto": cfg["mailto"],
        }
        data = http_get("https://api.openalex.org/works?" + urllib.parse.urlencode(params))
        if not data:
            continue
        for it in data.get("results", []):
            doi = (it.get("doi") or "").lower().replace("https://doi.org/", "")
            p = by_doi.get(doi)
            if p:
                p["abstract"] = reconstruct_abstract(it.get("abstract_inverted_index"))
        time.sleep(1)  # OpenAlex polite rate limit


def translate_zh(text):
    """Translate an abstract into Traditional Chinese via Gemini; "" on failure."""
    if not GEMINI_API_KEY or not text:
        return ""
    prompt = (
        "Translate the following academic abstract into Traditional Chinese "
        "(繁體中文, Taiwan usage). Keep technical terms accurate and do not add "
        "any preamble or notes; output only the translation.\n\n" + text
    )
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2},
    }).encode("utf-8")
    req = urllib.request.Request(
        GEMINI_URL,
        data=body,
        headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY},
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.load(resp)
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        except Exception as e:
            if attempt == 2:
                print(f"  translate skipped ({e})", file=sys.stderr)
                return ""
            time.sleep(5 * (attempt + 1))


def fetch_journal(journal, cfg):
    since = (datetime.now(timezone.utc) - timedelta(days=cfg["days_back"])).date()
    params = {
        "filter": f"from-created-date:{since},type:journal-article",
        "rows": str(cfg["max_per_journal"]),
        "sort": "created",
        "order": "desc",
        "mailto": cfg["mailto"],
    }
    url = API.format(issn=journal["issn"]) + "?" + urllib.parse.urlencode(params)
    data = http_get(url)
    if not data:
        return []
    papers = []
    for item in data["message"]["items"]:
        title = " ".join(item.get("title") or []).strip()
        title = re.sub(r"<[^>]+>", "", title)          # strip tags like <scp> found in Wiley titles
        title = re.sub(r"\s+", " ", title).strip()
        if not title:
            continue
        authors = [
            " ".join(filter(None, [a.get("given"), a.get("family")]))
            for a in item.get("author", [])
        ]
        created = item["created"]["date-time"][:10]
        papers.append({
            "doi": item["DOI"],
            "title": title,
            "abstract": clean_abstract(item.get("abstract")),
            "authors": authors,
            "journal": journal["name"],
            "url": f"https://doi.org/{item['DOI']}",
            "date": created,
        })
    return papers


def count_hits(keyword, text):
    return min(len(re.findall(re.escape(keyword), text)), 3)


def score_paper(paper, model):
    title = paper["title"].lower()
    abstract = paper["abstract"].lower()
    score = 0.0
    matched = []
    for kw, weight in model["keywords"].items():
        k = kw.lower()
        # short keywords (e.g. psm) use whole-word matching to avoid false hits
        if len(k) <= 4:
            pattern = r"\b" + re.escape(k) + r"\b"
            hits_t = min(len(re.findall(pattern, title)), 3)
            hits_a = min(len(re.findall(pattern, abstract)), 3)
        else:
            hits_t = count_hits(k, title)
            hits_a = count_hits(k, abstract)
        if hits_t or hits_a:
            score += weight * (hits_t * 2 + hits_a)
            matched.append(kw)
    score += model.get("journal_bonus", {}).get(paper["journal"], 0)
    paper["score"] = round(score, 1)
    paper["matched"] = matched
    return paper


def main():
    cfg = load_json(ROOT / "config.json")
    model = load_json(ROOT / "interest_model.json")

    # load previous data to preserve first_seen
    old = {}
    if OUTPUT.exists():
        for p in load_json(OUTPUT).get("papers", []):
            old[p["doi"]] = p

    seen = {}
    for journal in cfg["journals"]:
        print(f"Fetching {journal['name']} ...")
        for p in fetch_journal(journal, cfg):
            if p["doi"] not in seen:
                seen[p["doi"]] = p
        time.sleep(1)  # Crossref polite rate limit

    backfill_abstracts(list(seen.values()), cfg)

    cutoff = (datetime.now(timezone.utc) - timedelta(days=cfg["keep_days"])).date().isoformat()
    today = datetime.now(timezone.utc).date().isoformat()

    papers = []
    for doi, p in seen.items():
        p["first_seen"] = old.get(doi, {}).get("first_seen", today)
        papers.append(score_paper(p, model))
    # keep older entries that are not yet expired but fell outside this fetch (tracked journals only)
    tracked = {j["name"] for j in cfg["journals"]}
    for doi, p in old.items():
        if doi not in seen and p.get("first_seen", today) >= cutoff and p["journal"] in tracked:
            papers.append(score_paper(p, model))

    papers.sort(key=lambda p: (-p["score"], p["date"]), reverse=False)

    # Traditional-Chinese abstracts: reuse cached translations, translate the
    # rest (high-score first, so a daily quota cap still covers what matters).
    translated = 0
    for p in papers:
        if not p.get("abstract_zh"):
            p["abstract_zh"] = old.get(p["doi"], {}).get("abstract_zh", "")
        if not p["abstract_zh"] and GEMINI_API_KEY and p["abstract"]:
            p["abstract_zh"] = translate_zh(p["abstract"])
            if p["abstract_zh"]:
                translated += 1
                time.sleep(7)  # stay under ~10 requests/min on the free tier
    if GEMINI_API_KEY:
        print(f"Translated {translated} new abstract(s) to Traditional Chinese")

    result = {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "papers": papers,
    }
    OUTPUT.parent.mkdir(exist_ok=True)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    print(f"Done: {len(papers)} papers written to {OUTPUT}")


if __name__ == "__main__":
    main()
