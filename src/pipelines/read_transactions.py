# from snowflake_connection import get_snowflake_connection

# conn = get_snowflake_connection()

# try:
#     cursor = conn.cursor()

#     cursor.execute("""
#         SELECT
#             TRANSACTION_DATE,
#             DESCRIPTION,
#             AMOUNT,
#             CATEGORY,
#             TRANSACTION_TYPE
#         FROM FINPILOT_DB.RAW.PERSONAL_TRANSACTIONS
#         ORDER BY TRANSACTION_DATE DESC
#         LIMIT 10
#     """)

#     print("\nLatest 10 transactions:\n")

#     for row in cursor.fetchall():
#         print(row)

#     cursor.close()

# finally:
#     conn.close()

from snowflake_connection import get_snowflake_connection

print("Connecting to Snowflake...")

conn = get_snowflake_connection()

try:
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            TRANSACTION_DATE,
            DESCRIPTION,
            AMOUNT,
            CATEGORY,
            TRANSACTION_TYPE
        FROM FINPILOT_DB.RAW.PERSONAL_TRANSACTIONS
        ORDER BY TRANSACTION_DATE DESC
        LIMIT 10
    """)

    print("\nLatest 10 transactions:\n")

    for row in cursor.fetchall():
        print(row)

    cursor.close()

finally:
    conn.close()
    print("\nConnection closed.")