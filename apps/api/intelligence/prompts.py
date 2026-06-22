"""Business-tuned prompts for tender and quote extraction.

These prompts inject domain knowledge (机电材料/招投标 terminology) and
constrain the LLM to return strict JSON matching the corresponding schema.
"""

TENDER_PROMPT = """你是上海建工一建集团的机电材料招投标助理。

请仔细阅读图片中的招标文件，提取以下信息：
1. 项目基本信息：项目名称、招标编号、招标日期、投标截止日期
2. 采购材料清单：每一行材料的名称、品类、规格型号、单位、数量、备注、品类专属技术参数

提取要求：
- 材料名称按文档原文，不要简化或归一
- 品类必须从以下选项中选择最匹配的：桥架、母线槽、配电箱、阀门、不锈钢管、水箱、潜水泵、风口风阀、风机盘管、空调泵；无法判断时留空字符串
- 数量若为'若干'/'按图'/'见图纸'等非数字，留 null
- 规格优先使用 DN/Φ/型号等技术参数
- 无法识别的字段返回空字符串或 null，**不要瞎猜**
- 不要把表头当成数据行

品类专属技术参数（放在 extended_attrs 对象中）：
- 桥架：surface(表面处理)、thickness(板材厚度mm)、load_type(荷载等级)、fire_rating(防火等级)
- 母线槽：rated_current(额定电流A)、ip_rating(防护等级)、conductor(导体材质)、insulation(绝缘方式)
- 配电箱：circuit_count(回路数)、breaker_brand(元器件品牌)、box_material(箱体材质)、ip_rating(防护等级)
- 阀门：valve_type(阀门类型)、pressure(公称压力MPa)、body_material(阀体材质)、connection(连接方式)
- 不锈钢管：steel_grade(钢种牌号)、wall_thickness(壁厚mm)、connection(连接方式)
- 水箱：tank_material(材质)、volume(容积m³)、insulation(保温方式)
- 潜水泵：flow_rate(流量m³/h)、head(扬程m)、power(功率kW)、material(过流部件材质)
- 风口风阀：type(类型)、material(材质)、drive_type(驱动方式)
- 风机盘管：cooling_cap(制冷量kW)、air_volume(风量m³/h)、install_type(安装方式)、coil_rows(盘管排数)
- 空调泵：flow_rate(流量m³/h)、head(扬程m)、power(功率kW)

仅提取文档中明确标注的参数，没有的不要填。extended_attrs 中只保留有值的字段，没有任何品类参数时留空对象 {}。
"""


