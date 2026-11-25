#!/usr/bin/env python3
"""nidhogg-newswire: read a few public news feeds once, log matches, and send
tier 1 matches to a phone through ntfy.sh.

Read-only. No trading, no logins, no accounts. Python standard library only.

    python poll.py              normal run (what GitHub Actions does)
    python poll.py --dry-run    fetch and print; send nothing, write nothing
    python poll.py --test       replay old real items and send ONE test push
    python poll.py --last24h    print the last 24 hours of tier 2 hits
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path

HERE = Path(__file__).resolve().parent
WATCHLIST = HERE / "watchlist.json"
SEEN = HERE / "seen.json"
ALERTS = HERE / "alerts.jsonl"
TEST_ITEMS = HERE / "test_items.json"

UA = "nidhogg-research"
# sec.gov wants a contact email in the User-Agent. It comes from the SEC_USER_AGENT setting
# (a GitHub repo secret) so no email is written in this public repo.
SEC_UA = os.environ.get("SEC_USER_AGENT", "").strip() or UA
TIMEOUT = 20          # seconds per request
PAUSE = 0.5           # seconds between any two requests (keeps sec.gov far under 5/second)
SEEN_DAYS = 14        # forget dedupe entries older than this
FAIL_PUSH_EVERY = 3600  # at most one "sources failed" push per hour

SEC_URL = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&count=100&output=atom"
FR_URL = "https://www.federalregister.gov/api/v1/documents.json?per_page=20&order=newest"
BBC_URL = "https://feeds.bbci.co.uk/news/business/rss.xml"
NYT_URL = "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml"
FAA_URL = "https://nasstatus.faa.gov/api/airport-status-information"
NWS_URL = "https://api.weather.gov/alerts/active?status=actual&message_type=alert&severity=Extreme"
# category + tags filter returns only the earnings series (~190 KB) instead of all 16,000+ series
KALSHI_URL = "https://api.elections.kalshi.com/trade-api/v2/series?category=Mentions&tags=Earnings"
NTFY_URL = "https://ntfy.sh/"


# ---------- time ----------

def _nth_sunday_utc(year, month, n):
    first = datetime(year, month, 1, tzinfo=timezone.utc)
    return first + timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))


def to_eastern(dt):
    """Convert any datetime to US Eastern (EST/EDT, US rules since 2007)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    u = dt.astimezone(timezone.utc)
    dst_start = _nth_sunday_utc(u.year, 3, 2) + timedelta(hours=7)   # 2:00 EST
    dst_end = _nth_sunday_utc(u.year, 11, 1) + timedelta(hours=6)    # 2:00 EDT
    hours = -4 if dst_start <= u < dst_end else -5
    return u.astimezone(timezone(timedelta(hours=hours)))


def iso_et(dt):
    return to_eastern(dt).isoformat(timespec="seconds")


def now_utc():
    return datetime.now(timezone.utc)


def eastern_midnight(date_str):
    y, m, d = (int(x) for x in date_str.split("-"))
    offset = to_eastern(datetime(y, m, d, 12, tzinfo=timezone.utc)).utcoffset()
    return datetime(y, m, d, tzinfo=timezone(offset))


# ---------- polite fetching ----------

