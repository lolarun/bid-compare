<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  ApartmentOutlined,
  CheckCircleOutlined,
  CloudUploadOutlined,
  DatabaseOutlined,
  FileDoneOutlined,
  FileSearchOutlined,
  HomeOutlined,
  LoginOutlined,
  SettingOutlined,
  TeamOutlined,
} from '@ant-design/icons-vue'

const router = useRouter()
const activeSection = ref('compare')

const sections = [
  { key: 'compare', label: '比价怎么做' },
  { key: 'invite', label: '邀标怎么做' },
  { key: 'data', label: '数据怎么进来' },
  { key: 'rules', label: '规则怎么配置' },
  { key: 'materials', label: '物料怎么维护' },
  { key: 'faq', label: '常见问题' },
]

const compareSteps = [
  { title: '新建比价', desc: '进入招标比价分析，选择本次项目或品类。' },
  { title: '上传报价 PDF', desc: '直接上传供应商报价文件，不需要先选择供应商。' },
  { title: '核对识别结果', desc: '检查物料名称、规格型号、单位、价格和供应商名称。' },
  { title: '确认入库', desc: '确认后系统沉淀供应商、物料和历史报价。' },
  { title: '查看建议', desc: '系统按规则标出差异、匹配情况和推荐结果。' },
]

const inviteSteps = [
  { title: '上传招标清单', desc: '上传招标文件或物料清单，形成待邀标物料。' },
  { title: '核对物料', desc: '确认系统识别出的名称、专业、品类和规格。' },
  { title: '生成推荐', desc: '系统按历史报价、品类能力和规则推荐候选供应商。' },
  { title: '人工筛选', desc: '采购人员可增删供应商，形成邀请名单。' },
  { title: '保存结果', desc: '保存后作为本次邀标依据，后续可继续追溯。' },
]

const dataItems = [
  { icon: CloudUploadOutlined, title: '报价 PDF', desc: '比价时上传，系统自动识别供应商、物料和价格。' },
  { icon: FileSearchOutlined, title: '招标文件', desc: '邀标时上传，用来生成待采购物料清单。' },
  { icon: DatabaseOutlined, title: '主数据维护', desc: '物料、供应商和历史价格可在确认后持续沉淀。' },
]

const ruleItems = [
  { title: '偏差阈值', desc: '用于判断报价是否明显高于或低于其他报价。' },
  { title: '评分权重', desc: '用于综合价格、历史合作和匹配程度，形成推荐顺序。' },
  { title: '品牌档位', desc: '用于处理同一物料下不同品牌等级的价格差异。' },
]

const brandTierTips = [
  { title: '为什么会弹出', desc: '确认入库时，系统发现报价 PDF 中有新品牌未录入档位表。' },
  { title: '该怎么处理', desc: '真实品牌选择一档、二档或三档后保存；测试时可以先点“稍后再说”。' },
  { title: '需要注意', desc: '如果“球墨铸铁 EPDM”这类材质被识别成品牌，不建议直接保存为品牌档位，应在核对时修正。' },
]

