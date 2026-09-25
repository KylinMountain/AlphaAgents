# AlphaAgents：交易员生命周期二次深度审查

日期：2026-09-25  
审查基线：`ce83ce6b507be55b190c585c4f3d09495c17dd19`  
已提交修复：`2af26fb8fbaef817b6d2e2add2dfb2bcbdd0921f`  
结论：**REWORK**。模拟研究可以继续；不能把“每天生成复盘、守则发生改变”认定为“交易能力已经进化”。

## 1. 范围、证据与本次交付

本次检查看盘、候选选择、计划、账户执行、持仓论点、逐笔与收盘复盘、守则、候选实验、策略晋升及调度链路。依据是源码、测试和下述合成反例，不是逐行覆盖全仓的形式化证明。

用户提供的七轮历史结果没有在本次重新执行，也没有访问用户本地行情库、原始七轮日志或模型服务。用户已确认各轮使用同一个底层模型，因此不把“更换模型”列为差异解释。相同模型下的随机性、输入和状态差异仍须通过配对试验识别。

已修复并提交：

- T+1/sector-first 计划器不再把无理由的 `orders: []` 当成有效观望。保留 `no_trade_reason`、逐候选 `rejected`，经不可变 opportunity context 进入收盘复盘。非法订单全部被代码拒绝与自主观望分别表达。
- 合法空守则可清空；畸形非空列表不会误清空或部分覆盖旧守则。保留过去版本和删除记录。
- 规则内容变更、删除后重引入有独立版本和生效起点。逐笔复盘按下单日前版本绑定规则，前向统计要求版本匹配且下单发生在该版本之后；旧的未绑定复盘不追认为新版本证据。

边界：没有强制交易、提高仓位、增加 LLM 调用或重试到买入；没有证明收益提升。此次解释契约修复覆盖 T+1/sector-first seam，并不等于所有不同的 live 入口都已统一。规则绑定仍是日级历史重建，不是实际决策时刻的完整状态快照。

验证：GitHub Actions run **36086737643**、job **107920192499**，锁定依赖 `uv sync --frozen --extra dev`。针对性测试 **108 passed**；全套 **3435 passed, 20 skipped, 2 warnings**。harness、docs、policy 三项检查通过。两条 pytest 警告来自多线程进程使用 fork 的现有测试。跳过项不能视作通过，缺少真实行情和模型的验证不在这些结果内。详见 [验证记录](evidence/2026-09-25-trader-lifecycle/verification.json)。

## 2. 表面能力与代码证据

| 环节 | 已有实现 | 尚未成立的结论 |
|---|---|---|
| 看盘 | 新闻窗口、主题状态、资金和量价工具、研究包 | 定时调用这些工具不等于连续跟踪同一套市场假设 |
| 交易 | 意图、预约、成交、T+1、论点和失效唤醒 | 共用部分账本不等于回放与在线决策一致 |
| 复盘 | 逐笔复盘、当天市场诊断、守则引用；本次补计划拒绝理由 | 正确统计日线极值不等于准确解释当时能做什么 |
| 学习 | 个人 MEMORY、历史版本、候选证据 | 写进下一天提示词的经验不一定有适用域或足够反证 |
| 进化 | forward gate、producer coverage、注册表、自动晋升调用 | 特定预测映射可晋升不等于交易策略的任意行为都能受控进化 |
| 验证 | 单元测试、严格录制回放、隔离 sandbox | 可重复播放一次轨迹不等于估计了随机性或因果效果 |

核心偏差：**可信事实、经验解释、行为更新目前没有处处使用同一个时间边界、身份边界和版本边界。** 不建议重写工程或继续堆 agent；优先把已有链路接成一致的闭环。

## 3. 主要发现

### AL-01 / P1：经验直接改变行为，正式策略验证只覆盖另一部分

代码：[context_builder.py](../../alpha_agents/evolution/context_builder.py) `build_morning_context`；[trade_review.py](../../alpha_agents/evolution/trade_review.py) `inject`；[policy_sources.py](../../alpha_agents/evolution/policy_sources.py) `collect`。

