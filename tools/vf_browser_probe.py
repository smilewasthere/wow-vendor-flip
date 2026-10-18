"""Manual live probe: python tools/vf_browser_probe.py <item_id> [<item_id> ...]

Uses the real Browser driver against live Wowhead. Opens a visible Edge
window (expected). Prints extract_sold_by JSON per item.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import browser


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    profile = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "data", "vf-edge-profile")
    ids = [int(a) for a in argv[1:]]
    with browser.Browser(profile_dir=profile) as br:
        for item_id in ids:
            result = br.extract_sold_by(item_id)
            print(item_id, json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
