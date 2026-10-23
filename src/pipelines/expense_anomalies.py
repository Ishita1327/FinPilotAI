from snowflake_connection import get_snowflake_connection

conn = get_snowflake_connection()

try:
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            TRANSACTION_DATE,
            DESCRIPTION,
            CATEGORY,
            AMOUNT,
            YEAR_MONTH
        FROM FINPILOT_DB.RAW.PERSONAL_TRANSACTIONS
        WHERE SIGNED_AMOUNT < 0
          AND COALESCE(IS_TRANSFER_CANDIDATE, FALSE) = FALSE
          AND YEAR_MONTH IN ('2018-05', '2019-06')
        ORDER BY AMOUNT DESC
        LIMIT 20
    """)

    print("\nLARGEST EXPENSES IN HIGH-SPENDING MONTHS")
    print("-" * 85)

    for date, merchant, category, amount, month in cursor.fetchall():
        print(
            f"{str(date):<12} "
            f"{str(merchant):<25} "
            f"{str(category):<22} "
            f"${amount:>10,.2f}"
        )

    cursor.close()

finally:
    conn.close()
    print("\nConnection closed.")