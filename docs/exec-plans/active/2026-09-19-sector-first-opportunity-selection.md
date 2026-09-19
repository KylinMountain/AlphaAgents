# 板块优先的选股架构：先选方向，再选交易，而不是继续优化双榜

状态：active（S0–S4 验证基础设施已基本就绪；正式四臂被 fund-flow strict-PIT capability 门控，策略结果仍为 n=0；Sector-First 专属 S5 尚未实现；默认仍为 `dual_rank_v0`）
创建：2026-09-19
代码核对基线：`main@72ce91a96e8ab0ee499dae0d6e71deef36e67ddd`；2026-09-20 进度校准与补实现见本文末记录
策略效果：n=0（尚未完成预注册 A/B/C/D 正式验证；不声明收益改善）
范围：A 股模拟交易的候选生成、决策契约及验证；不接真实券商，不改变账本与交易物理规则。

## 目标

将“全市场涨幅/换手双榜先截取个股，再补板块解释”的 T1 路径，升级为可检验的
“市场风险预算 → 行业/主题研究 → 板块内候选 → 股票级交易论点 → 组合检查”路径。
保留全市场异常发现通道，避免新主题在形成前被永久挡在门外。
先验证机会发现架构是否优于现有基线，再开放相应 RSI 参数；不是先假定板块优先一定赚钱。

本方案补充 [Dream RSI Selection](../../DREAM_RSI_SELECTION.md)，不重定义其中已经存在的模块；
交易、学习与晋升边界仍以 [Trader Core Design](../../TRADER_CORE_DESIGN.md) 为准。
“本方案被提交”“功能实现”“实验胜出”“获准上线”是四个不同状态。

## 1. 当前事实、偏差与本次不做什么

以下是上述 commit 的静态代码观察，不是生产账户运行结论。

| 已有承载 | 核对到的行为 | 本方案的处理 |
| --- | --- | --- |
| `alpha_agents/prompts/morning_scan.md` | 默认晨扫已要求看市场、概念资金流，再从概念内找标的 | 保留研究意图，不能照搬其中未经验证的阈值与建议 |
| `scripts/walk_forward.py` 的 `_build_panel` | T1 先用全市场涨幅/换手排名构造面板，概念与资金等随后附加 | 将现有行为冻结为可复现对照，不删除 |
| `alpha_agents/data/selection_policy.py` | `selection_rank.change_share` 控制两条候选排名的混合 | 保留为旧策略参数，不把它当作已经验证的 alpha 来源 |
| `alpha_agents/data/opportunity_journal.py` | 已记录股票级候选、研究与选择状态 | 在其上游补方向级记录，不伪造旧记录 |
| `alpha_agents/evolution/selection_gate.py` | 计算配对统计量，最终改善判断主要是均值差超过阈值 | 新架构不得仅凭此获得生产晋升资格；见第 8 节 |
| `scripts/walk_forward.py` 的订单创建 | 订单主题来自 run-level `ctx.theme` | 新模式必须由股票自己的论点绑定主题 |
| `alpha_agents/data/event_expectations.py` | 已有事件、预期与实际结果快照接口 | 接入来源与时间语义要验证，表存在不等于数据可用 |

**根本偏差：回放脚手架变成了默认策略假设。**
控制参数能改变候选池，只证明干预可达；并不证明它值得被优化。
默认晨扫与 T1 路径必须共享决策语义，不能一个研究板块、另一个只回放双榜，
然后把后者的 Dream 结果宣称为前者的能力。

本次及第一轮实现不做：新增多智能体团队、模型训练、自动修改 evaluator、自动上线、
新撮合器、扩大数据采购权限、同时搜索几十个权重。也不修改现有 active policy 指针。

## 2. 待验证的策略命题

**H1：** 在相同风险、执行与研究预算下，先研究共同驱动因素与板块状态，
再进行板块内选择，能改善未来可交易机会的质量及组合下行表现。

**H2：** 资金流向在价格、成交和板块状态之外可能提供增量信息；
它必须通过消融检验，而不是因为被命名为“主力”就天然加权。

**H3：** Agent 的优势应体现在信息解释、论点与相对比较上；
若简单规则在相同方向内表现相当且成本更低，应保留简单规则。

这三条均为假设。不得写成事实工具的结论，也不得预先写入“已验证经验”。

### 2.1 三类信息的职责

- **行业：** 经济/业务关系。识别谁受益、谁可能承担成本，不能将上下游默认同向。
- **主题/概念：** 当前事件与共同预期的关系。标签归属与真实业务暴露分别记录。
- **资金流向：** 带来源、算法与单位的交易行为观测，不等于机构身份或未来收益。

例如，Tushare 的 `moneyflow` 文档说明其口径涉及主动买卖与订单规模分类。
因此，本方案不把该字段命名为“机构净增持证明”；其他 provider 同样要单独查口径。[R4]

### 2.2 主线不是“今天涨幅第一”，龙头也不是默认买入对象

需要区分：方向仍有空间、当前价格可以买、组合还能承受，三者缺一不可。
龙头观察池用于判断行情是否成立；可交易候选池用于选择实际交易载体。
不规定“永远买龙头”，也不规定“涨得少就有补涨价值”。
阶段标签如启动、加速、分歧、退潮是版本化推断，必须附证据与反证，不冒充原始事实。

## 3. 目标流程及 v0 的克制

