"""Daily health check for the My Channels rows.

Asks PenguPlay for streams on every channel in catalog/tv/*.json and tracks
consecutive failed days in health.json. When a channel has no streams, it looks
the channel up by name in PenguPlay (some providers rotate their IDs) and swaps
in the new ID if that one plays. Writes alert.md when a channel has been down
for THRESHOLD days in a row (or when PenguPlay itself is unreachable).
"""
import glob, json, os, re, time, urllib.parse, urllib.request

BASE = os.environ["PENGU_URL"].rstrip("/").removesuffix("/manifest.json")
HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.environ.get("HEALTH_FILE", "health.json")
THRESHOLD = 3
UA = {"User-Agent": "ExoPlayerLib/2.19.1"}


def get(url, timeout=40):
    req = urllib.request.Request(url, headers=UA)
    return urllib.request.urlopen(req, timeout=timeout).read()


def live_streams(channel_id):
    """Number of streams PenguPlay lists for the channel. None = request failed."""
    for attempt in range(2):
        try:
            streams = json.loads(get(f"{BASE}/stream/tv/{channel_id}.json")).get("streams", [])
            break
        except Exception:
            time.sleep(30)
    else:
        return None
    # One request per channel: count real streams (the donate notice has no url).
    return sum(1 for s in streams if s.get("url"))


def norm(name):
    return re.sub(r"[^a-z0-9]", "", name.lower())


def find_new_id(name, old_id):
    """Search PenguPlay for a channel with the same name and a different ID that plays.
    Returns (meta, streams) or None."""
    q = urllib.parse.quote(name)
    try:
        metas = json.loads(get(f"{BASE}/catalog/tv/pp-live-channels/search={q}.json")).get("metas", [])
    except Exception:
        return None
    time.sleep(8)
    provider = old_id.split("~")[0]
    found = [m for m in metas if norm(m["name"]) == norm(name) and m["id"] != old_id]
    found.sort(key=lambda m: not m["id"].startswith(provider + "~"))  # same provider first
    for m in found[:2]:
        n = live_streams(m["id"])
        time.sleep(8)
        if n:
            return m, n
    return None


catalogs, channels = {}, []
for path in sorted(glob.glob(os.path.join(HERE, "catalog", "tv", "*.json"))):
    row = os.path.basename(path)[:-5].replace("my-", "").title()
    catalogs[path] = json.load(open(path, encoding="utf-8"))
    for m in catalogs[path]["metas"]:
        if m["id"].startswith("pp-live:"):  # free official rows don't use PenguPlay
            channels.append((row, m["name"], m["id"]))

state = json.load(open(STATE)) if os.path.exists(STATE) else {}
results, errors, swapped = {}, 0, {}
for row, name, cid in channels:
    n = live_streams(cid)
    errors += n is None
    print(f"{row:9} {name:24} {'ERROR' if n is None else n}")
    time.sleep(8)  # stay well under PenguPlay's rate limit
    if n == 0:
        hit = find_new_id(name, cid)
        if hit:
            meta, n = hit
            swapped[cid] = meta
            print(f"{'':9} -> new ID {meta['id']} ({n} streams)")
    results[cid] = n

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

today = time.strftime("%Y-%m-%d")
new_state, alerts = {}, []
for row, name, cid in channels:
    days = state.get(cid, {}).get("down_days", 0)
    if results[cid] is None:
        pass  # PenguPlay refused the request: no verdict today, keep the count
    elif results[cid] == 0 and state.get(cid, {}).get("checked") == today:
        pass  # second run today (manual re-run): count each day once
    else:
        days = days + 1 if results[cid] == 0 else 0
    new_state[swapped[cid]["id"] if cid in swapped else cid] = {"name": name, "down_days": days, "checked": today}
    # Alert on day 3, then a weekly reminder while it stays down.
    if days >= THRESHOLD and (days - THRESHOLD) % 7 == 0:
        alerts.append(f"- **{name}** ({row}): no working stream for {days} days in a row (no replacement found in PenguPlay)")
json.dump(new_state, open(STATE, "w"), indent=1)

if errors == len(channels):
    alerts = ["- **PenguPlay did not answer at all.** It may be down, or the addon link stopped working."]
if alerts:
    open("alert.md", "w").write(
        "My Channels daily check found a problem:\n\n" + "\n".join(alerts)
        + "\n\nAsk Claude: \"fix My Channels\" and paste this list.\n")
    print("\n".join(alerts))
