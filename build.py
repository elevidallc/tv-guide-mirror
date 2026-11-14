#!/usr/bin/env python3
"""Build one M3U playlist + one merged XMLTV guide per country.

Sources (all public, all free):
  * BuddyChewChew/app-m3u-generator  - Pluto TV / Samsung TV Plus / Plex FAST packs per region
  * matthuisman/i.mjh.nz             - the matching XMLTV guides (uncompressed)
  * bstillitano/free-iptv            - US extras (local affiliates etc.) + its guide (gz)
  * iptv-org/iptv                    - Portugal (no FAST pack exists) + epgshare01 PT guide, mapped by name
Outputs: playlists/<cc>.m3u and guides/<cc>.xml
"""
import gzip, io, os, re, sys, unicodedata, urllib.request
import xml.etree.ElementTree as ET

RAW_BASE = os.environ.get("RAW_BASE", "https://raw.githubusercontent.com/elevidallc/tv-guide-mirror/main")
BC = "https://raw.githubusercontent.com/BuddyChewChew/app-m3u-generator/main/playlists/"
MJH = "https://i.mjh.nz/"
FREE = "https://raw.githubusercontent.com/bstillitano/free-iptv/main/"
IPTVORG = "https://iptv-org.github.io/iptv/countries/"
EPGSHARE_PT = "https://epgshare01.online/epgshare01/epg_ripper_PT1.xml.gz"

COUNTRIES = {
    "us": {"name": "United States", "packs": ["plutotv_us", "samsungtvplus_us", "plex_us"],
           "guides": ["PlutoTV/us", "SamsungTVPlus/us", "Plex/us"], "extra_m3u": [FREE+"playlists/us-1.m3u", FREE+"playlists/us-2.m3u"],
           "extra_guides_gz": [FREE+"epg/epg.xml.gz"]},
    "ca": {"name": "Canada", "packs": ["plutotv_ca", "samsungtvplus_ca", "plex_ca"], "guides": ["PlutoTV/ca", "SamsungTVPlus/ca", "Plex/ca"]},
    "uk": {"name": "United Kingdom", "packs": ["plutotv_gb", "samsungtvplus_gb", "plex_gb"], "guides": ["PlutoTV/gb", "SamsungTVPlus/gb", "Plex/gb"]},
    "br": {"name": "Brazil", "packs": ["plutotv_br"], "guides": ["PlutoTV/br"]},
    "es": {"name": "Spain", "packs": ["plutotv_es", "samsungtvplus_es", "plex_es"], "guides": ["PlutoTV/es", "SamsungTVPlus/es", "Plex/es"]},
    "pt": {"name": "Portugal", "packs": [], "guides": [], "iptvorg": "pt", "epgshare_gz": EPGSHARE_PT},
}

def get(url, binary=False, tries=3):
    req = urllib.request.Request(url, headers={"User-Agent": "tv-guide-mirror/1.0"})
    last = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                data = r.read()
            return data if binary else data.decode("utf-8", "replace")
        except Exception as e:  # slow hosts (epgshare01) time out now and then
            last = e; print(f"  ! fetch failed ({i+1}/{tries}) {url.rsplit('/',1)[-1]}: {e}")
    raise last

def parse_m3u(text):
    """Return list of (extinf_line, url)."""
    out, ext = [], None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF"):
            ext = line
        elif line and not line.startswith("#") and ext:
            out.append((ext, line)); ext = None
    return out

def norm(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)              # drop "(720p)" "[Not 24/7]"
    s = re.sub(r"\b(hd|sd|fhd|4k|portugal|pt)\b", " ", s.lower())
    return re.sub(r"[^a-z0-9]+", "", s)

def merge_guides(xml_texts):
    root = ET.Element("tv"); seen = set(); nch = npr = 0
    for t in xml_texts:
        try:
            tree = ET.fromstring(t.encode("utf-8") if isinstance(t, str) else t)
        except ET.ParseError as e:
            print("  ! guide parse error, skipped:", e); continue
        for ch in tree.findall("channel"):
            cid = ch.get("id")
            if cid and cid not in seen:
                seen.add(cid); root.append(ch); nch += 1
        for pr in tree.findall("programme"):
            root.append(pr); npr += 1
    return root, nch, npr, seen