QUOTE_PROMPT = """你是上海建工一建集团的机电材料报价单解析助理。

请仔细阅读图片中的供应商报价单，提取每一行的报价明细。

━━ 一、供应商基本信息 ━━
关于 supplier_name（供应商名）——非常重要：
- supplier_name 是【投标/报价单位的公司全称】，通常出现在封面、投标函、报价单抬头或落款盖章处，
  一般带有"有限公司/股份/集团/厂/经营部/商行/贸易/实业/工程/设备/科技/中心"等机构后缀，例如"星辉机电设备有限公司"。
- 【绝对不要】把明细表"品牌"列里的产品品牌当作 supplier_name，例如 ALPHA、万通、金星、宏达 等是产品品牌，不是投标公司名。
- 找不到明确的公司全称时，supplier_name 留空字符串，不要瞎猜。

━━ 二、报价明细字段（逐列映射规则）━━

每行必须输出以下字段。字段含义由【表头文字】决定，不得按列位置硬编码：

  seq              序号（按原文，如"1"/"01"/"一"）
  name             材料名称（按原文）
  spec             规格（DN/Φ/尺寸等技术规格）
  model            型号（独立"型号"列的内容，与 spec 不同列时才填；规格型号合并在一列时归入 spec）
  unit             单位（个/台/套/m/m²/m³ 等）
  qty              数量（纯数字；'若干'/'按图' 留 null）
  brand            品牌（按原文）
  material_type    材质（独立"材质/牌号"列；无此列留空字符串）

价格字段——按表头文字严格区分，不按列顺序推断：

  unit_price_excl_tax    不含税单价
    → 表头含"不含税"/"excl"/"税前"时填此字段
    → 表头仅写"单价"且文档无含税/不含税标注时，留 null（见 unit_price）

  unit_price_incl_tax    含税单价
    → 表头含"含税"/"incl"/"价税"/"综合单价"时填此字段

  unit_price             单价（当表头仅写"单价"且无法判断含税/不含税时才填此字段）
    → 如表头已明确区分含税/不含税，则 unit_price 留 null，分别填上面两个字段

  total_price_excl_tax   不含税合计/金额
    → 表头含"不含税合计"/"金额(不含税)"/"合计(不含税)"时填此字段

  total_price_incl_tax   含税合计/价税合计
    → 表头含"价税合计"/"含税合计"/"合价(含税)"/"合计(含税)"/"含税合价"时填此字段
    → 注意区分：税额（= 合价×税率，行级税金总额）≠ 含税合价（= 不含税合价 + 税额）
    → "税额"列填入 tax_amount，不能填入 total_price_incl_tax

  total_price            合计（当表头仅写"合价"/"合计"且无法判断含税/不含税时才填此字段）
    → 同 unit_price 逻辑

  tax_rate               税率（小数，0.13 表示 13%；从"税率"列提取）
  tax_amount             税额（从"税额"/"增值税额"列提取；无此列留 null）
    → 税额 = 不含税合价 × 税率，是行级税金总额，不是含税单价也不是含税合价

备注：
  remark           付款方式、保修期、交货期等（精简到 50 字内）

数字规则：
- 文档已有值则按原文；不要自行计算
- 范围如 '100-200' 取下限
- 无法识别的字段返回空字符串或 null，**不要瞎猜**
- 不要把表头、合计行、小计行当作数据行

━━ 三、阀门类 canonical 结构化参数 ━━

对于阀门类材料（截止阀/闸阀/止回阀/球阀/蝶阀/减压阀/疏水阀/过滤器等），
额外填写 canonical 对象：
- valve_type: 阀门类型，如"截止阀"（按原文，不要缩写）
- dn: 公称直径，格式"DN25"；Φ57/2寸/50mm 请转换为 DN 格式
- pn: 公称压力，格式"PN16"；1.6MPa 请转换为 PN16
- material: 主材质，如"不锈钢"、"铸铁"
- connection: 连接方式，如"螺纹"、"法兰"
非阀门类材料，canonical 留空对象 {}。

━━ 四、OCR 纠错（阀门类形近字）━━

当材料名称存在明显形近字 OCR 错误时，填写：
- normalized_material: 纠错后的正确名称（确信是OCR错别字时才填，否则留空字符串）
- ocr_correction_reason: 纠错依据，格式：[错误词] + 词表命中 + 相邻行规格连续性

合法阀门词表（与下列词条高度相似但有错别字时才纠错）：
闸阀 / 截止阀 / 止回阀 / 球阀 / 蝶阀 / 安全阀 / 减压阀
橡胶瓣止回阀 / 节能消声止回阀 / 缓闭式止回阀 / 消声止回阀
低阻力倒流防止器 / 倒流防止器 / 小阻力可调式减压阀组 / 减压阀组
Y型过滤器 / 篮式过滤器

示例：
- "阀阀 DN50" 且同页有闸阀序列 → normalized_material="闸阀"
- "橡胶海止回阀 DN80" → "橡胶瓣止回阀"（"海"≈"瓣"形近）
- "倒流防上器" → "倒流防止器"（"上"≈"止"形近）

重要：name/material 字段仍按文档原文；normalized_material 仅在确认为OCR错别字时才填。
不确定时留空字符串，不要瞎猜。
"""


META_EXTRACTION_PROMPT = """你是机电材料招投标助理。下面是投标文件封面/汇总页的OCR HTML内容。
请提取以下元信息：

1. supplier_name: 投标单位/供应商公司全称（带"有限公司/集团/实业"等后缀）
2. bid_total: 投标总价数字（人民币元，仅数字，不含单位）
3. bid_total_basis: 总价口径，从以下选项选：tax_included（含税）/ tax_excluded（不含税）/ unknown
4. tax_rate: 税率小数（如0.13），若不明确留null

只返回JSON，不要解释。
返回格式：{"supplier_name": "...", "bid_total": 数字或null, "bid_total_basis": "...", "tax_rate": 数字或null}
若该页没有相关信息，对应字段返回null或空字符串。"""


