import re, sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ocr_file = Path("data/ocr_test/凯硕新正投标文件__ocr.txt")
content = ocr_file.read_text(encoding="utf-8")
page_re = re.compile(r"={60}\nPage (\d+)[^\n]*\n```html\n(.*?)```", re.DOTALL)
pages = {int(m.group(1)): m.group(2) for m in page_re.finditer(content)}

print(f"Total pages in cache: {len(pages)}")
for pnum in [4, 5, 6, 7]:
    html = pages[pnum]
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.DOTALL)
    print(f"\n=== Page {pnum} ({len(rows)} rows) ===")
    for r in rows[:3]:
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, re.DOTALL)
        cleaned = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
        print("  " + " | ".join(cleaned[:15]))