```text
同一时点 WorldSnapshot + 当前账本 + 固定 Mandate
                         |
                市场事实与风险预算
                         |
          行业/主题状态表 + 事件/预期证据
                         |
            0..K 个重点方向及反向理由
                         |
        板块内候选 -> 横向比较 -> 股票级论点
                         |
              入场/失效/期限/仓位意图
                         |
                原 Risk / Intent / Ledger
                         |
       成交、拒绝、空仓、结果 -> 分层复盘 / Dream

旁路：全市场异常或独立事件 -> discovery 观察队列
      -> 有效关联/论点 -> 回到同一个候选与风险入口
```

第一轮主实验采用 `sector_first_v0`，作为 opt-in challenger，不替换现有默认值。
v0 先改变“按方向分配研究注意力”的结构，板块内粗选可复用原有排名材料；
更精细的个股形态规则另开实验，避免一次同时改变方向、排名、仓位和退出。

### 3.1 可复现的首版调度草案

以下数字只用于限制工程规模，不是市场规律；M0 预注册后在一次实验内不得调整。

1. 数据层批量计算全部可覆盖方向的状态表，不逐股票调用 LLM。
2. 将方向按固定口径的 5 日相对收益中位数、参与广度、广度改善三个排名的平均名次粗排；
   并列用稳定方向 ID 排序。该粗排是透明基线，记录所有被截断方向，不宣称最优。
3. 最多向 Trader 展示 8 个方向的紧凑状态卡，再由同一 Trader 选择 0..3 个重点方向；
   要写支持、反对、未知和放弃条件。资金、事件是展示证据，不自动变成单一总分。
4. 重点方向内按冻结的规则生成候选，轮转分配面板名额并全局去重，上限暂为 40；
   每个方向的原始候选、截断顺序、去重前后名额都落盘。
5. 复用同一个研究预算，全部阶段合计不超过现有预算，不是“每层再给 20 次”。
   最多深挖 4 只、每次决策目标上限 2 个新仓；数量约束由代码执行，不能只靠 prompt。
6. 发现旁路在 v0 只留观察记录，不偷加交易名额。作为独立实验臂启用时，预注册名额比例，
   从同一个 40 只上限中分配；不使用额外资金或额外模型预算换取表面优势。

方向质量不足时允许选择 0 个，不能因名额空缺自动补入弱方向。
行情/数据缺失、模型失败、主动空仓必须分别计数。

### 3.2 市场环境与预算

市场状态先影响“可研究什么、需要什么确认、是否提出降低仓位”，不直接修改硬风控。
第一轮对照中，市场风险控制器、初始资本、单票/相关主题上限保持相同。
后续再单独研究状态依赖的仓位策略，避免将“买得更少”误读为“选得更好”。

## 4. 方向状态与板块内比较

### 4.1 状态表输出向量，不输出“买这个板块”

| 维度 | 最小观测 | 防误判要求 |
| --- | --- | --- |
| 共同性 | 成分收益中位数、上涨占比、跑赢市场占比 | 同期同口径 median-to-median；不只看市值加权指数 |
| 持续性 | 1/5/20 日相对表现，连续参与与成交份额变化 | 窗口事前固定；不从最漂亮窗口倒推 |
| 头部依赖 | 剔除头部 1/3 只后的表现；对候选的 leave-one-out 表现 | 一只股票拉高板块后，不能再把板块强度当作它的独立证据 |
| 资金参与 | 净流/同口径成交额、持续天数、个股流入占比 | 分母缺失时不归一化；不混用不同 provider 的单位与算法 |
| 透支与事件 | 前期涨幅、波动、集中度；事件时间、预期版本与实际值 | “priced in”是推断；没有 consensus 就保留 unknown |
| 可交易性 | 可评估成员数、近期受限情况、流动性与持仓重叠 | 决策只看当时已知条件；未来成交量与能否成交由执行引擎判断 |

所有派生量带 `formula_version`、输入快照 hash、分母和缺失覆盖率。
三项高度相关的观测不计为三份独立确认。
已知停牌或不可买的龙头仍可进入观察池，不能为便于买入而从市场理解中删除。

### 4.2 个股决策卡

每个被深挖的标的要同时回答：
业务/题材关联是什么；相对同方向其他成员有什么优势；目前属于何种交易形态；
为什么是现在而不是等一下；什么事实会使论点失效；与现有仓位是否重复承担风险。

把“公司好”“方向好”“这笔交易好”分开。事件分析使用
[Event Expectations](../../EVENT_EXPECTATIONS.md) 的数据契约，不写“利好落地必跌”。

最终输出 `StockThesis`，由适配层转成现有意图：
`code / primary_theme_id / supporting_theme_ids / membership_snapshot_id /
theme_thesis_id / relation_evidence_ids / setup / entry / invalidations /
horizon_sessions / size / counterevidence / information_cutoff`。

默认晨扫、T1、回放与 shadow 使用同一份契约与校验器。
不强制所有模式调用同一个异步函数，但不得复制一套含不同规则的筛选器。

### 4.3 主题归属和组合风险

`primary_theme_id` 必须在下单前选定，并能解析到当时的真实关系或已批准的事件论点。
一只股票允许多个 supporting themes；收益主归因只用冻结的主主题，风险暴露计算所有
相关主题簇，不能靠换标签绕过集中度限制。

关系不明确时返回 `theme_unresolved`，进入观察/拒绝记录。
不得退回整个 run 的 `ctx.theme`，也不能给独立事件挂一个虚假的强主题来通过门控。
独立事件通道要进入交易，必须先定义并测试合法的事件论点类型与对应风险规则。

主题选择是注意力分配，不与执行门控机械叠加成两次相同的“强度打分”。
实施时复核现有 `theme_gate` 的生命周期/论点失效职责；改变门控属于单独的版本变更。
未完成适配前不允许通过关闭 gate 来让新策略成交。

