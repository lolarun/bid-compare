// ECharts 按需引入封装
import * as echarts from 'echarts/core'
import {
  LineChart,
  BarChart,
  PieChart,
  TreemapChart,
  ScatterChart,
  GraphChart,
  RadarChart,
} from 'echarts/charts'
import {
  GridComponent,
  TooltipComponent,
  TitleComponent,
  LegendComponent,
  DataZoomComponent,
  ToolboxComponent,
  MarkLineComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

echarts.use([
  LineChart,
  BarChart,
  PieChart,
  TreemapChart,
  ScatterChart,
  GraphChart,
  RadarChart,
  GridComponent,
  TooltipComponent,
  TitleComponent,
  LegendComponent,
  DataZoomComponent,
  ToolboxComponent,
  MarkLineComponent,
  CanvasRenderer,
])

export default echarts
export type { EChartsType } from 'echarts/core'
