"""Pull USF basketball games from ESPN's public API, one file per game.

Each completed game is saved once (data/<league>/<season>/games/<event_id>.json) with
every USF assisted basket (passer, scorer, period, clock, points) and the box-score
roster, so the season network can be rebuilt game by game. Rerunning only fetches
games that are not on disk yet, which is the per-game update.

    python -I fetch_games.py --league mens --season 2027
    python -I fetch_games.py --league womens --season 2026 --refresh

Season is ESPN's label: the year the season ends (2026-27 = 2027).
Standard library only, so it runs unchanged in a scheduled job.
"""

import argparse
import datetime
import json
import os
import re
import sys
import time
import urllib.request

TEAM_ID = "2539"  # San Francisco Dons on ESPN, same id for men and women
BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/{league}-college-basketball"
UA = "Mozilla/5.0 (usf-hoops-network)"
HERE = os.path.dirname(os.path.abspath(__file__))


def pacific_date(utc_iso):
    """ESPN tip times are UTC, so a 7 pm Pacific tip carries the next day's date. Convert by US DST rules
    (second Sunday of March to first Sunday of November, 2 am local), so no time-zone database is needed."""
    s = utc_iso.rstrip("Z")
    t = datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S" if s.count(":") == 2 else "%Y-%m-%dT%H:%M")

    def nth_sunday(year, month, n):
        d = datetime.datetime(year, month, 1)
        d += datetime.timedelta(days=(6 - d.weekday()) % 7)
        return d + datetime.timedelta(weeks=n - 1)

    dst_on = nth_sunday(t.year, 3, 2) + datetime.timedelta(hours=10)   # 2 am PST in UTC
    dst_off = nth_sunday(t.year, 11, 1) + datetime.timedelta(hours=9)  # 2 am PDT in UTC
    offset = -7 if dst_on <= t < dst_off else -8
    return (t + datetime.timedelta(hours=offset)).strftime("%Y-%m-%d")


def get_json(url, tries=3):
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # network hiccup: back off and retry
            if k == tries - 1:
                raise RuntimeError(f"GET failed after {tries} tries: {url} ({e})")
            time.sleep(2 * (k + 1))


def schedule(league, season):
    """USF games for the season, regular season and postseason merged: (completed, upcoming)."""
    games, upcoming = {}, {}
    for stype in (2, 3):
        url = f"{BASE.format(league=league)}/teams/{TEAM_ID}/schedule?season={season}&seasontype={stype}"
        for e in get_json(url).get("events", []):
            c = e["competitions"][0]
            usf = next(x for x in c["competitors"] if x["team"]["id"] == TEAM_ID)
            opp = next(x for x in c["competitors"] if x["team"]["id"] != TEAM_ID)
            meta = {
                "event_id": e["id"],
                "date": pacific_date(e["date"]),
                "tip_utc": e["date"],
                "opponent": opp["team"].get("displayName"),
                "opponent_short": opp["team"].get("shortDisplayName") or opp["team"].get("location")
                                  or opp["team"].get("displayName"),
                "opponent_id": opp["team"]["id"],
                "site": "N" if c.get("neutralSite") else ("H" if usf.get("homeAway") == "home" else "A"),
                "conference_game": bool(c.get("conferenceCompetition")),
                "season_type": stype,
            }
            (games if c["status"]["type"].get("completed") else upcoming)[e["id"]] = meta
    order = lambda g: (g["date"], g["event_id"])
    return sorted(games.values(), key=order), sorted(upcoming.values(), key=order)


def last_name(full):
    parts = (full or "").replace(".", "").split()
    suffixes = {"jr", "sr", "ii", "iii", "iv", "v"}
    while parts and parts[-1].lower() in suffixes:
        parts.pop()
    return parts[-1].lower() if parts else ""


def text_names(text):
    """(scorer, passer) as written in the play text; ESPN uses two formats."""
    m = re.match(r"^(.*?) made .*?\. Assisted by (.*?)\.?$", text)
    if m:
        return m.group(1), m.group(2)
    m = re.match(r"^(.*?) makes .*?\((.*?) assists\)\s*$", text)
    if m:
        return m.group(1), m.group(2)
    return None, None