class Fetcher:
    """One request at a time, honest User-Agent, timeout, pause between requests.
    A 403 or 429 means the host said no: skip it for the rest of this run."""

    def __init__(self):
        self.blocked = {}
        self.last = 0.0

    def request(self, url, ua=UA, data=None, headers=None):
        host = urllib.parse.urlsplit(url).hostname
        if host in self.blocked:
            raise RuntimeError(f"skipped: {host} returned HTTP {self.blocked[host]} earlier this run")
        wait = PAUSE - (time.monotonic() - self.last)
        if wait > 0:
            time.sleep(wait)
        hdrs = {"User-Agent": ua}
        hdrs.update(headers or {})
        req = urllib.request.Request(url, data=data, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                self.blocked[host] = e.code
                raise RuntimeError(f"HTTP {e.code} from {host}; skipping this host for this run") from None
            raise RuntimeError(f"HTTP {e.code} from {host}") from None
        except urllib.error.URLError as e:
            raise RuntimeError(f"network error for {host}: {e.reason}") from None
        finally:
            self.last = time.monotonic()


# ---------- sources -> list of items ----------
# item = {id, source, kind, title, text, url, ts (datetime), items (8-K only)}

ATOM = "{http://www.w3.org/2005/Atom}"


def parse_sec(raw):
    out, seen = [], set()
    for e in ET.fromstring(raw).iter(ATOM + "entry"):
        eid = e.findtext(ATOM + "id") or ""
        acc = eid.split("accession-number=")[-1]
        if acc in seen:
            continue
        seen.add(acc)
        title = (e.findtext(ATOM + "title") or "").strip()
        summary = unescape(e.findtext(ATOM + "summary") or "")
        link = e.find(ATOM + "link")
        company = re.sub(r"^\S+\s*-\s*", "", title)
        company = re.sub(r"\s*\(\d{10}\).*$", "", company)
        out.append({
            "id": "sec:" + acc, "source": "SEC 8-K", "kind": "8-K",
            "title": title, "text": company,
            "url": link.get("href") if link is not None else "",
            "ts": datetime.fromisoformat(e.findtext(ATOM + "updated")),
            "items": re.findall(r"Item (\d+\.\d+)", summary),
        })
    return out


def parse_fr(raw, fr_keywords):
    out = []
    for r in json.loads(raw).get("results", []):
        title = r.get("title") or ""
        text = title + " " + (r.get("abstract") or "")
        presidential = r.get("type") == "Presidential Document"
        if not presidential and not fr_keywords.search(text):
            continue
        out.append({
            "id": "fr:" + r.get("document_number", ""), "source": "Federal Register",
            "kind": "presidential" if presidential else "fr",
            "title": title, "text": r.get("abstract") or "",
            "url": r.get("html_url") or "",
            "ts": eastern_midnight(r["publication_date"]),  # the API gives a date only
        })
    return out


def parse_rss(raw, source):
    out = []
    for it in ET.fromstring(raw).iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        desc = re.sub(r"<[^>]+>", " ", unescape(it.findtext("description") or ""))
        pub = it.findtext("pubDate")
        out.append({
            "id": f"{source}:" + ((it.findtext("guid") or "").strip() or link),
            "source": source, "kind": "news",
            "title": title, "text": desc, "url": link,
            "ts": parsedate_to_datetime(pub) if pub else now_utc(),  # pubDate is GMT
        })
    return out


def parse_faa(raw):
    root = ET.fromstring(raw)
    upd = root.findtext("Update_Time") or ""
    try:
        ts = datetime.strptime(upd.strip(), "%a %b %d %H:%M:%S %Y GMT").replace(tzinfo=timezone.utc)
    except ValueError:
        ts = now_utc()
    out = []
    for dtype in root.iter("Delay_type"):
        name = (dtype.findtext("Name") or "").strip()
        for el in dtype.iter():
            arpt = el.findtext("ARPT")
            if arpt is None:
                continue
            reason = (el.findtext("Reason") or "").strip()
            out.append({
                "id": f"faa:{name}:{arpt}:{reason}", "source": "FAA", "kind": "faa",
                "title": f"{name}: {arpt.strip()} ({reason})", "text": "",
                "url": "https://nasstatus.faa.gov/", "ts": ts,
            })
    return out


def parse_nws(raw):
    out = []
    for f in json.loads(raw).get("features", []):
        p = f.get("properties", {})
        title = p.get("headline") or p.get("event") or "NWS alert"
        out.append({
            "id": "nws:" + (p.get("id") or f.get("id", "")), "source": "NWS", "kind": "nws",
            "title": title, "text": f"{p.get('event', '')} {p.get('areaDesc', '')}",
            "url": p.get("@id") or f.get("id", ""),
            "ts": datetime.fromisoformat(p["sent"]) if p.get("sent") else now_utc(),
        })
    return out


def kalshi_name(title):
    """Turn a messy Kalshi series title into a company name, e.g.
    'What will Walmart say during their earnings call?' -> 'Walmart',
    'Palantir earnings mention' -> 'Palantir'."""
    t = title.strip()
    m = re.match(r"(?i)what will (.+?) say\b", t)
    if m:
        t = m.group(1)
    t = re.sub(r"(?i)(\s*\b(earnings?|call|mention|menti\w*|m)\b)+\s*$", "", t)
    t = re.sub(r"(?i)[,\s]+(inc|corp|corporation|co|ltd|plc)\.?$", "", t)
    return t.strip(" .,")


def fetch_kalshi_names(fetcher, prefix):
    names, cursor = set(), ""
    for _ in range(20):
        url = KALSHI_URL + (f"&cursor={urllib.parse.quote(cursor)}" if cursor else "")
        d = json.loads(fetcher.request(url))
        for s in d.get("series") or []:
            if s.get("ticker", "").startswith(prefix):
                n = kalshi_name(s.get("title", ""))
                if len(n) >= 2:
                    names.add(n)
        cursor = d.get("cursor") or ""
        if not cursor:
            break
    return sorted(names)


# ---------- matching ----------

def word_regex(words, flags=re.I):
    alts = "|".join(re.escape(w).replace(r"\ ", " ").replace(" ", r"\s+") for w in words)
    return re.compile(r"\b(?:" + alts + r")(?:s|es)?\b", flags)


class Matcher:
    def __init__(self, cfg, kalshi_names):
        t1, t2 = cfg["tier1"], cfg["tier2"]
        self.t1_companies = [(name, word_regex(aliases)) for name, aliases in t1["companies"].items()]
        self.t1_keywords = word_regex(t1["keywords"])
        self.items_8k = set(t2["sec_8k_items"])
        names = sorted(set(kalshi_names) | set(t2.get("extra_companies", [])))
        # short all-caps names (AMD, KKR) must match case exactly; others ignore case
        self.t2_companies = [
            (n, word_regex([n], 0 if (n.isupper() and len(n) <= 4) else re.I)) for n in names
        ]

    def classify(self, item):
        """Return (tier, reason) or None."""
        text = item["title"] + " " + item["text"]
        company_text = item["text"] if item["kind"] == "8-K" else text
        for name, rx in self.t1_companies:
            if rx.search(company_text):
                return 1, name
        m = self.t1_keywords.search(text)
        if m:
            return 1, m.group(0).lower()
        if item["kind"] == "8-K":
            hit = [i for i in item.get("items", []) if i in self.items_8k]
            if hit:
                return 2, "8-K item " + ", ".join(hit)
        for name, rx in self.t2_companies:
            if rx.search(company_text):
                return 2, "Kalshi earnings-mention company: " + name
        if item["kind"] == "presidential":
            return 2, "presidential document"
        if item["kind"] == "fr":
            return 2, "Federal Register keyword"
        if item["kind"] == "nws":
            return 2, "NWS extreme alert"
        return None


# ---------- files ----------

def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def append_alerts(records):
    with ALERTS.open("a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def record(item, tier, reason, detected):
    return {
        "item_time": iso_et(item["ts"]), "detected": iso_et(detected), "tier": tier,
        "source": item["source"], "title": item["title"], "url": item["url"], "match": reason,
    }


# ---------- phone push ----------

def push(fetcher, topic, title, sentence, link):
    safe_title = title.encode("ascii", "ignore").decode()[:80] or "Nidhogg newswire"
    body = f"{sentence}\n{link}".encode("utf-8")
    headers = {"Title": safe_title}
    if link:
        headers["Click"] = link
    fetcher.request(NTFY_URL + urllib.parse.quote(topic, safe=""), data=body, headers=headers)


def push_for(item, reason):
    when = to_eastern(item["ts"]).strftime("%H:%M ET")
    headline = item["title"] if len(item["title"]) <= 200 else item["title"][:197] + "..."
    return f"{reason} ({item['source']})", f"{item['source']} at {when}: {headline.rstrip('.')}.", item["url"]


# ---------- one run ----------

def collect(fetcher, cfg):
    """Fetch every source. Returns (items, kalshi_names, failures)."""
    fr_kw = word_regex(cfg["federal_register_keywords"])
    jobs = [
        ("SEC 8-K", lambda: parse_sec(fetcher.request(SEC_URL, ua=SEC_UA))),
        ("Federal Register", lambda: parse_fr(fetcher.request(FR_URL), fr_kw)),
        ("BBC Business", lambda: parse_rss(fetcher.request(BBC_URL), "BBC Business")),
        ("NYT Business", lambda: parse_rss(fetcher.request(NYT_URL), "NYT Business")),
        ("FAA", lambda: parse_faa(fetcher.request(FAA_URL))),
        ("NWS", lambda: parse_nws(fetcher.request(NWS_URL))),
    ]
    items, failures = [], []
    for name, job in jobs:
        try:
            got = job()
            items.extend(got)
            print(f"  ok    {name}: {len(got)} items")
        except Exception as e:  # one bad source must not stop the others
            failures.append((name, str(e) or type(e).__name__))
            print(f"  FAIL  {name}: {e}")
    try:
        names = fetch_kalshi_names(fetcher, cfg["tier2"]["kalshi_series_prefix"])
        print(f"  ok    Kalshi series list: {len(names)} company names")
    except Exception as e:
        names = []
        failures.append(("Kalshi series list", str(e) or type(e).__name__))
        print(f"  FAIL  Kalshi series list: {e}")
    return items, names, failures


def run(dry_run):
    cfg = load_json(WATCHLIST, None)
    fetcher = Fetcher()
    started = now_utc()
    print(f"nidhogg-newswire run at {iso_et(started)}{' (dry run)' if dry_run else ''}")
    items, names, failures = collect(fetcher, cfg)
    matcher = Matcher(cfg, names)

    seen_exists = SEEN.exists()
    seen = load_json(SEEN, {"items": {}, "last_fail_push": None})
    before = json.dumps(seen, sort_keys=True)
    detected = now_utc()

    hits = []
    for it in items:
        c = matcher.classify(it)
        if c and it["id"] not in seen["items"]:
            hits.append((it, c[0], c[1]))
            seen["items"][it["id"]] = iso_et(detected)

    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if dry_run:
        for it, tier, reason in hits:
            print(f"  [tier {tier}] {it['source']} | {iso_et(it['ts'])} | {reason} | {it['title'][:110]}")
            print(f"           {it['url']}")
        print(f"{len(hits)} new hits (tier 1: {sum(1 for h in hits if h[1] == 1)}). Nothing sent, nothing written.")
        return 0

    if not seen_exists:
        # First ever run: remember what is already out there, but do not flood the phone with old news.
        print(f"First run: marked {len(hits)} existing items as seen. No pushes, nothing logged.")
        hits = []

    records = [record(it, tier, reason, detected) for it, tier, reason in hits]
    for name, why in failures:
        records.append({"item_time": iso_et(detected), "detected": iso_et(detected), "tier": "error",
                        "source": name, "title": "source failed: " + why, "url": "", "match": ""})

    if hits and not topic:
        print("WARNING: NTFY_TOPIC is not set, so tier 1 hits are logged but not pushed.")
    for it, tier, reason in hits:
        print(f"  [tier {tier}] {it['source']} | {reason} | {it['title'][:110]}")
        if tier == 1 and topic:
            try:
                push(fetcher, topic, *push_for(it, reason))
            except Exception as e:
                print(f"  push failed: {e}")

    if len(failures) >= 3 and topic:
        last = seen.get("last_fail_push")
        if not last or (detected - datetime.fromisoformat(last)).total_seconds() >= FAIL_PUSH_EVERY:
            run_url = ""
            if os.environ.get("GITHUB_RUN_ID"):
                run_url = (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
                           f"{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/{os.environ['GITHUB_RUN_ID']}")
            names_failed = ", ".join(n for n, _ in failures)
            try:
                push(fetcher, topic, f"Newswire: {len(failures)} sources failed",
                     f"These sources failed in this run: {names_failed}.", run_url)
                seen["last_fail_push"] = iso_et(detected)
            except Exception as e:
                print(f"  failure push failed: {e}")

    cutoff = detected - timedelta(days=SEEN_DAYS)
    seen["items"] = {k: v for k, v in seen["items"].items() if datetime.fromisoformat(v) >= cutoff}

    if records:
        append_alerts(records)
    if json.dumps(seen, sort_keys=True) != before or not seen_exists:
        SEEN.write_text(json.dumps(seen, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"done: {len(hits)} new hits, {len(failures)} failed sources.")
    return 0


def test_mode():
    cfg = load_json(WATCHLIST, None)
    fetcher = Fetcher()
    print("Test mode: replaying old real items through the matching logic.")
    try:
        names = fetch_kalshi_names(fetcher, cfg["tier2"]["kalshi_series_prefix"])
        print(f"  Kalshi series list: {len(names)} company names")
    except Exception as e:
        names = []
        print(f"  Kalshi series list failed ({e}); tier 2 company matching skipped")
    matcher = Matcher(cfg, names)
    items = load_json(TEST_ITEMS, [])
    tier1 = []
    for it in items:
        it["ts"] = datetime.fromisoformat(it["ts"])
        c = matcher.classify(it)
        label = f"tier {c[0]} ({c[1]})" if c else "no match"
        print(f"  {label:<48} {it['source']} | {it['title'][:90]}")
        if c and c[0] == 1:
            tier1.append((it, c[1]))
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        print("NTFY_TOPIC is not set, so no test push was sent.")
        return 1
    link = tier1[0][0]["url"] if tier1 else ""
    sentence = (f"Test push: {len(tier1)} of {len(items)} replayed items matched tier 1, "
                f"so phone alerts are working")
    if tier1:
        sentence += f"; example: {tier1[0][0]['title'][:120].rstrip('.')}"
    push(fetcher, topic, "Nidhogg newswire test", sentence + ".", link)
    print("Sent ONE test push. Nothing written.")
    return 0


def last24h():
    cutoff = now_utc() - timedelta(hours=24)
    n = 0
    if ALERTS.exists():
        for line in ALERTS.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            if r.get("tier") == 2 and datetime.fromisoformat(r["detected"]) >= cutoff:
                n += 1
                print(f"{r['detected']}  {r['source']:<16} {r['match']}\n    {r['title']}\n    {r['url']}")
    print(f"{n} tier 2 hits in the last 24 hours.")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", help="fetch and print; send nothing, write nothing")
    g.add_argument("--test", action="store_true", help="replay old real items and send ONE test push")
    g.add_argument("--last24h", action="store_true", help="print the last 24 hours of tier 2 hits")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if a.last24h:
        return last24h()
    if a.test:
        return test_mode()
    return run(dry_run=a.dry_run)


if __name__ == "__main__":
    sys.exit(main())
