"""Daily health check for the My Channels rows.

1. Official free links (stream/tv/mynews-*.json): checks each URL still returns a
   playlist. Uses no PenguPlay requests.
2. PenguPlay cards whose provider rotates IDs (ROTATING): asks PenguPlay for streams.
   When a card has none, it searches PenguPlay by name and swaps in the new ID if
   that one plays. Every PenguPlay request counts against MAX_PENGU_REQUESTS, a hard
   cap because the token's rate limit is shared with Tiago's TV viewing.

Writes alert.md when a channel has been down for THRESHOLD days in a row.
"""
import glob, gzip, json, os, re, time, urllib.parse, urllib.request

BASE = os.environ.get("PENGU_URL", "").rstrip("/").removesuffix("/manifest.json")
SKIP_PENGU = os.environ.get("SKIP_PENGU") == "1" or not BASE  # free-links-only run
HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.environ.get("HEALTH_FILE", "health.json")
THRESHOLD = 3
ROTATING = ("cinevid~", "daddylive~")
MAX_PENGU_REQUESTS = 16
MAX_SEARCHES = 3
UA = {"User-Agent": "ExoPlayerLib/2.19.1"}

pengu_used = searches_used = 0


def get(url, timeout=40, headers=None):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    body = urllib.request.urlopen(req, timeout=timeout).read()
    return gzip.decompress(body) if body[:2] == bytes([0x1F, 0x8B]) else body  # RTP sends gzip unasked


def pengu(path):
    """One PenguPlay request, or None when the daily budget is spent or it fails."""
    global pengu_used
    if pengu_used >= MAX_PENGU_REQUESTS:
        return None
    pengu_used += 1
    try:
        data = json.loads(get(f"{BASE}/{path}"))
    except Exception:
        data = None
    time.sleep(float(os.environ.get("PENGU_SLEEP", 8)))  # stay well under PenguPlay's rate limit
    return data


def live_streams(channel_id):
    """1 if a listed stream actually plays, 0 if none do. None = no verdict.
    Test-plays hit the stream's own host, not PenguPlay, so they cost no token requests."""
    data = pengu(f"stream/tv/{channel_id}.json")
    if data is None:
        return None
    streams = [s for s in data.get("streams", []) if s.get("url")]  # donate notice has no url
    for s in streams[:5]:
        if urllib.parse.urlsplit(s["url"]).hostname.endswith("pengu.uk"):
            return 1  # PenguPlay-hosted link: testing it would spend the token budget
        headers = (s.get("behaviorHints") or {}).get("proxyHeaders", {}).get("request")
        if url_ok(s["url"], headers):
            return 1
    print(f"{'':12} {len(streams)} listed, none of the first 5 played")
    return 0


def norm(name):
    return re.sub(r"[^a-z0-9]", "", name.lower())


def find_new_id(name, old_id):
    """Search PenguPlay for a same-name channel with a different ID that plays."""
    global searches_used
    if searches_used >= MAX_SEARCHES:
        return None
    searches_used += 1
    data = pengu(f"catalog/tv/pp-live-channels/search={urllib.parse.quote(name)}.json")
    if not data:
        return None
    provider = old_id.split(":", 1)[1].split("~")[0]
    found = [m for m in data.get("metas", []) if norm(m["name"]) == norm(name) and m["id"] != old_id]
    found.sort(key=lambda m: provider not in m["id"])  # same provider first
    for m in found[:2]:
        n = live_streams(m["id"])
        if n:
            return m, n
    return None


def url_ok(url, headers=None):
    for attempt in range(2):  # one retry: jmp2.uk redirects flap
        try:
            if get(url, timeout=25, headers=headers).lstrip()[:7] == b"#EXTM3U":
                return True
        except Exception:
            pass
        time.sleep(3)
    return False


catalogs, rows = {}, {}
for path in sorted(glob.glob(os.path.join(HERE, "catalog", "tv", "*.json"))):
    catalogs[path] = json.load(open(path, encoding="utf-8"))
    for m in catalogs[path]["metas"]:
        rows[m["id"]] = (os.path.basename(path)[:-5].replace("my-", "").replace("-", " ").title(), m["name"])

results, swapped = {}, {}

# 1. Official free links: one result per stream file (all its URLs must fail to count as down).
for path in sorted(glob.glob(os.path.join(HERE, "stream", "tv", "mynews-*.json"))):
    cid = os.path.basename(path)[:-5]
    urls = [s["url"] for s in json.load(open(path, encoding="utf-8"))["streams"] if s.get("url")]
    ok = any(url_ok(u) for u in urls)
    results[cid] = 1 if ok else 0
    rows.setdefault(cid, ("(not in a row)", cid))
    print(f"{rows[cid][0]:12} {rows[cid][1]:28} {'ok' if ok else 'DOWN'}")

# 2. PenguPlay cards on providers that rotate IDs.
pengu_cards = [] if SKIP_PENGU else [c for c in rows if c.startswith("pp-live:") and c.split(":", 1)[1].startswith(ROTATING)]
for cid in pengu_cards:
    results[cid] = live_streams(cid)
    print(f"{rows[cid][0]:12} {rows[cid][1]:28} {'no verdict' if results[cid] is None else results[cid]}")
# Searches only after every card has been checked, so they never eat a card's check.
for cid in [c for c in pengu_cards if results[c] == 0]:
    hit = find_new_id(rows[cid][1].removesuffix(" (backup)"), cid)  # search by the real channel name
    if hit:
        swapped[cid], results[cid] = hit
        print(f"{rows[cid][1]} -> new ID {hit[0]['id']} ({hit[1]} streams)")
print(f"PenguPlay requests used: {pengu_used}/{MAX_PENGU_REQUESTS}")

# Write the new IDs into the catalogs; keep our channel names.
for path, cat in catalogs.items():
    changed = False
    for i, m in enumerate(cat["metas"]):
        if m["id"] in swapped:
            cat["metas"][i] = {**swapped[m["id"]], "name": m["name"]}
            changed = True
    if changed:
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(json.dumps(cat, ensure_ascii=False))

state = json.load(open(STATE)) if os.path.exists(STATE) else {}
today = time.strftime("%Y-%m-%d")
new_state = {k: v for k, v in state.items() if k in rows and k not in results}  # keep unchecked cards' counts
alerts = []
for cid, n in results.items():
    row, name = rows[cid]
    prev = state.get(cid, {})
    days = prev.get("down_days", 0)
    if n is None:
        pass  # no verdict today: keep the count
    elif n == 0 and prev.get("checked") == today:
        pass  # second run today (manual re-run): count each day once
    else:
        days = days + 1 if n == 0 else 0
    new_state[swapped[cid]["id"] if cid in swapped else cid] = {"name": name, "down_days": days, "checked": today}
    # Alert on day 3, then a weekly reminder while it stays down.
    if n == 0 and days >= THRESHOLD and (days - THRESHOLD) % 7 == 0:
        where = "official link stopped working" if not cid.startswith("pp-live:") else "no replacement found in PenguPlay"
        alerts.append(f"- **{name}** ({row}): down for {days} days in a row ({where})")
json.dump(new_state, open(STATE, "w"), indent=1)

if pengu_cards and all(results[c] is None for c in pengu_cards):
    alerts.append("- **PenguPlay did not answer at all.** It may be down, or the addon link stopped working.")
if alerts:
    open("alert.md", "w").write(
        "My Channels daily check found a problem:\n\n" + "\n".join(alerts)
        + "\n\nAsk Claude: \"fix My Channels\" and paste this list.\n")
    print("\n".join(alerts))
