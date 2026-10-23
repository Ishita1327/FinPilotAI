from snowflake_connection import get_snowflake_connection

conn = get_snowflake_connection()

try:
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            DESCRIPTION AS MERCHANT,
            COUNT(*) AS PAYMENT_COUNT,
            COUNT(DISTINCT YEAR_MONTH) AS ACTIVE_MONTHS,
            ROUND(AVG(AMOUNT), 2) AS AVG_PAYMENT,
            ROUND(SUM(AMOUNT), 2) AS TOTAL_PAID
        FROM FINPILOT_DB.RAW.PERSONAL_TRANSACTIONS
        WHERE COALESCE(IS_SUBSCRIPTION_CANDIDATE, FALSE) = TRUE
          AND SIGNED_AMOUNT < 0
          AND COALESCE(IS_TRANSFER_CANDIDATE, FALSE) = FALSE
        GROUP BY DESCRIPTION
        ORDER BY TOTAL_PAID DESC
    """)

    print("\nPOTENTIAL RECURRING SUBSCRIPTIONS")
    print("-" * 85)
    print(
        f"{'Merchant':<25} "
        f"{'Payments':>10} "
        f"{'Months':>8} "
        f"{'Average':>12} "
        f"{'Total':>12}"
    )

    for merchant, payments, months, average, total in cursor.fetchall():
        print(
            f"{str(merchant):<25} "
            f"{payments:>10} "
            f"{months:>8} "
            f"${average:>11,.2f} "
            f"${total:>11,.2f}"
        )

    cursor.close()

finally:
    conn.close()
    print("\nConnection closed.")