def parse_game(meta, summary):
    box = summary.get("boxscore", {})

    roster = {}
    for side in box.get("players", []):
        if side["team"]["id"] != TEAM_ID:
            continue
        for block in side.get("statistics", []):
            names = block.get("names") or block.get("keys") or []
            for a in block.get("athletes", []):
                ath = a.get("athlete") or {}
                if not ath.get("id"):  # ESPN sometimes lists a box-score row with no athlete id
                    continue
                stats = dict(zip(names, a.get("stats", [])))
                roster[ath["id"]] = {
                    "name": ath.get("displayName"),
                    "short": ath.get("shortName"),
                    "jersey": ath.get("jersey"),
                    "position": (ath.get("position") or {}).get("abbreviation"),
                    "starter": bool(a.get("starter")),
                    "minutes": stats.get("MIN"),
                    "points": stats.get("PTS"),
                    "assists": stats.get("AST"),
                    "dnp": bool(a.get("didNotPlay")),
                }

    box_ast, box_fgm = {}, {}
    for t in box.get("teams", []):
        st = {s.get("name"): s.get("displayValue") for s in t.get("statistics", [])}
        box_ast[t["team"]["id"]] = int(st["assists"]) if st.get("assists", "").isdigit() else None
        fg = (st.get("fieldGoalsMade-fieldGoalsAttempted") or "").split("-")[0]
        box_fgm[t["team"]["id"]] = int(fg) if fg.isdigit() else None

    hdr = summary.get("header", {}).get("competitions", [{}])[0]
    score = {c["team"]["id"]: int(c.get("score") or 0) for c in hdr.get("competitors", [])}

    assists, unattributed, name_checks, name_misses, made_fg = [], 0, 0, [], 0
    for p in summary.get("plays", []):
        if (p.get("team") or {}).get("id") != TEAM_ID:
            continue
        if not (p.get("scoringPlay") and p.get("shootingPlay") and p.get("scoreValue") in (2, 3)):
            continue
        made_fg += 1
        text = p.get("text", "")
        ids = [x.get("athlete", {}).get("id") for x in p.get("participants", [])]
        says_assist = bool(re.search(r"assist", text, re.I))
        if len(ids) < 2:
            unattributed += says_assist
            continue
        scorer, passer = ids[0], ids[1]
        # Participants come as [scorer, passer]; confirm against the play text by last name.
        t_scorer, t_passer = text_names(text)
        if t_scorer and t_passer and scorer in roster and passer in roster:
            name_checks += 1
            ok = (last_name(roster[scorer]["name"])[:4] in t_scorer.lower()
                  and last_name(roster[passer]["name"])[:4] in t_passer.lower())
            if not ok:
                name_misses.append(text)
        assists.append({
            "seq": int(p.get("sequenceNumber") or 0),
            "period": (p.get("period") or {}).get("number"),
            "clock": (p.get("clock") or {}).get("displayValue"),
            "passer": passer,
            "scorer": scorer,
            "points": p.get("scoreValue"),
            "shot": (p.get("type") or {}).get("text"),
            "text": text,
        })

    usf_box = box_ast.get(TEAM_ID)
    opp_id = meta["opponent_id"]
    return {
        **meta,
        "usf_score": score.get(TEAM_ID),
        "opp_score": score.get(opp_id),
        "result": None if score.get(TEAM_ID) is None else ("W" if score[TEAM_ID] > score.get(opp_id, 0) else "L"),
        "check": {
            "box_assists": usf_box,
            "pbp_assists": len(assists),
            "matches_box": usf_box == len(assists),
            # Fewer made shots in the play-by-play than the box score means plays are missing,
            # not just untagged assists.
            "box_fgm": box_fgm.get(TEAM_ID),
            "pbp_made_fg": made_fg,
            "assist_text_without_ids": unattributed,
            "name_checks": name_checks,
            "name_mismatches": name_misses,
        },
        "roster": roster,
        "assists": assists,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--league", choices=["mens", "womens"], required=True)
    ap.add_argument("--season", type=int, required=True)
    ap.add_argument("--refresh", action="store_true", help="refetch games already on disk")
    ap.add_argument("--out", default=os.path.join(HERE, "data"))
    args = ap.parse_args()

    gdir = os.path.join(args.out, args.league, str(args.season), "games")
    os.makedirs(gdir, exist_ok=True)

    sched, upcoming = schedule(args.league, args.season)
    new = 0
    for meta in sched:
        path = os.path.join(gdir, meta["event_id"] + ".json")
        if os.path.exists(path) and not args.refresh:
            continue
        summary = get_json(f"{BASE.format(league=args.league)}/summary?event={meta['event_id']}")
        game = parse_game(meta, summary)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(game, f, ensure_ascii=False, indent=1)
        new += 1
        time.sleep(0.4)

    # Season index, rebuilt from every game file on disk, in date order.
    index = []
    for fn in os.listdir(gdir):
        if fn.endswith(".json"):
            with open(os.path.join(gdir, fn), encoding="utf-8") as f:
                g = json.load(f)
            index.append({k: g.get(k) for k in ("event_id", "date", "opponent", "opponent_short", "site",
                                                "conference_game", "season_type", "usf_score", "opp_score",
                                                "result", "check")})
    index.sort(key=lambda g: (g["date"], g["event_id"]))
    # Rewritten only when something new arrived, so a run that finds no new game changes no file
    # (the daily site job commits only real updates).
    spath = os.path.join(args.out, args.league, str(args.season), "season.json")
    if new or args.refresh or not os.path.exists(spath):
        with open(spath, "w", encoding="utf-8") as f:
            json.dump({"league": args.league, "season": args.season, "team_id": TEAM_ID,
                       "fetched": time.strftime("%Y-%m-%d %H:%M"), "games": index, "upcoming": upcoming[:3]},
                      f, ensure_ascii=False, indent=1)

    bad = [g for g in index if not g["check"]["matches_box"]]
    print(f"{args.league} {args.season}: {len(sched)} completed on ESPN, {new} fetched now, {len(index)} on disk, "
          f"{len(index) - len(bad)} match the box score")
    for g in bad:
        c = g["check"]
        print(f"  MISMATCH {g['date']} vs {g['opponent']}: assists box {c['box_assists']} / play-by-play {c['pbp_assists']}; "
              f"made FG box {c['box_fgm']} / play-by-play {c['pbp_made_fg']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
