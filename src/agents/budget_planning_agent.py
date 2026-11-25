"""FinPilotAI Budget Planning Agent.

A separate, read-only agent using the existing Snowflake connection.
No changes to expense_analysis_agent.py or the data pipelines are needed.

Install alongside the expense agent at src/agents/budget_planning_agent.py.
Budget limits live in config/budget_limits.json (optional until you set them).

Run from the repository root:
    python -u src/agents/budget_planning_agent.py
    python -u src/agents/budget_planning_agent.py --test-gemini
"""

import json
import os
import re
import sys
from decimal import Decimal, ROUND_HALF_UP
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
BUDGET_FILE = ROOT / "config" / "budget_limits.json"
TOOLS = ("budget_status", "budget_overview", "suggest_budget")
CENT = Decimal("0.01")
CATEGORY_ALIASES = {
    "grocery": "Groceries", "groceries": "Groceries",
    "restaurant": "Restaurants", "restaurants": "Restaurants",
    "utility": "Utilities", "utilities": "Utilities",
    "gas": "Gas & Fuel", "fuel": "Gas & Fuel", "gas & fuel": "Gas & Fuel",
    "rent": "Mortgage & Rent", "mortgage": "Mortgage & Rent",
}


def canonical_category(category):
    """Map a few common singular names to actual Snowflake categories."""
    return CATEGORY_ALIASES.get(category.strip().casefold(), category.strip())


def money(value):
    """Return a decimal amount rounded to cents, for safe financial math."""
    return Decimal(str(value or 0)).quantize(CENT, rounding=ROUND_HALF_UP)


def load_budget_limits():
    """Read user-entered MONTHLY category limits; never invent budgets."""
    if not BUDGET_FILE.exists():
        return {}
    with BUDGET_FILE.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict) or not isinstance(data.get("monthly_category_limits"), dict):
        raise ValueError("Budget file must contain a 'monthly_category_limits' object")
    limits = {}
    for category, amount in data["monthly_category_limits"].items():
        if not isinstance(category, str) or not category.strip() or len(category) > 100:
            raise ValueError("Budget categories must be nonempty strings")
        if isinstance(amount, bool) or not isinstance(amount, (int, float, str)):
            raise ValueError(f"Invalid budget amount for {category}")
        try:
            value = Decimal(str(amount))
        except Exception as exc:
            raise ValueError(f"Invalid budget amount for {category}") from exc
        if not value.is_finite() or value < 0:
            raise ValueError(f"Budget amount for {category} must be nonnegative and finite")
        normalized = category.strip().casefold()
        if normalized in limits:
            raise ValueError(f"Duplicate category in budget file: {category}")
        limits[normalized] = {"category": category.strip(), "limit": money(value)}
    return limits


def choose_tool(client, question):
    """Gemini chooses one supported budget tool and extracts parameters."""
    prompt = (
        "You are the router for a BUDGET PLANNING agent. "
        "Return ONLY JSON with keys tool, month, category. "
        "Allowed tool values: budget_status, budget_overview, suggest_budget, unsupported. "
        "budget_status: check whether spending in ONE category is above/below a set budget for a month. "
        "budget_overview: check all category budgets for a month. "
        "suggest_budget: derive a category budget baseline from historical spending; "
        "a target month may be given, meaning use earlier months only. "
        "unsupported: a question unrelated to budgets (e.g., general expenses or subscriptions). "
        "month must be YYYY-MM or null, category must be a category string or null. "
        "Never invent a month, category, or budget amount. "
        "Examples: 'Was I over my grocery budget in July 2019?' => "
        "{\"tool\":\"budget_status\",\"month\":\"2019-07\",\"category\":\"Groceries\"}; "
        "'Suggest a grocery budget for August 2019' => "
        "{\"tool\":\"suggest_budget\",\"month\":\"2019-08\",\"category\":\"Groceries\"}. "
        "Question: " + question
    )
    response = client.models.generate_content(
        model=MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0),
    )
    data = json.loads(response.text)
    if not isinstance(data, dict):
        raise ValueError("Gemini did not return a JSON object")
    tool = data.get("tool")
    if tool not in (*TOOLS, "unsupported"):
        raise ValueError("Gemini selected an unsupported tool")
    month = data.get("month")
    if month is not None:
        month = str(month)
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            raise ValueError("Invalid month from Gemini")
    category = data.get("category")
    if category is not None:
        if not isinstance(category, str) or not category.strip() or len(category) > 100:
            raise ValueError("Invalid category from Gemini")
        category = category.strip()
    return tool, month, category


