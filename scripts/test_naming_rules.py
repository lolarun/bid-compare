"""Test material naming standardization rules."""
import sys
sys.path.insert(0, ".")

from apps.api.services.standardize import standardize_name

cases = [
    # Critical: 热浸镀锌 vs 热镀锌 must NOT be merged
    ("热浸镀锌桥架", None, "热浸镀锌桥架", "should stay as-is"),
    ("热镀锌桥架", None, "热镀锌桥架", "should stay as-is (different from 热浸镀锌)"),
    ("热浸锌桥架", None, "热浸镀锌桥架", "热浸锌 is alias for 热浸镀锌"),

    # Bridge type aliases
    ("线槽", None, "槽式桥架", "线槽 → 槽式桥架"),
    ("槽盒", None, "槽式桥架", "槽盒 → 槽式桥架"),
    ("消防桥架", None, "防火桥架", "消防桥架 → 防火桥架"),
    ("室外桥架", None, "防水桥架", "室外桥架 → 防水桥架"),
    ("电缆桥架", None, "桥架", "电缆桥架 → 桥架"),

    # Valve aliases (unchanged)
    ("蝶型阀", None, "蝶阀", "蝶型阀 → 蝶阀"),
    ("逆止阀", None, "止回阀", "逆止阀 → 止回阀"),

    # Surface treatment
    ("冷镀锌", None, "电镀锌", "冷镀锌 → 电镀锌"),
]

passed = 0
failed = 0
for text, category, expected, description in cases:
    result = standardize_name(text, category)
    actual = result["standardized"]
    ok = actual == expected
    status = "PASS" if ok else "FAIL"
    if not ok:
        print(f"  {status}: {description}")
        print(f"    input='{text}' expected='{expected}' got='{actual}'")
        print(f"    changes={result['changes']}")
        failed += 1
    else:
        print(f"  {status}: {description}")
        passed += 1

print(f"\n{passed} passed, {failed} failed")
if failed:
    sys.exit(1)