def write_guide(cc, root):
    os.makedirs("guides", exist_ok=True)
    ET.ElementTree(root).write(f"guides/{cc}.xml", encoding="utf-8", xml_declaration=True)

def write_playlist(cc, entries):
    os.makedirs("playlists", exist_ok=True)
    with open(f"playlists/{cc}.m3u", "w", encoding="utf-8") as f:
        f.write(f'#EXTM3U url-tvg="{RAW_BASE}/guides/{cc}.xml" x-tvg-url="{RAW_BASE}/guides/{cc}.xml"\n')
        for ext, url in entries:
            f.write(ext + "\n" + url + "\n")

def build(cc, spec):
    print(f"== {cc} ({spec['name']})")
    entries, seen_urls = [], set()
    def add(ext, url):
        if url not in seen_urls:
            seen_urls.add(url); entries.append((ext, url))
    for p in spec.get("packs", []):
        n0 = len(entries)
        for ext, url in parse_m3u(get(BC + p + ".m3u")): add(ext, url)
        print(f"  pack {p}: +{len(entries)-n0}")
    for u in spec.get("extra_m3u", []):
        n0 = len(entries)
        for ext, url in parse_m3u(get(u)): add(ext, url)
        print(f"  extra {u.rsplit('/',1)[-1]}: +{len(entries)-n0}")
    guide_texts = [get(MJH + g + ".xml") for g in spec.get("guides", [])]
    for u in spec.get("extra_guides_gz", []):
        guide_texts.append(gzip.decompress(get(u, binary=True)).decode("utf-8", "replace"))
    if spec.get("iptvorg"):
        raw = parse_m3u(get(IPTVORG + spec["iptvorg"] + ".m3u"))
        gz = gzip.decompress(get(spec["epgshare_gz"], binary=True)).decode("utf-8", "replace")
        tree = ET.fromstring(gz.encode("utf-8"))
        names = {}
        for ch in tree.findall("channel"):
            for dn in ch.findall("display-name"):
                if dn.text: names.setdefault(norm(dn.text), ch.get("id"))
            names.setdefault(norm(ch.get("id").replace(".pt", "").replace(".", " ")), ch.get("id"))
        mapped = 0
        for ext, url in raw:
            m = re.search(r'tvg-name="([^"]*)"', ext); nm = m.group(1) if m else ext.rsplit(",", 1)[-1]
            key = norm(nm); gid = names.get(key)
            if gid:
                ext = re.sub(r'tvg-id="[^"]*"', f'tvg-id="{gid}"', ext); mapped += 1
            add(ext, url)
        print(f"  iptv-org {spec['iptvorg']}: +{len(raw)}  guide-mapped by name: {mapped}")
        guide_texts.append(gz)
    root, nch, npr, ids = merge_guides(guide_texts)
    covered = sum(1 for ext, _ in entries if (re.search(r'tvg-id="([^"]*)"', ext) or [None, ""])[1] in ids) if ids else 0
    write_playlist(cc, entries); write_guide(cc, root)
    print(f"  -> playlists/{cc}.m3u: {len(entries)} channels | guides/{cc}.xml: {nch} channels, {npr} programmes | channels with guide: {covered}")
    return len(entries), covered, nch, npr

if __name__ == "__main__":
    only = sys.argv[1:] or list(COUNTRIES)
    rows = []
    for cc in only:
        rows.append((cc, COUNTRIES[cc]["name"]) + build(cc, COUNTRIES[cc]))
    with open("README.md", "w", encoding="utf-8") as f:
        f.write("# tv-guide-mirror\n\nFree live-TV playlists (Pluto TV, Samsung TV Plus, Plex, free-to-air) with merged XMLTV guides, "
                "rebuilt twice a day by GitHub Actions. Single-commit history so the repo never grows.\n\n"
                "| Country | Playlist | Guide | Channels | With guide data |\n|---|---|---|---|---|\n")
        for cc, name, n, cov, nch, npr in rows:
            f.write(f"| {name} | `{RAW_BASE}/playlists/{cc}.m3u` | `{RAW_BASE}/guides/{cc}.xml` | {n} | {cov} |\n")
        f.write("\nAll streams and guide data belong to their upstream sources: BuddyChewChew/app-m3u-generator, matthuisman/i.mjh.nz, bstillitano/free-iptv, iptv-org/iptv, epgshare01.\n")
