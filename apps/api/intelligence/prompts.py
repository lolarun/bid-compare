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

请仔细阅读图片中的供应商报价单，提取每一行的报价明细：
1. 供应商基本信息：供应商/投标单位名称、报价日期
2. 报价明细：材料名称、规格型号、品牌、材质、单位、数量、含税单价、不含税单价、总价、税率、备注

关于 supplier_name（供应商名）——非常重要：
- supplier_name 是【投标/报价单位的公司全称】，通常出现在封面、投标函、报价单抬头或落款盖章处，
  一般带有"有限公司/股份/集团/厂/经营部/商行/贸易/实业/工程/设备/科技/中心"等机构后缀，例如"上海绵存设备有限公司"。
- 【绝对不要】把明细表"品牌"列里的产品品牌当作 supplier_name，例如 KITZ、伯尔梅特、正泰、良工 等是产品品牌，不是投标公司名。
- 如果整张报价单只报一个品牌，也不能用该品牌充当公司名；找不到明确的公司全称时，supplier_name 留空字符串，不要瞎猜。

提取要求：
- material_type（材质）：若报价表有独立"材质/牌号"列（如不锈钢/球墨铸铁/碳钢/黄铜），按原文填入；无则留空字符串
- 区分 unit_price（含税单价）与 unit_price_excl_tax（不含税单价）；若只有一个价格数字，默认填到 unit_price
- 总价若文档已标注则使用文档值；否则留 null（不要自己算）
- 税率以小数表示，如 0.13 表示 13%
- 品牌按原文保留，不强制归一
- 备注字段保留付款方式、保修期、交货期等关键条款摘要（精简到 50 字内）
- 数字字段如果是范围（如 '100-200'），取下限
- 无法识别的字段返回空字符串或 null，**不要瞎猜**
- 不要把表头、合计行、小计行当作数据行

对于阀门类材料（截止阀/闸阀/止回阀/球阀/蝶阀/减压阀/疏水阀/过滤器等），
额外填写 canonical 对象，提取结构化技术参数：
- valve_type: 阀门类型，如"截止阀"、"止回阀"、"Y型过滤器"（按原文，不要缩写）
- dn: 公称直径，格式"DN25"（含前缀）；Φ57/2寸/50mm 请转换为 DN 格式
- pn: 公称压力，格式"PN16"；1.6MPa 请转换为 PN16
- material: 主材质，如"不锈钢"、"铸铁"、"球墨铸铁"、"黄铜"
- connection: 连接方式，如"螺纹"、"法兰"、"焊接"、"卡箍"
非阀门类材料，canonical 留空对象 {}。

OCR 纠错（阀门类）——新增字段 normalized_material / ocr_correction_reason：
当你发现材料名称存在明显形近字 OCR 错误时，填写：
- normalized_material: 纠错后的正确名称（确信是OCR错别字时才填，否则留空字符串）
- ocr_correction_reason: 纠错依据，格式：[错误词] + 词表命中 + 相邻行规格连续性

合法阀门词表（材料名称与下列词条高度相似但有错别字，则填写纠错）：
闸阀 / 截止阀 / 止回阀 / 球阀 / 蝶阀 / 安全阀 / 减压阀
橡胶瓣止回阀 / 节能消声止回阀 / 缓闭式止回阀 / 消声止回阀
低阻力倒流防止器 / 倒流防止器
小阻力可调式减压阀组 / 减压阀组
Y型过滤器 / 篮式过滤器

形近字纠错示例：
- "阀阀 DN50" 且同页有闸阀规格序列 → normalized_material="闸阀"（"阀" OCR重复）
- "橡胶海止回阀 DN80" 且同页有橡胶瓣止回阀系列 → "橡胶瓣止回阀"（"海"≈"瓣"形近）
- "橡胶脚止回阀 DN100" 同理 → "橡胶瓣止回阀"
- "倒流防上器" → "倒流防止器"（"上"≈"止"形近）
- "减压阀组" 完整正确 → normalized_material 留空

重要：material 字段仍按文档原文填写；normalized_material 仅在确认为OCR错别字时才填。
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