# ── 招标投标清单逐行抽取（PDF 第 14-18 页「阀门投标清单」）──────────────────
TENDER_BIDLIST_PROMPT = """你是上海建工一建集团的机电材料招投标助理。下面是 OCR 识别出的招标文件「投标清单」HTML 表格内容（可能是多页清单中的一页）。
请逐行提取采购清单明细，返回严格 JSON。

表格列含义（按此识别，不要错位）：
序号 | 专业 | 项目名称 | 规格 | 型号 | 工作压力 | 材质(阀体/阀芯/阀板/阀杆/密封圈) | 单位 | 数量 | 单价/合价/税率/税额(忽略) | 品牌 | 备注

提取要求：
- 【逐行完整】表格有多少数据行就返回多少条，一行不漏；不要表头、合计行、小计行。
- 【价格忽略】单价、合价、税率、税额等价格列一律不抽取（招标清单价格为空/0，无意义）。
- 【合并单元格向下填充】专业、项目名称、工作压力常是跨行合并单元格，下方空行要继承上方的值。
- 【材质五子列各自独立】materials 对象的 阀体/阀芯/阀板/阀杆/密封圈 分别填写，某子列空则留空字符串 ""；整行无材质则五项全空。
- 序号 seq 按原文（整数或字符串）。
- 规格 spec 优先含 DN（如 DN20）；型号 model 单列；工作压力 pressure 按原文（如 1.6Mpa）。
- 数量 qty 为数字；'若干'/'按图' 等非数字留 null。
- 品牌 brand、备注 remark 按原文，无则留空字符串。
- 无法识别的字段返回空字符串或 null，**不要瞎猜**。

返回 JSON 格式：
{"items": [{"seq": 1, "profession": "给排水", "name": "Y型过滤器", "spec": "DN20", "model": "", "pressure": "1.6Mpa", "materials": {"阀体": "", "阀芯": "", "阀板": "", "阀杆": "", "密封圈": ""}, "unit": "个", "qty": 1, "brand": "", "remark": "给水系统"}]}

如果该页不是投标清单（如正文、封面、招标情况表），返回 {"items": []}。"""


# ── 招标情况表（PDF 第 13 页）品牌要求 + 投标单位参与品牌 ────────────────────
TENDER_BRANDTABLE_PROMPT = """你是机电材料招投标助理。下面是 OCR 识别出的招标文件「招标情况表」HTML 内容。
该表登记了：材料类别、业主招标品牌要求、各投标单位及其参与品牌。请提取，返回严格 JSON。

提取要求：
- material_class：材料类别（如 水阀门）。
- brand_requirement：业主招标品牌要求列表。品牌常为「英文+中文」组合（如 ALFA 阿法、VEGA 威盖、ORION 猎户），拆成 brand_en / brand_cn。只有中文或只有英文时，另一个留空字符串。
- supplier_brands：每个投标单位一条，supplier_name 为公司全称（带"有限公司/集团/科技/设备"等后缀），brand 为该单位的参与品牌（中文）。
- 不要把品牌当成投标单位，也不要把投标单位当成品牌。
- 无法识别的字段返回空字符串，**不要瞎猜**。

返回 JSON 格式：
{"material_class": "水阀门", "brand_requirement": [{"brand_en": "ALFA", "brand_cn": "阿法"}, {"brand_en": "VEGA", "brand_cn": "威盖"}, {"brand_en": "ORION", "brand_cn": "猎户"}], "supplier_brands": [{"supplier_name": "星辉（上海）机电设备科技有限公司", "brand": "阿法"}, {"supplier_name": "上海宏达机电设备有限公司", "brand": "威盖"}, {"supplier_name": "上海金星阀门有限公司", "brand": "猎户"}]}

如果该页不是招标情况表，返回 {"material_class": "", "brand_requirement": [], "supplier_brands": []}。"""
