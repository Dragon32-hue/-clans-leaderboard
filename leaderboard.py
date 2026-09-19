#!/usr/bin/env python3
"""SplitGDPS Clan Rate-Points Leaderboard -> Discord webhook (incremental).

Tracks cumulative points per clan seeded from a manual baseline, then on every
update recomputes the full rating delta for every clan level: new ratings add
points, upgrades add the difference, and demotions / un-ratings subtract it.

Scoring per rated level by its highest tier:
  star rated  =  1 pt
  featured    =  5 pts
  epic        = 10 pts
  legendary   = 20 pts
  mythic      = 40 pts

Totals persist in `state.json` (seeded with the manual baseline). Rosters come
from the public SplitGDPS dashboard; rated levels come from the game API.

Usage:
  python leaderboard.py --reset-baseline    # (re)seed state from manual baseline
  python leaderboard.py --webhook URL       # update + post to Discord
  python leaderboard.py --dry-run           # print the payload, do not post
  python leaderboard.py --state FILE        # use a custom state path
"""

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

DASH = "https://splitgdps.alwaysdata.net/dashboard"
INDEX_URL = DASH + "/clans/"
API_URL = "https://splitgdps.alwaysdata.net/getGJLevels21.php"
USER_AGENT = "SplitGDPS-Clans-Leaderboard-Bot/4.0"

TIER_POINTS = {"mythic": 40, "legendary": 20, "epic": 10, "featured": 5, "star": 1}
TIER_ORDER = ("mythic", "legendary", "epic", "featured", "star")
TIER_ICON = {"mythic": "🌟", "legendary": "👑", "epic": "💎", "featured": "❕", "star": "⭐"}

MEDALS = {1: "1️⃣", 2: "2️⃣", 3: "3️⃣", 4: "4️⃣"}
BASELINE = {"RED": 615, "GREEN": 555, "BLUE": 323, "YELLO": 158}


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", "replace")


def api(filters: dict, page: int = 0) -> tuple:
    params = {"secret": "Wmfd2893gb7", "type": "0", "page": str(page),
              "total": "0", "gameVersion": "22", "binaryVersion": "45"}
    params.update(filters)
    req = urllib.request.Request(API_URL, data=urllib.parse.urlencode(params).encode())
    req.add_header("User-Agent", USER_AGENT)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return parse_levels(resp.read().decode("utf-8", "replace"))


def parse_levels(resp: str) -> tuple:
    if "#" not in resp:
        return [], {}
    main, userstr = resp.split("#", 1)
    userpart, _, _ = userstr.partition("#")
    users = {}
    for entry in userpart.split("|"):
        fields = entry.split(":")
        if fields and len(fields) >= 2:
            users[fields[0]] = fields[1]
    levels = []
    for chunk in main.split("|"):
        if not chunk.strip():
            continue
        toks = chunk.split(":")
        level = {toks[i]: toks[i + 1] for i in range(0, len(toks) - 1, 2)}
        levels.append(level)
    return levels, users


def fetch_filter(filters: dict) -> tuple:
    levels, users = {}, {}
    for page in range(0, 200):
        page_levels, page_users = api(filters, page)
        users.update(page_users)
        if not page_levels:
            break
        for lv in page_levels:
            levels.setdefault(lv.get("1"), lv)
    return levels, users


def fetch_current_rating() -> tuple:
    """Return {level_id: (user_id, tier, name)} for every currently rated level
    and {user_id: username} from the user strings."""
    tiers = {}
    users = {}
    # Note: per GD protocol the legendary/mythic search params are swapped;
    # `legendary` actually filters MYTHIC levels and `mythic` filters LEGENDARY.
    for name, filters in (("mythic", {"legendary": "1"}),
                          ("legendary", {"mythic": "1"}),
                          ("epic", {"epic": "1"}),
                          ("featured", {"featured": "1"}),
                          ("star", {"star": "1"})):
        levels, page_users = fetch_filter(filters)
        users.update(page_users)
        for lid, lv in levels.items():
            uid = lv.get("6", "?")
            entry = tiers.get(lid)
            if entry is None or TIER_ORDER.index(name) < TIER_ORDER.index(entry[1]):
                tiers[lid] = (uid, name, lv.get("2", "?"))
    return tiers, users


def parse_clans(html: str) -> list[dict]:
    clans = []
    for chunk in html.split('<div class="profile clanscard">')[1:]:
        match = re.search(r'color:#([0-9a-fA-F]{6})[^>]*>\s*(\[[A-Za-z]+\])?\s*([^<]+?)\s*</span>', chunk)
        if not match:
            continue
        color, tag, name = match.group(1), (match.group(2) or ""), match.group(3).strip()
        members_m = re.search(r"<b>(\d+)</b>\s*members?", chunk)
        slug_m = re.search(r"clan/([A-Za-z0-9_]+)", chunk)
        if name and slug_m:
            clans.append({
                "name": f"{tag} {name}".strip(),
                "tag": tag.strip("[]") if tag else "",
                "color": int(color, 16),
                "members": int(members_m.group(1)) if members_m else 0,
                "slug": slug_m.group(1),
            })
    return clans


def parse_roster(html: str) -> set:
    names = {m.group(1).strip() for m in
             re.finditer(r'profilenick clanmembernick[^>]*>.*?<img[^>]*>([^<]+)', html, re.S)}
    names |= {m.group(1).strip() for m in
              re.finditer(r'clanownernick[^>]*>.*?<img[^>]*>([^<]+)', html, re.S)}
    return {n for n in names if n}


