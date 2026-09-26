"""The settings page's API: ``.env``, trader files and trader handbooks.

``.env`` stays the single source of truth — the page rewrites it in place
(``env_file.update_env``) and every value the page shows is read back from it.
Settings are read when a process starts, so a save takes effect on the next
restart of the scheduler and of this web process; ``changed_since_boot`` says
when that is still owed.

Secrets are write-only: the API returns ``sk-…a3f2`` and whether one is set,
never the value. There is no access control on this API (a decision recorded
in ``docs/exec-plans/active/settings-page.md``), so this is what keeps a key
out of a screenshot, not out of reach.

The most useful thing here is :func:`test_llm`. This system's usual failure
is a pipeline that runs with an error string for content, so a model is
"configured" only once it has answered — the page shows the reply text.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from alpha_agents import config, env_file

logger = logging.getLogger(__name__)

router = APIRouter()

_BOOTED_AT = time.time()

SECRET, TEXT, URL, BOOL, NUMBER = "secret", "text", "url", "bool", "number"


def _f(key, label, kind=TEXT, *, default="", help="", role=None, on=None):
    return {"key": key, "label": label, "kind": kind, "default": default,
            "help": help, "role": role, "on": on}


#: The four LLM roles the page configures. ``SUMMARY`` inherits ``DIGEST``.
LLM_ROLES = [
    ("AGENT", "决策 Agent", "晨扫、选方向、选股、交易计划、卖出、收盘复盘、守则改写。要聪明——交易质量取决于它"),
    ("DIGEST", "新闻过滤", "新闻筛选与事件链接，可以便宜"),
    ("SUMMARY", "总结", "周报与上下文压缩，可以便宜；不填则沿用「新闻过滤」"),
    ("EMBEDDING", "向量", "概念与新闻的语义检索"),
]

PARENT = {"SUMMARY": "DIGEST"}


def _llm_fields() -> list[dict]:
    out = [_f("SILICONFLOW_API_KEY", "SiliconFlow Key（兜底）", SECRET,
              help="各角色没填 Key 时都用它；https://siliconflow.cn 可免费注册")]
    for role, label, help_ in LLM_ROLES:
        url, model = config.LLM_DEFAULTS.get(role, ("", ""))
        out += [
            _f(f"{role}_API_KEY", f"{label} · API Key", SECRET, role=role, help=help_),
            _f(f"{role}_BASE_URL", f"{label} · Base URL", URL, default=url, role=role),
            _f(f"{role}_MODEL", f"{label} · 模型", TEXT, default=model, role=role),
        ]
    return out


GROUPS = [
    {"id": "llm", "title": "模型", "fields": _llm_fields()},
    {"id": "trading", "title": "交易", "fields": [
        _f("AGENT_ENTRY_PRICING", "挂单价由 agent 决定", BOOL, default="1"),
        _f("HARD_STOP_PCT", "定仓风险距离下限（%）", NUMBER, default="8.0",
           help="只在风险预算定仓开启时用于估算单笔风险；不会触发任何卖出——"
                "没有止损也没有止盈，卖出全部由 agent 决定"),
        _f("TOTAL_CAPITAL", "默认交易员本金（元）", NUMBER, default="1000000"),
        _f("DEFAULT_POSITION_PCT", "默认仓位（比例）", NUMBER, default="0.03",
           help="agent 没写仓位时使用，0.03 = 3%"),
        _f("MAX_POSITION_PCT", "单票上限（比例）", NUMBER, default="0.10",
           help="兜底，不是目标"),
        _f("TRADABLE_PREFIXES", "可交易代码前缀", TEXT, default="60,000,001,002,003,300,301",
           help="逗号分隔。默认沪深主板 + 创业板，不含科创板、北交所、B 股"),
    ]},
    {"id": "notify", "title": "推送", "fields": [
        _f("NOTIFY_FEISHU_WEBHOOK", "飞书机器人 Webhook", SECRET, on="feishu"),
        _f("NOTIFY_DINGTALK_WEBHOOK", "钉钉机器人 Webhook", SECRET, on="dingtalk"),
        _f("NOTIFY_WECOM_WEBHOOK", "企业微信机器人 Webhook", SECRET, on="wecom"),
        _f("NOTIFY_TELEGRAM_BOT_TOKEN", "Telegram Bot Token", SECRET, on="telegram"),
        _f("NOTIFY_TELEGRAM_CHAT_ID", "Telegram Chat ID", TEXT, on="telegram"),
    ]},
    {"id": "data", "title": "数据源与超时", "fields": [
        _f("TUSHARE_TOKEN", "Tushare Token", SECRET,
           help="龙虎榜、北向、两融等表；不填不影响主流程"),
        _f("TUSHARE_HTTP_URL", "Tushare 端点", URL, help="只有走代理时才填"),
        _f("CF_WORKER_URL", "Cloudflare Worker 地址", URL,
           help="海外新闻源经它转发；不填时海外源失败，国内源照常"),
        _f("CF_WORKER_AUTH_TOKEN", "Cloudflare Worker 口令", SECRET),
        _f("MONITOR_INTERVAL_SECONDS", "新闻轮询间隔（秒）", NUMBER, default="300"),
        _f("NEWS_FETCH_LIMIT", "每源每次拉取条数", NUMBER, default="50"),
        _f("NEWS_INDEX_RETENTION_DAYS", "新闻向量保留天数", NUMBER, default="30"),
        _f("FUTURES_NIGHT_SESSION", "期货夜盘时段也分析", BOOL, default="0"),
        _f("MORNING_TIMEOUT", "晨扫超时（秒）", NUMBER, default="420",
           help="推理模型慢时先调大它"),
        _f("TOOL_TIMEOUT", "单次工具调用超时（秒）", NUMBER, default="25"),
        _f("EXIT_DECISION_TIMEOUT", "卖出决策超时（秒）", NUMBER, default="120"),
        _f("ENTRY_PRICING_TIMEOUT", "挂单定价超时（秒）", NUMBER, default="240"),
        _f("ORDER_REVIEW_TIMEOUT", "挂单复核超时（秒）", NUMBER, default="120"),
        _f("DIGEST_TIMEOUT", "新闻过滤超时（秒）", NUMBER, default="240"),
    ]},
]

FIELDS = {f["key"]: f for g in GROUPS for f in g["fields"]}

#: What ``.env.example`` ships with. A key still equal to one of these was
#: copied, not configured.
_PLACEHOLDER = re.compile(r"^(sk-(ant-|or-)?x+|x+|your[-_].*|<.*>)$", re.I)


def env_path() -> Path:
    return env_file.default_path()


def is_placeholder(value: str) -> bool:
    return bool(_PLACEHOLDER.match((value or "").strip()))


def mask(value: str) -> str:
    v = (value or "").strip()
    if not v:
        return ""
    if len(v) <= 8:
        return "•" * len(v)
    return f"{v[:3]}…{v[-4:]}"


def _current() -> tuple[dict[str, str], dict[str, str]]:
    """(the file's values, the values this process started with)."""
    return env_file.read_env(env_path()), dict(os.environ)


def _value(key: str, file: dict, environ: dict, overlay: dict | None = None) -> str:
    """What a key resolves to: an unsaved edit, then the file, then the
    environment the process was given (docker ``env_file``, an export)."""
    if overlay and overlay.get(key) not in (None, ""):
        return str(overlay[key]).strip()
    return (file.get(key) or environ.get(key) or "").strip()


def effective_llm(role: str, file: dict, environ: dict,
                  overlay: dict | None = None) -> dict:
    """The (key, url, model) a role will run on, as ``config`` resolves it."""
    def get(k):
        return _value(k, file, environ, overlay)
    key, url, model = get(f"{role}_API_KEY"), get(f"{role}_BASE_URL"), get(f"{role}_MODEL")
    parent = PARENT.get(role)
    if parent and not (key and url and model):
        p = effective_llm(parent, file, environ, overlay)
        key, url, model = key or p["key"], url or p["url"], model or p["model"]
        inherited = parent
    else:
        inherited = None
        d_url, d_model = config.LLM_DEFAULTS.get(role, ("", ""))
        key = key or get("SILICONFLOW_API_KEY")
        url, model = url or d_url, model or d_model
    return {"key": key, "url": url, "model": model, "inherited": inherited,
            "configured": bool(key) and not is_placeholder(key)}


def snapshot() -> dict:
    file, environ = _current()
    groups = []
    for g in GROUPS:
        fields = []
        for f in g["fields"]:
            key = f["key"]
            in_file, in_env = key in file, bool(environ.get(key))
            raw = _value(key, file, environ)
            row = dict(f, source=("file" if in_file else "environment" if in_env
                                  else "default"))
            if f["kind"] == SECRET:
                row.update(value="", set=bool(raw), masked=mask(raw),
                           placeholder=is_placeholder(raw))
            else:
                row.update(value=raw)
            fields.append(row)
        groups.append({"id": g["id"], "title": g["title"], "fields": fields})
    roles = []
    for role, label, _ in LLM_ROLES:
        e = effective_llm(role, file, environ)
        roles.append({"role": role, "label": label, "model": e["model"], "url": e["url"],
                      "inherited": e["inherited"], "configured": e["configured"],
                      "key": mask(e["key"])})
    path = env_path()
    mtime = path.stat().st_mtime if path.exists() else None
    return {
        "groups": groups,
        "roles": roles,
        "env_path": str(path),
        "env_exists": path.exists(),
        "changed_since_boot": bool(mtime and mtime > _BOOTED_AT),
        "onboarding": {"agent_configured": roles[0]["configured"]},
    }


def validate(values: dict) -> dict[str, str | None]:
    """The updates to write, or a ValueError naming the bad field."""
    out: dict[str, str | None] = {}
    for key, value in values.items():
        f = FIELDS.get(key)
        if f is None:
            raise ValueError(f"{key}: 设置页不管理这个变量")
        if value is None:
            out[key] = None
            continue
        v = str(value).strip()
        if f["kind"] == SECRET and v == "":
            continue                       # blank secret: leave it as it is
        if v == "":
            out[key] = None                # blank field: back to the default
            continue
        if f["kind"] == BOOL:
            v = "1" if v.lower() in ("1", "true", "yes", "on") else "0"
        elif f["kind"] == NUMBER:
            try:
                float(v)
            except ValueError as e:
                raise ValueError(f"{f['label']}：不是数字（{v}）") from e
        elif f["kind"] == URL and not v.startswith(("http://", "https://")):
            raise ValueError(f"{f['label']}：要以 http:// 或 https:// 开头")
        out[key] = v
    return out


@router.get("/api/settings")
async def get_settings():
    return JSONResponse(await asyncio.to_thread(snapshot))


@router.put("/api/settings")
async def put_settings(body: dict):
    try:
        updates = validate(body.get("values") or {})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if updates:
        await asyncio.to_thread(env_file.update_env, updates, env_path())
    return JSONResponse(dict(await asyncio.to_thread(snapshot), saved=len(updates)))


# ── tests ──────────────────────────────────────────────────────


async def _chat(key: str, url: str, model: str) -> str:
    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=key, base_url=url, timeout=45, max_retries=0)
    # Room for a reasoning model to think first: with 40 tokens one spent 54
    # on thinking and returned empty content, which reads as "broken".
    resp = await client.chat.completions.create(
        model=model, max_tokens=1024,
        messages=[{"role": "user", "content": "请只回复四个字：连接正常"}])
    if not resp.choices:
        return ""
    text = (resp.choices[0].message.content or "").strip()
    if not text and resp.choices[0].finish_reason == "length":
        raise RuntimeError("模型在回答前用完了 1024 个 token（多半是推理模型的思考过程），"
                           "正式任务的额度更大，但建议确认它能正常作答")
    return text


