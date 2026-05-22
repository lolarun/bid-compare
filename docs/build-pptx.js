/**
 * MEMPAS 功能演示 PPT 生成脚本
 * Run: node docs/build-pptx.js
 */
const pptxgen = require("pptxgenjs");
const path = require("path");
const fs = require("fs");

const SHOTS = path.join(__dirname, "demo-screenshots");
const OUT = path.join(__dirname, "MEMPAS_功能演示.pptx");

// ── Color Palette (Ocean Professional) ──
const C = {
  primary:   "1677FF",  // MEMPAS blue
  dark:      "0D1B3E",  // deep navy for title slides
  darkAlt:   "132952",  // slightly lighter navy
  white:     "FFFFFF",
  offWhite:  "F7F8FA",
  text:      "1D2129",
  textSub:   "4E5969",
  textMuted: "86909C",
  accent:    "52C41A",  // green
  warn:      "FA8C16",  // orange
  danger:    "FF4D4F",  // red
  blue10:    "E8F4FF",  // light blue bg
};

// Helper: shadow factory (fresh object each call)
const cardShadow = () => ({ type: "outer", blur: 4, offset: 1, angle: 135, color: "000000", opacity: 0.08 });

// Helper: load image as base64
function imgData(filename) {
  const fp = path.join(SHOTS, filename);
  if (!fs.existsSync(fp)) { console.warn("MISSING:", fp); return null; }
  const buf = fs.readFileSync(fp);
  return "image/jpeg;base64," + buf.toString("base64");
}