function scrollToSection(key: string) {
  activeSection.value = key
  document.getElementById(key)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

function openInNewWindow(path: string) {
  window.open(router.resolve(path).href, '_blank', 'noopener,noreferrer')
}
</script>

<template>
  <div class="help-page">
    <header class="help-header">
      <button class="help-brand" type="button" @click="openInNewWindow('/login')">
        <img src="@/assets/logo.svg" alt="MEMPAS" />
        <span>MEMPAS 帮助中心</span>
      </button>
      <div class="help-header__actions">
        <a-button @click="openInNewWindow('/login')">
          <template #icon><LoginOutlined /></template>
          登录系统
        </a-button>
        <a-button type="primary" @click="openInNewWindow('/compare')">
          <template #icon><HomeOutlined /></template>
          开始比价
        </a-button>
      </div>
    </header>

    <main class="help-shell">
      <aside class="help-nav">
        <button
          v-for="item in sections"
          :key="item.key"
          type="button"
          :class="['help-nav__item', { 'help-nav__item--active': activeSection === item.key }]"
          @click="scrollToSection(item.key)"
        >
          {{ item.label }}
        </button>
      </aside>

      <div class="help-content">
        <section id="compare" class="help-section">
          <div class="section-heading">
            <div>
              <p class="eyebrow">场景一</p>
              <h1>招标比价：上传多家报价 PDF，系统自动比对</h1>
            </div>
            <a-tag color="blue">不用先选供应商</a-tag>
          </div>

          <div class="callout">
            <CloudUploadOutlined />
            <span>操作重点：把供应商报价 PDF 上传上去即可，系统会从文件里识别供应商和报价明细。</span>
          </div>

          <div class="step-grid">
            <article v-for="(step, index) in compareSteps" :key="step.title" class="step-card">
              <span class="step-card__num">{{ index + 1 }}</span>
              <h3>{{ step.title }}</h3>
              <p>{{ step.desc }}</p>
            </article>
          </div>

          <div class="result-strip">
            <div>
              <CheckCircleOutlined />
              横向价格矩阵
            </div>
            <div>
              <FileDoneOutlined />
              差异与异常提示
            </div>
            <div>
              <ApartmentOutlined />
              推荐成交建议
            </div>
          </div>

          <div class="brand-tier-help">
            <h3>确认入库时出现“发现新品牌”怎么办</h3>
            <div class="info-grid">
              <article v-for="item in brandTierTips" :key="item.title" class="info-card">
                <h3>{{ item.title }}</h3>
                <p>{{ item.desc }}</p>
              </article>
            </div>
          </div>
        </section>

        <section id="invite" class="help-section">
          <div class="section-heading">
            <div>
              <p class="eyebrow">场景二</p>
              <h2>邀标推荐：先识别清单，再推荐可邀请供应商</h2>
            </div>
            <a-tag color="green">适合找供应商</a-tag>
          </div>

          <div class="step-grid">
            <article v-for="(step, index) in inviteSteps" :key="step.title" class="step-card">
              <span class="step-card__num">{{ index + 1 }}</span>
              <h3>{{ step.title }}</h3>
              <p>{{ step.desc }}</p>
            </article>
          </div>

          <p class="plain-text">
            邀标不是直接比较报价，而是根据本次招标清单，结合历史报价、供应商供货品类和规则配置，
            帮采购人员先生成一份候选邀请名单。
          </p>
        </section>

        <section id="data" class="help-section">
          <div class="section-heading">
            <div>
              <p class="eyebrow">数据来源</p>
              <h2>物料、供应商和价格从哪里进入系统</h2>
            </div>
          </div>
          <div class="info-grid">
            <article v-for="item in dataItems" :key="item.title" class="info-card">
              <component :is="item.icon" class="info-card__icon" />
              <h3>{{ item.title }}</h3>
              <p>{{ item.desc }}</p>
            </article>
          </div>
          <p class="plain-text">
            比价确认后，系统会把识别出的供应商、物料和报价记录沉淀下来；下次邀标或比价时，这些记录会继续参与匹配和推荐。
          </p>
        </section>

        <section id="rules" class="help-section">
          <div class="section-heading">
            <div>
              <p class="eyebrow">规则配置</p>
              <h2>比价规则决定系统如何标记差异和给出推荐</h2>
            </div>
            <SettingOutlined class="section-icon" />
          </div>
          <div class="info-grid">
            <article v-for="item in ruleItems" :key="item.title" class="info-card">
              <h3>{{ item.title }}</h3>
              <p>{{ item.desc }}</p>
            </article>
          </div>
        </section>

        <section id="materials" class="help-section">
          <div class="section-heading">
            <div>
              <p class="eyebrow">物料主数据</p>
              <h2>物料用于把不同报价中的同类产品归到一起</h2>
            </div>
            <DatabaseOutlined class="section-icon" />
          </div>
          <div class="definition-list">
            <div>
              <strong>新增 / 编辑</strong>
              <span>维护物料名称、规格型号、专业、品类、子类、材质、单位和推荐品牌。</span>
            </div>
            <div>
              <strong>属性</strong>
              <span>用于查看系统识别和匹配时关注的物料字段，帮助判断两个报价行是否属于同一标准物料。</span>
            </div>
            <div>
              <strong>停用</strong>
              <span>停用后物料不再出现在默认列表和后续选择中，历史报价仍可追溯。</span>
            </div>
          </div>
        </section>

        <section id="faq" class="help-section">
          <div class="section-heading">
            <div>
              <p class="eyebrow">常见问题</p>
              <h2>常见的几个问题</h2>
            </div>
          </div>
          <a-collapse :default-active-key="['1', '2', '3', '4', '5']">
            <a-collapse-panel key="1" header="比价前需要先录入供应商吗？">
              不需要。上传供应商报价 PDF 后，系统会从文件内容中识别供应商；确认结果后会自动沉淀到供应商数据中。
            </a-collapse-panel>
            <a-collapse-panel key="2" header="系统为什么提示差异？">
              差异通常表示同一物料下某些报价字段或价格偏离规则阈值，需要人工核对是否为规格、品牌、单位或识别结果不同。
            </a-collapse-panel>
            <a-collapse-panel key="3" header="匹配是什么意思？">
              匹配是系统把报价 PDF 中的明细行归并到已有物料主数据的过程。匹配越准确，横向比价和供应商推荐越可靠。
            </a-collapse-panel>
            <a-collapse-panel key="4" header="停用物料会删除历史数据吗？">
              不会。停用只是不再作为后续可选物料使用，历史报价和分析记录仍保留。
            </a-collapse-panel>
            <a-collapse-panel key="5" header="确认入库时提示“发现新品牌”是什么意思？">
              这是系统发现报价中的品牌未配置档位。一档、二档、三档用于让不同品牌等级的报价更可比。
              真实品牌建议补录档位；如果识别出来的是材质或密封形式，可以先点“稍后再说”，后续在识别结果中修正。
            </a-collapse-panel>
          </a-collapse>

          <div class="footer-actions">
            <a-button @click="openInNewWindow('/login')">
              <template #icon><TeamOutlined /></template>
              回到登录页
            </a-button>
            <a-button type="primary" @click="openInNewWindow('/invite')">进入邀标推荐</a-button>
          </div>
        </section>
      </div>
    </main>
  </div>
</template>

<style scoped>
.help-page {
  min-height: 100vh;
  background: #f5f7fb;
  color: #1f2937;
}

.help-header {
  position: sticky;
  top: 0;
  z-index: 10;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  height: 64px;
  padding: 0 28px;
  background: #ffffff;
  border-bottom: 1px solid #e5e7eb;
}

.help-brand {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
  border: 0;
  background: transparent;
  padding: 0;
  color: #111827;
  font-size: 18px;
  font-weight: 700;
  cursor: pointer;
}

.help-brand img {
  width: 32px;
  height: 32px;
}

.help-header__actions {
  display: flex;
  align-items: center;
  gap: 10px;
}

.help-shell {
  display: grid;
  grid-template-columns: 220px minmax(0, 1fr);
  gap: 28px;
  width: min(1220px, calc(100% - 40px));
  margin: 0 auto;
  padding: 28px 0 48px;
}

.help-nav {
  position: sticky;
  top: 88px;
  align-self: start;
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 8px;
  background: #ffffff;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
}

.help-nav__item {
  width: 100%;
  min-height: 38px;
  padding: 8px 12px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  text-align: left;
  color: #4b5563;
  cursor: pointer;
}

.help-nav__item:hover,
.help-nav__item--active {
  background: #eaf2ff;
  color: #1677ff;
}

.help-content {
  min-width: 0;
}

.help-section {
  scroll-margin-top: 88px;
  padding: 28px;
  margin-bottom: 18px;
  background: #ffffff;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
}

.section-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 18px;
}

