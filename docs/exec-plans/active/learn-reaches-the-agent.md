# 经验要回到 agent 手里：Learn 的四个断点

2026-09-22。用户：「回不到 那这个agent每天都是新的，还怎么成长呢？
搞笑啊。。。咱们把这个Learn也要搞起来啊」。

## 现状：写了，读不到

```
交易平仓
   ↓
复盘总结出原则                      ✅ 2 条原则、1 个 playbook，内容是实的
   ↓
reinforce_trading_principle(...)    ❌ 零调用方 → win_rate 永远 NULL
   ↓
knowledge_snapshots.approve(...)    ❌ 零调用方 → 快照表 0 行
   ↓
政策版本 knowledge.snapshot_id      ❌ None
   ↓
agent 的【交易经验手册】            ❌「未生效：没有任何已批准的知识快照在生效」
```

`advance_candidate` 是第四个零调用方（21 条候选全停在 `observation`）。

**四个函数全部实现完毕，全部没有调用方。** 不是 Learn 没做，是 Learn
做完了没接上电。用户那句「agent 每天都是新的」字面成立。

## 两条被我先后否掉的路，以及为什么

**不能用证据里的 `outcome` 算 win_rate。** 那个字段是 `lessons.py` 让
**LLM 自己填**的（`{"code","date","outcome"}`）。拿它打分就是让模型给
自己的主张打分，违反 `GOLDEN_PRINCIPLES` 的「utility comes from market
data only」，而且正是这套系统最容易自欺的地方。

**不能用现成的 `holdout_gate`。** 它要求版本绑定一个 shadow run，而
shadow 的产出器是**概率映射**（`constant_0.5`、`remap_confidence`）。
那条闸测的是**校准**，不是「这条经验有没有让交易变好」。
**知识快照没有对应的市场数据闸——这是设计缺口，不是缺调用方。**

## 做什么

1. **安装第一份快照，并说清它是安装不是晋升。**
   `auto_promote` 自己写着「Installing the first version is a different
   act」。现在没有任何知识在生效，所以没有「带知识的现任」可被超越，
   闸无从谈起。安装的代价用 A/B 回放当场量，不是无限期等一个不存在的闸。
2. **A/B 即闸。** 同一窗口跑两轮，一轮快照生效一轮不生效，比**超额收益**
   （median-to-median，AGENTS.md 的规矩）。这正是今天对照组/修复组的做法，
   机器已经验证过。
3. **装上之后立刻量，结果如实记录**——包括它让事情变差的情况。变差就
   retire，那也是 Learn 的一部分。
4. **第二份及以后是晋升**，必须过 A/B 闸才动指针。

## 不在本次范围（说明白，免得看起来像漏了）

- `advance_candidate` 的生命周期引擎：候选走到 `validated`
  **本来就不改变任何行为**（docstring 自述），得先接上「validated 之后
  干什么」才有意义。
- 给知识快照做一条像 shadow 那样的日常前向闸：那是新机制，本次用 A/B
  回放替代，并把缺口记进技术债。

## 验收标准

1. `knowledge_snapshots` 行数 **> 0**，且在生效的政策版本
   `sources['knowledge']['snapshot_id']` **不是 None**。
2. 跑一次真实决策，从 **LLM journal 里读到**【交易经验手册】那一栏
   不再是「未生效」，且包含那两条原则的正文。
3. A/B 两轮的超额收益中位数对比被记录下来，无论结果好坏。
4. `uv run pytest tests/ -q` 全绿；两个 lint 通过。

## 决策日志

- 2026-09-22 不用证据里的 `outcome` 算 win_rate：那是 LLM 自填字段。
- 2026-09-22 不用 `holdout_gate` 当知识闸：它测校准，不测知识。
- 2026-09-22 第一份快照按「安装 + 立刻 A/B 量」处理，而不是等一个
  尚不存在的闸——否则 Learn 永远不会通电，而用户要的正是通电。