morning context 直接注入个人守则、逐笔复盘和昨日诊断，baseline 模式也包含这部分；`knowledge_in_force` 则有自己的批准快照路径。`collect` 覆盖 prompts 目录、声明模型、部分常量、批准知识及 decision 参数，不完整描述 Python 内嵌的学习提示词和实际注入的个人状态。因此，某些行为变化并不会体现为已批准知识版本的变化。

不是要禁止交易员阅读日记，也不是每条经验都由人批准。应分清：事实记忆、暂定假说、条件化交易经验、强制约束。未验证经验可供参考，但不能无声变成硬性准入规则。规则升级应有声明的权限和实验范围。

建议同时记录稳定的 `policy_build_hash`（实现、全部提示词、更新算法和配置）与每次决定的 `state_snapshot_hash`（实际记忆、组合、引用证据）。不能通过把每一天的新日记都当作新策略版本，制造无意义版本爆炸。需要记录的是“哪种更新算法，在什么状态下做了什么”。

验收：修改内嵌更新提示词会改变 build hash；改变私人记忆会改变 state hash；同一时点的复盘能取回真正注入的文本。基线明确是否允许学习，禁止名为 baseline 却带有未声明的学习状态。

### AL-02 / P1：从经验候选到交易行为的可执行映射仍有断口

代码：[variant.py](../../alpha_agents/evolution/variant.py) `_SUPPORTED_DELTAS`、`_delta_to_change`；[shadow.py](../../alpha_agents/evolution/shadow.py) `PRODUCERS`；[auto_promote.py](../../alpha_agents/evolution/auto_promote.py)；[review.py](../../alpha_agents/pipeline/tasks/review.py) 的自动晋升调用。

当前 `_SUPPORTED_DELTAS = {}`。旧映射被删除是正确的防误映射措施，但因此这条 evidence-candidate 到参数变体的转换目前没有受支持项。注册的 shadow candidate producer 是 `remap_confidence`，覆盖置信度先验映射，不是任意入场、退出、仓位或守则变化的完整交易员生产器。代码确实有自动晋升调用，不能再笼统说“没有自动晋升”。

应准确描述为：**有受控晋升设施，但可产生、可执行、可验证的交易行为变体覆盖不足。**

建议先选一个行为族贯通，例如“某条候选否决条件的适用范围”：结构化变更、固定上游输入、不同计划输出、按真实执行规则计分、封存前向证据、晋升与回滚。暂不同时开放几十种自由文本策略修改。

验收：一个来自真实决策记录的候选，能沿 candidate→variant→producer→decision→outcome→gate→active→rollback 全链路走通；unsupported delta 显式失败，不能只修改叙述或标签。

### AL-03 / P1：晋升判断是点估计容忍，不是统计上证实改善

代码：[holdout_gate.py](../../alpha_agents/evolution/holdout_gate.py) `evaluate_candidate`；[policy_registry.py](../../alpha_agents/data/policy_registry.py)；[auto_promote.py](../../alpha_agents/evolution/auto_promote.py)。

默认最小样本 20，配对键是 date/code。最终条件为 `mean_diff <= 0.005`；t 统计量展示出来，却不参与该布尔决策。这里是一个带容忍区间的均值比较，不是置信区间意义上的非劣效证明，更不是优越性检验。

合成探针：同一天 20 个不同代码，双方 Brier 完全相同时，纯 evaluator 返回 promote；candidate 平均 Brier 更差 0.004 时也返回 promote。**探针只调用纯 evaluator，没有调用实际批准或晋升，不能据此声称绕过了来源、封存、注册表及 CAS 前置条件。**

建议分开 compatibility/non-inferiority 与 improvement；以日期/连续时间块处理相关样本，明确预测期限重叠、独立日数、候选数量和重复查看的预算。一个日期的 20 只股票不应被解释为 20 个独立市场阶段。校准改善还需验证交易行为及成本后效用，不宜只靠 Brier 推断盈利提升。

验收：相同表现不能被报告为 improved；仅样本点估计略好但区间宽时返回 insufficient；重复检查同一批样本不得制造新证据；每个 gate 记录完整评估协议及适用行为族。

