"""The daily site job's runner, inside the usf-playoff-watch repo at _pipeline/basketball/.

    python -I -B _pipeline/basketball/update_site.py           # rebuild only if a new game arrived
    python -I -B _pipeline/basketball/update_site.py --force   # rebuild regardless (the code changed)

Fetches the current season for both teams. When a new game arrived, or with --force, it rebuilds
basketball/index.html (men) and basketball/women/index.html (women) at the repo root. Under GitHub
Actions it writes changed=true|false and through=<Mon D> to $GITHUB_OUTPUT for the commit step.

The source of this file is lanes/ad/usf_hoops_network/ in Alex's lab repo; publish_site.py copies it here.
"""

import datetime
import glob
import importlib.util
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def pacific_today():
    spec = importlib.util.spec_from_file_location("fetch_games", os.path.join(HERE, "fetch_games.py"))
    fg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fg)
    return fg.pacific_date(datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%MZ"))


def game_files(season):
    return set(glob.glob(os.path.join(HERE, "data", "*", season, "games", "*.json")))


def main():
    force = "--force" in sys.argv[1:]
    today = pacific_today()
    year, month = int(today[:4]), int(today[5:7])
    season = str(year + 1 if month >= 8 else year)  # ESPN labels a season by the year it ends

    before = game_files(season)
    for league in ("mens", "womens"):
        r = subprocess.run([sys.executable, "-I", "-B", os.path.join(HERE, "fetch_games.py"),
                            "--league", league, "--season", season])
        if r.returncode:
            return r.returncode
    new = sorted(game_files(season) - before)
    changed = bool(new) or force
    if changed:
        r = subprocess.run([sys.executable, "-I", "-B", os.path.join(HERE, "build_page.py"),
                            "--site-dir", os.path.join(ROOT, "basketball"), "--combined", "none",
                            "--metrics", "none", "--built", today])
        if r.returncode:
            return r.returncode

    dates = []
    for path in game_files(season):
        with open(path, encoding="utf-8") as f:
            dates.append(json.load(f)["date"])
    through = ""
    if dates:
        last = max(dates)
        through = MONTHS[int(last[5:7]) - 1] + " " + str(int(last[8:10]))
    print(f"{len(new)} new game(s); pages {'rebuilt' if changed else 'left as they were'}; games through {through or '-'}")

    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"changed={'true' if changed else 'false'}\nthrough={through}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
