#!/usr/bin/env python
"""One-time migration to strip recent_matches in transfermarkt_cache.json.

Rules:
- Complete form (>=5 finished matches): recent_matches -> []
- Partial form (<5 finished matches): strip to only {score, home_away}
- Drop opponent_id (always null)
"""

import json
from pathlib import Path


def strip_fixture(f: dict) -> dict:
    """Return fixture stripped to only fields needed for form computation."""
    if f.get("score") is not None:
        return {
            "score": f["score"],
            "home_away": f.get("home_away", "H"),
        }
    return None


def migrate_cache(cache_path: Path) -> dict:
    with open(cache_path) as f:
        cache = json.load(f)

    total_teams = 0
    complete = 0
    partial = 0
    empty = 0

    for lid, ldata in cache.get("leagues", {}).items():
        for tk, td in ldata.get("teams", {}).items():
            total_teams += 1
            rm = td.get("recent_matches", [])
            if not rm:
                empty += 1
                continue

            finished = [m for m in rm if m.get("score") is not None]
            if len(finished) >= 5:
                # Complete form -> empty list
                td["recent_matches"] = []
                complete += 1
            else:
                # Partial form -> strip to {score, home_away}
                stripped = [strip_fixture(m) for m in finished]
                td["recent_matches"] = [s for s in stripped if s is not None]
                partial += 1

    print(f"Total teams: {total_teams}")
    print(f"Complete form (>=5 finished): {complete} -> recent_matches: []")
    print(f"Partial form (<5 finished): {partial} -> stripped {{score, home_away}}")
    print(f"Empty: {empty}")

    return cache


def main():
    cache_path = Path("data/transfermarkt_cache.json")
    backup_path = Path("data/transfermarkt_cache.json.backup")

    # Create backup
    import shutil
    shutil.copy2(cache_path, backup_path)
    print(f"Backup created: {backup_path}")

    # Migrate
    migrated = migrate_cache(cache_path)

    # Write migrated cache
    with open(cache_path, "w") as f:
        json.dump(migrated, f, separators=(",", ":"))

    # Report size reduction
    orig_size = backup_path.stat().st_size
    new_size = cache_path.stat().st_size
    print(f"Original size: {orig_size:,} bytes ({orig_size/1024:.1f} KB)")
    print(f"New size: {new_size:,} bytes ({new_size/1024:.1f} KB)")
    print(f"Reduction: {orig_size - new_size:,} bytes ({(orig_size - new_size)/1024:.1f} KB)")


if __name__ == "__main__":
    main()