## 5. 时间、数据来源与可用性

统一条件是 `available_at <= decision_at`，并区分发生、发布、采集、修订、可用时间。
统一时区存储并保存原时区；美国盘后与中国次日开盘不能按日期字符串随意对齐。

### 5.1 先探测，不先声称没有历史

沿用现有 source probe，检查本地归档、Tushare、AKShare 及获授权来源。
逐数据集报告：历史覆盖、账号权限、发布时间精度、历史成分、修订/vintage、
复权与单位口径、缺失、采集时间及 PIT 等级。没有 token 或未实测，状态为 unknown，
不等于无历史；也不因接口有 `ann_date` 就自动认定整个记录都是 PIT。

重点验证历史行业/主题成员、板块资金、公司预告、卖方预测与披露计划。
公司 guidance 与卖方 consensus 分开；最终实际披露日不得倒灌成早先已知计划。

### 5.2 数据降级是不同实验，不是静默替换

- 历史概念成员未经证实时，不用当前成员组成“历史板块”，然后声称严格回放。
- 可验证的历史行业版本可作为独立行业实验；不能自动代替概念实验并沿用名称。
- 预期版本缺失时，保留事件事实与价格背景；移除 expectation 维度并标记能力。
- 股价、停牌、ST、退市与复权覆盖按时点审计；不得只从今天仍存在的股票构造全集。
- 上午决策只用已完成的前日 bar；完整日线不能当成 14:55 当时可知。
  原 synthetic-close 模式只能列为单独敏感性实验，不用于严格 PIT 有效性结论。
- 标签按交易所日历成熟，不按个股下一条可用 K 线“跳过”停牌日期；
  缺失/退市不能记 0，也不能从样本静默删除。标签须带可用时点及未成熟原因。

这些均为上线前要求，不表示本次文档提交已经修复全部旧路径。

## 6. 证据对象：从方向到订单的完整链

新增的是职责与契约；具体模块拆分沿用仓库层次，不在文档中冒充已存在文件。
依赖方向遵循 [AGENTS.md](../../../AGENTS.md)，数据模块不得反向导入 Agent。

| 对象 | 最小字段/职责 | 接入方式 |
| --- | --- | --- |
| `SectorSnapshot` | 方向 ID、类型、成员版本、状态向量、覆盖率、来源/公式 hash、可用时点 | 批量计算的共享事实与派生量 |
| `ThemeOpportunitySet` | 全部评估方向、候选/选中/未选/未研究/数据不足、预算、输入 hash | 补在股票 Opportunity Journal 上游 |
| `ThemeThesis` | 方向命题、期限、支持/反对证据、阶段推断、失效条件、作者/版本 | 不由 LLM 自评效果 |
| `StockThesis` | 股票级主/辅主题、真实关联、交易形态、入场/失效/期限/仓位 | 转现有 `TradeIntent` |
| `ExperimentManifest` 扩展 | 选择架构、信息集、模型、预算、样本/停止规则、主指标、风险边界、全部试验计数 | 复用现有实验/归档边界 |

沿用 `run_id / trader_id / policy_version / decision_id / episode_id`。
关系链为：方向快照 → 方向选择 → 个股候选 → 股票论点 → 意图 → 成交/拒绝 → 结果。
历史旧记录缺主题/候选信息就标 `legacy_unknown`，不能事后补上一个有利解释。

对“研究过但没买”只存观测与原文理由；模型没给出的拒绝原因不能由复盘补写。
快照内容可解析、可校验，不能只存找不到原文的 hash。
真正被读到的工具/知识引用与仅可用的资料分别记录，不能混称“影响了决策”。

## 7. 先验证架构，再放开 RSI

### 7.1 四个预注册对照

| 实验臂 | 变化 | 所回答的问题 |
| --- | --- | --- |
| A：`dual_rank_v0` | 冻结现有双榜及同一 Trader | 当前起点的真实基线 |
| B：`sector_first_v0` | 先方向后个股，使用同一 Trader | 机会发现顺序是否值得改变 |
| C：`sector_first_simple_selector` | 复用 B 冻结的方向选择，板块内使用预注册简单规则 | LLM 个股选择是否有增量 |
| D：`sector_first_no_flow` | 与 B 相同，但移除方向判断中的资金流证据 | 资金流是否有独立贡献 |

A/B 的候选池本就不同，不能为了“公平”强制它们用同一股票面板。
它们共享的是完整时点世界、全局可交易全集、数据能力、资本、成本、退出和研究预算。
B/C 比较则固定方向、候选与可交易约束，以隔离最终个股选择。
D 从输入与工具可见面一起移除目标特征，不能一边消融一边从新闻摘要读回同一信息。

第一轮不同时变更市场仓位控制、买卖时点与退出策略。
需要改变这些因素时另建实验臂，禁止把多个改动的收益统称为“板块选股有效”。

### 7.2 分层指标与评价单位

- **方向层：** 相对完整方向集的前向表现、方向选择覆盖及机会损失。
- **个股层：** 同板块、同日、同可交易条件下的配对收益差与分位；mean 对 mean，
  median 对 median；不能用挑中赢家的最大值代表整个仓位。
- **执行层：** 订单接受/成交/阻断、成交成本、等待损失；市场标签与实际模拟成交分开。
- **组合层：** 净收益、最大回撤、最差连续窗口、尾部损失、换手、风险簇暴露。
- **效率层：** 每次完整决策的调用数、token、耗时、失败率；不能只对成交日计成本。

