"""
database access for the lost pet tracker.

run this file once to create the database and its tables:

    python database.py
    python database.py --reset      deletes every report and starts again

rows are returned as dictionaries keyed by lower-case column name,
for example report["postid"] or report["lostorfoundat"].
"""

import os
import shutil
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from psycopg import sql
from psycopg.rows import dict_row


BACKEND_FOLDER = Path(__file__).resolve().parent
SCHEMA_FILE = BACKEND_FOLDER.parent.parent / "lost-pet-database.sql"
UPLOADS_FOLDER = BACKEND_FOLDER / "uploads"

load_dotenv(BACKEND_FOLDER / ".env")


# ----- connecting and running SQL -----

def connect(database_name=None, **options):
    """open a connection using the DB_* values in .env.

    inside a `with` block the connection commits at the end,
    or rolls back if an error is raised.
    """
    return psycopg.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=os.getenv("DB_PORT", "5432"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD") or None,
        dbname=database_name or os.getenv("DB_NAME", "lostpet"),
        row_factory=dict_row,
        **options,
    )


def query(conn, statement, values=(), one=False):
    """run a statement and return its rows, or only the first row when one=True."""
    cursor = conn.execute(statement, values)

    if cursor.description is None:
        return None

    if one:
        return cursor.fetchone()

    return cursor.fetchall()


def insert(conn, table, **columns):
    """insert one row and return it.

    example: insert(conn, "PetColor", PostID=1, Color="tan")
    """
    statement = sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING *").format(
        sql.Identifier(table.lower()),
        sql.SQL(", ").join(sql.Identifier(name.lower()) for name in columns),
        sql.SQL(", ").join(sql.Placeholder() * len(columns)),
    )
    return conn.execute(statement, list(columns.values())).fetchone()


# ----- lookups shared by the backend -----

def settings(conn):
    """the administrator settings in use: the newest row of the settings table."""
    return query(
        conn,
        "SELECT * FROM Settings ORDER BY SettingsID DESC LIMIT 1",
        one=True,
    )


def two_factor_email(conn, account_id):
    """the email row that receives an account's verification codes, or None."""
    return query(
        conn,
        """
        SELECT Email.*
        FROM UseFor2FA
        JOIN Email USING (AccountID, ContactID)
        WHERE UseFor2FA.AccountID = %s
        """,
        (account_id,),
        one=True,
    )


def contacts(conn, account_id):
    """an account's contact methods as rows of type, value and visibility."""
    return query(
        conn,
        """
        SELECT CASE WHEN Email.ContactID IS NULL THEN 'phone' ELSE 'email' END AS type,
               COALESCE(Email.EmailAddress, Phone.PhoneNumber) AS value,
               ContactMethod.Visibility AS visibility
        FROM ContactMethod
        LEFT JOIN Email USING (AccountID, ContactID)
        LEFT JOIN Phone USING (AccountID, ContactID)
        WHERE ContactMethod.AccountID = %s
        ORDER BY ContactMethod.ContactID
        """,
        (account_id,),
    )


def load_report(conn, post_id):
    """one report with its colors, markings and photo addresses, or None."""
    report = query(conn, "SELECT * FROM Post WHERE PostID = %s", (post_id,), one=True)
    if report is None:
        return None

    color_rows = query(
        conn,
        "SELECT Color FROM PetColor WHERE PostID = %s ORDER BY Color",
        (post_id,),
    )
    marking_rows = query(
        conn,
        "SELECT Marking FROM PetMarking WHERE PostID = %s ORDER BY Marking",
        (post_id,),
    )
    photo_rows = query(
        conn,
        "SELECT FilePath FROM Photo WHERE PostID = %s ORDER BY PhotoID",
        (post_id,),
    )

    report["colors"] = [row["color"] for row in color_rows]
    report["markings"] = [row["marking"] for row in marking_rows]
    report["photos"] = ["/uploads/" + row["filepath"] for row in photo_rows]
    return report


# ----- creating the database -----

def create_database(reset=False):
    """create the database if needed, then create its tables from the schema file."""
    database_name = os.getenv("DB_NAME", "lostpet")

    with connect("postgres", autocommit=True) as server:
        exists = query(
            server,
            "SELECT 1 FROM pg_database WHERE datname = %s",
            (database_name,),
            one=True,
        )
        if not exists:
            server.execute(
                sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name))
            )

    with connect() as conn:
        if has_reports(conn) and not reset:
            sys.exit(
                "The database already holds reports.\n"
                "To delete them and start again, run: python database.py --reset"
            )
        conn.execute(SCHEMA_FILE.read_text(encoding="utf-8"))

    shutil.rmtree(UPLOADS_FOLDER, ignore_errors=True)
    print(f"Database '{database_name}' is ready. Start the site with: python server.py")


def has_reports(conn):
    table = query(conn, "SELECT to_regclass('post') AS name", one=True)
    if table["name"] is None:
        return False

    count = query(conn, "SELECT count(*) AS total FROM Post", one=True)
    return count["total"] > 0


def main():
    reset = "--reset" in sys.argv
    create_database(reset)


if __name__ == "__main__":
    main()
