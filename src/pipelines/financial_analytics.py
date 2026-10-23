from snowflake_connection import get_snowflake_connection

conn = get_snowflake_connection()

try:
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            YEAR_MONTH,
            ROUND(SUM(
                CASE
                    WHEN SIGNED_AMOUNT > 0 THEN SIGNED_AMOUNT
                    ELSE 0
                END
            ), 2) AS INCOME,
            ROUND(SUM(
                CASE
                    WHEN SIGNED_AMOUNT < 0 THEN -SIGNED_AMOUNT
                    ELSE 0
                END
            ), 2) AS EXPENSES,
            ROUND(SUM(SIGNED_AMOUNT), 2) AS NET_CASH_FLOW
        FROM FINPILOT_DB.RAW.PERSONAL_TRANSACTIONS
        WHERE COALESCE(IS_TRANSFER_CANDIDATE, FALSE) = FALSE
        GROUP BY YEAR_MONTH
        ORDER BY YEAR_MONTH
    """)

    print("\nMONTHLY FINANCIAL SUMMARY")
    print("-" * 65)
    print(f"{'Month':<12} {'Income':>15} {'Expenses':>15} {'Net':>15}")

    for month, income, expenses, net in cursor.fetchall():
        print(
            f"{str(month):<12} "
            f"${income:>14,.2f} "
            f"${expenses:>14,.2f} "
            f"${net:>14,.2f}"
        )

    cursor.close()

finally:
    conn.close()
    print("\nConnection closed.")