import base64
import configparser
import getpass
import hashlib
import secrets
import shutil
import sys
from pathlib import Path

import psycopg
from cryptography.fernet import Fernet, InvalidToken
from psycopg import sql
from psycopg.rows import dict_row


BACKEND_FOLDER = Path(__file__).resolve().parent
CONFIG_FILE = BACKEND_FOLDER / "config.ini"
SCHEMA_FILE = BACKEND_FOLDER.parent.parent / "lost-pet-database.sql"
UPLOADS_FOLDER = BACKEND_FOLDER / "uploads"

MAX_TRIES = 3
MIN_PASSPHRASE_LENGTH = 8

REQUIRED_SETTINGS = {
    "database": ("host", "port", "user", "name"),
    "site": ("port",),
}


# ----- settings from config.ini -----

def read_config():
    config = configparser.ConfigParser()
    if not config.read(CONFIG_FILE, encoding="utf-8"):
        sys.exit(f"The settings file is missing: {CONFIG_FILE}")

    for section, keys in REQUIRED_SETTINGS.items():
        for key in keys:
            if not config.has_option(section, key):
                sys.exit(f"config.ini is missing '{key}' in the [{section}] section.")

    return config


CONFIG = read_config()
DATABASE = CONFIG["database"]

# typed in at start-up by ask_for_password and kept only in memory
PASSWORD = None


# ----- connecting and running SQL -----

def connect(database_name=None, **options):
    """open a connection using the [database] settings in config.ini and the typed password.

    inside a `with` block the connection commits at the end,
    or rolls back if an error is raised.
    """
    return psycopg.connect(
        host=DATABASE["host"],
        port=DATABASE["port"],
        user=DATABASE["user"],
        password=PASSWORD,
        dbname=database_name or DATABASE["name"],
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


# ----- typing in the password and passphrase -----

def ask_for_password():
    """ask for the database password without showing it, and check that it works."""
    global PASSWORD

    print(
        f"Database '{DATABASE['name']}' on {DATABASE['host']}:{DATABASE['port']}, "
        f"user '{DATABASE['user']}' (change these in config.ini)"
    )

    for _ in range(MAX_TRIES):
        PASSWORD = getpass.getpass("Database password: ")
        try:
            with connect("postgres"):
                return
        except psycopg.OperationalError:
            print("Could not log in to the database. Check the password and that PostgreSQL is running.")

    sys.exit("Stopped after three failed tries.")


def ask_for_server_key():
    """ask for the server passphrase and return the secret key it unlocks."""
    with connect() as conn:
        stored = load_stored_key(conn)

    if stored is None:
        sys.exit(
            "This database has no server key yet.\n"
            "Run: python database.py --reset"
        )

    for _ in range(MAX_TRIES):
        passphrase = getpass.getpass("Server passphrase: ")
        key = unlock_server_key(stored, passphrase)
        if key is not None:
            return key
        print("That passphrase is not correct.")

    sys.exit("Stopped after three failed tries.")


def choose_passphrase():
    """ask for a new server passphrase twice, without showing it."""
    while True:
        passphrase = getpass.getpass("Choose a server passphrase: ")
        if len(passphrase) < MIN_PASSPHRASE_LENGTH:
            print(f"Use at least {MIN_PASSPHRASE_LENGTH} characters.")
            continue

        if getpass.getpass("Type it again: ") == passphrase:
            return passphrase
        print("The two passphrases did not match. Try again.")


# ----- the server's secret key -----
# a random key is made once and stored encrypted in the ServerKey table.
# the encryption key comes from the passphrase through scrypt, a deliberately
# slow hash, so guessing passphrases from a copy of the database is slow too.

def passphrase_cipher(passphrase, kdf_salt):
    key = hashlib.scrypt(
        passphrase.encode(),
        salt=kdf_salt,
        n=2 ** 15,
        r=8,
        p=1,
        maxmem=64 * 1024 * 1024,
        dklen=32,
    )
    return Fernet(base64.urlsafe_b64encode(key))


def create_server_key(conn, passphrase):
    """make a new random secret key, store it encrypted and return it."""
    secret_key = secrets.token_bytes(32)
    kdf_salt = secrets.token_bytes(16)
    encrypted_key = passphrase_cipher(passphrase, kdf_salt).encrypt(secret_key)

    insert(conn, "ServerKey", KdfSalt=kdf_salt, EncryptedKey=encrypted_key)
    return secret_key


def load_stored_key(conn):
    """the ServerKey row, or None when the table or its row is missing."""
    table = query(conn, "SELECT to_regclass('serverkey') AS name", one=True)
    if table["name"] is None:
        return None

    return query(conn, "SELECT * FROM ServerKey", one=True)


def unlock_server_key(stored, passphrase):
    """the decrypted secret key, or None when the passphrase is wrong."""
    cipher = passphrase_cipher(passphrase, bytes(stored["kdfsalt"]))
    try:
        return cipher.decrypt(bytes(stored["encryptedkey"]))
    except InvalidToken:
        return None


# ----- creating the database -----

def create_database_if_missing():
    database_name = DATABASE["name"]

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


def has_reports(conn):
    table = query(conn, "SELECT to_regclass('post') AS name", one=True)
    if table["name"] is None:
        return False

    count = query(conn, "SELECT count(*) AS total FROM Post", one=True)
    return count["total"] > 0


def create_tables(passphrase):
    """create every table from the schema file, then the server key."""
    with connect() as conn:
        conn.execute(SCHEMA_FILE.read_text(encoding="utf-8"))
        create_server_key(conn, passphrase)

    shutil.rmtree(UPLOADS_FOLDER, ignore_errors=True)


def main():
    reset = "--reset" in sys.argv
    database_name = DATABASE["name"]

    ask_for_password()
    create_database_if_missing()

    with connect() as conn:
        if has_reports(conn) and not reset:
            sys.exit(
                "The database already holds reports.\n"
                "To delete them and start again, run: python database.py --reset"
            )

    passphrase = choose_passphrase()
    create_tables(passphrase)
    print(f"Database '{database_name}' is ready. Start the site with: python server.py")


if __name__ == "__main__":
    main()
