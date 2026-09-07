# Execution plans

Two tiers, by cost of being wrong.

**Small change** — no file. Make it, test it, commit it. A plan for a
one-line fix is overhead.

**Anything spanning files, changing behaviour, or that a future reader
would ask "why" about** — write `active/<slug>.md` first, from the
template below. Move it to `completed/` when done. Plans are versioned
alongside the code so an agent can work without external context.

## Template

```markdown
# <what this achieves>

状态：active
创建：YYYY-MM-DD

## 目标
One paragraph. What is true after this that is not true now.

## 验收
Machine-checkable. "跑 X 输出 Y"，不是"改进 Z"。
- [ ] 具体命令 → 具体输出
- [ ] 测试覆盖 <行为>

## 决策记录
Append as you go. Each entry: what was chosen, what was rejected, why.
The rejected option is the part worth writing down — the next agent will
otherwise reconsider it from scratch.

## 风险
What could this break, and what would tell us.
```

`lint_docs.py` fails an active plan with no 验收 section: a criterion
nobody can check is not a criterion.

## Archive

`completed/` is a historical record. Its links and module references are
not maintained and are exempt from the doc linter — they described the
world at the time, and rewriting history to satisfy a linter would
destroy the only thing an archive is for.