诊断标签可保留 1/3/5 个交易日，主期限在预注册中只指定一个。
同一天同板块股票、重叠持有窗口不是独立样本；至少按交易日聚合配对结果，
并用覆盖完整横截面的连续日期块做不确定性估计。
主题层样本量、交易日数、订单数、实际成交数同时报告。

### 7.3 时间划分与停止规则

训练/分析窗口 → candidate 冻结 → 未参与反思的后续验证窗口 → 真实前向影子。
按标签最大持有期进行 purge/embargo；训练标签不得跨入验证信息边界。
看过的验证结果一旦用于改候选，该窗口即成为开发数据，不再当独立验证复用。

规划默认验证预算为 4 个连续且互不重叠的 30 交易日窗口；
真正日期由 M0 数据审计后、查看策略结果前登记。不足就报告覆盖缺口，不挑漂亮片段。
`n_days < 50` 不发布新策略优越性结论；达到该数也不自动等于证据充分，
需结合重叠、尾部风险及现有治理门槛。不得降低仓库原有门槛。

LLM 再采样不是相同判断的 replay。固定模型标识与配置并保存请求/工具响应；
逐快照实验比较实际意图，整路径实验比较预注册重复次数下的分布与组合结果，
不把不同模型随机输出导致的路径分叉都归因于策略参数。

## 8. 晋升必须比“候选池均值更高”严格

现有 selection gate 的记录可用于诊断，不能直接给本方案的新架构授权。
在新 gate contract 完成前，所有 sector-first 实验默认 `promotion_eligible=false`。
旧系统历史记录与既有策略不被本文重新判定或重写。

新 gate 的三层条件：

1. **有效性：** 无未来读取/身份错配/不明标签；完整样本框、缺失与成本可解释；
   实验调用的 producer 确实执行了所有 changed genes。
2. **风险否决：** 不突破 Mandate，回撤/尾损/集中度/换手不跨预注册边界。
   边界必须在实验前填成数字；空缺配置拒绝开实验，不能运行后解释。
3. **改善证据：** 预注册主指标达到事前的最小有意义改善幅度；
   配对、日期块估计的不确定性仍无法排除退化时，输出 `insufficient`。
   面板标签改善而实际成交组合变差，不通过。

回撤优先：先满足风险约束，再比较收益；不临时从收益切换成胜率、某板块子样本或
其他更好看的指标。置信区间方法、块长度、重复数、比较数量均写入 manifest。
所有尝试包括无效、无变化、失败候选进入 archive，防止只保留赢家。
到停止点冻结精确样本 ID 集、结果与模型/数据/evaluator hash；不允许手动追加样本重问同一 gate。

人仍负责批准。批准记录和 active policy CAS 机制复用，不另设后门。
新代码提交不等于变更线上策略；历史 Dream 永远不伪装成前向证据。

## 9. Dream 的正确学习单位

链路是：方向记录 → 方向结果 → 股票记录 → 股票结果 → 执行与组合结果。
不是“发现一个亏损案例就把权重调一下”。

先诊断错误在哪层：
好股票没进可研究方向，检查机会发现；
研究了好股票但没选，检查比较；
选对但不可成交，检查交易构造；
所有仓位押同一风险，检查组合；
假论点只是运气赚钱，也不能蒸馏成原则。

只有得到跨 episode 的支持/反对证据后，才提出行为 delta。
新 gene 须满足：
`真实决策读取 → 固定世界下可干预 → Dream 可观察 → forward evaluator 可观察`。
发现层 gene 改变候选时，要冻结完整时点世界/候选全集；旧的 40 只面板不足以评价
“本来未进入这 40 只的股票”。无法重建的 branch 返回 unsupported，不能编造结果。

v0 冻结方向粗排、名额、排序和风险参数；先比较架构，不启动自动搜索。
未来可研究主题名额、发现名额、板块内排序等，但不在本方案提交时默认开放。
学习者不可修改会计、结算、市场规则、数据截断、评价器、停止规则或硬风险上限。

## 10. 实施阶段与验收

勾选只表示对应工程 contract 已有实现与针对性测试，不表示策略效果已被证明。
未勾选项保留为真实缺口；“partial”说明已有一部分机制，但还不足以满足整条验收。
本方案的 S0..S5 是局部里程碑，不替代 Trader Core 的全局阶段定义。

### S0：基线、数据与协议冻结

- [ ] **partial**：manifest 已强制冻结 A 的 `code_ref / policy_ref / input_hash`，模型也在协议内；
      仍缺“两次 recorded A replay 的意图与账本逐项一致”这一条端到端证明。
- [x] 本地数据探测输出 `capabilities.json`，逐项标明 PIT/unknown/current-only、权限与覆盖；
      未实测来源保持 unknown，不把“未调用”写成“没有历史”。
- [x] `experiment_manifest.json` 固定 A/B/C/D、模型、预算、成本、退出、日期切分、主指标、
      最小有意义改善幅度、风险边界、样本/停止规则和 block 方法；任一必填项为空会验证失败。
- [x] 协议可用 `register` 按内容 hash 归档为 `manifests/<hash>.json`；
      同内容幂等，任何修改产生新 hash/新文件，不覆盖旧协议。

### S1：时点关系与方向快照

- [x] 固定输入连续构建两次，方向 membership 与 feature/input hashes 一致。
- [ ] **partial**：未来 membership 与未来 bar 已有注入测试；event expectation/realization
      以 decision cutoff 读取 PIT snapshot refs，后续 expectation 修订和 realization 不会倒灌。
      fund-flow audit 现在会把只有 `trade_date`、没有 revision/capture vintage 的本地归档标为
      B 级且 `strict_replay_eligible=false`；正式 B/D 对照会 fail closed。仍缺真正可验证的
      fund-flow vintage 源/归档，不能把“被门控”写成“已经有严格 PIT 数据”。
