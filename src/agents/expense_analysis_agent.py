"""FinPilotAI: Gemini-powered expense agent using read-only Snowflake tools.

Place in src/agents/expense_analysis_agent.py and run from the project root:
    python src/agents/expense_analysis_agent.py
"""
import json
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / '.env', override=True)
sys.path.insert(0, str(ROOT / 'src' / 'pipelines'))
from snowflake_connection import get_snowflake_connection

TABLE = 'FINPILOT_DB.RAW.PERSONAL_TRANSACTIONS'
MODEL = os.getenv('GEMINI_MODEL', 'gemini-2.5-flash')
TOOLS = ('monthly_summary', 'category_spending', 'high_spending_months', 'largest_expenses')


def choose_tool(client, question):
    prompt = (
        'Choose ONE financial analytics tool for the user question. '
        'Return only a JSON object with keys tool, month, category. '
        'tool must be one of: monthly_summary, category_spending, '
        'high_spending_months, largest_expenses. '
        'month must be YYYY-MM or null; category must be a string or null. '
        'Use monthly_summary for income, expenses, cash flow, or month comparisons; '
        'category_spending for category totals; high_spending_months for unusually '
        'high months or why spending spiked; largest_expenses for individual big purchases. '
        'Never invent a month or category not stated by the user.\nQuestion: ' + question
    )
    response = client.models.generate_content(
        model=MODEL, contents=prompt,
        config=types.GenerateContentConfig(response_mime_type='application/json', temperature=0),
    )
    data = json.loads(response.text)
    tool = data.get('tool')
    if tool not in TOOLS:
        raise ValueError('Gemini selected an unsupported tool')
    month = data.get('month')
    if month is not None and not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', str(month)):
        raise ValueError('Invalid month from Gemini')
    category = data.get('category')
    if category is not None and (not isinstance(category, str) or len(category) > 100):
        raise ValueError('Invalid category from Gemini')
    return tool, month, category


def run_tool(tool, month, category):
    filters = ['COALESCE(IS_TRANSFER_CANDIDATE, FALSE) = FALSE']
    params = []
    if month:
        filters.append('YEAR_MONTH = %s')
        params.append(month)
    where = ' AND '.join(filters)

    if tool == 'monthly_summary':
        sql = f'''SELECT YEAR_MONTH,
            ROUND(SUM(IFF(SIGNED_AMOUNT > 0, SIGNED_AMOUNT, 0)), 2) AS INCOME,
            ROUND(SUM(IFF(SIGNED_AMOUNT < 0, -SIGNED_AMOUNT, 0)), 2) AS EXPENSES,
            ROUND(SUM(SIGNED_AMOUNT), 2) AS NET_CASH_FLOW
            FROM {TABLE} WHERE {where}
            GROUP BY YEAR_MONTH ORDER BY YEAR_MONTH DESC LIMIT 36'''
        columns = ['month', 'income', 'expenses', 'net_cash_flow']
    elif tool == 'category_spending':
        filters.append('SIGNED_AMOUNT < 0')
        if category:
            filters.append('LOWER(CATEGORY) = LOWER(%s)')
            params.append(category)
        sql = f'''SELECT CATEGORY, COUNT(*), ROUND(SUM(-SIGNED_AMOUNT), 2)
            FROM {TABLE} WHERE {' AND '.join(filters)}
            GROUP BY CATEGORY ORDER BY 3 DESC LIMIT 30'''
        columns = ['category', 'transactions', 'total_spent']
    elif tool == 'high_spending_months':
        # Same rule as the existing financial_insights.py: > 2x average monthly expenses.
        if month:
            params.append(month)
        sql = f'''WITH monthly AS (
            SELECT YEAR_MONTH, ROUND(SUM(IFF(SIGNED_AMOUNT < 0, -SIGNED_AMOUNT, 0)), 2) AS EXPENSES
            FROM {TABLE} WHERE COALESCE(IS_TRANSFER_CANDIDATE, FALSE) = FALSE
            GROUP BY YEAR_MONTH),
            baseline AS (SELECT AVG(EXPENSES) AS AVG_EXPENSES FROM monthly)
            SELECT m.YEAR_MONTH, m.EXPENSES, ROUND(b.AVG_EXPENSES, 2)
            FROM monthly m CROSS JOIN baseline b
            WHERE m.EXPENSES > 2 * b.AVG_EXPENSES
            {'AND m.YEAR_MONTH = %s' if month else ''}
            ORDER BY m.EXPENSES DESC LIMIT 24'''
        columns = ['month', 'expenses', 'average_monthly_expenses']
    else:  # largest_expenses
        filters.append('SIGNED_AMOUNT < 0')
        sql = f'''SELECT TRANSACTION_DATE, DESCRIPTION, CATEGORY,
            ROUND(-SIGNED_AMOUNT, 2) AS EXPENSE
            FROM {TABLE} WHERE {' AND '.join(filters)}
            ORDER BY EXPENSE DESC LIMIT 15'''
        columns = ['date', 'merchant', 'category', 'amount']

    conn = get_snowflake_connection()
    try:
        cursor = conn.cursor()
        try:
            cursor.execute(sql, tuple(params))
            rows = [dict(zip(columns, [str(v) if v is not None else None for v in row]))
                    for row in cursor.fetchall()]
            if tool == 'high_spending_months':
                for item in rows:
                    cursor.execute(f'''SELECT DESCRIPTION, CATEGORY, ROUND(-SIGNED_AMOUNT, 2)
                        FROM {TABLE}
                        WHERE YEAR_MONTH = %s AND SIGNED_AMOUNT < 0
                          AND COALESCE(IS_TRANSFER_CANDIDATE, FALSE) = FALSE
                        ORDER BY -SIGNED_AMOUNT DESC LIMIT 1''', (item['month'],))
                    biggest = cursor.fetchone()
                    if biggest:
                        item['largest_expense'] = {
                            'merchant': str(biggest[0]), 'category': str(biggest[1]),
                            'amount': str(biggest[2])}
            return rows
        finally:
            cursor.close()
    finally:
        conn.close()