def _month_has_data(cursor, month):
    cursor.execute(
        f"SELECT COUNT(*) FROM {TABLE} "
        "WHERE YEAR_MONTH = %s AND COALESCE(IS_TRANSFER_CANDIDATE, FALSE) = FALSE",
        (month,),
    )
    return cursor.fetchone()[0] > 0


def run_tool(tool, month, category):
    """Execute parameterized, read-only Snowflake SQL and deterministic budget math."""
    if tool not in TOOLS:
        raise ValueError("Unsupported budget tool")
    if tool in ("budget_status", "budget_overview") and not month:
        raise ValueError("Please include a month and year, e.g. July 2019")
    if tool in ("budget_status", "suggest_budget") and not category:
        raise ValueError("Please include a spending category, e.g. Groceries")
    if category:
        category = canonical_category(category)

    limits = load_budget_limits() if tool != "suggest_budget" else {}
    conn = get_snowflake_connection()
    try:
        cursor = conn.cursor()
        try:
            if tool == "budget_status":
                if not _month_has_data(cursor, month):
                    return [{"status": "no_data", "month": month}]
                cursor.execute(
                    f"""SELECT COUNT(*), COALESCE(ROUND(SUM(-SIGNED_AMOUNT), 2), 0)
                        FROM {TABLE}
                        WHERE YEAR_MONTH = %s AND LOWER(CATEGORY) = LOWER(%s)
                          AND SIGNED_AMOUNT < 0
                          AND COALESCE(IS_TRANSFER_CANDIDATE, FALSE) = FALSE""",
                    (month, category),
                )
                count, spent_raw = cursor.fetchone()
                spent = money(spent_raw)
                limit_entry = limits.get(category.casefold())
                result = {
                    "status": "ok" if limit_entry else "budget_not_set",
                    "month": month, "category": category,
                    "transactions": int(count), "spent": str(spent),
                    "budget": None, "remaining": None, "over_budget": None,
                }
                if limit_entry:
                    budget = limit_entry["limit"]
                    result.update(budget=str(budget), remaining=str(money(budget - spent)),
                                  over_budget=spent > budget)
                return [result]

            if tool == "budget_overview":
                if not _month_has_data(cursor, month):
                    return [{"status": "no_data", "month": month}]
                cursor.execute(
                    f"""SELECT CATEGORY, COUNT(*), ROUND(SUM(-SIGNED_AMOUNT), 2)
                        FROM {TABLE}
                        WHERE YEAR_MONTH = %s AND SIGNED_AMOUNT < 0
                          AND COALESCE(IS_TRANSFER_CANDIDATE, FALSE) = FALSE
                        GROUP BY CATEGORY ORDER BY 3 DESC""",
                    (month,),
                )
                results = []
                seen = set()
                for db_category, count, spent_raw in cursor.fetchall():
                    key = str(db_category or "Uncategorized").casefold()
                    seen.add(key)
                    entry = limits.get(key)
                    spent = money(spent_raw)
                    budget = entry["limit"] if entry else None
                    results.append({
                        "status": "ok" if entry else "budget_not_set",
                        "month": month, "category": str(db_category or "Uncategorized"),
                        "transactions": int(count), "spent": str(spent),
                        "budget": str(budget) if budget is not None else None,
                        "remaining": str(money(budget - spent)) if budget is not None else None,
                        "over_budget": spent > budget if budget is not None else None,
                    })
                for key, entry in limits.items():
                    if key not in seen:
                        results.append({
                            "status": "ok", "month": month, "category": entry["category"],
                            "transactions": 0, "spent": "0.00",
                            "budget": str(entry["limit"]),
                            "remaining": str(entry["limit"]), "over_budget": False,
                        })
                return results

            # suggest_budget: use the last three observed transaction months,
            # optionally strictly before the target month, including zero-spend months.
            cutoff = "AND YEAR_MONTH < %s" if month else ""
            params = ([month] if month else []) + [category]
            cursor.execute(
                f"""WITH recent_months AS (
                       SELECT DISTINCT YEAR_MONTH
                       FROM {TABLE}
                       WHERE COALESCE(IS_TRANSFER_CANDIDATE, FALSE) = FALSE
                       {cutoff}
                       ORDER BY YEAR_MONTH DESC LIMIT 3
                   )
                   SELECT m.YEAR_MONTH,
                       COALESCE(ROUND(SUM(IFF(t.SIGNED_AMOUNT < 0
                           AND LOWER(t.CATEGORY) = LOWER(%s),
                           -t.SIGNED_AMOUNT, 0)), 2), 0) AS SPENT
                   FROM recent_months m
                   LEFT JOIN {TABLE} t
                       ON t.YEAR_MONTH = m.YEAR_MONTH
                       AND COALESCE(t.IS_TRANSFER_CANDIDATE, FALSE) = FALSE
                   GROUP BY m.YEAR_MONTH
                   ORDER BY m.YEAR_MONTH DESC""",
                tuple(params),
            )
            history = [{"month": str(m), "spent": str(money(v))}
                       for m, v in cursor.fetchall()]
            if not history:
                return [{"status": "no_data", "category": category, "target_month": month}]
            average = money(sum(Decimal(x["spent"]) for x in history) / len(history))
            buffer = money(average * Decimal("1.10"))
            return [{
                "status": "ok", "category": category, "target_month": month,
                "months_used": len(history), "history": history,
                "historical_average": str(average),
                "optional_10pct_buffer": str(buffer),
                "method": "Average category spending across up to three most recent observed "
                          "transaction months; optional 10% cushion. Not a user-set budget.",
            }]
        finally:
            cursor.close()
    finally:
        conn.close()