- [x] 剔除头部 1/3 名与候选股票级 leave-one-out 均已实现并有 fixture；
      候选自己的价格变化不能改变“剔除自己后的主方向 5 日证据”，并直接展示在股票卡片。
- [ ] **partial**：缺失、真实零值与负流入已有独立 fixture；source probe 也会把 missing /
      trade-date-only / 未验证 captured-at 分别保留为不可正式回放状态。正式四臂必须绑定
      content-addressed capability report，fund-flow 只有人工验证为 A 级、带 reviewer /
      verified_at / evidence 后才可启动。仍缺 provider-backed vintage 数据本身与更细的
      snapshot-level unsupported reason。
- [x] 方向级 Opportunity Journal 区分 selected、agent-rejected、offered-not-researched、
      evaluated-not-offered、unassessable 和 unreadable decision，且 append-only。

### S2：统一决策契约与主题归属

- [ ] 默认晨扫与 T1 对同一 frozen fixture 使用相同关系解析与资格校验结果。
- [x] Sector-First 订单使用每只股票自己的 `primary_theme`；重叠主题进入
      `supporting_themes`，不再统一写成 run-level theme。
- [ ] **partial**：strict PIT membership、未来 membership 和 outside-shortlist 均 fail closed；
      每个 `code × theme` 已有 membership-bound `relation_evidence_id`，panel/placed order
      携带 snapshot id/hash 与主/辅方向关系证据；执行前还会按 frozen membership 重新计算并校验
      主/辅关系 evidence，缺失或篡改统一写成 `theme_unresolved` refusal，不再 fallback 到 run theme。
      `theme_thesis_id` 及独立事件关系类型仍未落地。
- [ ] 主题观察名单可含不可买龙头；交易名单/执行拒绝另有记录。
- [ ] **partial**：方向选择与个股选择复用同一个 `ResearchBudget`，并已有 top-8 / 0..3 /
      panel-size / picks 等代码上限；还缺统一的超限 refusal code 与全阶段预算验收测试。
- [x] 原 `dual_rank_v0` 仍为 CLI 默认值，Sector-First 仅 opt-in challenger；
      当前 active policy 未被本计划自动修改。

### S3：接入原执行内核与组合检查

- [x] Sector-First 只替换候选/选择路径，订单继续进入原 `create_pending_order`、
      Reservation / Settlement / Ledger；没有新增第二套撮合实现。
- [x] 原入场、止损、T+1、涨跌停/停牌、费用、部分成交/拒绝回归已随 #8 的完整 CI 通过；
      Tests、Harness/knowledge-base checks 与 build 均为 green。
- [x] 多概念重叠仍只形成一份股票订单；side-car exposure 对 primary/supporting themes
      都计入组合风险，缺 mark 时返回 unverified 而不是 0。
- [x] 关系/来源缺失或 evidence/hash 错配会在 Intent 前 fail closed，并作为
      `theme_unresolved` / `relation_validation` refusal 写入 Opportunity Journal；
      Sector-First 已移除缺主主题时回退 `ctx.theme` 的路径，因此不会表现成模型主动空仓或错误主题订单。
- [ ] Sector-First 专属 forward shadow 尚未接入；不能拿旧 `selection_rank` shadow
      的隔离性代替本条验收。

### S4：四臂历史比较与失败结论

- [x] A/B/C/D architecture、manifest/runtime binding、C 的 frozen-B directions、
      D 的 no-flow 消融及 compare 已实现；`run-matrix` 能按 A→B→freeze→C→D→compare 跑隔离四臂。
      正式运行还必须绑定注册后的 capability report；fund-flow 未达到 A/strict-PIT 会在开跑前拒绝。
      `comparison.json` 顶层汇总每臂的模型/工具预算、decision/refusal counters、
      capability/coverage 与 errors，正式评审无需再人工拼四份 run artifacts。
- [x] `comparison.json` 显式携带 direction / stock / execution / portfolio 四层证据；
      direction/stock 是标签/选择诊断，portfolio 来自 Ledger equity，语义不混写。
- [x] A-B / B-C / B-D 在共同交易日上做 moving-block bootstrap；
      block 长度与重复数来自预注册 manifest，同一天多股票不会伪增独立样本。
- [ ] **partial**：validation window 与 training window 重叠会被拒绝，方向 outcome 的停牌/缺失进入 coverage；
      还需补完整的未来标签、部分成熟和退市 fixture。
- [x] comparator 永远 `promotion_eligible=false`，只输出开发证据；
      B 不优于 A、C 更简单或 D 消融更好都不会自动切换策略。
- [x] 数据/风险证据不完整、统计 CI 不足以区分、以及完全无 executable fills
      分别进入 `evidence_status.data/statistics/execution`，不会写成 PASS。

### S5：前向影子与受控演化

Sector-First 专属 S5 **尚未实现**。仓库已有严格的 `selection_rank` forward shadow /
seal / gate / human approve / promote 链，可复用其治理模式，但它只观察
`decision.selection_rank.*`，不能冒充新架构的 forward evidence。

- [ ] 前向注册时间由真实记录约束，不能传入过去 `opened_on` 将历史伪装成前向。
- [ ] producer/evaluator 的 exact changed-gene coverage 有正例和负例测试；
      未观测的新参数不能靠宽泛前缀混入。
