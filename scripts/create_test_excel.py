"""Create a test Excel file with cable tray quotes from 3 suppliers."""
import openpyxl

wb = openpyxl.Workbook()
ws = wb.active
ws.title = "报价"
headers = ["名称", "规格型号", "品牌", "单位", "含税单价", "数量", "供应商"]
ws.append(headers)
rows = [
    ["镀锌桥架", "200x100", "泰瑞安", "米", 68.5, 100, "上海泰瑞安电气有限公司"],
    ["镀锌桥架", "300x100", "泰瑞安", "米", 85.0, 200, "上海泰瑞安电气有限公司"],
    ["镀锌桥架", "400x150", "泰瑞安", "米", 128.0, 50, "上海泰瑞安电气有限公司"],
    ["镀锌桥架", "200x100", "华通", "米", 72.0, 100, "江苏华通电气集团"],
    ["镀锌桥架", "300x100", "华通", "米", 90.0, 200, "江苏华通电气集团"],
    ["镀锌桥架", "400x150", "华通", "米", 135.0, 50, "江苏华通电气集团"],
    ["镀锌桥架", "200x100", "凯隆", "米", 65.0, 100, "浙江凯隆电器有限公司"],
    ["镀锌桥架", "300x100", "凯隆", "米", 82.0, 200, "浙江凯隆电器有限公司"],
    ["镀锌桥架", "400x150", "凯隆", "米", 125.0, 50, "浙江凯隆电器有限公司"],
]
for row in rows:
    ws.append(row)
wb.save("docs/test/test_qiaojia_quotes.xlsx")
print(f"Created test Excel file with {len(rows)} rows, 3 suppliers")