def clean_username(name: str) -> str:
    return re.sub(r"^\[[^\]]+\]\s*", "", name)


def load_state(path: str) -> dict:
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return {"points": dict(BASELINE), "levels": {}}


def save_state(path: str, state: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
    os.replace(tmp, path)


def build_payload(clans: list[dict], points: dict, net: dict,
                  changes: list, total_levels: int) -> dict:
    info = [(c, points.get(c["tag"], 0)) for c in clans]
    info.sort(key=lambda x: x[1], reverse=True)

    lines = []
    for rank, (clan, pts) in enumerate(info, start=1):
        delta = net.get(clan["tag"], 0)
        sign = f"  {'▲ +' if delta > 0 else '▼'}{abs(delta):,}" if delta else ""
        lines.append(f"{MEDALS.get(rank, '⬜')} **{clan['name']}** — {pts:,} pts{sign}")
    desc = "\n\n".join(lines)

    if changes:
        shown = sorted(changes, key=lambda c: c["points"], reverse=True)[:15]
        recap = "\n".join(
            f"   {TIER_ICON[c['tier']]} **{c['name']}** by *{c['user']}* "
            f"— {c['label']} ({'+' if c['points'] > 0 else ''}{c['points']})"
            for c in shown)
        if len(changes) > 15:
            recap += f"\n   *…and {len(changes) - 15} more*"
        desc += "\n\n**Rating changes since last check:**\n" + recap
    else:
        desc += "\n\n_No rating changes since the last check._"

    embed = {
        "title": "🏆 SplitGDPS Clan Rate Leaderboard",
        "description": desc,
        "color": 0xF1C40F,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
        "footer": {"text": f"{total_levels} rated levels · star1 / feature5 / epic10 / legendary20 / mythic40"},
    }
    return {"content": "**Clan rate-points leaderboard update:**", "embeds": [embed]}


def main() -> int:
    webhook = os.environ.get("WEBHOOK_URL")
    dry = False
    reset = False
    state_path = "state.json"
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--webhook" and i + 1 < len(args):
            webhook = args[i + 1]
            i += 2
        elif args[i] == "--state" and i + 1 < len(args):
            state_path = args[i + 1]
            i += 2
        elif args[i] == "--dry-run":
            dry = True
            i += 1
        elif args[i] == "--reset-baseline":
            reset = True
            i += 1
        else:
            i += 1
    if not webhook and not dry and not reset:
        print("No webhook URL given (use --webhook URL or WEBHOOK_URL env var).", file=sys.stderr)
        return 2

    index = fetch(INDEX_URL)
    clans = parse_clans(index)
    if not clans:
        print("No clans found on the dashboard.", file=sys.stderr)
        return 1

    username = {}
    for clan in clans:
        detail = fetch(f"{DASH}/clan/{clan['slug']}")
        for uname in parse_roster(detail):
            username.setdefault(clean_username(uname), clan["tag"])

    tiers, users = fetch_current_rating()

    if reset:
        state = {"points": dict(BASELINE),
                 "levels": {lid: {"tier": tier, "uid": uid,
                                  "user": clean_username(users.get(uid, "?"))}
                            for lid, (uid, tier, _) in tiers.items()}}
        save_state(state_path, state)
        print(f"Baseline reseeded: {state['points']} ({len(state['levels'])} levels recorded)")
        return 0

    state = load_state(state_path)
    points = dict(state["points"])
    seen = state.get("levels", {})
    net = {c["tag"]: 0 for c in clans}
    changes = []

    for lid, (uid, tier, name) in tiers.items():
        entry = seen.get(lid)
        old_tier = entry["tier"] if entry else None
        if old_tier == tier:
            continue
        diff = TIER_POINTS[tier] - TIER_POINTS.get(old_tier, 0)
        user = clean_username(users.get(uid, "?"))
        clan = username.get(user)
        if clan and diff:
            points[clan] += diff
            net[clan] += diff
        if old_tier:
            changes.append({"name": name, "user": user, "tier": max(tier, old_tier, key=lambda t: TIER_POINTS.get(t, 0)),
                            "label": f"{old_tier} → {tier}", "points": diff})
        else:
            changes.append({"name": name, "user": user, "tier": tier,
                            "label": f"rated {tier}", "points": diff})
        seen[lid] = {"tier": tier, "uid": uid, "user": user}

    for lid, entry in list(seen.items()):
        if lid in tiers or not isinstance(entry, dict):
            continue
        old_p = TIER_POINTS.get(entry["tier"], 0)
        if old_p and entry.get("user"):
            clan = username.get(entry["user"])
            if clan:
                points[clan] -= old_p
                net[clan] -= old_p
        del seen[lid]

    state["points"] = points
    state["levels"] = seen
    if not dry:
        save_state(state_path, state)

    payload = build_payload(clans, points, net, changes, len(tiers))
    if dry:
        print(json.dumps(payload, indent=2))
        print("TOTAL LEVELS TRACKED:", len(seen), file=sys.stderr)
    else:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(webhook, data=data, method="POST",
                                     headers={"Content-Type": "application/json",
                                              "User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
        print("Posted leaderboard update to Discord.")
    return 0


if __name__ == "__main__":
    sys.exit(main())