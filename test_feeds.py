"""Check every feed in sources.yaml and report which ones are alive.

Usage:
    python test_feeds.py

Prints an OK/DEAD line per source. Doesn't modify sources.yaml — remove
dead entries by hand once you've seen the report.
"""

import sys

import feedparser
import requests
import yaml

SOURCES_FILE = "sources.yaml"
TIMEOUT = 10
USER_AGENT = "Mozilla/5.0 (compatible; aviation-digest-bot/1.0)"


def load_sources(path=SOURCES_FILE):
    with open(path, "r") as f:
        return yaml.safe_load(f) or []


def check_feed(url):
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    except requests.RequestException as e:
        return False, f"request failed: {e}"

    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}"

    parsed = feedparser.parse(resp.content)
    if parsed.bozo and not parsed.entries:
        return False, f"invalid XML: {parsed.bozo_exception}"
    if not parsed.entries:
        return False, "parsed OK but zero entries"

    return True, f"{len(parsed.entries)} entries"


def main():
    sources = load_sources()
    if not sources:
        print(f"No sources found in {SOURCES_FILE}")
        sys.exit(1)

    dead = []
    for source in sources:
        name, url = source["name"], source["url"]
        ok, detail = check_feed(url)
        status = "OK  " if ok else "DEAD"
        print(f"[{status}] {name:<30} {detail}")
        if not ok:
            dead.append(name)

    print()
    if dead:
        print(f"{len(dead)} dead feed(s): {', '.join(dead)}")
        print("Remove these from sources.yaml.")
    else:
        print("All feeds OK.")


if __name__ == "__main__":
    main()