- [ ] 样本封存恰好达到预注册数量；批量补数据不能多写再截取有利部分。
- [ ] “标签变好但组合回撤越界”被拒；“无行为变化”不能获得有效改善结论。
- [ ] 未达到证据要求保持 incumbent；任何生产策略变化仍需独立人工批准。

### 命令与交付约定

以下命令已经存在；正式比较前必须先验证并注册协议：

```bash
uv run python scripts/sector_first.py audit --as-of DATE --out DIR

# audit 不会自己把 fund-flow 升级成 A。先人工核验 provider / 归档的 revision semantics，
# 在 DIR/capabilities.json 记录 point_in_time_grade=A、
# strict_replay_eligible=true、verified_by / verified_at / evidence，再注册。
uv run python scripts/sector_first.py register-capabilities \
  --capabilities DIR/capabilities.json --out DIR

# capability 内容一旦变化 hash 就变化；把注册后的 hash 写回 manifest.capabilities_hash，
# 再填写其余预注册字段、验证并注册 protocol。
uv run python scripts/sector_first.py verify --manifest DIR/experiment_manifest.json --out DIR
uv run python scripts/sector_first.py register --manifest DIR/experiment_manifest.json --out DIR

# 推荐：正式窗口使用一条 fail-closed orchestration。
# 每个 arm 有独立 replay state；B 完成后自动 freeze directions 给 C。
uv run python scripts/sector_first.py run-matrix \
  --manifest DIR/manifests/<manifest_hash>.json \
  --capabilities DIR/capabilities/<capabilities_hash>.json \
  --sector-membership PIT_MEMBERSHIP.json \
  --corpus data \
  --window-index 0 \
  --out DIR/window-0

# 低层命令仍保留，便于诊断单个 arm：
uv run python scripts/sector_first.py freeze-directions --run-id B_RUN --out-file DIR/frozen_b_directions.json
uv run python scripts/sector_first.py compare \
  --manifest DIR/manifests/<manifest_hash>.json \
  --arms-dir ARMS_DIR \
  --out DIR
```

`audit` 默认只读、不自动购买/扩张 API 权限，也不会凭字段名自动把数据升成严格 PIT；
`register-capabilities` 对人工核验后的 capability report 做 content-addressed 封存；
`verify` 只验证 protocol 完整性，`register` 封存 manifest；`run-matrix` 同时要求注册后的
manifest 与 capabilities，二者 hash 必须绑定，fund-flow 未经 A 级语义验证会在开跑前拒绝。
四个 arm 使用独立 replay state，模型/预算/费用/退出/decision config 漂移同样 fail closed；
`compare` 只写隔离实验结果。所有命令都没有 `promote` 后门。

各阶段提交前使用现有命令：

```bash
uv run pytest tests/ -q
uv run python scripts/lint_harness.py
uv run python scripts/lint_docs.py
uv run python scripts/lint_policy.py
```

2026-09-20 的实现均以 PR CI 为验收：#8、#9、#10、#11 的完整 Tests / Harness 或
Invariants / build / CodeQL 已通过后才合入；静态阅读只用于定位 contract，不替代实跑结果。
个别独立的 GitHub Advanced Security agent job 曾在“Processing Request”阶段失败，但对应
CodeQL 分析成功，未被当成代码通过证据。

## 11. 决策记录

| 决定 | 否掉的替代方案 | 理由 |
| --- | --- | --- |
| 板块优先是 challenger，不直接替换双榜 | 先认定新思路对，再让 RSI 优化 | 先验证交易假设，避免优化错误起点 |
| 保留原双榜与显式发现队列 | 删除全市场入口，只交易现有主线 | 保持对照，也保留发现新方向的能力 |
| 状态向量 + 有来源的论点 | 把涨停数/流入金额压成未经验证的总分 | 区分事实、阶段推断与行动 |
| v0 先隔离方向选择的改动 | 一次更换方向、因子、仓位、退出 | 有利于解释改善来自哪里 |
| 主主题由股票论点绑定 | 所有订单沿用 `ctx.theme` | 防止门控、风险与复盘归因错位 |
| 复用执行/治理，扩展证据 contract | 为 sector-first 新造回测与晋升系统 | 避免规则分叉和自我授权 |
| 数据能力由本地 probe 决定 | 断言没有历史，或拿最新值假装历史 | 尊重历史数据与版本语义的不确定性 |
| S1 先实现纯 PIT 方向状态与方向级 Journal，不立刻接交易路径 | 一次把数据、Agent、执行都改完 | 先把世界和证据对象做对，再让行为改变 |
| 先风险有效，再检验组合改善 | 面板均值更高就晋升整个 Trader | 局部选择收益不能覆盖整体交易风险 |

## 12. 风险、回退与待定项

主要风险：热点追涨换了一个外壳；粗排漏掉早期新主题；概念重复造成集中；
主力资金字段口径误解；多层模型消耗过高；数据修订前视；
反复 Dream 导致多重试验偏差；研究结果很好但实际交易无法成交。

每个风险分别通过旧基线、发现记录、相关簇检查、资金消融、总预算、
PIT 测试、完整试验档案与原执行内核验证。
模式切换采用版本化 feature flag。回退只影响后续新决策，现存仓位仍由原内核管理；
不得删除订单、经验、失败候选或重写已冻结世界。

M0 尚需确定：本地能验证的历史分类/预期源；统一市场基准与公司行动口径；
严格 PIT 可比较窗口；组合风险的具体数值；训练/验证/前向截止点。
这些不是运行中可让 Agent 自行补齐的参数；未冻结则不得启动正式胜负比较。

完成标准不是“又写出一个复杂 Agent”，而是：
**能说明方向、个股选择、执行和风险分别贡献了什么，并据此接受新架构或保留旧策略。**