(async () => {
  const pres = new pptxgen();
  pres.layout = "LAYOUT_16x9"; // 10" x 5.625"
  pres.author = "MEMPAS";
  pres.title = "MEMPAS 机电材料查询比价分析系统 功能演示";

  // ============================================================
  // SLIDE 1: Title
  // ============================================================
  {
    const s = pres.addSlide();
    s.background = { color: C.dark };
    // Decorative shape
    s.addShape(pres.shapes.RECTANGLE, { x: 0, y: 0, w: 10, h: 0.06, fill: { color: C.primary } });
    // Logo mark
    s.addShape(pres.shapes.RECTANGLE, {
      x: 4.15, y: 1.1, w: 0.55, h: 0.55, fill: { color: C.primary },
      rectRadius: 0.08,
    });
    s.addText("M", { x: 4.15, y: 1.1, w: 0.55, h: 0.55, fontSize: 22, bold: true, color: C.white, align: "center", valign: "middle", margin: 0 });
    s.addText("MEMPAS", { x: 4.75, y: 1.1, w: 3, h: 0.55, fontSize: 26, bold: true, color: C.white, valign: "middle", margin: 0, charSpacing: 4 });
    // Title
    s.addText("机电材料查询比价分析系统", { x: 1, y: 2.1, w: 8, h: 0.7, fontSize: 32, bold: true, color: C.white, align: "center", margin: 0 });
    s.addText("功能演示", { x: 1, y: 2.85, w: 8, h: 0.6, fontSize: 22, color: C.primary, align: "center", margin: 0 });
    // Divider
    s.addShape(pres.shapes.LINE, { x: 3.5, y: 3.7, w: 3, h: 0, line: { color: C.primary, width: 1.5 } });
    // Subtitle info
    s.addText([
      { text: "上海建工一建集团有限公司", options: { fontSize: 14, color: C.textMuted, breakLine: true } },
      { text: "2026年5月", options: { fontSize: 12, color: C.textMuted } },
    ], { x: 1, y: 4.0, w: 8, h: 0.8, align: "center", margin: 0 });
    // Bottom bar
    s.addShape(pres.shapes.RECTANGLE, { x: 0, y: 5.425, w: 10, h: 0.2, fill: { color: C.primary } });
  }

  // ============================================================
  // SLIDE 2: Overview / KPI
  // ============================================================
  {
    const s = pres.addSlide();
    s.background = { color: C.white };
    s.addShape(pres.shapes.RECTANGLE, { x: 0, y: 0, w: 10, h: 0.04, fill: { color: C.primary } });
    s.addText("系统概述", { x: 0.6, y: 0.3, w: 8, h: 0.5, fontSize: 24, bold: true, color: C.text, margin: 0 });
    s.addText("覆盖 物料标准化 → 供应商管理 → 智能邀标 → 横向比价 → 历史追溯 全链条", {
      x: 0.6, y: 0.85, w: 8.8, h: 0.4, fontSize: 13, color: C.textSub, margin: 0
    });

    // KPI cards
    const kpis = [
      { num: "11,983", label: "物料标准库", color: C.primary },
      { num: "57",     label: "供应商档案", color: C.accent },
      { num: "3",      label: "覆盖专业",   color: C.warn },
      { num: "10+",    label: "覆盖品类",   color: C.danger },
      { num: "11,983", label: "历史报价",   color: C.primary },
    ];
    const cardW = 1.65, gap = 0.2, totalW = kpis.length * cardW + (kpis.length - 1) * gap;
    const startX = (10 - totalW) / 2;
    kpis.forEach((k, i) => {
      const cx = startX + i * (cardW + gap);
      s.addShape(pres.shapes.RECTANGLE, { x: cx, y: 1.55, w: cardW, h: 1.1, fill: { color: C.offWhite }, shadow: cardShadow() });
      s.addShape(pres.shapes.RECTANGLE, { x: cx, y: 1.55, w: cardW, h: 0.05, fill: { color: k.color } });
      s.addText(k.num, { x: cx, y: 1.7, w: cardW, h: 0.5, fontSize: 24, bold: true, color: k.color, align: "center", margin: 0 });
      s.addText(k.label, { x: cx, y: 2.2, w: cardW, h: 0.35, fontSize: 11, color: C.textMuted, align: "center", margin: 0 });
    });

    // Feature bullets
    const features = [
      "AI 驱动：Qwen-VL OCR + 3 级 fallback 智能抽取引擎",
      "五维评分：价格竞争力 / 历史合作 / 报价完整度 / 品牌合规 / 商务条款",
      "横向比价矩阵：多供应商同屏对比，色标告警一目了然",
      "全链条导出：6 个模块均支持一键导出 Excel",
    ];
    s.addText(features.map((f, i) => ({
      text: f,
      options: { bullet: true, fontSize: 12, color: C.textSub, breakLine: i < features.length - 1 }
    })), { x: 0.8, y: 3.0, w: 8.4, h: 2.2, paraSpaceAfter: 6, margin: 0 });
  }

  // ============================================================
  // SLIDE 3: Login
  // ============================================================
  {
    const s = pres.addSlide();
    s.background = { color: C.offWhite };
    s.addShape(pres.shapes.RECTANGLE, { x: 0, y: 0, w: 10, h: 0.04, fill: { color: C.primary } });
    s.addText("1. 系统入口", { x: 0.6, y: 0.25, w: 5, h: 0.45, fontSize: 22, bold: true, color: C.text, margin: 0 });
    const d = imgData("01-login.jpg");
    if (d) {
      s.addImage({ data: d, x: 1.2, y: 0.9, w: 7.6, h: 3.54, sizing: { type: "contain", w: 7.6, h: 3.54 }, shadow: cardShadow() });
    }
    s.addText([
      { text: '账号密码登录  |  支持"记住密码"  |  上海建工一建', options: { fontSize: 11, color: C.textMuted } },
    ], { x: 0.6, y: 4.7, w: 8.8, h: 0.35, align: "center", margin: 0 });
  }

  // ============================================================
  // SLIDE 4: Dashboard
  // ============================================================
  {
    const s = pres.addSlide();
    s.background = { color: C.offWhite };
    s.addShape(pres.shapes.RECTANGLE, { x: 0, y: 0, w: 10, h: 0.04, fill: { color: C.primary } });
    s.addText("2. 仪表盘 — 全局概览", { x: 0.6, y: 0.2, w: 6, h: 0.45, fontSize: 22, bold: true, color: C.text, margin: 0 });
    s.addText("工作台 / 仪表盘", { x: 7, y: 0.2, w: 2.5, h: 0.45, fontSize: 11, color: C.textMuted, align: "right", margin: 0 });
    const d = imgData("02-dashboard-top.jpg");
    if (d) {
      s.addImage({ data: d, x: 0.4, y: 0.8, w: 5.5, h: 2.56, sizing: { type: "contain", w: 5.5, h: 2.56 }, shadow: cardShadow() });
    }
    // Right side: key points
    s.addText("四大核心指标卡", { x: 6.2, y: 0.85, w: 3.4, h: 0.35, fontSize: 14, bold: true, color: C.primary, margin: 0 });
    const items = [
      "累计入库材料 11,983 条",
      "本月比价次数 11,983 次",
      "偏差预警 3 项",
      "覆盖 3 专业 / 342 类",
    ];
    s.addText(items.map((t, i) => ({
      text: t, options: { bullet: true, fontSize: 11, color: C.textSub, breakLine: i < items.length - 1 }
    })), { x: 6.2, y: 1.3, w: 3.4, h: 1.6, paraSpaceAfter: 4, margin: 0 });

    // Heatmap below
    s.addText("品类全景热力图", { x: 6.2, y: 2.85, w: 3.4, h: 0.35, fontSize: 14, bold: true, color: C.primary, margin: 0 });
    s.addText([
      { text: "矩形面积 = 数据量", options: { bullet: true, fontSize: 11, color: C.textSub, breakLine: true } },
      { text: "桥架、配电箱数据最厚", options: { bullet: true, fontSize: 11, color: C.textSub, breakLine: true } },
      { text: "支持气泡图切换", options: { bullet: true, fontSize: 11, color: C.textSub } },
    ], { x: 6.2, y: 3.2, w: 3.4, h: 1.2, paraSpaceAfter: 4, margin: 0 });

    const d2 = imgData("03-dashboard-heatmap.jpg");
    if (d2) {
      s.addImage({ data: d2, x: 0.4, y: 3.55, w: 5.5, h: 1.7, sizing: { type: "contain", w: 5.5, h: 1.7 }, shadow: cardShadow() });
    }
  }

  // ============================================================
  // SLIDE 5: Materials
  // ============================================================
  {
    const s = pres.addSlide();
    s.background = { color: C.offWhite };
    s.addShape(pres.shapes.RECTANGLE, { x: 0, y: 0, w: 10, h: 0.04, fill: { color: C.primary } });
    s.addText("3. 物料主数据 — 标准化底座", { x: 0.6, y: 0.2, w: 6, h: 0.45, fontSize: 22, bold: true, color: C.text, margin: 0 });
    s.addText("数据管理 / 物料主数据", { x: 7, y: 0.2, w: 2.5, h: 0.45, fontSize: 11, color: C.textMuted, align: "right", margin: 0 });
    const d = imgData("04-materials.jpg");
    if (d) {
      s.addImage({ data: d, x: 0.4, y: 0.8, w: 6.0, h: 2.8, sizing: { type: "contain", w: 6.0, h: 2.8 }, shadow: cardShadow() });
    }
    // Right: feature list
    s.addText("分类树 + 明细表", { x: 6.6, y: 0.85, w: 3.0, h: 0.35, fontSize: 14, bold: true, color: C.primary, margin: 0 });
    const mItems = [
      "三级专业分类（电气/给排水/暖通）",
      "物料编码、标准名称、规格材质",
      "推荐品牌、参考价格区间",
      "AI 名称标准化",
      "一键导出 Excel",
    ];
    s.addText(mItems.map((t, i) => ({
      text: t, options: { bullet: true, fontSize: 11, color: C.textSub, breakLine: i < mItems.length - 1 }
    })), { x: 6.6, y: 1.3, w: 3.0, h: 2.2, paraSpaceAfter: 4, margin: 0 });

    // Bottom: classification tree summary
    s.addShape(pres.shapes.RECTANGLE, { x: 0.6, y: 3.85, w: 8.8, h: 1.45, fill: { color: C.white }, shadow: cardShadow() });
    const treeData = [
      [
        { text: "专业", options: { bold: true, color: C.white, fill: { color: C.primary }, align: "center" } },
        { text: "品类数", options: { bold: true, color: C.white, fill: { color: C.primary }, align: "center" } },
        { text: "主要品类", options: { bold: true, color: C.white, fill: { color: C.primary }, align: "center" } },
      ],
      ["电气", "6,620", "桥架 2049 / 配电箱 4433 / 母线槽 138"],
      ["给排水", "2,125", "阀门 914 / 潜水泵 617 / 不锈钢管 580"],
      ["暖通", "3,238", "风口风阀 3100 / 风机盘管 92 / 空调泵 46"],
    ];
    s.addTable(treeData, {
      x: 0.8, y: 3.95, w: 8.4, colW: [1.2, 1.2, 6.0],
      fontSize: 11, color: C.textSub,
      border: { pt: 0.5, color: "E5E6EB" },
      rowH: [0.3, 0.28, 0.28, 0.28],
    });
  }

  // ============================================================
  // SLIDE 6: Suppliers
  // ============================================================
  {
    const s = pres.addSlide();
    s.background = { color: C.offWhite };
    s.addShape(pres.shapes.RECTANGLE, { x: 0, y: 0, w: 10, h: 0.04, fill: { color: C.primary } });
    s.addText("4. 供应商管理 — 五维评分体系", { x: 0.6, y: 0.2, w: 6, h: 0.45, fontSize: 22, bold: true, color: C.text, margin: 0 });
    s.addText("数据管理 / 供应商管理", { x: 7, y: 0.2, w: 2.5, h: 0.45, fontSize: 11, color: C.textMuted, align: "right", margin: 0 });
    const d = imgData("05-suppliers.jpg");
    if (d) {
      s.addImage({ data: d, x: 0.4, y: 0.8, w: 5.8, h: 2.7, sizing: { type: "contain", w: 5.8, h: 2.7 }, shadow: cardShadow() });
    }
    // Right side
    s.addText("五维评分", { x: 6.4, y: 0.85, w: 3.2, h: 0.35, fontSize: 14, bold: true, color: C.primary, margin: 0 });
    const dims = [
      { name: "价格竞争力", pct: "40%" },
      { name: "历史合作",   pct: "20%" },
      { name: "报价完整度", pct: "15%" },
      { name: "品牌合规",   pct: "15%" },
      { name: "商务条款",   pct: "10%" },
    ];
    dims.forEach((d, i) => {
      const y = 1.3 + i * 0.38;
      s.addShape(pres.shapes.RECTANGLE, { x: 6.5, y: y, w: 2.6, h: 0.22, fill: { color: "E8E8E8" } });
      const pctNum = parseInt(d.pct) / 100;
      s.addShape(pres.shapes.RECTANGLE, { x: 6.5, y: y, w: 2.6 * pctNum, h: 0.22, fill: { color: C.primary } });
      s.addText(d.name, { x: 6.4, y: y - 0.02, w: 1.8, h: 0.26, fontSize: 10, color: C.text, margin: 0 });
      s.addText(d.pct, { x: 9.15, y: y - 0.02, w: 0.5, h: 0.26, fontSize: 10, bold: true, color: C.primary, margin: 0 });
    });

    // Bottom: highlights
    s.addShape(pres.shapes.RECTANGLE, { x: 0.6, y: 3.8, w: 8.8, h: 1.5, fill: { color: C.white }, shadow: cardShadow() });
    s.addText("供应商画像", { x: 0.8, y: 3.9, w: 2, h: 0.3, fontSize: 13, bold: true, color: C.primary, margin: 0 });
    s.addText([
      { text: "AI 五维评分雷达图", options: { bullet: true, fontSize: 11, color: C.textSub, breakLine: true } },
      { text: "历史合作时间线", options: { bullet: true, fontSize: 11, color: C.textSub, breakLine: true } },
      { text: "供应品类分布", options: { bullet: true, fontSize: 11, color: C.textSub, breakLine: true } },
      { text: "批量导入/导出名单", options: { bullet: true, fontSize: 11, color: C.textSub } },
    ], { x: 0.8, y: 4.25, w: 4.0, h: 1.0, paraSpaceAfter: 2, margin: 0 });

    // KPI mini cards
    const supKpis = [
      { n: "57", l: "供应商总数" }, { n: "17", l: "高频合作" },
    ];
    supKpis.forEach((k, i) => {
      const cx = 6.0 + i * 1.8;
      s.addText(k.n, { x: cx, y: 4.0, w: 1.5, h: 0.5, fontSize: 26, bold: true, color: C.primary, align: "center", margin: 0 });
      s.addText(k.l, { x: cx, y: 4.5, w: 1.5, h: 0.3, fontSize: 10, color: C.textMuted, align: "center", margin: 0 });
    });
  }

  // ============================================================
  // SLIDE 7: Compare Config + Workflow
  // ============================================================
  {
    const s = pres.addSlide();
    s.background = { color: C.offWhite };
    s.addShape(pres.shapes.RECTANGLE, { x: 0, y: 0, w: 10, h: 0.04, fill: { color: C.primary } });
    s.addText("5. 招标比价分析 — 核心功能", { x: 0.6, y: 0.2, w: 6, h: 0.45, fontSize: 22, bold: true, color: C.text, margin: 0 });
    s.addText("业务功能 / 招标比价分析", { x: 7, y: 0.2, w: 2.5, h: 0.45, fontSize: 11, color: C.textMuted, align: "right", margin: 0 });

    // 3-step flow
    const steps = ["配置任务", "录入报价", "比价结果"];
    steps.forEach((st, i) => {
      const cx = 1.5 + i * 2.8;
      s.addShape(pres.shapes.OVAL, { x: cx, y: 0.8, w: 0.4, h: 0.4, fill: { color: i < 2 ? C.accent : C.primary } });
      s.addText(String(i + 1), { x: cx, y: 0.8, w: 0.4, h: 0.4, fontSize: 12, bold: true, color: C.white, align: "center", valign: "middle", margin: 0 });
      s.addText(st, { x: cx - 0.3, y: 1.22, w: 1.0, h: 0.3, fontSize: 11, bold: true, color: i < 2 ? C.accent : C.primary, align: "center", margin: 0 });
      if (i < 2) {
        s.addShape(pres.shapes.LINE, { x: cx + 0.45, y: 1.0, w: 2.3, h: 0, line: { color: "D9D9D9", width: 1.5 } });
      }
    });

    const d = imgData("06-compare-config.jpg");
    if (d) {
      s.addImage({ data: d, x: 0.4, y: 1.7, w: 5.5, h: 2.56, sizing: { type: "contain", w: 5.5, h: 2.56 }, shadow: cardShadow() });
    }

    // Right side config
    s.addText("配置内容", { x: 6.2, y: 1.75, w: 3.4, h: 0.3, fontSize: 14, bold: true, color: C.primary, margin: 0 });
    s.addText([
      { text: "项目（可选，不选则跨项目比价）", options: { bullet: true, fontSize: 11, color: C.textSub, breakLine: true } },
      { text: "品类（必选，桥架/母线槽/配电箱…）", options: { bullet: true, fontSize: 11, color: C.textSub, breakLine: true } },
      { text: "参与供应商（可多选）", options: { bullet: true, fontSize: 11, color: C.textSub } },
    ], { x: 6.2, y: 2.1, w: 3.4, h: 1.2, paraSpaceAfter: 4, margin: 0 });

    s.addText("报价录入", { x: 6.2, y: 3.3, w: 3.4, h: 0.3, fontSize: 14, bold: true, color: C.primary, margin: 0 });
    s.addText([
      { text: "上传 PDF/图片 → AI OCR 自动识别", options: { bullet: true, fontSize: 11, color: C.textSub, breakLine: true } },
      { text: "或直接使用历史数据跳过上传", options: { bullet: true, fontSize: 11, color: C.textSub } },
    ], { x: 6.2, y: 3.65, w: 3.4, h: 0.8, paraSpaceAfter: 4, margin: 0 });
  }

  // ============================================================
  // SLIDE 8: Bid Matrix (CORE)
  // ============================================================
  {
    const s = pres.addSlide();
    s.background = { color: C.dark };
    s.addShape(pres.shapes.RECTANGLE, { x: 0, y: 0, w: 10, h: 0.04, fill: { color: C.primary } });
    s.addText("横向比价矩阵", { x: 0.6, y: 0.2, w: 5, h: 0.5, fontSize: 24, bold: true, color: C.white, margin: 0 });
    s.addText("⭐ 核心价值展示", { x: 7, y: 0.2, w: 2.5, h: 0.5, fontSize: 13, color: C.primary, align: "right", margin: 0 });

    const d = imgData("07-bid-matrix.jpg");
    if (d) {
      // Full width screenshot
      s.addImage({ data: d, x: 0.3, y: 0.8, w: 9.4, h: 4.38, sizing: { type: "contain", w: 9.4, h: 4.38 }, shadow: cardShadow() });
    }

    // Bottom info bar
    s.addShape(pres.shapes.RECTANGLE, { x: 0, y: 5.25, w: 10, h: 0.375, fill: { color: C.primary } });
    s.addText("3 供应商同屏对比  |  色标告警（绿/黄/红）  |  AI 推荐  |  虚拟滚动 500+ 行  |  一键导出 Excel", {
      x: 0.5, y: 5.25, w: 9, h: 0.375, fontSize: 11, color: C.white, align: "center", valign: "middle", margin: 0
    });
  }

  // ============================================================
  // SLIDE 9: Matrix Detail
  // ============================================================
  {
    const s = pres.addSlide();
    s.background = { color: C.white };
    s.addShape(pres.shapes.RECTANGLE, { x: 0, y: 0, w: 10, h: 0.04, fill: { color: C.primary } });
    s.addText("比价矩阵 — 详解", { x: 0.6, y: 0.2, w: 6, h: 0.45, fontSize: 22, bold: true, color: C.text, margin: 0 });

    // Matrix structure table
    s.addText("矩阵结构", { x: 0.6, y: 0.8, w: 4, h: 0.3, fontSize: 14, bold: true, color: C.primary, margin: 0 });
    const matrixCols = [
      [
        { text: "列", options: { bold: true, color: C.white, fill: { color: C.primary }, align: "center" } },
        { text: "说明", options: { bold: true, color: C.white, fill: { color: C.primary } } },
      ],
      ["材料", "物料名称 + 规格"],
      ["历史均价", "所有历史报价的均值"],
      ["合理史低", "剔除异常后的历史最低价"],
      ["A/B/C 供应商", "各供应商报价 + 偏差率 + 告警"],
      ["最低偏差", "最优供应商的偏差百分比"],
      ["推荐", "AI 综合推荐的供应商代号"],
    ];
    s.addTable(matrixCols, {
      x: 0.6, y: 1.15, w: 4.3, colW: [1.5, 2.8],
      fontSize: 11, color: C.textSub,
      border: { pt: 0.5, color: "E5E6EB" },
      rowH: [0.28, 0.26, 0.26, 0.26, 0.26, 0.26, 0.26],
    });

    // Alert system
    s.addText("色标告警系统", { x: 5.4, y: 0.8, w: 4, h: 0.3, fontSize: 14, bold: true, color: C.primary, margin: 0 });
    // Green
    s.addShape(pres.shapes.RECTANGLE, { x: 5.5, y: 1.25, w: 4.0, h: 0.55, fill: { color: "F6FFED" } });
    s.addText([
      { text: "● 正常", options: { bold: true, fontSize: 12, color: C.accent } },
      { text: "  偏差 ≤ 黄色阈值", options: { fontSize: 11, color: C.textSub } },
    ], { x: 5.6, y: 1.25, w: 3.8, h: 0.55, valign: "middle", margin: 0 });
    // Yellow
    s.addShape(pres.shapes.RECTANGLE, { x: 5.5, y: 1.9, w: 4.0, h: 0.55, fill: { color: "FFFBE6" } });
    s.addText([
      { text: "● 需关注", options: { bold: true, fontSize: 12, color: C.warn } },
      { text: "  黄色 < 偏差 ≤ 红色阈值", options: { fontSize: 11, color: C.textSub } },
    ], { x: 5.6, y: 1.9, w: 3.8, h: 0.55, valign: "middle", margin: 0 });
    // Red
    s.addShape(pres.shapes.RECTANGLE, { x: 5.5, y: 2.55, w: 4.0, h: 0.55, fill: { color: "FFF2F0" } });
    s.addText([
      { text: "● 异常", options: { bold: true, fontSize: 12, color: C.danger } },
      { text: "  偏差 > 红色阈值", options: { fontSize: 11, color: C.textSub } },
    ], { x: 5.6, y: 2.55, w: 3.8, h: 0.55, valign: "middle", margin: 0 });

    // Summary info
    s.addText("汇总信息栏", { x: 0.6, y: 3.5, w: 4, h: 0.3, fontSize: 14, bold: true, color: C.primary, margin: 0 });
    const summaryItems = [
      { k: "品类", v: "母线槽" }, { k: "物料数", v: "556" }, { k: "供应商", v: "3 家" },
      { k: "推荐", v: "靖江启达" }, { k: "最优总价", v: "¥4,113,882" }, { k: "异常项", v: "536 项" },
    ];
    summaryItems.forEach((item, i) => {
      const col = i % 3, row = Math.floor(i / 3);
      const cx = 0.6 + col * 3.1, cy = 3.9 + row * 0.6;
      s.addText(item.k, { x: cx, y: cy, w: 1.0, h: 0.3, fontSize: 10, color: C.textMuted, margin: 0 });
      s.addText(item.v, { x: cx + 1.0, y: cy, w: 2.0, h: 0.3, fontSize: 13, bold: true, color: C.primary, margin: 0 });
    });

    // Tech highlight
    s.addShape(pres.shapes.RECTANGLE, { x: 5.4, y: 3.5, w: 4.0, h: 1.5, fill: { color: C.blue10 } });
    s.addText("技术亮点", { x: 5.6, y: 3.55, w: 3.6, h: 0.3, fontSize: 13, bold: true, color: C.primary, margin: 0 });
    s.addText([
      { text: "虚拟滚动：500+ 行流畅，DOM 减少 97%", options: { bullet: true, fontSize: 11, color: C.textSub, breakLine: true } },
      { text: "一键导出 Excel：带色标、带汇总行", options: { bullet: true, fontSize: 11, color: C.textSub } },
    ], { x: 5.6, y: 3.9, w: 3.6, h: 0.9, paraSpaceAfter: 4, margin: 0 });
  }

  // ============================================================
  // SLIDE 10: Invite
  // ============================================================
  {
    const s = pres.addSlide();
    s.background = { color: C.offWhite };
    s.addShape(pres.shapes.RECTANGLE, { x: 0, y: 0, w: 10, h: 0.04, fill: { color: C.primary } });
    s.addText("6. 邀标建议 — AI 智能推荐", { x: 0.6, y: 0.2, w: 6, h: 0.45, fontSize: 22, bold: true, color: C.text, margin: 0 });
    s.addText("业务功能 / 邀标建议", { x: 7, y: 0.2, w: 2.5, h: 0.45, fontSize: 11, color: C.textMuted, align: "right", margin: 0 });

    // Flow
    s.addShape(pres.shapes.RECTANGLE, { x: 0.6, y: 0.8, w: 8.8, h: 0.4, fill: { color: C.blue10 } });
    s.addText("上传招标文件 → 自动识别材料清单 → AI 推荐优秀供应商 → 一键发起邀请", {
      x: 0.8, y: 0.8, w: 8.4, h: 0.4, fontSize: 12, color: C.primary, align: "center", valign: "middle", margin: 0
    });

    const d = imgData("08-invite.jpg");
    if (d) {
      s.addImage({ data: d, x: 0.4, y: 1.4, w: 5.5, h: 2.56, sizing: { type: "contain", w: 5.5, h: 2.56 }, shadow: cardShadow() });
    }

    s.addText("推荐四维评分", { x: 6.2, y: 1.45, w: 3.4, h: 0.3, fontSize: 14, bold: true, color: C.primary, margin: 0 });
    s.addText([
      { text: "历史中标记录", options: { bullet: true, fontSize: 11, color: C.textSub, breakLine: true } },
      { text: "品类匹配度", options: { bullet: true, fontSize: 11, color: C.textSub, breakLine: true } },
      { text: "价格竞争力", options: { bullet: true, fontSize: 11, color: C.textSub, breakLine: true } },
      { text: "合作信用", options: { bullet: true, fontSize: 11, color: C.textSub } },
    ], { x: 6.2, y: 1.85, w: 3.4, h: 1.2, paraSpaceAfter: 4, margin: 0 });

    s.addText("功能亮点", { x: 6.2, y: 3.15, w: 3.4, h: 0.3, fontSize: 14, bold: true, color: C.primary, margin: 0 });
    s.addText([
      { text: "3 级 fallback 智能抽取", options: { bullet: true, fontSize: 11, color: C.textSub, breakLine: true } },
      { text: "每项附带 AI 推荐理由", options: { bullet: true, fontSize: 11, color: C.textSub, breakLine: true } },
      { text: "勾选后一键保存邀标名单", options: { bullet: true, fontSize: 11, color: C.textSub } },
    ], { x: 6.2, y: 3.5, w: 3.4, h: 1.0, paraSpaceAfter: 4, margin: 0 });
  }

  // ============================================================
  // SLIDE 11: History
  // ============================================================
  {
    const s = pres.addSlide();
    s.background = { color: C.offWhite };
    s.addShape(pres.shapes.RECTANGLE, { x: 0, y: 0, w: 10, h: 0.04, fill: { color: C.primary } });
    s.addText("7. 采购数据分析 — 历史追溯", { x: 0.6, y: 0.2, w: 6, h: 0.45, fontSize: 22, bold: true, color: C.text, margin: 0 });
    s.addText("数据管理 / 采购数据分析", { x: 7, y: 0.2, w: 2.5, h: 0.45, fontSize: 11, color: C.textMuted, align: "right", margin: 0 });
    const d = imgData("09-history.jpg");
    if (d) {
      s.addImage({ data: d, x: 0.4, y: 0.8, w: 6.0, h: 2.8, sizing: { type: "contain", w: 6.0, h: 2.8 }, shadow: cardShadow() });
    }
    s.addText("多维筛选", { x: 6.6, y: 0.85, w: 3.0, h: 0.3, fontSize: 14, bold: true, color: C.primary, margin: 0 });
    s.addText([
      { text: "专业类别（电气/给排水/暖通）", options: { bullet: true, fontSize: 11, color: C.textSub, breakLine: true } },
      { text: "关键词模糊搜索", options: { bullet: true, fontSize: 11, color: C.textSub, breakLine: true } },
      { text: "供应商（57 家可选）", options: { bullet: true, fontSize: 11, color: C.textSub, breakLine: true } },
      { text: "日期范围", options: { bullet: true, fontSize: 11, color: C.textSub, breakLine: true } },
      { text: "价格范围", options: { bullet: true, fontSize: 11, color: C.textSub } },
    ], { x: 6.6, y: 1.25, w: 3.0, h: 1.8, paraSpaceAfter: 4, margin: 0 });

    s.addShape(pres.shapes.RECTANGLE, { x: 6.6, y: 3.2, w: 2.8, h: 0.8, fill: { color: C.blue10 } });
    s.addText("11,983", { x: 6.6, y: 3.2, w: 2.8, h: 0.5, fontSize: 24, bold: true, color: C.primary, align: "center", margin: 0 });
    s.addText("条历史报价记录", { x: 6.6, y: 3.65, w: 2.8, h: 0.3, fontSize: 11, color: C.textMuted, align: "center", margin: 0 });
  }

  // ============================================================
  // SLIDE 12: Settings
  // ============================================================
  {
    const s = pres.addSlide();
    s.background = { color: C.offWhite };
    s.addShape(pres.shapes.RECTANGLE, { x: 0, y: 0, w: 10, h: 0.04, fill: { color: C.primary } });
    s.addText("8. 系统设置 — 灵活可配", { x: 0.6, y: 0.2, w: 6, h: 0.45, fontSize: 22, bold: true, color: C.text, margin: 0 });

    // Left: weights
    s.addText("评分权重", { x: 0.5, y: 0.75, w: 2, h: 0.3, fontSize: 13, bold: true, color: C.primary, margin: 0 });
    const dw = imgData("10-settings-weights.jpg");
    if (dw) {
      s.addImage({ data: dw, x: 0.3, y: 1.1, w: 4.6, h: 2.14, sizing: { type: "contain", w: 4.6, h: 2.14 }, shadow: cardShadow() });
    }

    // Right: thresholds
    s.addText("偏差阈值（分品类）", { x: 5.2, y: 0.75, w: 3, h: 0.3, fontSize: 13, bold: true, color: C.primary, margin: 0 });
    const dt = imgData("11-settings-thresholds.jpg");
    if (dt) {
      s.addImage({ data: dt, x: 5.1, y: 1.1, w: 4.6, h: 2.14, sizing: { type: "contain", w: 4.6, h: 2.14 }, shadow: cardShadow() });
    }

    // Bottom description
    s.addShape(pres.shapes.RECTANGLE, { x: 0.6, y: 3.5, w: 8.8, h: 1.8, fill: { color: C.white }, shadow: cardShadow() });
    s.addText([
      { text: "评分权重", options: { bold: true, fontSize: 12, color: C.primary, breakLine: true } },
      { text: "五维权重合计 100%，调整后即时生效，影响供应商画像和比价推荐排序", options: { fontSize: 11, color: C.textSub, breakLine: true, breakLine: true } },
      { text: "偏差阈值", options: { bold: true, fontSize: 12, color: C.primary, breakLine: true } },
      { text: "按品类独立配置黄色（需关注）和红色（异常）预警阈值", options: { fontSize: 11, color: C.textSub, breakLine: true, breakLine: true } },
      { text: "品牌档位", options: { bold: true, fontSize: 12, color: C.primary, breakLine: true } },
      { text: "按品类维护一线/二线/三线品牌分档，影响品牌合规维度评分", options: { fontSize: 11, color: C.textSub } },
    ], { x: 0.8, y: 3.6, w: 8.4, h: 1.6, paraSpaceAfter: 2, margin: 0 });
  }

  // ============================================================
  // SLIDE 13: Export
  // ============================================================
  {
    const s = pres.addSlide();
    s.background = { color: C.white };
    s.addShape(pres.shapes.RECTANGLE, { x: 0, y: 0, w: 10, h: 0.04, fill: { color: C.primary } });
    s.addText("9. 数据导出能力", { x: 0.6, y: 0.2, w: 6, h: 0.45, fontSize: 22, bold: true, color: C.text, margin: 0 });
    s.addText("全模块一键导出 Excel", { x: 0.6, y: 0.7, w: 8, h: 0.35, fontSize: 13, color: C.textSub, margin: 0 });

    const exportData = [
      [
        { text: "模块", options: { bold: true, color: C.white, fill: { color: C.primary }, align: "center" } },
        { text: "导出内容", options: { bold: true, color: C.white, fill: { color: C.primary } } },
        { text: "文件示例", options: { bold: true, color: C.white, fill: { color: C.primary } } },
      ],
      ["仪表盘", "采购概览 + 品类统计（双 Sheet）", "MEMPAS_仪表盘报表_20260522.xlsx"],
      ["供应商", "全量供应商名单含评分", "MEMPAS_供应商名单_20260522.xlsx"],
      ["物料", "物料标准库（按品类筛选）", "MEMPAS_物料主数据_桥架_20260522.xlsx"],
      ["采购历史", "历史报价含告警色标", "MEMPAS_采购数据_20260522.xlsx"],
      ["比价矩阵", "横向对比+偏差率+推荐+汇总行", "MEMPAS_比价矩阵_母线槽_20260522.xlsx"],
      ["操作日志", "审计日志记录", "MEMPAS_操作日志_20260522.xlsx"],
    ];
    s.addTable(exportData, {
      x: 0.6, y: 1.15, w: 8.8, colW: [1.3, 3.5, 4.0],
      fontSize: 11, color: C.textSub,
      border: { pt: 0.5, color: "E5E6EB" },
      rowH: [0.32, 0.3, 0.3, 0.3, 0.3, 0.3, 0.3],
    });

    // Excel features
    s.addText("Excel 文件特性", { x: 0.6, y: 3.6, w: 4, h: 0.3, fontSize: 14, bold: true, color: C.primary, margin: 0 });
    s.addText([
      { text: "蓝色表头 + 白色字体", options: { bullet: true, fontSize: 12, color: C.textSub, breakLine: true } },
      { text: "告警色标（绿/黄/红）自动着色", options: { bullet: true, fontSize: 12, color: C.textSub, breakLine: true } },
      { text: "列宽自适应", options: { bullet: true, fontSize: 12, color: C.textSub, breakLine: true } },
      { text: "支持中文文件名（RFC 5987）", options: { bullet: true, fontSize: 12, color: C.textSub } },
    ], { x: 0.6, y: 3.95, w: 8.8, h: 1.4, paraSpaceAfter: 4, margin: 0 });
  }

  // ============================================================
  // SLIDE 14: Architecture
  // ============================================================
  {
    const s = pres.addSlide();
    s.background = { color: C.white };
    s.addShape(pres.shapes.RECTANGLE, { x: 0, y: 0, w: 10, h: 0.04, fill: { color: C.primary } });
    s.addText("10. 技术架构", { x: 0.6, y: 0.2, w: 6, h: 0.45, fontSize: 22, bold: true, color: C.text, margin: 0 });

    // 3-tier architecture boxes
    const layers = [
      { label: "前端 (Port 3000)", detail: "Vue 3 + Vite + Ant Design Vue + ECharts\n虚拟滚动 (@tanstack/vue-virtual)", color: C.primary, y: 0.9 },
      { label: "后端 (Port 8002)", detail: "FastAPI + SQLAlchemy + SQLite\nAI: Qwen-VL (OCR) + 3级 Fallback\nExcel: openpyxl", color: "0D9488", y: 2.3 },
      { label: "数据层", detail: "SQLite\n11,983 物料 / 57 供应商 / 58 项目", color: "7C3AED", y: 3.85 },
    ];
    layers.forEach(l => {
      const h = l.label.includes("后端") ? 1.2 : 0.95;
      s.addShape(pres.shapes.RECTANGLE, { x: 1.5, y: l.y, w: 7, h: h, fill: { color: C.offWhite }, shadow: cardShadow() });
      s.addShape(pres.shapes.RECTANGLE, { x: 1.5, y: l.y, w: 0.08, h: h, fill: { color: l.color } });
      s.addText(l.label, { x: 1.8, y: l.y + 0.1, w: 6.5, h: 0.3, fontSize: 14, bold: true, color: l.color, margin: 0 });
      s.addText(l.detail, { x: 1.8, y: l.y + 0.4, w: 6.5, h: h - 0.5, fontSize: 11, color: C.textSub, margin: 0 });
    });
    // Arrows between layers
    s.addShape(pres.shapes.LINE, { x: 5, y: 1.88, w: 0, h: 0.4, line: { color: "D9D9D9", width: 2 } });
    s.addShape(pres.shapes.LINE, { x: 5, y: 3.52, w: 0, h: 0.3, line: { color: "D9D9D9", width: 2 } });
  }

  // ============================================================
  // SLIDE 15: Thank You
  // ============================================================
  {
    const s = pres.addSlide();
    s.background = { color: C.dark };
    s.addShape(pres.shapes.RECTANGLE, { x: 0, y: 0, w: 10, h: 0.06, fill: { color: C.primary } });
    // Logo
    s.addShape(pres.shapes.RECTANGLE, { x: 4.5, y: 1.3, w: 0.55, h: 0.55, fill: { color: C.primary }, rectRadius: 0.08 });
    s.addText("M", { x: 4.5, y: 1.3, w: 0.55, h: 0.55, fontSize: 22, bold: true, color: C.white, align: "center", valign: "middle", margin: 0 });
    s.addText("MEMPAS", { x: 5.1, y: 1.3, w: 2, h: 0.55, fontSize: 22, bold: true, color: C.white, valign: "middle", margin: 0, charSpacing: 3 });

    s.addText("感谢观看", { x: 1, y: 2.3, w: 8, h: 0.7, fontSize: 36, bold: true, color: C.white, align: "center", margin: 0 });
    s.addShape(pres.shapes.LINE, { x: 3.5, y: 3.2, w: 3, h: 0, line: { color: C.primary, width: 1.5 } });
    s.addText([
      { text: "机电材料查询比价分析系统", options: { fontSize: 14, color: C.textMuted, breakLine: true } },
      { text: "上海建工一建集团有限公司", options: { fontSize: 12, color: C.textMuted } },
    ], { x: 1, y: 3.5, w: 8, h: 0.8, align: "center", margin: 0 });
    s.addShape(pres.shapes.RECTANGLE, { x: 0, y: 5.425, w: 10, h: 0.2, fill: { color: C.primary } });
  }

  // ── Write file ──
  await pres.writeFile({ fileName: OUT });
  const size = (fs.statSync(OUT).size / 1024 / 1024).toFixed(1);
  console.log(`Done! ${OUT} (${size} MB)`);
})();
