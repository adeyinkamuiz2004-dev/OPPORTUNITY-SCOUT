#!/usr/bin/env python3
"""
Opportunity Scout
Scans opportunity sites and search feeds every day for internships, fellowships,
scholarships, MOOCs, certificates and research-writing programmes that suit
political science graduates. Scores each item, finds deadlines, removes
duplicates, and publishes a daily digest (HTML, Markdown, CSV, Telegram, email).

Usage:
    python scout.py                 # normal run
    python scout.py --deep          # also open each good link to read the deadline
    python scout.py --no-notify     # build files only, send nothing
    python scout.py --selftest      # offline test of scoring and deadline logic
"""
import argparse
import csv
import hashlib
import html
import os
import re
import smtplib
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import quote_plus, urlparse, urlunparse, parse_qsl, urlencode

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

ROOT = Path(__file__).parent
DATA = ROOT / "data"
DOCS = ROOT / "docs"
UA = "Mozilla/5.0 (compatible; OpportunityScout/1.0; personal research use)"

MONTHS = (r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
          r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?")
DATE_DMY = re.compile(rf"(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({MONTHS})\.?,?\s*(\d{{4}})?", re.I)
DATE_MDY = re.compile(rf"({MONTHS})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s*(\d{{4}}))?", re.I)
DEADLINE_KEY = re.compile(
    r"(deadline|closing date|closes?|apply by|apply before|applications? (?:close|due|end)|"
    r"last date|submit(?:ted)? by|due date|until)", re.I)


@dataclass
class Item:
    title: str
    url: str
    summary: str = ""
    source: str = ""
    published: date | None = None
    weight: float = 1.0
    category: str = "Other"
    score: float = 0.0
    deadline: date | None = None
    tags: list = field(default_factory=list)
    why: list = field(default_factory=list)
    is_new: bool = True
    deep_ok: bool = True


# ---------------------------------------------------------------- helpers
def clean_text(s: str) -> str:
    if not s:
        return ""
    s = BeautifulSoup(s, "html.parser").get_text(" ")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def norm_url(u: str) -> str:
    p = urlparse(u)
    q = [(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith(("utm_", "fbclid", "ref"))]
    return urlunparse((p.scheme, p.netloc.lower(), p.path.rstrip("/"), "", urlencode(q), ""))


def item_id(title: str) -> str:
    key = re.sub(r"[^a-z0-9]", "", title.lower())
    return hashlib.sha1(key.encode()).hexdigest()[:16]


_rx_cache = {}


def has(term: str, text: str) -> bool:
    rx = _rx_cache.get(term)
    if rx is None:
        rx = re.compile(r"(?<![a-z0-9])" + re.escape(term.lower()) + r"(?![a-z0-9])")
        _rx_cache[term] = rx
    return bool(rx.search(text))


def parse_date_snippet(snip: str, today: date) -> date | None:
    """Turn '15th November 2026' / 'Nov 3' into a date. Missing years are guessed."""
    for rx, order in ((DATE_DMY, "dm"), (DATE_MDY, "md")):
        m = rx.search(snip)
        if not m:
            continue
        a, b, y = m.groups()
        day, mon = (int(a), b) if order == "dm" else (int(b), a)
        try:
            mon_num = dateparser.parse(mon[:3] + " 1 2000").month
            year = int(y) if y else today.year
            d = date(year, mon_num, day)
        except (ValueError, OverflowError):
            continue
        if not y and d < today and (today - d).days > 45:
            try:
                d = date(year + 1, mon_num, day)
            except ValueError:
                pass
        return d
    return None


def extract_deadline(text: str, today: date) -> date | None:
    """Find a date shortly after a deadline-type keyword."""
    found = []
    for m in DEADLINE_KEY.finditer(text):
        window = text[m.end(): m.end() + 60]
        d = parse_date_snippet(window, today)
        if d and today - timedelta(days=400) < d < today + timedelta(days=500):
            found.append(d)
    if not found:
        return None
    future = [d for d in found if d >= today]
    return min(future) if future else max(found)


# ---------------------------------------------------------------- scoring
def classify(text: str, cfg) -> str:
    for cat in cfg["categories"]:
        if any(has(t, text) for t in cat["terms"]):
            return cat["name"]
    return "Other"


def score_item(it: Item, cfg, today: date) -> bool:
    """Fill score/category/tags/deadline. Return False if the item should be dropped."""
    title_l = it.title.lower()
    text = f"{it.title}. {it.summary}".lower()

    if any(has(b, title_l) for b in cfg.get("title_blocklist", [])):
        return False
    if not any(has(t, text) for t in cfg["opportunity_terms"]):
        return False

    rel = 0
    hits = []
    for term, w in cfg["relevance_terms"].items():
        if has(term, text):
            # a hit in the title counts for more
            rel += w * (1.4 if has(term, title_l) else 1.0)
            hits.append(term)
    # research / academic writing skills count as relevant on their own
    rw = [t for t in cfg["research_writing_terms"] if has(t, text)]
    if rw:
        rel += 10
    rel = min(rel, 45)
    if rel < 8:
        return False  # nothing to do with the field

    score = rel
    why = []
    if hits:
        why.append("field match: " + ", ".join(hits[:4]))

    if rw:
        score += cfg["research_writing_boost"]
        it.tags.append("research-writing")
        why.append("research/writing skills: " + rw[0])

    scrubbed = text.replace("no application fee", "").replace("no registration fee", "")
    free_pts = 0
    for term, w in cfg["free_positive"].items():
        if has(term, text):
            free_pts = max(free_pts, w) if term != "free" else max(free_pts, w)
    if free_pts:
        score += free_pts
        it.tags.append("free/funded")
    for term, w in cfg["paid_negative"].items():
        if has(term, scrubbed):
            score -= w
            it.tags.append("may-cost-money")
            break

    for term, w in cfg["eligibility_positive"].items():
        if has(term, text):
            score += w
            if term in ("nigeria", "nigerian", "africa", "african"):
                it.tags.append("africa-eligible")
    for term, w in cfg["eligibility_negative"].items():
        if has(term, text):
            score -= w
            it.tags.append("check-eligibility")
            why.append("eligibility restriction: " + term)
    for term, w in cfg["level_positive"].items():
        if has(term, text):
            score += w
    for term, w in cfg["level_negative"].items():
        if has(term, text):
            score -= w
            it.tags.append("level-mismatch")

    it.deadline = extract_deadline(f"{it.title}. {it.summary}", today)
    if it.deadline:
        days = (it.deadline - today).days
        if days < 0 and cfg["settings"]["drop_expired"]:
            return False
        if 0 <= days <= 14:
            score += 4

    score *= it.weight
    it.score = round(score, 1)
    it.category = classify(text, cfg)
    it.tags = list(dict.fromkeys(it.tags))
    it.why = why
    return True


# ---------------------------------------------------------------- fetching
session = requests.Session()
session.headers.update({"User-Agent": UA})


def get(url: str, timeout=25):
    r = session.get(url, timeout=timeout)
    r.raise_for_status()
    return r


def entry_date(e) -> date | None:
    for key in ("published_parsed", "updated_parsed"):
        t = e.get(key)
        if t:
            return date(*t[:3])
    return None


def fetch_feed(name, url, weight, max_age, today, deep_ok=True):
    items = []
    parsed = feedparser.parse(get(url).content)
    if parsed.bozo and not parsed.entries:
        raise ValueError("feed could not be parsed")
    for e in parsed.entries:
        pub = entry_date(e)
        if pub and (today - pub).days > max_age:
            continue
        title = clean_text(e.get("title", ""))
        link = e.get("link", "")
        if not title or not link:
            continue
        summary = clean_text(e.get("summary", "") or e.get("description", ""))
        if "content" in e and e.content:
            summary = (summary + " " + clean_text(e.content[0].get("value", "")))[:3000]
        items.append(Item(title, link, summary, name, pub, weight, deep_ok=deep_ok))
    return items


def fetch_google_news(cfg, today):
    gn = cfg["google_news"]
    r = gn["region"]
    out, errors = [], 0
    for q in gn["queries"]:
        url = (f"https://news.google.com/rss/search?q={quote_plus(q + ' when:' + gn['when'])}"
               f"&hl={r['hl']}&gl={r['gl']}&ceid={r['ceid']}")
        try:
            for it in fetch_feed(f"Search: {q}", url, 0.8, cfg["settings"]["max_age_days"], today, deep_ok=False):
                if " - " in it.title:
                    it.title = it.title.rsplit(" - ", 1)[0]
                out.append(it)
        except Exception as ex:
            errors += 1
            print(f"  ! search failed [{q}]: {ex}", file=sys.stderr)
        time.sleep(0.4)
    return out, errors


def fetch_html_page(p, today):
    soup = BeautifulSoup(get(p["url"]).text, "html.parser")
    items = []
    for a in soup.select(p["link_selector"]):
        href = a.get("href")
        title = clean_text(a.get_text(" "))
        if href and title:
            href = requests.compat.urljoin(p["url"], href)
            items.append(Item(title, href, "", p["name"], None, p.get("weight", 1.0)))
    return items


def deep_read(it: Item, today: date, cfg):
    """Open the page: read the real deadline and a better description."""
    try:
        soup = BeautifulSoup(get(it.url, timeout=20).text, "html.parser")
    except Exception:
        return
    for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
        tag.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(" "))[:60000]
    d = extract_deadline(text, today)
    if d:
        it.deadline = d
    meta = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    if meta and meta.get("content") and len(meta["content"]) > len(it.summary) * 0.6:
        it.summary = clean_text(meta["content"])
    # extra tags from the full text
    low = text.lower()
    if "no application fee" in low or "free of charge" in low:
        if "free/funded" not in it.tags:
            it.tags.append("free/funded")


# ---------------------------------------------------------------- storage
def open_db():
    DATA.mkdir(exist_ok=True)
    db = sqlite3.connect(DATA / "seen.db")
    db.execute("""CREATE TABLE IF NOT EXISTS items(
        id TEXT PRIMARY KEY, title TEXT, url TEXT, source TEXT, category TEXT,
        score REAL, deadline TEXT, tags TEXT, summary TEXT, first_seen TEXT)""")
    return db


def save_items(db, items, today):
    for it in items:
        cur = db.execute(
            "INSERT OR IGNORE INTO items VALUES (?,?,?,?,?,?,?,?,?,?)",
            (item_id(it.title), it.title, it.url, it.source, it.category, it.score,
             it.deadline.isoformat() if it.deadline else None, ",".join(it.tags),
             it.summary[:400], today.isoformat()))
        it.is_new = cur.rowcount == 1
        if not it.is_new and it.deadline:  # update deadline if we learned one
            db.execute("UPDATE items SET deadline=? WHERE id=? AND deadline IS NULL",
                       (it.deadline.isoformat(), item_id(it.title)))
    db.commit()


def closing_soon(db, today, days):
    rows = db.execute(
        "SELECT title,url,source,category,score,deadline,tags,summary FROM items "
        "WHERE deadline IS NOT NULL AND deadline>=? AND deadline<=? ORDER BY deadline, score DESC",
        (today.isoformat(), (today + timedelta(days=days)).isoformat())).fetchall()
    out = []
    for t, u, s, c, sc, d, tg, sm in rows:
        it = Item(t, u, sm, s, category=c, score=sc, deadline=date.fromisoformat(d),
                  tags=[x for x in tg.split(",") if x])
        out.append(it)
    return out


def export_csv(db):
    rows = db.execute("SELECT first_seen,category,score,title,source,deadline,tags,url FROM items "
                      "ORDER BY first_seen DESC, score DESC").fetchall()
    with open(DATA / "all_opportunities.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["first_seen", "category", "score", "title", "source", "deadline", "tags", "url"])
        w.writerows(rows)


# ---------------------------------------------------------------- output
CAT_ORDER = ["Scholarship / Funded study", "Fellowship", "Internship / Trainee",
             "Course / MOOC / Certificate", "Competition / Call for papers", "Job / Opening", "Other"]


def short(s, n=230):
    s = s.strip()
    return s if len(s) <= n else s[:n].rsplit(" ", 1)[0] + "..."


def dl_label(it, today):
    if not it.deadline:
        return "deadline not found, check the page"
    days = (it.deadline - today).days
    return f"{it.deadline.strftime('%d %b %Y')} ({days} day{'s' if days != 1 else ''} left)"


def group(items):
    g = {}
    for it in items:
        g.setdefault(it.category, []).append(it)
    for v in g.values():
        v.sort(key=lambda i: -i.score)
    return [(c, g[c]) for c in CAT_ORDER if c in g]


def build_markdown(new_items, soon, today, stats):
    L = [f"# Opportunity digest, {today.strftime('%A %d %B %Y')}", "",
         f"{len(new_items)} new, {len(soon)} closing within the next two weeks. "
         f"({stats['fetched']} items scanned from {stats['sources_ok']} sources.)", ""]
    if soon:
        L += ["## Closing soon", ""]
        for it in soon:
            L.append(f"- **{it.title}** , {dl_label(it, today)}  \n  {it.url}")
        L.append("")
    for cat, its in group(new_items):
        L += [f"## {cat} ({len(its)})", ""]
        for it in its:
            tags = " ".join(f"`{t}`" for t in it.tags)
            L += [f"### {it.title}",
                  f"Score {it.score:.0f} | {it.source} | {dl_label(it, today)}  ",
                  f"{tags}  " if tags else "",
                  short(it.summary), f"{it.url}", ""]
    if stats["failed"]:
        L += ["---", "Sources that failed today: " + ", ".join(stats["failed"])]
    return "\n".join(L)


CSS = """
:root{--bg:#fafaf7;--fg:#1c1c1a;--mut:#6b6b66;--card:#fff;--bd:#e4e2da;--ac:#0b6e4f;--warn:#b45309}
@media (prefers-color-scheme:dark){:root{--bg:#161614;--fg:#ecebe6;--mut:#9a9890;--card:#1f1f1c;--bd:#33332f;--ac:#5fd3a5;--warn:#f0b35a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.55 system-ui,sans-serif}
main{max-width:860px;margin:0 auto;padding:20px 16px 60px}h1{font-size:1.5rem;margin:.2em 0}
.sub{color:var(--mut);margin-bottom:18px}h2{margin-top:32px;font-size:1.15rem;border-bottom:1px solid var(--bd);padding-bottom:6px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:12px 14px;margin:10px 0}
.card a.t{font-weight:600;color:var(--ac);text-decoration:none}.meta{font-size:.85rem;color:var(--mut);margin:4px 0}
.tag{display:inline-block;font-size:.72rem;border:1px solid var(--bd);border-radius:20px;padding:1px 8px;margin:2px 4px 0 0}
.tag.w{color:var(--warn);border-color:var(--warn)}.tag.g{color:var(--ac);border-color:var(--ac)}
.soon{border-left:4px solid var(--warn)}p{margin:6px 0}
input{width:100%;padding:10px;border-radius:8px;border:1px solid var(--bd);background:var(--card);color:var(--fg);font-size:1rem}
"""


def card_html(it, today):
    tags = "".join(
        f'<span class="tag {"g" if t in ("free/funded", "research-writing", "africa-eligible") else "w" if t in ("may-cost-money", "check-eligibility", "level-mismatch") else ""}">{html.escape(t)}</span>'
        for t in it.tags)
    return (f'<div class="card{" soon" if it.deadline and (it.deadline - today).days <= 14 else ""}">'
            f'<a class="t" href="{html.escape(it.url)}" target="_blank" rel="noopener">{html.escape(it.title)}</a>'
            f'<div class="meta">Score {it.score:.0f} · {html.escape(it.source)} · {html.escape(dl_label(it, today))}</div>'
            f'<p>{html.escape(short(it.summary))}</p>{tags}</div>')


def build_html(new_items, soon, today, stats):
    parts = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
             f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
             f"<title>Opportunity digest {today.isoformat()}</title><style>{CSS}</style></head><body><main>",
             f"<h1>Opportunity digest</h1><div class='sub'>{today.strftime('%A %d %B %Y')} · "
             f"{len(new_items)} new · {len(soon)} closing soon · {stats['fetched']} items scanned</div>",
             "<input id='q' placeholder='Filter this page (try: free, fellowship, writing)' "
             "oninput=\"document.querySelectorAll('.card').forEach(c=>c.style.display="
             "c.textContent.toLowerCase().includes(this.value.toLowerCase())?'':'none')\">"]
    if soon:
        parts.append("<h2>Closing soon</h2>")
        parts += [card_html(i, today) for i in soon]
    for cat, its in group(new_items):
        parts.append(f"<h2>{html.escape(cat)} ({len(its)})</h2>")
        parts += [card_html(i, today) for i in its]
    if stats["failed"]:
        parts.append(f"<p class='meta'>Sources that failed today: {html.escape(', '.join(stats['failed']))}</p>")
    parts.append("</main></body></html>")
    return "".join(parts)


def build_text(new_items, soon, today, limit_per_cat=6):
    L = [f"Opportunity digest, {today.strftime('%d %b %Y')}", f"{len(new_items)} new, {len(soon)} closing soon", ""]
    if soon:
        L.append("CLOSING SOON")
        for it in soon[:10]:
            L += [f"- {it.title} ({dl_label(it, today)})", f"  {it.url}"]
        L.append("")
    for cat, its in group(new_items):
        L.append(cat.upper())
        for it in its[:limit_per_cat]:
            tag = " [" + ", ".join(it.tags[:3]) + "]" if it.tags else ""
            L += [f"- {it.title}{tag}", f"  {dl_label(it, today)}", f"  {it.url}"]
        L.append("")
    return "\n".join(L)


# ---------------------------------------------------------------- notify
def send_telegram(text):
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return False
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3800:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    for c in chunks:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": c, "disable_web_page_preview": True}, timeout=30)
        if not r.ok:
            print("  ! telegram error:", r.text[:200], file=sys.stderr)
            return False
        time.sleep(1)
    return True


def send_email(subject, html_body, text_body):
    host, user, pw, to = (os.getenv(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "MAIL_TO"))
    if not (host and user and pw and to):
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587"))) as s:
        s.starttls()
        s.login(user, pw)
        s.sendmail(user, [x.strip() for x in to.split(",")], msg.as_string())
    return True


# ---------------------------------------------------------------- main
def run(args):
    cfg = yaml.safe_load(open(ROOT / args.config, encoding="utf-8"))
    S = cfg["settings"]
    today = datetime.now(timezone.utc).date()
    raw, failed, ok = [], [], 0

    print("Reading feeds...")
    for f in cfg.get("feeds", []):
        if f.get("enabled", True) is False:
            continue
        try:
            got = fetch_feed(f["name"], f["url"], f.get("weight", 1.0), S["max_age_days"], today)
            raw += got
            ok += 1
            print(f"  ok  {f['name']}: {len(got)} posts")
        except Exception as ex:
            failed.append(f["name"])
            print(f"  ERR {f['name']}: {ex}", file=sys.stderr)
        time.sleep(0.5)

    for p in cfg.get("html_pages", []):
        if p.get("enabled", True) is False:
            continue
        try:
            got = fetch_html_page(p, today)
            raw += got
            ok += 1
            print(f"  ok  {p['name']}: {len(got)} links")
        except Exception as ex:
            failed.append(p["name"])
            print(f"  ERR {p['name']}: {ex}", file=sys.stderr)

    if cfg.get("google_news", {}).get("enabled"):
        print("Running search queries...")
        got, errs = fetch_google_news(cfg, today)
        raw += got
        if got:
            ok += 1
        if errs:
            failed.append(f"{errs} search queries")

    print(f"Scoring {len(raw)} raw items...")
    seen_ids, kept = set(), []
    for it in raw:
        iid = item_id(it.title)
        if iid in seen_ids:
            continue
        seen_ids.add(iid)
        if score_item(it, cfg, today) and it.score >= S["min_score"]:
            kept.append(it)
    kept.sort(key=lambda i: -i.score)

    if args.deep:
        print("Reading pages for deadlines...")
        n = 0
        for it in kept:
            if n >= S["max_deep_fetch"]:
                break
            if it.deep_ok and not it.deadline:
                deep_read(it, today, cfg)
                n += 1
                time.sleep(S["request_delay"])
        if S["drop_expired"]:
            kept = [i for i in kept if not (i.deadline and i.deadline < today)]

    db = open_db()
    save_items(db, kept, today)
    new_items = [i for i in kept if i.is_new]
    soon = closing_soon(db, today, S["closing_soon_days"])
    export_csv(db)
    db.close()

    stats = {"fetched": len(raw), "sources_ok": ok, "failed": failed}
    DOCS.mkdir(exist_ok=True)
    (DOCS / "archive").mkdir(exist_ok=True)
    md = build_markdown(new_items, soon, today, stats)
    page = build_html(new_items, soon, today, stats)
    (DATA / f"digest-{today.isoformat()}.md").write_text(md, encoding="utf-8")
    (DOCS / "index.html").write_text(page, encoding="utf-8")
    (DOCS / "archive" / f"{today.isoformat()}.html").write_text(page, encoding="utf-8")
    print(f"Done: {len(new_items)} new, {len(soon)} closing soon. Files in docs/ and data/.")

    if not args.no_notify and (new_items or soon):
        text = build_text(new_items, soon, today)
        if send_telegram(text):
            print("Sent to Telegram.")
        if send_email(f"Opportunity digest {today.isoformat()}", page, text):
            print("Sent by email.")


# ---------------------------------------------------------------- selftest
def selftest():
    cfg = yaml.safe_load(open(ROOT / "config.yaml", encoding="utf-8"))
    today = date(2026, 10, 28)
    cases = [
        ("Fully funded fellowship in public policy for young Africans",
         "Open to Nigerian graduates in political science, governance and international relations. Deadline: 15th November 2026. Stipend provided.", True),
        ("Free online course: Academic Writing for Social Scientists (free certificate)",
         "A MOOC on research writing and literature review for graduates. Apply by Dec 5.", True),
        ("US Citizens only: Congressional internship in political science",
         "Applicants must be U.S. citizens only. Deadline 12 November 2026", None),
        ("Mechanical engineering scholarship in robotics", "Apply for a scholarship. Deadline 30 November 2026", False),
        ("Policy analysis internship, application closed",
         "Governance think tank. Deadline: 3 October 2026", False),
        ("Pay to enrol: paid course on international relations", "Tuition fee required. Early bird offer.", None),
    ]
    ok = True
    for title, summ, expect in cases:
        it = Item(title, "https://x.org/" + item_id(title), summ, "test")
        kept = score_item(it, cfg, today)
        shown = f"kept score={it.score:>5}  cat={it.category:<30} deadline={it.deadline} tags={it.tags}" if kept else "dropped"
        print(f"- {title[:60]:<60} -> {shown}")
        if expect is True and not (kept and it.score >= cfg['settings']['min_score']):
            ok = False
        if expect is False and kept:
            ok = False
    assert extract_deadline("Closing date: 3 Jan", date(2026, 12, 20)) == date(2027, 1, 3)
    assert extract_deadline("apply by March 14, 2027", today) == date(2027, 3, 14)
    print("\nSELFTEST", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--deep", action="store_true", help="open pages to read deadlines")
    ap.add_argument("--no-notify", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    sys.exit(selftest() if a.selftest else run(a) or 0)
