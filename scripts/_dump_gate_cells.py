"""Dump all pending/exclude cells for project 62 session 1 to UTF-8 JSON
so the gate-misfire audit can be built against real strings."""
from __future__ import annotations
import json, os, re, sqlite3, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from apps.api.services.canonical import extract_valve_canonical

conn = sqlite3.connect("data/mempas.db")
conn.row_factory = sqlite3.Row

# anchor canonical map
arow = conn.execute(
    "SELECT anchors_json FROM tender_list_sessions WHERE project_id=62 AND is_current=1"
).fetchone()
anchors = {str(a["seq"]): a for a in json.loads(arow["anchors_json"])}

groups = conn.execute(
    "SELECT id, anchor_seq FROM bid_alignment_groups "
    "WHERE project_id=62 AND status='confirmed' AND tender_list_session_id=1"
).fetchall()


def parse_flags(note: str | None) -> list[str]:
    if not note:
        return []
    m = re.search(r"cos=\d+\.?\d*\s+(.*)", note)
    return [f for f in m.group(1).split(",") if f.strip()] if m else []


def parse_cos(note: str | None):
    if not note:
        return None
    m = re.search(r"cos=(\d+\.?\d*)", note)
    return float(m.group(1)) if m else None


cells = []
for g in groups:
    seq = str(g["anchor_seq"])
    anchor = anchors.get(seq, {})
    items = conn.execute(
        "SELECT supplier_id, action, quote_id, spec_note, name_note "
        "FROM bid_alignment_items WHERE group_id=?", (g["id"],)
    ).fetchall()
    for it in items:
        if it["action"] not in ("pending", "exclude"):
            continue
        q = conn.execute(
            "SELECT m.standard_name AS mat_name, m.spec AS mat_spec, q.brand, q.remark "
            "FROM quotes q LEFT JOIN materials m ON q.material_id=m.id WHERE q.id=?",
            (it["quote_id"],)
        ).fetchone()
        sup = conn.execute("SELECT name FROM suppliers WHERE id=?", (it["supplier_id"],)).fetchone()
        q_name = q["mat_name"] if q else ""
        q_spec = q["mat_spec"] if q else ""
        q_canon = extract_valve_canonical(q_name or "", q_spec or "")
        cells.append({
            "anchor_seq": seq,
            "anchor_name": anchor.get("name", ""),
            "anchor_spec": anchor.get("spec", ""),
            "anchor_pressure": anchor.get("pressure", ""),
            "anchor_canonical": anchor.get("canonical", {}),
            "supplier_id": it["supplier_id"],
            "supplier_name": sup["name"] if sup else str(it["supplier_id"]),
            "action": it["action"],
            "quote_id": it["quote_id"],
            "quote_material": q_name,
            "quote_spec": q_spec,
            "quote_canonical": q_canon,
            "flags": parse_flags(it["spec_note"]),
            "cos": parse_cos(it["spec_note"]),
            "evidence": it["name_note"] or "",
        })

conn.close()

# sort by seq
cells.sort(key=lambda c: (int(c["anchor_seq"]) if c["anchor_seq"].isdigit() else 999,
                          c["supplier_id"]))

out = ROOT / "outputs" / "_gate_audit_raw.json"
out.write_text(json.dumps(cells, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"dumped {len(cells)} pending/exclude cells -> {out}")
