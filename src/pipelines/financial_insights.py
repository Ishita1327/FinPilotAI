from pathlib import Path
import sys

PIPELINES_DIR = Path(__file__).resolve().parents[1] / "pipelines"
sys.path.insert(0, str(PIPELINES_DIR))

from snowflake_connection import get_snowflake_connection


def generate_insights():
    conn = get_snowflake_connection()

    try:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                YEAR_MONTH,
                ROUND(SUM(
                    CASE WHEN SIGNED_AMOUNT < 0
                         THEN -SIGNED_AMOUNT ELSE 0 END
                ), 2) AS EXPENSES
            FROM FINPILOT_DB.RAW.PERSONAL_TRANSACTIONS
            WHERE COALESCE(IS_TRANSFER_CANDIDATE, FALSE) = FALSE
            GROUP BY YEAR_MONTH
            ORDER BY YEAR_MONTH
        """)

        monthly = cursor.fetchall()

        if not monthly:
            print("No financial data found.")
            return

        avg_expenses = sum(float(row[1]) for row in monthly) / len(monthly)

        print("\nFINPILOTAI FINANCIAL INSIGHTS")
        print("-" * 55)
        print(f"Average monthly expenses: ${avg_expenses:,.2f}")

        for month, expenses in monthly:
            if float(expenses) > avg_expenses * 2:
                cursor.execute("""
                    SELECT
                        DESCRIPTION,
                        CATEGORY,
                        ROUND(-SIGNED_AMOUNT, 2)
                    FROM FINPILOT_DB.RAW.PERSONAL_TRANSACTIONS
                    WHERE YEAR_MONTH = %s
                      AND SIGNED_AMOUNT < 0
                      AND COALESCE(IS_TRANSFER_CANDIDATE, FALSE) = FALSE
                    ORDER BY -SIGNED_AMOUNT DESC
                    LIMIT 1
                """, (month,))

                largest = cursor.fetchone()

                print(f"\nUnusually high spending in {month}")
                print(f"  Total expenses: ${float(expenses):,.2f}")

                if largest:
                    merchant, category, amount = largest
                    print(
                        f"  Largest expense: {merchant} "
                        f"({category}) - ${float(amount):,.2f}"
                    )

        cursor.close()

    finally:
        conn.close()


if __name__ == "__main__":
    generate_insights()