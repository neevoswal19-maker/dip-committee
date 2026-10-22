"""Build the public demo page (site/index.html) for GitHub Pages.

Run by .github/workflows/public-site.yml after each morning scan and intraday
session. Reads research output only - see src/public_site.py for what is and
is not shown. `--out` picks the folder; the workflow publishes it.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import db, public_site
from src.config import load_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("public_site")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the public demo page")
    parser.add_argument("--out", default="site")
    args = parser.parse_args()

    cfg = load_config()
    db.init_db()
    db.assert_encrypted()

    def market():
        from src.strategy import regime
        return regime.current(cfg)

    snapshot = public_site.gather(cfg, regime_fn=market)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(public_site.render(snapshot), encoding="utf-8")
    (out / ".nojekyll").write_text("", encoding="utf-8")
    log.info("Built %s: %d candidates, %d recent calls, %d paper trades", out / "index.html",
             len(snapshot["candidates"]), len(snapshot["calls"]), snapshot["intraday"].get("trades", 0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
