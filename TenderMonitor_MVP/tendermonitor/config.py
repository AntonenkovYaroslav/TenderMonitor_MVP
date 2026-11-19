from pathlib import Path
import json
ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
REPORTS = ROOT / "reports"
LOGS = ROOT / "logs"
DB = DATA / "tenders.db"
def settings():
    with (ROOT / "config" / "keywords.json").open(encoding="utf-8") as f:
        return json.load(f)