async def _embed(key: str, url: str, model: str) -> str:
    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=key, base_url=url, timeout=45, max_retries=0)
    resp = await client.embeddings.create(model=model, input=["连接测试"])
    return f"返回 {len(resp.data[0].embedding)} 维向量"


async def test_llm(role: str, overlay: dict | None = None) -> dict:
    """Call the role's model once and return what it said."""
    if role not in dict((r, 1) for r, *_ in LLM_ROLES):
        return {"ok": False, "error": f"未知角色 {role}"}
    file, environ = _current()
    e = effective_llm(role, file, environ, overlay)
    if not e["configured"]:
        return {"ok": False, "model": e["model"], "url": e["url"],
                "error": "没有可用的 API Key（为空，或仍是 .env.example 里的占位符）"}
    started = time.monotonic()
    try:
        call = _embed if role == "EMBEDDING" else _chat
        reply = await call(e["key"], e["url"], e["model"])
    except Exception as exc:                          # noqa: BLE001
        return {"ok": False, "model": e["model"], "url": e["url"],
                "latency_ms": int((time.monotonic() - started) * 1000),
                "error": f"{type(exc).__name__}: {exc}"[:600]}
    return {"ok": bool(reply), "model": e["model"], "url": e["url"],
            "latency_ms": int((time.monotonic() - started) * 1000),
            "reply": reply or "", "error": "" if reply else "模型返回了空内容"}


