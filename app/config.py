import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("HARSCRAPER_DATA", BASE_DIR / "data"))
OUTPUT_DIR = DATA_DIR / "output"

# Panel login (set these in the environment before running in production)
ADMIN_USER = os.environ.get("HARSCRAPER_USER", "admin")
ADMIN_PASS = os.environ.get("HARSCRAPER_PASS", "change-me")

USER_AGENT_SUFFIX = os.environ.get("HARSCRAPER_UA_SUFFIX", "")
REQUEST_TIMEOUT = float(os.environ.get("HARSCRAPER_TIMEOUT", "30"))
MAX_RESPONSE_BYTES = 20 * 1024 * 1024

# Key for the read-only data feed (/feed/...) that your own website reads.
# Set HARSCRAPER_FEED_KEY, otherwise a random key is created once in DATA_DIR/feed_key.txt
FEED_KEY = os.environ.get("HARSCRAPER_FEED_KEY", "")