def explain_results(client, question, tool, rows):
    """Explain query output without inventing financial values."""
    if not rows:
        return ("No matching financial records were returned. "
                "I can't determine the answer from the available data.")

    prompt = (
        "You are FinPilotAI, a financial analysis assistant. "
        "Answer the user's question using ONLY the query results supplied below. "
        "Mention relevant amounts and dates; never invent transactions, causes, "
        "or explanations that are not supported by the data. "
        "Be clear, concise, and say when the data cannot establish a cause.\n"
        "User question: " + question + "\n"
        "Selected analytics tool: " + tool + "\n"
        "Query results (JSON): " + json.dumps(rows, default=str)
    )
    response = client.models.generate_content(
        model=MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(temperature=0),
    )
    return response.text or "Gemini did not return a text explanation."


def main():
    api_key = os.getenv('GEMINI_API_KEY')
    if not api_key:
        print('GEMINI_API_KEY is missing from the project .env file.')
        return

    client = genai.Client(api_key=api_key)

    # Gemini-only test: does not connect to Snowflake.
    if len(sys.argv) > 1 and sys.argv[1] == '--test-gemini':
        question = 'Why was my spending unusually high in June 2019?'
        print('Testing Gemini tool selection for:', question)
        try:
            tool, month, category = choose_tool(client, question)
            print('Selected tool:', tool)
            print('Month:', month)
            print('Category:', category)
            print('Gemini tool selection test passed.')
        except Exception as exc:
            print('Gemini tool selection test failed:', type(exc).__name__, str(exc))
        return

    print('FinPilotAI Expense Analysis Agent (type quit to exit)')
    while True:
        try:
            question = input('\nYou: ').strip()
        except (EOFError, KeyboardInterrupt):
            print('\nGoodbye!')
            break

        if question.lower() in ('quit', 'exit', 'q'):
            print('Goodbye!')
            break
        if not question:
            continue

        try:
            tool, month, category = choose_tool(client, question)
            print('Selected tool:', tool, '| month:', month, '| category:', category)
            print('Fetching financial results from Snowflake...')
            rows = run_tool(tool, month, category)
            print('\nFinPilotAI:', explain_results(client, question, tool, rows))
        except Exception as exc:
            print('Unable to complete this request:', type(exc).__name__, str(exc))
            print('Gemini may be working even if the Snowflake connection is unavailable.')


if __name__ == '__main__':
    main()