@router.post("/api/settings/test-llm")
async def post_test_llm(body: dict):
    return JSONResponse(await test_llm(str(body.get("role") or ""),
                                       body.get("values") or {}))


def test_notify(channel: str, overlay: dict | None = None) -> dict:
    from alpha_agents import notify
    file, environ = _current()

    def get(k):
        return _value(k, file, environ, overlay)
    title, text = "AlphaAgents 测试消息", "设置页发送的测试消息。收到即说明推送配置正确。"
    senders = {
        "feishu": lambda: notify.send_feishu(title, text, webhook=get("NOTIFY_FEISHU_WEBHOOK")),
        "dingtalk": lambda: notify.send_dingtalk(title, text, webhook=get("NOTIFY_DINGTALK_WEBHOOK")),
        "wecom": lambda: notify.send_wecom(title, text, webhook=get("NOTIFY_WECOM_WEBHOOK")),
        "telegram": lambda: notify.send_telegram(
            title, text, bot_token=get("NOTIFY_TELEGRAM_BOT_TOKEN"),
            chat_id=get("NOTIFY_TELEGRAM_CHAT_ID")),
    }
    if channel not in senders:
        return {"ok": False, "error": f"未知渠道 {channel}"}
    ok = senders[channel]()
    return {"ok": ok, "error": "" if ok else "发送失败：没有配置，或对方返回了错误（详见服务日志）"}


