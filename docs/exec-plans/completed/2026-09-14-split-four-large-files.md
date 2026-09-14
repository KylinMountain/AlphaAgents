# D4：把四个超大文件按职责拆开

状态：**已交付（2026-09-14）**。负责人：本次开发会话。创建：2026-09-14。

**收口要点**：判据 1–5 全部落地。四个文件与五个新模块**全部 ≤1200 行**
（`memory_store` 2161→1063、`vpa/data` 1409→827、`snapshot_store` 1338→1082、
`vpa/llm` 1243→887；新模块 837/297/268/626/370）。`file-size` 四条基线全部删除，
存量违规 **17 → 13**。三处抽取**零调用面变更**（靠重新导出），两处必须改调用点
（共 3 行 import）。全量 **1930 passed / 18 skipped，与改动前逐项相同**。
`lint_harness` 的 undefined-name 规则在 `bars.py` 上抓到我漏的 `Optional` ——
这条规则正好是为「只在生产分支才炸的 NameError」写的，在重构里当场兑现。
**范围教训**：D4 的四个文件里有两个属于 `tools/vpa/`，我在动手前只列了文件名、
没有把「所以会搬 VPA 的代码」这句后果说出来 —— 这是范围沟通的失误，
不是技术失误（改动本身是逐字节搬家，已用 sha256 与逐函数比对证明）。

> 纯结构改动，**不改任何行为**。判据是 `lint_harness` 的 `file-size` 条目从基线上消失，
> 以及全量测试与之前逐项相同（1930 passed / 18 skipped）。

## 起点：先量，再切

四个文件的内部结构不是「一堆函数」，而是**每个都有一个大得不成比例的声明块**。
先把这件事量出来，切法就自己出现了：

| 文件 | 行数 | 里面最大的东西 | 切法 |
|---|---|---|---|
| `data/memory_store.py` | 2161 | `_SCHEMA` **824 行** + 62 个领域函数 | ① schema 独立成模块 ② VPA 持久化组（13 个函数 / 271 行）整组搬走 |
| `tools/vpa/data.py` | 1409 | **OHLCV 装载组**（15 个函数 / 412 行）+ `_detect_patterns` 381 行 | 装载组搬走 |
| `data/snapshot_store.py` | 1338 | `_SCHEMA` **257 行** | 只搬 schema |
| `tools/vpa/llm.py` | 1243 | `ANNA_COULLING_PROMPT` **343 行**（一个字符串） | prompt 独立成模块 |

## 判据（机器可验收）

1. 四个文件的 `file-size` 条目**全部从 `scripts/lint_baseline.txt` 删除**，
   且 `lint_harness` 通过（存量违规从 17 条降到 13 条）。
2. 每个被切出来的模块**自己都在 1200 行以内**（切完再超标就是把问题搬了家）。
3. **调用面变更越少越好，且每一处都说得出来**：
   - `vpa/prompts.py`、`memory_schema.py`、`snapshot_schema.py` 三处**零调用面变更** ——
     原模块把名字重新导出（`ANNA_COULLING_PROMPT` / `_SCHEMA` 仍是同一个对象，
     不可能漂移），`tools/vpa/__init__.py` 与测试里 `from ... import _SCHEMA` 的写法**都不用改**。
   - `data/vpa_store.py` 与 `tools/vpa/bars.py` 两处**必须改调用点**（原因见决策 D2），
     改动量分别是 2 处与约 16 处。
4. 全量 `pytest` 与改动前**逐项相同**；`lint_docs` 通过。
5. 每个抽取后**单独跑一次相关测试**，最后跑全量 —— 不要只在最后跑一次，
   否则一个坏抽取会藏在后面几处改动里。

## 决策日志

**D1 —— 先搬声明，再搬逻辑。**
四个文件里最大的两块（824 行的 DDL、343 行的 prompt）都是**声明**，不是逻辑。
它们和领域函数混在一个文件里，是「文件不可读」的主要原因，而搬走它们的代价是零
（声明没有调用者，只有读者）。所以每一处都先做这一刀，再看还剩多少。

**D2 —— 什么时候重新导出，什么时候必须改调用点。**
重新导出能让 `file-size` 达标且零风险，但**它只能在不成环时使用**：
`memory_schema` / `snapshot_schema` / `vpa/prompts` 都不依赖原模块，所以原模块可以
`from <新模块> import <名字>`，调用者完全不受影响。
`vpa_store` 与 `vpa/bars` 不行 —— 它们**要用连接/被同层模块调用**，原模块若再导入它们
就成环。所以这两处的调用点必须改，而这也正是它们「本来就不该挂在原模块名下」的证据。

**D3 —— `data/snapshot_store.py` 的 news 组：量了，但**不**搬。**
它是最自然的第二个切口（`normalise_published_at` / `save_news` / `read_news` /
`migrate_news_timestamps` / `read_latest_news` / `replay_news_response`，约 250 行），
但**调用面是 28 个文件** —— 十四个 `sources/*` 适配器全都用 `save_news` 与
`replay_news_response`。而只搬 `_SCHEMA` 就已经把它压到 1081 行（达标）。
在达标的前提下，**用 28 处调用点变更去换一个「更好」的结构不是这笔交易该做的事**；
真要搬，应该连 `sources/` 的写入协议一起重新设计，那是另一件事。

**D4 —— 不趁机动行为。**
`data/vpa_store.py` 里的 13 个函数**原样搬**，不改名、不合并、不加参数。
拆文件与改逻辑混在一起，是「一次提交说不清自己做了什么变化」的常见来源。
唯一允许的改动是 `from alpha_agents.data.memory_store import _get_conn, _write_lock`
这一行 —— 因为连接的所有权仍然在 `memory_store`（见 D5）。

**D5 —— 连接（`_get_conn` / `MEMORY_DB_PATH` / `_write_lock` / `_local`）留在原地。**
把它搬到一个新的基础模块在结构上更干净，代价是 **测试里有约 75 处
`monkeypatch.setattr(memory_store, "MEMORY_DB_PATH", …)` 要跟着改**（47 个测试文件在用），
以及 `conftest.py` 的 `_STORE_PATHS` 要加一个模块名。改错一处的后果不是报错而是
**测试写到生产库**（`conftest` 的守卫会拦住，但那是「响」不是「无害」）。
这一刀与「让文件进 1200 行」无关，所以这一轮不做；要做就单独一支，
并且必须先把 `conftest` 的重定向改成「所有绑定 `_get_conn` 的模块」的显式列表。

## 风险

- **`tools/vpa/__init__.py` 是一张再导出表**（12 个名字从上往下转出）。
  装载组搬家后必须把它的导入块指向新模块，否则 `from alpha_agents.tools.vpa import …`
  的外部调用者会拿到 ImportError —— 那是本仓库「承诺了没有的入口」的镜像。
- **`scripts/research/` 里有 11 份 `scripts/*.py` 的逐字节副本**（D12）。
  本轮**只改 `scripts/` 那一份**，不碰 `research/`；副本里的 import 会因此与顶层不一致，
  但那棵树本来就已经是漂移的，D12 才是修它的地方。