.section-heading h1,
.section-heading h2 {
  margin: 0;
  color: #111827;
  font-size: 24px;
  line-height: 1.35;
  font-weight: 700;
}

.eyebrow {
  margin: 0 0 6px;
  color: #1677ff;
  font-size: 13px;
  font-weight: 600;
}

.section-icon {
  color: #1677ff;
  font-size: 28px;
}

.callout {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 14px 16px;
  margin-bottom: 18px;
  border: 1px solid #b7d4ff;
  border-radius: 8px;
  background: #f0f6ff;
  color: #174ea6;
  font-weight: 600;
}

.step-grid {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 12px;
}

.step-card,
.info-card {
  min-height: 142px;
  padding: 16px;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  background: #ffffff;
}

.step-card__num {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  margin-bottom: 12px;
  border-radius: 50%;
  background: #1677ff;
  color: #ffffff;
  font-weight: 700;
}

.step-card h3,
.info-card h3 {
  margin: 0 0 8px;
  color: #111827;
  font-size: 16px;
  font-weight: 700;
}

.step-card p,
.info-card p,
.plain-text {
  margin: 0;
  color: #4b5563;
  line-height: 1.75;
}

.result-strip {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
  margin-top: 16px;
}

.result-strip div {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  min-height: 48px;
  border-radius: 8px;
  background: #f7fafc;
  color: #1f2937;
  font-weight: 600;
}

