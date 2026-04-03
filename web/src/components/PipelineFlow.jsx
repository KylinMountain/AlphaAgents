import { useMemo } from 'react'
import {
  ReactFlow,
  Background,
  Controls,
  MarkerType,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import SourceNode from './nodes/SourceNode'
import StageNode from './nodes/StageNode'
import AgentNode from './nodes/AgentNode'

const nodeTypes = {
  source: SourceNode,
  stage: StageNode,
  agent: AgentNode,
}

// Source definitions
const DOMESTIC = [
  { id: 'eastmoney', label: '东方财富' },
  { id: 'eastmoney_live', label: '东财7x24' },
  { id: 'cls', label: '财联社' },
  { id: 'wallstreetcn', label: '华尔街见闻' },
  { id: 'jin10', label: '金十数据' },
  { id: 'xinhua', label: '新华社' },
  { id: 'pboc', label: '人民银行' },
]

const INTERNATIONAL = [
  { id: 'world_rss', label: 'BBC/CNBC' },
  { id: 'whitehouse', label: '白宫' },
  { id: 'fed', label: '美联储' },
  { id: 'sec', label: 'SEC' },
  { id: 'social', label: '社交媒体' },
]

function buildNodes(stages) {
  const nodes = []
  const getStatus = (id) => stages[`source_${id}`]?.status || stages[id]?.status || 'idle'
  const getMessage = (id) => stages[`source_${id}`]?.message || stages[id]?.message || ''

  // Source group labels
  nodes.push({
    id: 'label_domestic',
    type: 'default',
    position: { x: 30, y: 5 },
    data: { label: '国内数据源' },
    style: {
      background: 'transparent', border: 'none', color: '#94a3b8',
      fontSize: 13, fontWeight: 600, width: 100, padding: 0,
    },
    selectable: false, draggable: false,
  })
  nodes.push({
    id: 'label_intl',
    type: 'default',
    position: { x: 30, y: 265 },
    data: { label: '国际数据源' },
    style: {
      background: 'transparent', border: 'none', color: '#94a3b8',
      fontSize: 13, fontWeight: 600, width: 100, padding: 0,
    },
    selectable: false, draggable: false,
  })

  // Domestic sources (left column, stacked vertically)
  DOMESTIC.forEach((s, i) => {
    nodes.push({
      id: `source_${s.id}`,
      type: 'source',
      position: { x: 20, y: 30 + i * 38 },
      data: { label: s.label, status: getStatus(s.id), message: getMessage(s.id) },
      draggable: false,
    })
  })

  // International sources
  INTERNATIONAL.forEach((s, i) => {
    nodes.push({
      id: `source_${s.id}`,
      type: 'source',
      position: { x: 20, y: 290 + i * 38 },
      data: { label: s.label, status: getStatus(s.id), message: getMessage(s.id) },
      draggable: false,
    })
  })

  // Fetch + Dedup stage
  const fetchStatus = stages['fetch']?.status || 'idle'
  const fetchMsg = stages['fetch']?.message || '等待抓取'
  const dedupMsg = stages['dedup']?.message || ''
  nodes.push({
    id: 'fetch',
    type: 'stage',
    position: { x: 220, y: 130 },
    data: {
      label: '抓取 & 去重',
      icon: '📡',
      status: fetchStatus,
      message: dedupMsg || fetchMsg,
      stats: stages['dedup']?.data || stages['fetch']?.data,
    },
    draggable: false,
  })

  // Digest stage
  const digestStatus = stages['digest']?.status || 'idle'
  const digestMsg = stages['digest']?.message || '等待筛选'
  nodes.push({
    id: 'digest',
    type: 'stage',
    position: { x: 430, y: 130 },
    data: {
      label: '新闻摘要 & 分类',
      icon: '🧠',
      status: digestStatus,
      message: digestMsg,
      stats: stages['digest']?.data,
    },
    draggable: false,
  })

  // Agent stage
  const agentStatus = stages['agent']?.status || 'idle'
  const agentMsg = stages['agent']?.message || '等待分析'
  nodes.push({
    id: 'agent',
    type: 'agent',
    position: { x: 660, y: 100 },
    data: {
      label: 'Claude Agent',
      status: agentStatus,
      message: agentMsg,
      stats: stages['agent']?.data,
      subActions: ['search_stocks', 'get_sector_data', 'filter_stocks', 'get_watchlist'],
    },
    draggable: false,
  })

  // Report output
  const pipelineStatus = stages['pipeline']?.status || 'idle'
  nodes.push({
    id: 'report',
    type: 'stage',
    position: { x: 910, y: 130 },
    data: {
      label: '分析报告',
      icon: '📊',
      status: pipelineStatus === 'success' ? 'success' : 'idle',
      message: stages['pipeline']?.message || '等待输出',
    },
    draggable: false,
  })

  return nodes
}

function buildEdges(stages) {
  const edges = []
  const edgeBase = {
    type: 'smoothstep',
    markerEnd: { type: MarkerType.ArrowClosed, width: 14, height: 14, color: '#475569' },
    style: { stroke: '#475569', strokeWidth: 2 },
  }
  const activeEdge = {
    ...edgeBase,
    animated: true,
    style: { stroke: '#3b82f6', strokeWidth: 2.5 },
    markerEnd: { ...edgeBase.markerEnd, color: '#3b82f6' },
  }

  const fetchRunning = stages['fetch']?.status === 'running'
  const digestRunning = stages['digest']?.status === 'running'
  const agentRunning = stages['agent']?.status === 'running'

  // Sources → Fetch
  const allSources = [...DOMESTIC, ...INTERNATIONAL]
  allSources.forEach(s => {
    const srcRunning = stages[`source_${s.id}`]?.status === 'running'
    edges.push({
      id: `source_${s.id}-fetch`,
      source: `source_${s.id}`,
      target: 'fetch',
      ...(srcRunning || fetchRunning ? activeEdge : edgeBase),
    })
  })

  // Fetch → Digest
  edges.push({
    id: 'fetch-digest',
    source: 'fetch',
    target: 'digest',
    ...(digestRunning ? activeEdge : edgeBase),
  })

  // Digest → Agent
  edges.push({
    id: 'digest-agent',
    source: 'digest',
    target: 'agent',
    ...(agentRunning ? activeEdge : edgeBase),
  })

  // Agent → Report
  edges.push({
    id: 'agent-report',
    source: 'agent',
    target: 'report',
    ...(stages['pipeline']?.status === 'success' ? activeEdge : edgeBase),
  })

  return edges
}

export default function PipelineFlow({ stages }) {
  const nodes = useMemo(() => buildNodes(stages), [stages])
  const edges = useMemo(() => buildEdges(stages), [stages])

  return (
    <div style={{ width: '100%', height: 500 }}>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        fitView
        fitViewOptions={{ padding: 0.15 }}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
        panOnScroll
        minZoom={0.5}
        maxZoom={1.5}
        proOptions={{ hideAttribution: true }}
      >
        <Background color="#1e293b" gap={20} />
        <Controls
          showInteractive={false}
          style={{ background: '#1e293b', borderColor: '#334155' }}
        />
      </ReactFlow>
    </div>
  )
}