@router.post("/api/settings/test-notify")
async def post_test_notify(body: dict):
    return JSONResponse(await asyncio.to_thread(
        test_notify, str(body.get("channel") or ""), body.get("values") or {}))


# ── traders and handbooks ──────────────────────────────────────

_TRADER_ID = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_TRADER_FIELDS = ("id", "name", "capital", "prompt_file", "default_size_pct",
                  "max_position_pct", "default_horizon_days", "enabled", "note",
                  "tags", "extra_prompt")


def _traders_dir() -> Path:
    from alpha_agents.data import trader
    return trader.TRADERS_DIR


def _trader_file(tid: str) -> Path:
    d = _traders_dir()
    for ext in (".yaml", ".yml"):
        if (d / f"{tid}{ext}").exists():
            return d / f"{tid}{ext}"
    return d / f"{tid}.yaml"


def _prompt_files() -> list[str]:
    return sorted(p.name for p in config.PROMPTS_DIR.glob("*.md"))


def traders() -> dict:
    import yaml

    from alpha_agents.data import trader as T
    from alpha_agents.evolution import handbook
    out = [{"id": T.DEFAULT_TRADER, "builtin": True, "name": "默认交易员",
            "fields": {}, "has_handbook": handbook.path(T.DEFAULT_TRADER).exists()}]
    d = _traders_dir()
    files = sorted(d.glob("*.yaml")) + sorted(d.glob("*.yml")) if d.is_dir() else []
    for path in files:
        row = {"id": path.stem, "builtin": False, "file": path.name}
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            row["fields"] = {k: raw.get(k) for k in _TRADER_FIELDS if k in raw}
            row["name"] = str(raw.get("name") or path.stem)
            row["valid"] = T._coerce(raw, path) is not None
        except yaml.YAMLError as e:
            row.update(fields={}, name=path.stem, valid=False, error=str(e)[:300])
        row["has_handbook"] = handbook.path(row["id"]).exists()
        out.append(row)
    live = {t.id: t for t in T.load_traders()}
    for row in out:
        t = live.get(row["id"])
        row["running"] = bool(t)
        row["legacy"] = bool(t and t.legacy)
    return {"traders": out, "prompt_files": _prompt_files()}


def _leading_comments(text: str) -> str:
    lines = []
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            lines.append(line)
            continue
        break
    return "\n".join(lines).rstrip() + "\n\n" if any(
        ln.startswith("#") for ln in lines) else ""


