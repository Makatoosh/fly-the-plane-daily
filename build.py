"""Build the daily aviation digest: fetch feeds, dedupe, filter to the last
24 hours, summarize with Claude, and render index.html.

Requires ANTHROPIC_API_KEY in the environment.
"""

import calendar
import datetime
import difflib
import html
import os
import re
import smtplib
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import anthropic
import feedparser
import requests
import yaml
from jinja2 import Environment, FileSystemLoader

SOURCES_FILE = "sources.yaml"
TEMPLATE_DIR = "templates"
PAGE_TEMPLATE = "index.html.j2"
EMAIL_TEMPLATE = "email.html.j2"
OUTPUT_FILE = "index.html"
LOG_FILE = "feed_health.log"
PAGE_URL = "https://makatoosh.github.io/fly-the-plane-daily/"

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465

USER_AGENT = "Mozilla/5.0 (compatible; aviation-digest-bot/1.0)"
FETCH_TIMEOUT = 15
LOOKBACK_HOURS = 24
TITLE_SIMILARITY_THRESHOLD = 0.72
SUMMARY_MODEL = os.environ.get("SUMMARY_MODEL", "claude-haiku-4-5-20251001")

TAG_RE = re.compile(r"<[^>]+>")


def log(message):
    timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    line = f"[{timestamp}] {message}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def load_sources(path=SOURCES_FILE):
    with open(path, "r") as f:
        return yaml.safe_load(f) or []


def strip_html(raw):
    if not raw:
        return ""
    return html.unescape(TAG_RE.sub("", raw)).strip()


def fetch_entries(source):
    name, url = source["name"], source["url"]
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=FETCH_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        log(f"FETCH FAILED  {name}: {e}")
        return []

    parsed = feedparser.parse(resp.content)
    if parsed.bozo and not parsed.entries:
        log(f"PARSE FAILED  {name}: {parsed.bozo_exception}")
        return []

    entries = []
    for entry in parsed.entries:
        published_struct = entry.get("published_parsed") or entry.get("updated_parsed")
        if not published_struct:
            continue
        published_dt = datetime.datetime.fromtimestamp(
            calendar.timegm(published_struct), tz=datetime.timezone.utc
        )
        entries.append(
            {
                "source": name,
                "title": strip_html(entry.get("title", "")),
                "link": entry.get("link", ""),
                "excerpt": strip_html(entry.get("summary", "")),
                "published_dt": published_dt,
            }
        )
    return entries


def normalize_title(title):
    lowered = title.lower()
    stripped = re.sub(r"[^a-z0-9\s]", "", lowered)
    return re.sub(r"\s+", " ", stripped).strip()


def filter_last_24h(entries, now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - datetime.timedelta(hours=LOOKBACK_HOURS)
    return [e for e in entries if e["published_dt"] >= cutoff]


def dedupe(entries):
    entries_sorted = sorted(entries, key=lambda e: e["published_dt"])
    kept = []
    kept_norms = []
    for entry in entries_sorted:
        norm = normalize_title(entry["title"])
        is_duplicate = any(
            difflib.SequenceMatcher(None, norm, seen).ratio() >= TITLE_SIMILARITY_THRESHOLD
            for seen in kept_norms
        )
        if not is_duplicate:
            kept.append(entry)
            kept_norms.append(norm)
    kept.sort(key=lambda e: e["published_dt"], reverse=True)
    return kept


def summarize(client, entry):
    excerpt = entry["excerpt"][:1500] or entry["title"]
    prompt = (
        "Rewrite this aviation news excerpt as exactly one clear sentence in "
        "your own words. No preamble, no quotation marks, just the sentence.\n\n"
        f"Headline: {entry['title']}\n"
        f"Excerpt: {excerpt}"
    )
    try:
        response = client.messages.create(
            model=SUMMARY_MODEL,
            max_tokens=120,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()
    except Exception as e:
        log(f"SUMMARY FAILED  {entry['source']} - {entry['title']}: {e}")
        return entry["excerpt"][:200] or entry["title"]


def render(template_name, items, source_count, **extra):
    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR))
    template = env.get_template(template_name)
    now = datetime.datetime.now(datetime.timezone.utc)
    return template.render(
        items=items,
        item_count=len(items),
        source_count=source_count,
        generated_at=now.strftime("%Y-%m-%d %H:%M UTC"),
        **extra,
    )


def send_email(html_body, item_count):
    sender = os.environ.get("GMAIL_ADDRESS")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    recipient = os.environ.get("DIGEST_RECIPIENT")

    if not (sender and password and recipient):
        log("EMAIL SKIPPED  GMAIL_ADDRESS/GMAIL_APP_PASSWORD/DIGEST_RECIPIENT not fully set")
        return

    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    message = MIMEMultipart("alternative")
    message["Subject"] = f"Fly The Plane Daily - {today} ({item_count} stories)"
    message["From"] = sender
    message["To"] = recipient
    message.attach(MIMEText(f"{item_count} aviation stories today. View as HTML for the full digest.", "plain"))
    message.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
            server.login(sender, password)
            server.sendmail(sender, [recipient], message.as_string())
        log(f"EMAIL SENT  to {recipient}")
    except smtplib.SMTPException as e:
        log(f"EMAIL FAILED  {e}")


def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        log("ERROR: ANTHROPIC_API_KEY not set")
        sys.exit(1)

    sources = load_sources()
    log(f"Loaded {len(sources)} sources")

    all_entries = []
    for source in sources:
        entries = fetch_entries(source)
        all_entries.extend(entries)

    recent = filter_last_24h(all_entries)
    log(f"{len(recent)} entries in the last {LOOKBACK_HOURS}h (of {len(all_entries)} fetched)")

    deduped = dedupe(recent)
    log(f"{len(deduped)} entries after dedup")

    client = anthropic.Anthropic()
    items = []
    for entry in deduped:
        items.append(
            {
                "title": entry["title"],
                "link": entry["link"],
                "source": entry["source"],
                "published": entry["published_dt"].strftime("%Y-%m-%d %H:%M UTC"),
                "summary": summarize(client, entry),
            }
        )

    page_html = render(PAGE_TEMPLATE, items, len(sources))
    with open(OUTPUT_FILE, "w") as f:
        f.write(page_html)
    log(f"Wrote {OUTPUT_FILE} with {len(items)} items")

    email_html = render(EMAIL_TEMPLATE, items, len(sources), page_url=PAGE_URL)
    send_email(email_html, len(items))


if __name__ == "__main__":
    main()
