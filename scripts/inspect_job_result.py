import json, sqlite3
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
conn = sqlite3.connect(str(ROOT / "data" / "mempas.db"))
cur = conn.cursor()
cur.execute("SELECT result FROM extraction_jobs WHERE id = ?",
            ("490dcd878d7e4113b60ae9defab93f82",))
row = cur.fetchone()
conn.close()
result = json.loads(row[0])
# Print doc_meta
meta = result.get("_doc_meta", {})
print("_doc_meta keys:", list(meta.keys()))
print("_doc_meta:", json.dumps(meta, ensure_ascii=False, indent=2)[:2000])