.brand-tier-help {
  margin-top: 18px;
  padding-top: 18px;
  border-top: 1px solid #eef2f7;
}

.brand-tier-help > h3 {
  margin: 0 0 12px;
  color: #111827;
  font-size: 17px;
  font-weight: 700;
}

.info-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 14px;
  margin-bottom: 16px;
}

.info-card__icon {
  margin-bottom: 12px;
  color: #1677ff;
  font-size: 26px;
}

.definition-list {
  display: grid;
  gap: 12px;
}

.definition-list div {
  display: grid;
  grid-template-columns: 110px minmax(0, 1fr);
  gap: 12px;
  padding: 14px 0;
  border-bottom: 1px solid #eef2f7;
}

.definition-list div:last-child {
  border-bottom: 0;
}

.definition-list strong {
  color: #111827;
}

.definition-list span {
  color: #4b5563;
  line-height: 1.7;
}

.footer-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  margin-top: 20px;
}

@media (max-width: 980px) {
  .help-shell {
    grid-template-columns: 1fr;
  }

  .help-nav {
    position: static;
    flex-direction: row;
    overflow-x: auto;
  }

  .help-nav__item {
    flex: 0 0 auto;
    width: auto;
    white-space: nowrap;
  }

  .step-grid,
  .info-grid,
  .result-strip {
    grid-template-columns: 1fr 1fr;
  }
}

@media (max-width: 640px) {
  .help-header {
    height: auto;
    padding: 14px 16px;
    align-items: flex-start;
    flex-direction: column;
  }

  .help-header__actions {
    width: 100%;
  }

  .help-header__actions :deep(.ant-btn) {
    flex: 1;
  }

  .help-shell {
    width: min(100% - 24px, 1220px);
    padding-top: 16px;
  }

  .help-section {
    padding: 20px;
  }

  .section-heading {
    flex-direction: column;
  }

  .section-heading h1,
  .section-heading h2 {
    font-size: 20px;
  }

  .step-grid,
  .info-grid,
  .result-strip,
  .definition-list div {
    grid-template-columns: 1fr;
  }

  .footer-actions {
    flex-direction: column;
  }
}
</style>
