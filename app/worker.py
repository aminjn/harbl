"""One collection run in its own process: python -m app.worker <site_id> <trigger> [category_id]

Started by main._job (at low priority) so the browser work never slows the panel's own process."""
import sys

from . import scraper
from .config import DATA_DIR


def main():
    site_id, trigger = int(sys.argv[1]), sys.argv[2]
    category_id = int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3] else None
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # no db.init() here: it resets every "running" run, including other sites' runs in other processes
    scraper.run_site(site_id, trigger, category_id)


if __name__ == "__main__":
    main()