### AL-04 / P1：回放和在线仍是不同的决策流程

代码：[walk_forward.py](../../scripts/walk_forward.py)；[morning_scan.py](../../alpha_agents/pipeline/tasks/morning_scan.py) `_scan_for`、`_cross_validate_recommendations`、`_save_recommendations_list`；[entry_pricing.py](../../alpha_agents/pipeline/tasks/entry_pricing.py)。

sector-first 回放是方向选择→个股选择→无工具的交易计划；在线 morning 使用另一套 analysis，之后有代码四维检查；在线盘中又有带工具的 entry pricing。不同入口的拒绝、未知、失败、仓位字段处理不完全一致。这意味着“该回放配置有改善”不能直接推出“线上交易员也改善”。

另需明确两个概率事件：morning prediction 记录 `confidence_to_prob(confidence, dims_passed)`，thesis 有 agent 自报 prob；calibration 模块读取 thesis。因此不是“所有概率校准都错了”，而是两种概率、标签和指标必须独立命名，不能互相当作证据。

仓位亦有差异：sector plan 提示 0.005–0.5，t1 parse 下界 0.0001 且无同样上界，thesis 又有自己的合法区间。应把 requested size、effective size、调整原因单独记录，而非悄悄归一化后称作 agent 的选择。

建议一套领域层 `Trader.step(DecisionFrame, TraderState)`，回放与在线仅更换数据/执行适配器；不是必须一次性改完，而是先抽取计划契约和状态快照。

验收：相同 frame、state 和录制模型回复，通过不同入口得到相同计划及订单语义；未知数据不能当成反对信号，也不能默认支持信号。

### AL-05 / P0（事实完整性）：缺失当天行情时会把旧涨幅写成今天

代码：[day_record.py](../../alpha_agents/evolution/day_record.py) `_bar`；[close_review.py](../../alpha_agents/pipeline/tasks/close_review.py) `_day_facts`、`_record`。

`_bar` 查询 `date <= day` 的最近两根 close，没有核验第一根就是请求日。合成数据只有 01-05、01-06，查询 01-07 仍得到 01-06 的 close 和涨幅，随后渲染为“今日”。`_day_facts` 的 has_today 只要求当天至少一行行情，并非相关组合与样本都完整。

另外，在线 `_record` 读取复盘时当前 active themes，却将其称为早盘跟踪主线；它不是早盘选择的冻结快照。不能据此准确判断“早晨为什么没看到”。

这些是学习输入的正确性问题，不是风格偏好，应先于更多学习实验处理。建议返回 bar timestamp、coverage、available_at，缺失时保留缺失；早盘决策从已保存的当时记录恢复，不能用收盘状态补写过去。

验收：请求日无行情时明确 missing；局部回填不宣称盘面完整；日内新增主题不会反向出现在早盘已见列表。合成反例见下方脚本。

### AL-06 / P1：复盘仍可能用不可达结果解释交易失误

代码：[trade_review.py](../../alpha_agents/evolution/trade_review.py) `facts`、`facts_line`；[market_review.py](../../alpha_agents/evolution/market_review.py) `_INSTRUCTIONS`；回放的 synthetic-close 声明。

逐笔峰值使用买入日至卖出日的整日日线高点。开盘卖出后当天才出现的高点、收盘买入前已出现的高点，也可能被算进“持有期最高/吐回”。源码已经把日线极值注明为上界，不能把它说成完全没有披露的泄漏；但该上界不等于可执行的错失利润，也不应直接教出“下次应该在那里卖”。

收盘诊断又以当日大涨未参与作为错过，容易把事后结果与当时应有的判断混在一起。仅靠一句“早盘能不能看出来”不足以校验结论。

建议复盘两张独立成绩单：决策质量仅根据当时信息与能力；结果质量根据预先声明的期限、成交路径、成本、风险及对照。`potential_intraday_high` 与 `achievable_exit` 分字段；缺少细粒度路径就不计算后者。T+1、停牌、涨跌停和成交容量须由代码限定可执行动作。

