"""Verify supplier-name OCR fix: brand should no longer be used as company name.

Re-OCRs the two PDFs that previously came out as a brand (KITZ / 伯尔梅特).
Prints the extracted supplier_name + a brand sample so we can confirm the
company name (or a safe blank) is returned instead of the brand.
"""
import os, sys, time, requests

API = "http://localhost:8002"
PDF_DIR = os.path.join(os.path.dirname(__file__), "..", "docs", "test")
# previously-broken files → what they wrongly resolved to before
TARGETS = {
    "凯硕新正投标文件.pdf": "KITZ (旧)",
    "泰科龙投标文件.pdf": "伯尔梅特 (旧)",
}


def main():
    tok = requests.post(f"{API}/api/auth/login",
                        json={"username": "admin", "password": "admin123"}).json()["access_token"]
    H = {"Authorization": f"Bearer {tok}"}
    for pdf, old in TARGETS.items():
        path = os.path.join(PDF_DIR, pdf)
        with open(path, "rb") as f:
            job = requests.post(f"{API}/api/intake/upload",
                                files={"file": (pdf, f, "application/pdf")},
                                data={"type": "quote"}, headers=H).json()
        jid = job["id"]
        print(f"[{pdf}] job={jid[:8]} uploading… (was: {old})", flush=True)
        start = time.time()
        while time.time() - start < 300:
            j = requests.get(f"{API}/api/intake/jobs/{jid}", headers=H).json()
            if j["status"] == "done":
                res = j.get("result") or {}
                items = res.get("items") or []
                brands = sorted({(it.get("brand") or "").strip() for it in items} - {""})
                print(f"  ✅ supplier_name = '{res.get('supplier_name','')}'  "
                      f"(items={len(items)}, 品牌列={brands[:4]})", flush=True)
                break
            if j["status"] == "failed":
                print(f"  ❌ FAILED: {j.get('error','')[:200]}", flush=True)
                break
            time.sleep(5)
        else:
            print("  ⏱ timeout", flush=True)


if __name__ == "__main__":
    main()
