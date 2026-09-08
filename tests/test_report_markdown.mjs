/* Report text → renderable markdown.
 *
 * The agents wrap their report in a bare ``` block, so the renderer showed
 * the whole thing as literal text. Position could not identify the block:
 * it opens after a preamble sentence and closes before an appended
 * 修订说明 section, so "the fence wraps the whole body" never matched.
 *
 * Run with: node --test tests/test_report_markdown.mjs
 */

import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
  reportHeadline, reportSummary, reportToMarkdown, stripWrappingFence,
} from '../web/src/lib/format.js'

test('unwraps a fence around report prose', () => {
  const md = reportToMarkdown('开场白\n\n```\n【核心事件】\n正文\n```\n\n【修订说明】\n后续')
  assert.equal(md.includes('```'), false)
  assert.match(md, /### 核心事件/)
  assert.match(md, /### 修订说明/, 'a section after the fence must survive')
})

test('leaves a tagged code block alone', () => {
  const md = reportToMarkdown('【小节】\n```python\nprint(1)\n```\n结尾')
  assert.match(md, /```python/)
})

test('leaves an untagged block that is not report prose alone', () => {
  const md = reportToMarkdown('【小节】\n```\nx = 1\n```\n结尾')
  assert.match(md, /```/)
})

test('an unterminated fence does not swallow the rest', () => {
  const md = reportToMarkdown('开场\n```\n【核心】\n正文没闭合')
  assert.equal(md.includes('```'), false)
  assert.match(md, /### 核心/)
})

test('full-width rules become one horizontal rule', () => {
  const md = reportToMarkdown('════════\n标题\n════════')
  assert.equal(md.split('\n').filter((l) => l === '---').length, 2)
})

test('an inline 【…】 inside a sentence stays text', () => {
  assert.match(reportToMarkdown('正文里出现【这种】不该变成标题'), /^正文里出现【这种】/)
})

test('headline skips the preamble and the report title bar', () => {
  const text = [
    'AlphaAgents 分析报告 | 2026-09-08 14:14:21 Tuesday',
    '',
    '【核心事件】',
    '[地缘] 俄乌冲突持续 — 重要性 4/5',
  ].join('\n')
  const h = reportHeadline(text)
  assert.match(h, /核心事件/)
  assert.equal(h.includes('AlphaAgents'), false)
})

test('summary does not repeat the headline', () => {
  const text = '【核心事件】\n[地缘] 冲突持续\n\n【市场环境】\n涨跌比 1.25'
  assert.equal(reportSummary(text).includes('[地缘] 冲突持续'), false)
  assert.match(reportSummary(text), /涨跌比/)
})

test('empty input is safe', () => {
  assert.equal(reportToMarkdown(''), '')
  assert.equal(reportToMarkdown(null), '')
  assert.equal(stripWrappingFence(''), '')
})
