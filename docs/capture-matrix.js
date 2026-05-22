/**
 * Capture bid matrix by rendering API data in a standalone page.
 */
const puppeteer = require('puppeteer-core');
const path = require('path');
const fs = require('fs');
const http = require('http');

const DIR = path.join(__dirname, 'demo-screenshots');
const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';

function postJSON(url, body) {
  return new Promise((resolve, reject) => {
    const payload = JSON.stringify(body);
    const u = new URL(url);
    const options = {
      hostname: u.hostname,
      port: u.port,
      path: u.pathname,
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(payload) },
    };
    const req = http.request(options, res => {
      let data = '';
      res.on('data', c => data += c);
      res.on('end', () => resolve(JSON.parse(data)));
    });
    req.on('error', reject);
    req.write(payload);
    req.end();
  });
}

(async () => {
  // Fetch real matrix data from API (POST endpoint)
  console.log('Fetching bid matrix from API...');
  const data = await postJSON('http://localhost:8002/api/analysis/bid-matrix', {
    supplier_ids: [57, 56, 55],
    category: '母线槽',
  });

  const suppliers = data.suppliers || [];
  const rows = (data.rows || []).slice(0, 15); // First 15 rows for screenshot
  const totals = data.totals || [];

  // Build HTML table that looks like the real app
  const supplierHeaders = suppliers.map(s =>
    `<th class="sup-price">${s.letter} ${s.name}<br><small>单价</small></th>
     <th class="sup-dev">${s.letter}<br><small>偏差</small></th>
     <th class="sup-alert">${s.letter}<br><small>告警</small></th>`
  ).join('');

  const dataRows = rows.map(r => {
    const supCells = r.suppliers.map(sc => {
      const price = sc.price != null ? `¥${Number(sc.price).toLocaleString()}` : '未报价';
      const dev = sc.deviation_pct != null ? `${(sc.deviation_pct * 100).toFixed(1)}%` : '—';
      const alert = sc.alert_level || 'normal';
      const alertClass = alert === 'red' ? 'alert-red' : alert === 'yellow' ? 'alert-yellow' : 'alert-green';
      const alertText = alert === 'red' ? '异常' : alert === 'yellow' ? '需关注' : '正常';
      return `<td class="num">${price}</td><td class="dev ${alertClass}">${dev}</td><td class="${alertClass} tag">${alertText}</td>`;
    }).join('');
    const minDev = r.min_deviation != null ? `${(r.min_deviation * 100).toFixed(1)}%` : '—';
    const rec = r.recommended || '';
    return `<tr>
      <td class="mat-name">${r.material_name || ''}<br><small>${r.spec || ''}</small></td>
      <td class="num">${r.historical_avg ? '¥' + Number(r.historical_avg.price).toLocaleString() : ''}</td>
      <td class="num low">${r.reasonable_low ? '¥' + Number(r.reasonable_low.price).toLocaleString() : ''}</td>
      ${supCells}
      <td class="num">${minDev}</td>
      <td class="rec">${rec}</td>
    </tr>`;
  }).join('');

  const html = `<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; background:#f5f5f5; padding:24px; }
.page { background:#fff; border-radius:8px; padding:20px; box-shadow: 0 1px 3px rgba(0,0,0,.08); }
.header { display:flex; align-items:center; gap:12px; margin-bottom:16px; }
.header h2 { font-size:18px; font-weight:600; color:#1d2129; }
.tags { display:flex; gap:8px; margin-bottom:16px; }
.tag-item { padding:4px 12px; border-radius:4px; font-size:13px; }
.tag-blue { background:#e8f4ff; color:#1677ff; }
.tag-green { background:#f6ffed; color:#52c41a; }
.tag-orange { background:#fff7e6; color:#fa8c16; }
.tag-red { background:#fff2f0; color:#ff4d4f; }
.count { font-size:13px; color:#86909c; margin-bottom:12px; }
table { width:100%; border-collapse:collapse; font-size:13px; }
th { background:#1677ff; color:#fff; padding:10px 8px; text-align:center; font-weight:500; white-space:nowrap; }
th small { font-weight:400; opacity:.8; }
td { padding:8px; border-bottom:1px solid #f0f0f0; }
.mat-name { min-width:140px; font-weight:500; color:#1d2129; }
.mat-name small { color:#86909c; font-weight:400; }
.num { text-align:right; font-family: "SF Mono", monospace; }
.low { color:#1677ff; }
.dev { text-align:center; }
.alert-red { background:#fff2f0; color:#ff4d4f; font-weight:500; }
.alert-yellow { background:#fffbe6; color:#fa8c16; }
.alert-green { background:#f6ffed; color:#52c41a; }
.tag { text-align:center; font-size:12px; border-radius:2px; }
.rec { text-align:center; font-weight:600; color:#1677ff; }
tr:hover { background:#fafafa; }
.sidebar { position:fixed; left:0; top:0; width:160px; height:100%; background:#001529; }
.sidebar .logo { padding:16px; color:#fff; font-size:16px; font-weight:600; display:flex; align-items:center; gap:8px; }
.sidebar .logo .icon { width:32px; height:32px; background:#1677ff; border-radius:6px; display:flex; align-items:center; justify-content:center; color:#fff; font-weight:700; }
.sidebar .menu-item { padding:10px 20px; color:rgba(255,255,255,.65); font-size:13px; }
.sidebar .menu-item.active { background:#1677ff; color:#fff; }
.main { margin-left:160px; }
.breadcrumb { font-size:13px; color:#86909c; margin-bottom:12px; }
.steps { display:flex; gap:0; margin-bottom:20px; }
.step { flex:1; text-align:center; padding:12px; position:relative; }
.step.done { color:#52c41a; }
.step.active { color:#1677ff; font-weight:600; }
.step-num { display:inline-block; width:24px; height:24px; border-radius:50%; text-align:center; line-height:24px; font-size:12px; margin-right:6px; }
.step.done .step-num { background:#52c41a; color:#fff; }
.step.active .step-num { background:#1677ff; color:#fff; }
.totals td { font-weight:600; background:#fafafa; border-top:2px solid #1677ff; }
</style></head><body>
<div class="sidebar">
  <div class="logo"><div class="icon">M</div> MEMPAS</div>
  <div class="menu-item">仪表盘</div>
  <div class="menu-item active">招标比价分析</div>
  <div class="menu-item">邀标建议</div>
  <div class="menu-item">物料主数据</div>
  <div class="menu-item">采购数据分析</div>
  <div class="menu-item">供应商管理</div>
</div>
<div class="main">
  <div class="breadcrumb">业务功能 / 招标比价分析</div>
  <div class="page">
    <div class="steps">
      <div class="step done"><span class="step-num">✓</span> 配置任务</div>
      <div class="step done"><span class="step-num">✓</span> 录入报价</div>
      <div class="step active"><span class="step-num">3</span> 比价结果</div>
    </div>
    <div class="header"><h2>横向对比矩阵</h2></div>
    <div class="tags">
      <span class="tag-item tag-blue">母线槽</span>
      <span class="tag-item tag-blue">${rows.length}+ 物料</span>
      <span class="tag-item tag-blue">${suppliers.length} 供应商</span>
      <span class="tag-item tag-green">推荐 ${suppliers.length > 0 ? suppliers[suppliers.length-1].name : ''}</span>
      <span class="tag-item tag-red">异常 ${rows.filter(r=>r.suppliers.some(s=>s.alert_level==='red')).length} 项</span>
    </div>
    <div class="count">共 ${data.rows ? data.rows.length : 0} 条材料</div>
    <table>
      <thead><tr>
        <th>材料</th><th>历史均价</th><th>合理史低</th>
        ${supplierHeaders}
        <th>最低偏差</th><th>推荐</th>
      </tr></thead>
      <tbody>${dataRows}</tbody>
      <tr class="totals"><td>汇总</td><td></td><td></td>
        ${totals.map(t => `<td class="num">¥${Number(t.total||0).toLocaleString()}</td><td class="dev">${t.avg_deviation ? (t.avg_deviation*100).toFixed(1)+'%' : ''}</td><td></td>`).join('')}
        <td></td><td></td>
      </tr>
    </table>
  </div>
</div>
</body></html>`;

  if (!fs.existsSync(DIR)) fs.mkdirSync(DIR, { recursive: true });
  const tmpHtml = path.join(DIR, '_matrix.html');
  fs.writeFileSync(tmpHtml, html);

  const browser = await puppeteer.launch({
    executablePath: CHROME,
    headless: 'new',
    args: ['--no-sandbox'],
    defaultViewport: { width: 1920, height: 1080 },
  });

  const page = await browser.newPage();
  await page.goto('file:///' + tmpHtml.replace(/\\/g, '/'), { waitUntil: 'networkidle2' });
  await new Promise(r => setTimeout(r, 1000));

  const fp = path.join(DIR, '07-bid-matrix.jpg');
  await page.screenshot({ path: fp, type: 'jpeg', quality: 92 });
  const size = (fs.statSync(fp).size / 1024).toFixed(1);
  console.log(`07-bid-matrix.jpg (${size} KB)`);

  fs.unlinkSync(tmpHtml); // cleanup temp HTML
  await browser.close();

  const files = fs.readdirSync(DIR).filter(f => f.endsWith('.jpg'));
  console.log(`Total: ${files.length} screenshots in ${DIR}`);
})();