def explain_results(client, question, tool, rows):
    """Explain only retrieved numbers; missing budgets are never assumed."""
    if not rows or rows[0].get("status") == "no_data":
        return "No matching transaction history is available for that request."
    if tool == "budget_status" and rows[0]["status"] == "budget_not_set":
        row = rows[0]
        return (
            f"In {row['month']}, {row['category']} spending was ${row['spent']} "
            f"across {row['transactions']} transactions. "
            "No monthly budget limit has been configured for this category, so I "
            "cannot say whether you are over budget. Set a limit in config/budget_limits.json."
        )
    if tool == "budget_overview" and all(r["budget"] is None for r in rows):
        return (
            "I retrieved the category spending, but no monthly budget limits are configured. "
            "Add your own category limits in config/budget_limits.json before comparing budgets. "
            "No budget amounts have been assumed."
        )
    prompt = (
        "You are FinPilotAI's Budget Planning Agent. Answer using ONLY the JSON data. "
        "Never invent budget limits, amounts, transactions, or months. "
        "A null budget means the user has NOT set a limit; do not call it over/under budget. "
        "For suggest_budget, explain the historical average and that the 10% cushion "
        "is OPTIONAL and is not a user-set budget or personalized financial advice. "
        "For budget_overview, mention which configured categories exceed their limits, "
        "and do not treat unconfigured categories as within budget. "
        "Keep the answer clear and concise.\n"
        "Question: " + question + "\nTool: " + tool + "\nData: " + json.dumps(rows)
    )
    response = client.models.generate_content(
        model=MODEL, contents=prompt,
        config=types.GenerateContentConfig(temperature=0),
    )
    return response.text or "Gemini did not return a text explanation."


def main():
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        print("GEMINI_API_KEY is missing from your project .env file.")
        return
    client = genai.Client(api_key=key)
    if len(sys.argv) > 1 and sys.argv[1] == "--test-gemini":
        question = "Suggest a grocery budget for August 2019"
        tool, month, category = choose_tool(client, question)
        print("Selected tool:", tool, "| month:", month, "| category:", category)
        print("Tool routing test:", "PASSED" if
              (tool == "suggest_budget" and month == "2019-08" and
               category and category.casefold() in ("grocery", "groceries")) else "CHECK RESULT")
        return
    print("FinPilotAI Budget Planning Agent (type quit to exit)")
    while True:
        try:
            question = input("\nYou: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nGoodbye!")
            break
        if question.casefold() in ("quit", "exit", "q"):
            print("Goodbye!")
            break
        if not question:
            continue
        try:
            tool, month, category = choose_tool(client, question)
            print("Selected tool:", tool, "| month:", month, "| category:", category)
            if tool == "unsupported":
                print("FinPilotAI: This question belongs to another agent. "
                      "Try the Expense Analysis Agent for general expense questions.")
                continue
            print("Fetching budget-related results from Snowflake...")
            rows = run_tool(tool, month, category)
            print("\nFinPilotAI:", explain_results(client, question, tool, rows))
        except Exception as exc:
            print("Unable to complete this request:", type(exc).__name__, str(exc))


if __name__ == "__main__":
    main()
