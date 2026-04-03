"""Daily prediction review — compare yesterday's predictions with actual market results.

Workflow:
1. Load yesterday's predictions from DB
2. Fetch actual sector performance data (akshare / eastmoney)
3. Compare: did bullish sectors go up? did bearish sectors go down?
4. Use cheap LLM to analyze discrepancies
5. Store review results + push notification
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta

from openai import AsyncOpenAI

from alpha_agents.config import DIGEST_API_KEY, DIGEST_BASE_URL, DIGEST_MODEL
from alpha_agents.data.report_store import (
    get_predictions_by_date,
    save_review,
)
from alpha_agents.notify import notify_all, format_review_notification

logger = logging.getLogger(__name__)

REVIEW_SYSTEM_PROMPT = """\
你是一个金融预测回顾分析师。你的任务是对比昨日的板块预测与实际表现，分析预测的准确性和偏差原因。

输入：
- 昨日的预测列表（看多/看空的板块、力度、理由）
- 当日的实际板块表现数据（涨跌幅）

输出JSON格式：
{
  "accuracy_summary": "总体准确率描述",
  "correct_predictions": [
    {"sector": "xxx", "predicted": "bullish", "actual_change": "+2.3%", "note": "预测正确"}
  ],
  "wrong_predictions": [
    {"sector": "xxx", "predicted": "bearish", "actual_change": "+1.5%", "reason": "为什么预测失败的分析"}
  ],
  "insights": [
    "从本次回顾中学到的规律或教训"
  ],
  "model_bias": "模型的系统性偏差分析（如：总是高估政策影响、总是低估情绪面等）"
}
"""


def _fetch_sector_performance() -> dict[str, float]:
    """Fetch actual sector performance data for today.

    Returns dict of {sector_name: change_pct}.
    """
    results = {}

    # Try akshare first (most comprehensive for A-share sectors)
    try:
        from alpha_agents.config import no_proxy
        import akshare as ak
        with no_proxy():
            df = ak.stock_board_concept_name_em()
            for _, row in df.iterrows():
                name = row.get("板块名称", "")
                change = row.get("涨跌幅", 0)
                if name:
                    results[name] = float(change)
    except Exception as e:
        logger.warning("akshare sector data failed: %s", e)

    # Fallback: try eastmoney API directly
    if not results:
        try:
            from alpha_agents.http_client import fetch
            resp = fetch(
                "https://push2.eastmoney.com/api/qt/clist/get",
                params={
                    "pn": "1", "pz": "100", "fs": "m:90",
                    "fields": "f12,f14,f3",
                },
                timeout=10,
            )
            data = resp.json()
            for item in data.get("data", {}).get("diff", []):
                name = item.get("f14", "")
                change = item.get("f3", 0)
                if name:
                    results[name] = float(change)
        except Exception as e:
            logger.warning("eastmoney sector API failed: %s", e)

    return results


def _match_prediction(pred_sector: str, actual_data: dict[str, float]) -> tuple[bool, float | None]:
    """Match a predicted sector name against actual data.

    Returns (found, change_pct). Uses fuzzy matching.
    """
    # Exact match
    if pred_sector in actual_data:
        return True, actual_data[pred_sector]

    # Substring match
    for name, change in actual_data.items():
        if pred_sector in name or name in pred_sector:
            return True, change

    return False, None


def _evaluate_predictions(predictions: list[dict], actual: dict[str, float]) -> tuple[int, int, list[dict]]:
    """Evaluate predictions against actual data.

    Returns (total_matched, correct_count, details_list).
    """
    total_matched = 0
    correct = 0
    details = []

    for pred in predictions:
        sector = pred["sector"]
        direction = pred["direction"]
        strength = pred.get("strength", 0)
        reason = pred.get("reason", "")

        found, change = _match_prediction(sector, actual)
        if not found:
            details.append({
                "sector": sector,
                "direction": direction,
                "strength": strength,
                "reason": reason,
                "actual_change": None,
                "matched": False,
                "correct": None,
            })
            continue

        total_matched += 1

        # Determine if prediction was correct
        if direction == "bullish":
            is_correct = change > 0
        else:
            is_correct = change < 0

        if is_correct:
            correct += 1

        details.append({
            "sector": sector,
            "direction": direction,
            "strength": strength,
            "reason": reason,
            "actual_change": f"{change:+.2f}%",
            "matched": True,
            "correct": is_correct,
        })

    return total_matched, correct, details


async def run_daily_review(target_date: str | None = None) -> dict:
    """Run daily prediction review.

    Args:
        target_date: YYYY-MM-DD to review. Defaults to yesterday.

    Returns:
        Review result dict.
    """
    if target_date is None:
        target_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    logger.info("Running daily review for %s", target_date)

    # 1. Load predictions
    predictions = get_predictions_by_date(target_date)
    if not predictions:
        logger.info("No predictions found for %s", target_date)
        return {"date": target_date, "status": "no_predictions"}

    logger.info("Found %d predictions for %s", len(predictions), target_date)

    # 2. Fetch actual market data
    actual_data = _fetch_sector_performance()
    if not actual_data:
        logger.warning("Could not fetch sector performance data")
        return {"date": target_date, "status": "no_market_data"}

    logger.info("Fetched performance for %d sectors", len(actual_data))

    # 3. Evaluate predictions
    total_matched, correct_count, details = _evaluate_predictions(predictions, actual_data)
    accuracy = correct_count / total_matched if total_matched > 0 else 0.0

    logger.info(
        "Review: %d/%d matched, %d correct (%.0f%% accuracy)",
        total_matched, len(predictions), correct_count, accuracy * 100,
    )

    # 4. Use LLM to analyze discrepancies
    review_text = ""
    if DIGEST_API_KEY:
        try:
            client = AsyncOpenAI(api_key=DIGEST_API_KEY, base_url=DIGEST_BASE_URL)
            user_msg = (
                f"日期: {target_date}\n\n"
                f"预测详情:\n{json.dumps(details, ensure_ascii=False, indent=2)}\n\n"
                f"整体准确率: {accuracy * 100:.0f}% ({correct_count}/{total_matched} 匹配的预测正确)"
            )
            response = await client.chat.completions.create(
                model=DIGEST_MODEL,
                max_tokens=2048,
                timeout=90,
                messages=[
                    {"role": "system", "content": REVIEW_SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
            )
            review_text = (response.choices[0].message.content if response.choices else "") or ""
        except Exception as e:
            logger.warning("LLM review analysis failed: %s", e)
            review_text = f"LLM分析失败: {e}"

    # 5. Save review
    review_id = save_review(
        date=target_date,
        predictions_count=len(predictions),
        correct_count=correct_count,
        accuracy=accuracy,
        review_text=review_text,
        market_data={"sector_count": len(actual_data), "matched": total_matched},
    )

    # 6. Push notification
    title, body = format_review_notification(
        target_date, accuracy, len(predictions),
        review_text[:500] if review_text else f"准确率: {accuracy * 100:.0f}%",
    )
    await asyncio.to_thread(notify_all, title, body)

    return {
        "date": target_date,
        "status": "completed",
        "review_id": review_id,
        "predictions_count": len(predictions),
        "matched": total_matched,
        "correct": correct_count,
        "accuracy": accuracy,
        "details": details,
        "review_text": review_text,
    }
