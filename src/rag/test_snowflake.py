import os
from pathlib import Path

import snowflake.connector
from dotenv import load_dotenv

# Load environment variables
project_root = Path(__file__).resolve().parents[2]
load_dotenv(project_root / ".env", override=True)

print("Starting Snowflake OAuth connection...", flush=True)

# Connect using OAuth
conn = snowflake.connector.connect(
    account=os.getenv("SNOWFLAKE_ACCOUNT"),
    authenticator="OAUTH_AUTHORIZATION_CODE",
    warehouse=os.getenv("SNOWFLAKE_WAREHOUSE"),
    database=os.getenv("SNOWFLAKE_DATABASE"),
    schema=os.getenv("SNOWFLAKE_SCHEMA"),
    login_timeout=30,
)

print("Snowflake connection established!", flush=True)

try:
    cursor = conn.cursor()

    cursor.execute("SELECT CURRENT_USER(), CURRENT_ROLE()")
    print("Connected as:", cursor.fetchone())

    cursor.execute("""
        SELECT COUNT(*)
        FROM FINPILOT_DB.RAW.PERSONAL_TRANSACTIONS
    """)

    print("Total transactions:", cursor.fetchone()[0])
    cursor.close()

finally:
    conn.close()
    print("Connection closed.")
