from snowflake_connection import get_snowflake_connection

conn = get_snowflake_connection()

try:
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            YEAR_MONTH,
            CATEGORY,
            COUNT(*) AS TRANSACTIONS,
            ROUND(SUM(-SIGNED_AMOUNT), 2) AS TOTAL_EXPENSES
        FROM FINPILOT_DB.RAW.PERSONAL_TRANSACTIONS
        WHERE SIGNED_AMOUNT < 0
          AND COALESCE(IS_TRANSFER_CANDIDATE, FALSE) = FALSE
          AND YEAR_MONTH IN ('2018-05', '2019-06')
        GROUP BY YEAR_MONTH, CATEGORY
        ORDER BY YEAR_MONTH, TOTAL_EXPENSES DESC
    """)

    print("\nEXPENSE BREAKDOWN BY MONTH AND CATEGORY")
    print("-" * 75)

    for month, category, count, total in cursor.fetchall():
        print(
            f"{month:<10} "
            f"{str(category):<25} "
            f"{count:>4} transactions "
            f"${total:>12,.2f}"
        )

    cursor.close()

finally:
    conn.close()
    print("\nConnection closed.")