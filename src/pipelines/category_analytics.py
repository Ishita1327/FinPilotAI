from snowflake_connection import get_snowflake_connection

conn = get_snowflake_connection()

try:
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            CATEGORY,
            COUNT(*) AS TRANSACTION_COUNT,
            ROUND(SUM(-SIGNED_AMOUNT), 2) AS TOTAL_SPENT
        FROM FINPILOT_DB.RAW.PERSONAL_TRANSACTIONS
        WHERE SIGNED_AMOUNT < 0
          AND COALESCE(IS_TRANSFER_CANDIDATE, FALSE) = FALSE
        GROUP BY CATEGORY
        ORDER BY TOTAL_SPENT DESC
    """)

    print("\nSPENDING BY CATEGORY")
    print("-" * 65)
    print(f"{'Category':<25} {'Transactions':>12} {'Spent':>15}")

    for category, count, total in cursor.fetchall():
        print(
            f"{str(category):<25} "
            f"{count:>12} "
            f"${total:>14,.2f}"
        )

    cursor.close()

finally:
    conn.close()
    print("\nConnection closed.")