def save_trader(tid: str, fields: dict) -> dict:
    import yaml

    from alpha_agents.data import trader as T
    if not _TRADER_ID.match(tid) or tid == T.DEFAULT_TRADER:
        raise ValueError("id 只能是小写字母开头的字母、数字、下划线（最多 32 位），且不能是 default")
    raw = {k: fields[k] for k in _TRADER_FIELDS if k in fields and fields[k] not in (None, "")}
    raw["id"] = tid
    if raw.get("prompt_file") and raw["prompt_file"] not in _prompt_files():
        raise ValueError(f"提示词文件不存在：{raw['prompt_file']}")
    if isinstance(raw.get("tags"), str):
        raw["tags"] = [t.strip() for t in raw["tags"].split(",") if t.strip()]
    path = _trader_file(tid)
    trader = T._coerce(raw, path)
    if trader is None:
        raise ValueError("字段类型不对（本金、天数要是整数，仓位要是小数）")
    for k in ("capital", "default_horizon_days"):
        if k in raw:
            raw[k] = int(raw[k])
    for k in ("default_size_pct", "max_position_pct"):
        if k in raw:
            raw[k] = float(raw[k])
    if "enabled" in raw:
        raw["enabled"] = raw["enabled"] in (True, "true", "1", 1)
    head = _leading_comments(path.read_text(encoding="utf-8")) if path.exists() else ""
    body = yaml.safe_dump({k: raw[k] for k in _TRADER_FIELDS if k in raw},
                          allow_unicode=True, sort_keys=False, width=1000)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(head + body, encoding="utf-8")
    logger.info("Settings page wrote trader %s", path)
    return {"id": tid, "file": path.name}


def _handbook_id(tid: str) -> str:
    from alpha_agents.data import trader as T
    if tid != T.DEFAULT_TRADER and not _TRADER_ID.match(tid):
        raise ValueError("非法的交易员 id")
    return tid


def handbook_view(tid: str) -> dict:
    from alpha_agents.evolution import handbook
    tid = _handbook_id(tid)
    p = handbook.path(tid)
    hist = handbook._history(tid)
    versions = sorted((x.stem for x in hist.glob("*.md")), reverse=True) if hist.is_dir() else []
    return {"id": tid, "text": p.read_text(encoding="utf-8") if p.exists() else "",
            "versions": versions, "path": str(p)}


def handbook_version(tid: str, version: str) -> str:
    from alpha_agents.evolution import handbook
    tid = _handbook_id(tid)
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", version):
        raise ValueError("版本名应是日期")
    p = handbook._history(tid) / f"{version}.md"
    if not p.exists():
        raise FileNotFoundError(version)
    return p.read_text(encoding="utf-8")


def save_handbook(tid: str, text: str) -> None:
    from alpha_agents.evolution import handbook
    tid = _handbook_id(tid)
    p = handbook.path(tid)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text.rstrip() + "\n", encoding="utf-8")
    logger.info("Settings page edited the handbook of %s", tid)


def _bad(e: Exception):
    return HTTPException(status_code=400, detail=str(e))


@router.get("/api/settings/traders")
async def get_traders():
    return JSONResponse(await asyncio.to_thread(traders))


@router.put("/api/settings/traders/{tid}")
async def put_trader(tid: str, body: dict):
    try:
        got = await asyncio.to_thread(save_trader, tid, body.get("fields") or {})
    except ValueError as e:
        raise _bad(e) from e
    return JSONResponse(got)


@router.get("/api/settings/traders/{tid}/handbook")
async def get_handbook(tid: str):
    try:
        return JSONResponse(await asyncio.to_thread(handbook_view, tid))
    except ValueError as e:
        raise _bad(e) from e


@router.get("/api/settings/traders/{tid}/handbook/{version}")
async def get_handbook_version(tid: str, version: str):
    try:
        text = await asyncio.to_thread(handbook_version, tid, version)
    except ValueError as e:
        raise _bad(e) from e
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail="没有这个版本") from e
    return JSONResponse({"version": version, "text": text})


@router.put("/api/settings/traders/{tid}/handbook")
async def put_handbook(tid: str, body: dict):
    try:
        await asyncio.to_thread(save_handbook, tid, str(body.get("text") or ""))
    except ValueError as e:
        raise _bad(e) from e
    return JSONResponse(await asyncio.to_thread(handbook_view, tid))