验收：开盘退出不能被当天随后高点判为“吐回”；收盘入场不能受当日早些时候极值影响；按规则做出合理决定但结果亏损不会自动生成禁止该类交易的教条。

### AL-07 / P1：复盘失败会留下“已复盘”记录而失去补做机会

代码：[trade_review.py](../../alpha_agents/evolution/trade_review.py) `write_words`、`save`、`unreviewed`、`review_closed`；[close_day.py](../../alpha_agents/evolution/close_day.py)。

无模型、异常或不可读返回 NO_WORDS；review_closed 仍保存、增加处理数。之后 unreviewed 只找没有 trade_reviews 行的仓位，INSERT OR IGNORE 也不更新已有行。于是事实虽然保存了，解释失败却不再作为待完成复盘返回。close_day 有 market/handbook 失败数，但没有同等级的逐笔 review 完成/失败计数。

建议事实记录与解释任务分离：facts_ready→review_pending→review_complete/review_failed，尝试记录追加，有限重试且幂等。记录 review_created_at 和可见时间，不把迟到复盘回填成过去已知。

验收：首次超时后事实保留，下一轮能补解释；处理计数不等于成功学习计数；重复任务不重复创建学习样本。

### AL-08 / P1：日内工作记忆没有交易员身份

代码：[session_memory.py](../../alpha_agents/pipeline/tasks/session_memory.py)；[intraday_monitor.py](../../alpha_agents/pipeline/tasks/intraday_monitor.py)。

`_declined` 按 code 存储，note/recall 没有 trader_id。不同交易员通过同一进程访问时，可能把别人的拒绝理由作为“你之前的决定”读回。记忆使用 wall clock 和进程内字典，重启丢失。

建议共享市场事实、隔离账户信念；键至少含 run/trader/session/code，决策记录可恢复。是否重问由新价格、新证据或论点状态的变化决定，不是简单禁问 45 分钟，也不是每五分钟重做一整轮。

验收：A 拒绝不污染 B；重启能重建当日已做决定；同输入未变化不重复昂贵研究，关键新证据到达可立即重新评估。

### AL-09 / P1：长分析任务可能阻塞同调度器的短周期任务

代码：[scheduler.py](../../alpha_agents/pipeline/scheduler.py) `run`；盘中账本管理及订单复核链路。

Scheduler 在同一个任务循环中逐个 await；一个慢任务会延迟同循环后面的任务。超时分支明确说明 to_thread 内阻塞工作仍可能继续。失败也写 last_run，这个字段不能代表成功完成。这里未测真实部署下的延迟，也不声称整个进程没有其他并发任务。

建议分离低延迟账户/执行控制与高延迟研究复盘；同一账户写入保持串行或有明确事务仲裁，不能直接 gather 全部任务。研究输出必须有有效期、对应状态版本、deadline，迟到结果禁止作用于已改变账户。

验收：注入长研究延迟时，不阻塞配置内的持仓检查期限；超时任务的晚到结果不能重复下单；last_attempt、last_success、next_retry 分开。

### AL-10 / P2：缺少便宜的决策实验层，文档与实际能力也有漂移

代码：[llm_journal.py](../../alpha_agents/llm_journal.py)；[walk_resume.py](../../scripts/walk_resume.py)；[walk_forward.py](../../scripts/walk_forward.py)；[ARCHITECTURE.md](../../ARCHITECTURE.md)。

journal 的顺序严格匹配适合恢复同一轨迹，不是变更提示词后的通用缓存。修改决定后，账户、复盘、守则都会分叉，不能继续重放原来的后续回答。walk_forward 当前为 5717 行；只有更明确的阶段接口，才能在不重跑整月的情况下定位决策差异。

文档部分段落仍描述没有生产晋升 caller，而代码已有 auto_promote；审查应以代码为准，后续文档应区分“模块存在”“接到在线流程”“实验覆盖”“收益证实”。

建议验证分四层：确定性契约测试；冻结现场的单阶段配对、多次独立采样；短连续轨迹分叉；未参与调参窗口和真正前向纸面运行。独立现场可以并行，依赖昨日账户与记忆的连续轨迹不能按日期随意并行。按事实缓存减少调用，不缓存掉待测随机性。