## 13. 研究依据与适用边界

以下来源在 2026-09-19 核对，仅支持研究方法，不证明本方案的 A 股短线收益。
不引用未经核验的近期 RSI 跑分来决定交易架构。

- [R1: Moskowitz & Grinblatt, Do Industries Explain Momentum? (1999)](https://doi.org/10.1111/0022-1082.00146)：
  提示行业与个股收益应分层研究；[作者研究介绍](https://www.aqr.com/Insights/Research/Journal-Article/Do-Industries-Explain-Momentum)
  强调其主要研究的是 6–12 个月中期窗口，不能据此宣称 1–5 日题材策略已被证明。
- [R2: Daniel & Moskowitz, Momentum Crashes](https://www.nber.org/papers/w20439)：
  提示状态与反弹风险值得单独验证；不将论文中的多空动量结果直接等同于本项目长仓策略。
- [R3: Bailey et al., The Probability of Backtest Overfitting](https://escholarship.org/uc/item/4w1110bb)：
  支持记录所有候选与控制选择偏差；本文采用的日期块验证仍需本项目实证，不宣称消除了过拟合。
- [R4: Tushare 个股资金流向口径](https://tushare.pro/document/2?doc_id=170)：
  用于区分订单分类统计与真实机构持仓；不将其字段口径推广到其他 provider。


### 2026-09-19 实施记录：S1 第一刀

已开始实现，不改变 incumbent 行为：

- 新增纯数据层的 PIT membership contract 与 sector snapshot builder；
- 状态表包含 1/5/20 日中位收益、相对市场中位数、上涨广度、跑赢市场广度、5 日广度变化、剔除头部 1/3 名后的 5 日中位数、资金覆盖与原始净额统计；
- sector_first_v0 粗排只对三个可观测字段做平均名次，不把资金、事件或标签压成一个未验证总分；
- strict replay 会拒绝 current-only membership；传入未来 bar 也不会改变过去 snapshot hash；
- 新增 append-only 方向级 Opportunity Journal，区分 selected、researched-not-selected、offered-not-researched、agent-rejected、evaluated-not-offered、unassessable。

本提交仍不切换默认 T1 候选池，也不把 current concept_stocks 冒充 PIT 数据。下一步是 S0 audit/manifest 与 S2 opt-in challenger 接线。


### 2026-09-19 实施记录：S0 audit / manifest

- 新增只读 sector source probe：当前 membership 表没有时点列时明确标为 C/current-only；出现 time-like 字段时也只标 U，未验证语义前不会自动升级为 PIT；
- 新增 A/B/C/D 固定实验契约与 manifest validator；风险边界、四个验证窗口、模型、预算、成本、退出、主指标、停止规则或 block 方法任一缺失都会拒绝；
- 新增 sector_first.py audit / verify。audit 生成 capabilities.json 与故意不完整的 experiment_manifest.json；verify 在填写并冻结之前返回非零。
- 尚未实现 compare，也尚未切换任何交易路径。


### 2026-09-19 实施记录：S2 方向选择契约

- 新增 sector_selector_v0：输入紧凑方向状态卡，只负责从 offered sectors 中选择 0..3 个继续研究的方向，不下单、不定仓位；
- JSON 契约要求每个方向写 thesis、counterevidence、unknowns、invalidations；空 themes 是合法决定；
- outside shortlist、重复方向、超过 3 个、invalidations 结构错误都会作为 refusal 留下；
- selector 可复用同一个 ResearchBudget 对象，后续接入股票阶段时总预算可跨两个阶段共享；
- 本轮仍未切换 incumbent；下一步直接提交 opt-in sector_first_v0 接线和 per-stock primary theme。


### 2026-09-19 实施记录：S2 opt-in 主链接线

- walk_forward 新增 selection_architecture=dual_rank_v0/sector_first_v0，默认仍为 dual_rank_v0；
- sector_first_v0 强制要求 LLM decider 和显式 PIT membership archive；没有历史成员快照直接拒绝，不退回 current concept_stocks；
- 09:00 路径现在是：PIT sector snapshots → top-8 direction cards → sector selector 选 0..3 → 方向内 round-robin stock panel → 原 T1 stock decider；
- stock panel 冻结 primary_theme/supporting_themes，真实订单使用股票自己的 primary_theme，不再统一挂 run-level theme；
- 方向级 ThemeOpportunityJournal 与股票级 OpportunityJournal 同时写入，并在股票 context 中记录 selection_architecture、membership hash、shortlist、selected themes；
- sector-first close-buy 仍 fail-closed，不能和 agent_exits 同时开启；这是下一阶段单独接线，避免拿 dual-rank close panel 冒充 sector-first。


### 2026-09-19 实施记录：S3 方向 outcome / Dream

- 新增 theme_forward_median_v1：按冻结的 membership snapshot 对方向内所有成员计算 1/3/5/10 日 forward median/mean、上涨成员占比、覆盖率与剔除未来头部 1 名后的中位收益；
- outcome 使用交易所实际出现的未来交易日做 horizon，停牌/缺失成员不会被填 0，而是进入 coverage；membership id/hash 不一致直接拒绝；
- 新增 DirectionDreamWorld：只在方向 Journal 的可评估项 outcome 成熟后冻结历史世界，Dream hash 覆盖方向状态、选择结果和未来标签；
- selection_skill 分开报告 direction discovery lift、Agent selection lift、regret、abstention best available、selected head-dependence gap；这些仍是 historical_dream_only，不具备 promotion 资格；
- 新增 transparent_rank_baseline：在同一方向世界里比较 Agent 选择与 sector_first_v0 的透明粗排 Top-K；
- 新增 scripts/dream_direction.py sweep/report/baseline，下一阶段据此接 A/B/C/D architecture compare，而不是直接把历史 Dream 结果当晋升证据。


### 2026-09-20 进度校准与 S0/S4 补实现

本次先按代码与测试证据回填 S0–S4，而不是把 2026-09-19 的“设计稿”状态继续留在顶部。
同时补了四个会直接影响正式实验可信度的缺口：

- Sector Experiment manifest 升级为 schema v2，正式协议必须冻结
  `code_ref / policy_ref / input_hash`，并预注册
  `minimum_meaningful_improvement_pct`；缺任一项即验证失败。
- 新增 content-addressed `register`：验证后的 manifest 写到
  `manifests/<hash>.json`，同内容重复注册幂等，任何字段变化得到新的文件；
  不再依赖“大家约定不要覆盖 experiment_manifest.json”。
- architecture comparator 现在把 direction / stock / execution / portfolio
  四层证据都写进 `comparison.json`，并保留原有 portfolio/execution 顶层字段兼容旧调用。
- 比较结果新增独立的 `evidence_status`：data、statistics、execution 分开；
  moving-block CI 相对预注册最小改善幅度输出
  `positive_beyond_floor / negative_beyond_floor / inconclusive / insufficient`，
  “没有任何可执行成交样本”也单独命名，不再都塞进一个模糊的 insufficient。

仍然**没有**做的事：没有跑完正式四臂窗口，没有改变 active policy，没有声明
Sector-First 优于旧双榜，也没有把旧 `selection_rank` 的 forward shadow 当成
Sector-First 的 S5。

### 2026-09-20 继续实现：relation evidence execution gate

- Sector-First 不再允许 `primary_theme` 缺失时退回 run-level `ctx.theme`。
- 每个待下单股票在 Intent 前重新从 frozen membership snapshot 按 id/hash 取回世界，
  重算 primary/supporting `relation_evidence_id`；缺字段、hash 错配、关系不成立或 evidence
  被改动都会产生结构化 `theme_unresolved` refusal。
- refusal 在 Opportunity Journal 的决策记录里可见，因此“没有下单”可以区分为模型主动放弃、
  模型输出不可读、交易约束拒绝和关系 provenance 失败。
- 正反测试分别证明合法证据可下单、缺 evidence 零下单、篡改 membership hash 被拒；#11
  全量 Tests / Invariants / build / CodeQL / security 均通过后合入。

### 2026-09-20 继续实现：formal capability gate

- fund-flow source probe 不再把“有 `trade_date`”等同于严格 PIT：没有 capture/revision vintage
  的本地归档明确标为 B，`strict_replay_eligible=false`；即使存在 `captured_at` 也先标 U，
  直到 provider revision semantics 被验证。
- capability report 自身改成 content-addressed artifact；内容被改动后旧 hash 立即失效。
- 正式 `run-matrix` 同时绑定 manifest 与 capability hash；B/D 资金流消融只有在
  fund-flow 被人工验证为 A 级，并留下 `verified_by / verified_at / evidence` 时才允许开跑。
- 这是 fail-closed 数据资格门，不是对数据质量的自动认证。当前本地 trade-date-only 资金流
  仍不满足正式四臂要求，因此策略效果继续保持 n=0。

同一轮审计还修复了一个 event provenance wiring 缺陷：`_event_snapshot_refs()`
误传未定义变量 `cut`，异常被 fail-soft 捕获后会让所有事件引用静默变成空列表。
现已改为真实 decision `cutoff`，并用测试钉住 as-of、subjects 与窗口参数；已有
Event Expectations 的 PIT 测试继续证明后续 expectation revision / realization 不会倒灌。
资金流侧新增负流入 fixture，但 revision/vintage contract 仍保留为未完成项。

2026-09-20 在 #8 合入并通过 CI 后，又补齐了 S4 的 operational summary：

- comparator 顶层新增 `operational_summary`，按 A/B/C/D 汇总
  `agent_tool_calls / model_calls_made / decision_counters / capability_matrix / errors`；
- 每臂对象同步暴露 `decision_counters` 与 `coverage`，报告读取路径唯一；
- 新增针对性测试，确保 refusal/budget/coverage 不会在 compare 阶段丢失；
- 因 #8 的 Tests、Invariants/knowledge base 与 build 均已成功，S3 的原执行回归项正式完成。

随后同一分支继续补了：

- formal replay 的 runtime binding：manifest 声称的 model、ResearchBudget、完整 A 股成本模型、
  exit policy 与 decision config 必须和实际进程逐项相等，否则开跑前拒绝；
- `run-matrix`：四臂各自 bootstrap 独立 trader state，共享只读 corpus，按
  A → B → freeze B directions → C → D → compare 顺序执行；复用输出目录会拒绝覆盖；
- candidate leave-one-out：股票不能用自己制造出来的板块强度给自己背书，股票卡片直接展示
  剔除自身后的主方向 5 日相对收益和 peer coverage；
- PIT relation provenance：每个 `code × theme` 关系有稳定、membership-bound 的 evidence id，
  非成员关系不能生成；股票 panel 与 placed order 都带 snapshot id/hash 和主/辅方向证据。

下一工程阶段优先补 S1 的 fund-flow/event vintage 污染 fixture、S2 的 theme thesis /
独立事件关系契约；S4 顶层 budget/refusal/coverage 汇总已补齐。之后再建立 Sector-First
专属 forward shadow / exact-gene evaluator。
