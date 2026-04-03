"""Push notification support — DingTalk, WeCom (企业微信), Telegram.

All three use simple webhook POST. Configure via env vars:
  NOTIFY_DINGTALK_WEBHOOK=https://oapi.dingtalk.com/robot/send?access_token=xxx
  NOTIFY_WECOM_WEBHOOK=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx
  NOTIFY_TELEGRAM_BOT_TOKEN=123456:ABC-DEF
  NOTIFY_TELEGRAM_CHAT_ID=123456789
"""

import json
import logging
import os

import httpx

logger = logging.getLogger(__name__)

_DINGTALK_WEBHOOK = os.environ.get("NOTIFY_DINGTALK_WEBHOOK", "")
_WECOM_WEBHOOK = os.environ.get("NOTIFY_WECOM_WEBHOOK", "")
_TELEGRAM_BOT_TOKEN = os.environ.get("NOTIFY_TELEGRAM_BOT_TOKEN", "")
_TELEGRAM_CHAT_ID = os.environ.get("NOTIFY_TELEGRAM_CHAT_ID", "")


def _post(url: str, payload: dict, timeout: int = 10) -> bool:
    """POST JSON to a webhook URL. Returns True on success."""
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, json=payload)
            if resp.status_code >= 400:
                logger.warning("Notify POST failed: %d %s", resp.status_code, resp.text[:200])
                return False
            return True
    except Exception as e:
        logger.warning("Notify POST error: %s", e)
        return False


def send_dingtalk(title: str, text: str) -> bool:
    """Send a DingTalk robot message (Markdown format)."""
    if not _DINGTALK_WEBHOOK:
        return False
    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": title,
            "text": f"## {title}\n\n{text}",
        },
    }
    return _post(_DINGTALK_WEBHOOK, payload)


def send_wecom(title: str, text: str) -> bool:
    """Send a WeCom (企业微信) robot message (Markdown format)."""
    if not _WECOM_WEBHOOK:
        return False
    # WeCom markdown has a 4096 char limit
    content = f"## {title}\n\n{text}"
    if len(content) > 4000:
        content = content[:3997] + "..."
    payload = {
        "msgtype": "markdown",
        "markdown": {"content": content},
    }
    return _post(_WECOM_WEBHOOK, payload)


def send_telegram(title: str, text: str) -> bool:
    """Send a Telegram bot message (Markdown format)."""
    if not _TELEGRAM_BOT_TOKEN or not _TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{_TELEGRAM_BOT_TOKEN}/sendMessage"
    # Telegram MarkdownV2 needs escaping, use HTML instead
    message = f"<b>{title}</b>\n\n{text}"
    if len(message) > 4000:
        message = message[:3997] + "..."
    payload = {
        "chat_id": _TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
    }
    return _post(url, payload)


def notify_all(title: str, text: str) -> dict[str, bool]:
    """Send to all configured channels. Returns {channel: success}."""
    results = {}
    if _DINGTALK_WEBHOOK:
        results["dingtalk"] = send_dingtalk(title, text)
    if _WECOM_WEBHOOK:
        results["wecom"] = send_wecom(title, text)
    if _TELEGRAM_BOT_TOKEN:
        results["telegram"] = send_telegram(title, text)
    if not results:
        logger.debug("No notification channels configured")
    return results


def format_report_notification(events: list[dict], report_preview: str) -> tuple[str, str]:
    """Format analysis report into notification title + body."""
    if not events:
        return "AlphaAgents: 无重要事件", "本轮未发现重要事件"

    top = events[0]
    title = f"AlphaAgents: [{top.get('category', '?')}] {top.get('event', '?')[:30]}"

    lines = [f"共 {len(events)} 条重要事件:\n"]
    for e in events[:5]:
        cat = e.get("category", "?")
        imp = e.get("importance", 0)
        evt = e.get("event", "?")
        lines.append(f"- [{cat}] {evt} (重要性 {imp}/5)")

    if len(events) > 5:
        lines.append(f"- ... 另有 {len(events) - 5} 条事件")

    lines.append(f"\n---\n{report_preview[:500]}")

    return title, "\n".join(lines)


def format_review_notification(date: str, accuracy: float,
                               predictions_count: int, review_summary: str) -> tuple[str, str]:
    """Format daily review into notification title + body."""
    pct = f"{accuracy * 100:.0f}%"
    title = f"AlphaAgents 回顾: {date} 预测准确率 {pct}"
    body = (
        f"日期: {date}\n"
        f"预测数: {predictions_count}\n"
        f"准确率: {pct}\n\n"
        f"{review_summary[:1000]}"
    )
    return title, body