验收：能读取某个决策点，显式只改一项经验，记录完整输入/输出差异；拒绝、合理观望、合理入场、坏数据场景均覆盖；报告成本、完整率和行为差异，不用“下单更多”作为统一胜利标准。

## 4. 目标：不是更多报告，而是同一个交易员持续更新判断

建议的最小领域闭环如下，名称为设计建议，不表示已经实现：

`MarketFrame → TraderState/Beliefs → Decision → Execution → Outcome → Review → Hypothesis → Experiment → PolicyUpdate`

MarketFrame 是当时已可知的事实；TraderState 包含组合、在跟踪的主题、待验证论点、过去接受/拒绝决定和关注预算；Decision 同时表达买、卖、加减、持有、等待、拒绝、数据不足。执行层仅保证市场规则、账户边界与事实，不替模型预设买卖答案。

Review 将“当时判断是否合理”与“结果怎样”分开。Hypothesis 不是新的绝对禁令，而是带条件、证据、反例、适用市场状态和到期/撤回条件的经验。Experiment 先验证局部机制，再验证连续运行。PolicyUpdate 更新的是可执行且被实验覆盖的行为，不能只更新一段自我评价。

每天看盘还需要一个持续的问题：“我现在的哪条市场判断正在被新信息支持或推翻？”而不是每次从头写一篇市场综述。现有 theme/thesis/wake 模块值得复用，重点是将身份、时间、版本和实际行动接通。

## 5. 下一阶段实施顺序与最低复审门槛

### M1：先保证复盘没有记错

处理 AL-05、AL-06、AL-07、AL-08，统一 Decision 状态和缺失语义。每次决定至少绑定 run/trader/phase、information_cutoff、policy_build_hash、state_snapshot_hash、证据版本、动作、拒绝/失败原因。没有上下文就标未知，不补写动机。

最低门槛：本文缺失行情、极值时序、跨交易员三组反例变成回归测试；复盘失败可补做且不重复计样本；既有 3435 测试继续通过；明确任何跳过项。

### M2：形成可以低成本验证的同一交易员

处理 AL-04、AL-09、AL-10。先抽出计划接口与 DecisionFrame，不先大拆整个项目。完成冻结现场测试及短轨迹分叉；把在线与回放的能力差异显式放进 frame，避免给日线回放虚构盘中能力。

最低门槛：同录制输入跨适配器产生同语义动作；正常/异常/拒绝均可完整回放；每次实验有预算及输入版本；慢研究不阻塞预定账户控制期限。

### M3：证明一类行为真的进化

处理 AL-01、AL-02、AL-03。只选一个行为族，贯通证据→可执行变体→独立前向验证→可撤销晋升。数据合法性和风险约束不允许由学习规则自行删除；个人观察仍可即时记忆，强约束升级必须走声明机制。

最低门槛：全链路可追溯；无改善不宣称 improved；未验证规则不以强制约束身份生效；版本支持回滚；用预先确定的期限和风险/成本指标检查全策略，而非仅靠预测分数、出手次数或一个盈利窗口。

这不是三项并行大工程：先记对，再做对照，最后谈进化。

## 6. 合成反例与复现

脚本：[reproduce_review_findings.py](evidence/2026-09-25-trader-lifecycle/reproduce_review_findings.py)  
结果：[reproduction_results.json](evidence/2026-09-25-trader-lifecycle/reproduction_results.json)

从仓库根目录执行：

```bash
python docs/reviews/evidence/2026-09-25-trader-lifecycle/reproduce_review_findings.py .
```

仅标准库、内存 SQLite 和源码函数，不调用模型、网络或业务数据库。当前断言描述审查时存在的行为；修复相应问题后，应同步替换为正确语义的正式测试，而不是维持反例继续成立。

工程方法参照：Anthropic《Demystifying evals for AI agents》（2026-01-09）区分轨迹与环境实际结果、独立多次试验及能力/回归评估。此文用于评估方法，不作为本项目盈利有效性的证据：https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents
