"""FinPilotAI Subscription Analysis Agent.

Uses the existing Gemini API and read-only Snowflake connection. It does NOT
modify the existing pipelines or other agents.

Put this file at src/agents/subscription_analysis_agent.py, then run from root:
    python -u src/agents/subscription_analysis_agent.py
    python -u src/agents/subscription_analysis_agent.py --test-gemini

IMPORTANT: IS_SUBSCRIPTION_CANDIDATE is a data flag, not proof of an active,
contractual, or cancellable subscription. Results are potential recurring
charges and should be reviewed by a person.
"""

import json
import os
import re
import sys
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env", override=True)
sys.path.insert(0, str(ROOT / "src" / "pipelines"))
from snowflake_connection import get_snowflake_connection

TABLE = "FINPILOT_DB.RAW.PERSONAL_TRANSACTIONS"
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
TOOLS = ("subscription_candidates", "subscription_spending", "merchant_history")


def response_text(response):
    """Read only text parts, avoiding non-text thought_signature warnings."""
    chunks = []
    for candidate in (getattr(response, "candidates", None) or []):
        content = getattr(candidate, "content", None)
        for part in (getattr(content, "parts", None) or []):
            text = getattr(part, "text", None)
            if isinstance(text, str) and text:
                chunks.append(text)
    return "\n".join(chunks).strip()


def choose_tool(client, question):
    """Route a subscription question and extract an optional month/merchant."""
    prompt = (
        "You are the router for a personal-finance SUBSCRIPTION ANALYSIS agent. "
        "Return ONLY a JSON object with keys tool, month, merchant. "
        "Allowed tools: subscription_candidates, subscription_spending, "
        "merchant_history, unsupported. "
        "subscription_candidates: list potential recurring subscription merchants "
        "and their payment patterns; also for 'what subscriptions do I have?'. "
        "subscription_spending: subscription-related total spending for a month, "
        "or monthly subscription-spending trend if no month is specified. "
        "merchant_history: list subscription-candidate payments for ONE named merchant. "
        "unsupported: unrelated questions, including general expenses and budgets. "
        "month must be YYYY-MM or null; merchant must be a merchant name or null. "
        "Never invent a month or merchant. Do not claim subscriptions are active. "
        "Examples: 'Which potential subscriptions do I have?' => "
        '{"tool":"subscription_candidates","month":null,"merchant":null}; '
        "'How much did I spend on subscriptions in July 2019?' => "
        '{"tool":"subscription_spending","month":"2019-07","merchant":null}; '
        "'Show my Netflix subscription payments' => "
        '{"tool":"merchant_history","month":null,"merchant":"Netflix"}. '
        "Question: " + question
    )
    response = client.models.generate_content(
        model=MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json", temperature=0
        ),
    )
    raw = response_text(response)
    if not raw:
        raise ValueError("Gemini returned no routing JSON")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("Gemini routing response was not a JSON object")
    tool = data.get("tool")
    if tool not in (*TOOLS, "unsupported"):
        raise ValueError("Gemini selected an unsupported tool")
    month = data.get("month")
    if month is not None:
        month = str(month)
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            raise ValueError("Invalid month from Gemini")
    merchant = data.get("merchant")
    if merchant is not None:
        if (not isinstance(merchant, str) or not merchant.strip()
                or len(merchant.strip()) > 100):
            raise ValueError("Invalid merchant from Gemini")
        merchant = merchant.strip()
    if tool == "merchant_history" and not merchant:
        raise ValueError("Please specify a merchant, such as 'Internet Service Provider'")
    return tool, month, merchant


def as_json_value(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def run_tool(tool, month=None, merchant=None):
    """Run approved read-only SQL; user text is never interpolated into SQL."""
    if tool not in TOOLS:
        raise ValueError("Unsupported subscription analysis tool")
    if month is not None and not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", str(month)):
        raise ValueError("Month must be YYYY-MM")
    if tool == "merchant_history" and (not merchant or not merchant.strip()):
        raise ValueError("A merchant name is required for merchant_history")

    base_filter = (
        "COALESCE(IS_SUBSCRIPTION_CANDIDATE, FALSE) = TRUE "
        "AND SIGNED_AMOUNT < 0 "
        "AND COALESCE(IS_TRANSFER_CANDIDATE, FALSE) = FALSE"
    )

    if tool == "subscription_candidates":
        # Calculate recurrence evidence across the full dataset, even if the
        # user asks which candidates had payments in a specific month.
        month_filter = (
            "WHERE EXISTS (SELECT 1 FROM flagged f "
            "WHERE f.MERCHANT = s.MERCHANT AND f.CATEGORY = s.CATEGORY "
            "AND f.YEAR_MONTH = %s)" if month else ""
        )
        sql = f"""
            WITH flagged AS (
                SELECT DESCRIPTION AS MERCHANT, CATEGORY, YEAR_MONTH,
                       TRANSACTION_DATE, -SIGNED_AMOUNT AS CHARGE
                FROM {TABLE}
                WHERE {base_filter}
            ), stats AS (
                SELECT MERCHANT, CATEGORY, COUNT(*) AS PAYMENT_COUNT,
                       COUNT(DISTINCT YEAR_MONTH) AS OBSERVED_MONTHS,
                       ROUND(AVG(CHARGE), 2) AS AVERAGE_CHARGE,
                       ROUND(SUM(CHARGE), 2) AS TOTAL_PAID,
                       MIN(TRANSACTION_DATE) AS FIRST_PAYMENT,
                       MAX(TRANSACTION_DATE) AS LAST_PAYMENT
                FROM flagged
                GROUP BY MERCHANT, CATEGORY
            )
            SELECT s.MERCHANT, s.CATEGORY, s.PAYMENT_COUNT,
                   s.OBSERVED_MONTHS, s.AVERAGE_CHARGE, s.TOTAL_PAID,
                   s.FIRST_PAYMENT, s.LAST_PAYMENT
            FROM stats s
            {month_filter}
            ORDER BY s.OBSERVED_MONTHS DESC, s.TOTAL_PAID DESC
            LIMIT 30
        """
        params = (month,) if month else ()
        columns = ("merchant", "category", "payment_count", "observed_months",
                   "average_charge", "total_paid", "first_payment", "last_payment")

    elif tool == "subscription_spending":
        month_filter = "AND YEAR_MONTH = %s" if month else ""
        sql = f"""
            SELECT YEAR_MONTH, COUNT(*) AS PAYMENT_COUNT,
                   COUNT(DISTINCT DESCRIPTION) AS MERCHANT_COUNT,
                   ROUND(SUM(-SIGNED_AMOUNT), 2) AS TOTAL_SPENT
            FROM {TABLE}
            WHERE {base_filter} {month_filter}
            GROUP BY YEAR_MONTH
            ORDER BY YEAR_MONTH DESC
            LIMIT 24
        """
        params = (month,) if month else ()
        columns = ("month", "payment_count", "merchant_count", "total_spent")

    else:  # merchant_history
        month_filter = "AND YEAR_MONTH = %s" if month else ""
        sql = f"""
            SELECT TRANSACTION_DATE, YEAR_MONTH, DESCRIPTION, CATEGORY,
                   ROUND(-SIGNED_AMOUNT, 2) AS AMOUNT
            FROM {TABLE}
            WHERE {base_filter}
              AND CONTAINS(LOWER(DESCRIPTION), LOWER(%s))
              {month_filter}
            ORDER BY TRANSACTION_DATE DESC, AMOUNT DESC
            LIMIT 30
        """
        params = (merchant, month) if month else (merchant,)
        columns = ("date", "month", "merchant", "category", "amount")

    conn = get_snowflake_connection()
    try:
        cursor = conn.cursor()
        try:
            cursor.execute(sql, params)
            results = [
                dict(zip(columns, (as_json_value(v) for v in record)))
                for record in cursor.fetchall()
            ]
        finally:
            cursor.close()
    finally:
        conn.close()

    if tool == "subscription_candidates":
        for item in results:
            item["recurrence_evidence"] = (
                "payments_observed_in_multiple_months"
                if int(item["observed_months"]) >= 2
                else "flagged_candidate_only_one_observed_month"
            )
    return results


def explain_results(client, question, tool, rows):
    """Explain results without claiming a subscription is definitely active."""
    if not rows:
        return (
            "No matching subscription-candidate transactions were found. "
            "This does not prove there are no subscriptions: the dataset or "
            "subscription-candidate flags may be incomplete."
        )
    prompt = (
        "You are FinPilotAI's Subscription Analysis Agent. "
        "Answer in short, clear PLAIN ENGLISH, NOT JSON or code fences. "
        "Use ONLY the supplied Snowflake results. "
        "IS_SUBSCRIPTION_CANDIDATE is a heuristic flag, NOT proof of a current "
        "active, cancellable, or contractual subscription. Call results 'potential "
        "subscriptions' or 'subscription-candidate payments'. "
        "For subscription_candidates, explain whether a merchant was observed "
        "in multiple months or only flagged once; mention merchant, count, and amounts. "
        "For subscription_spending, report totals for observed months only. "
        "For merchant_history, list only matching observed payments. "
        "Do not invent cancellation advice, billing schedules, missing months, "
        "subscriptions, or amounts. A flagged merchant may be a regular bill "
        "rather than a subscription. Be concise.\n"
        "Question: " + question + "\nTool: " + tool +
        "\nSnowflake data: " + json.dumps(rows, default=str)
    )
    response = client.models.generate_content(
        model=MODEL, contents=prompt,
        config=types.GenerateContentConfig(temperature=0),
    )
    answer = response_text(response)
    # Sometimes the model produces {"answer": "..."}; unwrap that safely.
    stripped = answer.strip()
    if stripped.startswith("```json") and stripped.endswith("```"):
        stripped = stripped[7:-3].strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict) and isinstance(parsed.get("answer"), str):
                return parsed["answer"]
        except ValueError:
            pass
    return answer or "Gemini did not return a text explanation."


def main():
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("GEMINI_API_KEY is missing from your project .env file.")
        return
    client = genai.Client(api_key=api_key)
    if len(sys.argv) > 1 and sys.argv[1] == "--test-gemini":
        question = "Which potential recurring subscriptions do I have?"
        tool, month, merchant = choose_tool(client, question)
        print("Selected tool:", tool, "| month:", month, "| merchant:", merchant)
        print("Tool routing test:",
              "PASSED" if tool == "subscription_candidates" else "CHECK RESULT")
        return

    print("FinPilotAI Subscription Analysis Agent (type quit to exit)")
    while True:
        try:
            question = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break
        if question.casefold() in ("quit", "exit", "q"):
            print("Goodbye!")
            break
        if not question:
            continue
        try:
            tool, month, merchant = choose_tool(client, question)
            print("Selected tool:", tool, "| month:", month,
                  "| merchant:", merchant)
            if tool == "unsupported":
                print("FinPilotAI: This question belongs to a different agent. "
                      "Try the Expense or Budget Planning Agent.")
                continue
            print("Fetching subscription-candidate data from Snowflake...")
            rows = run_tool(tool, month, merchant)
            print("\nFinPilotAI:", explain_results(client, question, tool, rows))
        except Exception as exc:
            print("Unable to complete this request:", type(exc).__name__, str(exc))
            print("If Snowflake reports an invalid identifier, verify that "
                  "IS_SUBSCRIPTION_CANDIDATE exists in the transactions table.")


if __name__ == "__main__":
    main()
