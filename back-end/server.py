"""
web server for the lost pet tracker.

start the site:

    python server.py

the port and the database to use are set in config.ini.
it asks for the database password and the server passphrase before starting.

routes:

    POST /api/reports     submit a lost or found pet report
    POST /api/code        email a new verification code
    POST /api/signin      check a code and sign in
    POST /api/signout     sign out
    GET  /api/my          the signed-in poster's report and matches
    POST /api/browse      search active reports with filters

everything else is served from the front-end folder.
"""

import re
import secrets
import shutil
from pathlib import Path

import psycopg
import uvicorn
from fastapi import FastAPI, File, Form, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

import backend
import database as db


FRONTEND_FOLDER = db.BACKEND_FOLDER.parent / "front-end"
SESSION_COOKIE = "session"
MAX_PHOTOS = 5
MAX_PHOTO_BYTES = 10 * 1024 * 1024

app = FastAPI(title="Lost Pet Tracker")


# ----- routes -----

@app.post("/api/reports")
def submit_report(
    post_type: str = Form("lost"),
    email_address: str = Form(""),
    email_visibility: str = Form("email_only"),
    phone_number: str = Form(""),
    phone_visibility: str = Form("email_only"),
    report_title: str = Form(""),
    species: str = Form(""),
    breed: str = Form(""),
    weight: str = Form(""),
    sex: str = Form(""),
    location: str = Form(""),
    last_seen_at: str = Form(""),
    colors: str = Form(""),
    markings: str = Form(""),
    details: str = Form(""),
    photo: list[UploadFile] = File([]),
):
    """save a lost or found pet report as unverified and email a verification code."""
    if post_type == "found":
        report = backend.FoundPetReport()
    else:
        report = backend.LostPetReport()

    report.set_pet_info(report_title, species, breed)
    report.set_location(*find_location(location))
    report.set_pet_details(weight, sex)
    report.set_last_seen_time(last_seen_at)
    report.description = details.strip() or None

    for color in split_list(colors, r",|;|/|&|\band\b"):
        report.add_color(color)

    for marking in split_list(markings, r",|;"):
        report.add_marking(marking)

    photo_folder = db.UPLOADS_FOLDER / secrets.token_hex(16)
    try:
        save_photos(photo, report, photo_folder)
        with db.connect() as conn:
            account_number = backend.create_report(
                conn,
                report,
                email_address,
                email_visibility,
                phone_number,
                phone_visibility,
            )
    except Exception:
        shutil.rmtree(photo_folder, ignore_errors=True)
        raise

    return {
        "account_number": account_number,
        "message": "Report received. We emailed you a verification code.",
    }


@app.post("/api/code")
def request_code(account_number: str = Form("")):
    """email a new code. the reply is the same whether or not the account exists."""
    with db.connect() as conn:
        account = backend.find_account(conn, account_number)
        if account:
            backend.email_new_code(conn, account["accountid"], account_number)

    return {"message": "If that account number exists, we emailed it a code."}


@app.post("/api/signin")
def sign_in(response: Response, account_number: str = Form(""), code: str = Form("")):
    """check a code. a correct code activates the report, runs matching and signs in."""
    code_accepted = False

    with db.connect() as conn:
        account = backend.find_account(conn, account_number)
        if account:
            code_accepted = backend.verify_auth_code(conn, account["accountid"], code)
        if code_accepted:
            backend.run_matching(conn, account["accountid"])

    # raised after the block above so that a wrong guess is still saved.
    if not code_accepted:
        raise backend.ValidationError(
            "That code is incorrect, expired or already used. Ask for a new one.",
            "code",
        )

    response.set_cookie(
        SESSION_COOKIE,
        backend.make_session(account["accountid"]),
        max_age=backend.SESSION_MINUTES * 60,
        httponly=True,
        samesite="lax",
    )
    return {"message": "You are signed in."}


@app.post("/api/signout")
def sign_out(response: Response):
    response.delete_cookie(SESSION_COOKIE)
    return {"message": "You are signed out."}


@app.get("/api/my")
def my_page(request: Request):
    """the signed-in poster's report, contacts and matches. empty when signed out."""
    account_id = backend.read_session(request.cookies.get(SESSION_COOKIE))
    if account_id is None:
        return {}

    with db.connect() as conn:
        return backend.my_page(conn, account_id)


@app.post("/api/browse")
def browse_reports(
    post_type: str = Form(""),
    species: str = Form(""),
    breed: str = Form(""),
    sex: str = Form(""),
    colors: str = Form(""),
    location: str = Form(""),
    radius: str = Form("10"),
    date_from: str = Form(""),
    date_to: str = Form(""),
    min_weight: str = Form(""),
    max_weight: str = Form(""),
):
    """search active reports. every filter is optional."""
    center = None
    if location.strip():
        center = find_location(location)

    with db.connect() as conn:
        reports = backend.search_reports(
            conn,
            post_type=post_type,
            species=species,
            breed=breed,
            sex=sex,
            colors=colors,
            date_from=date_from,
            date_to=date_to,
            min_weight=min_weight,
            max_weight=max_weight,
            center=center,
            radius_miles=radius,
        )

    return {"reports": reports}


# ----- errors -----
# every error reaches the page as {"error": "...", "field": "..."}.

@app.exception_handler(backend.ValidationError)
async def input_rejected(request, error):
    return JSONResponse(
        {"error": error.message, "field": error.field},
        status_code=400,
    )


@app.exception_handler(psycopg.Error)
async def database_error(request, error):
    rejected_by_database = isinstance(
        error,
        (psycopg.errors.IntegrityError, psycopg.errors.DataError),
    )

    if rejected_by_database:
        message = "The database did not accept that information."
        status_code = 400
    else:
        message = "The database could not be reached."
        status_code = 500

    return JSONResponse({"error": message, "field": None}, status_code=status_code)


# ----- helpers for the report form -----

def find_location(text):
    place = backend.locate(text)
    if place is None:
        raise backend.ValidationError(
            "We could not find that location. Type a US town and state such as "
            "New York, NY, a ZIP code such as 10001, or coordinates such as 40.75, -73.99.",
            "location",
        )
    return place


def split_list(text, separators):
    """split text such as "Tan and white" into its parts."""
    parts = re.split(separators, text, flags=re.IGNORECASE)
    return [part.strip() for part in parts if part.strip()]


def save_photos(uploads, report, folder):
    """check each uploaded photo and save it in the given uploads folder."""
    chosen = [upload for upload in uploads if upload.filename]

    for number, upload in enumerate(chosen[:MAX_PHOTOS], start=1):
        extension = Path(upload.filename).suffix.lower()
        file_path = f"{folder.name}/{number}{extension}"
        report.upload_photo(file_path)

        data = upload.file.read(MAX_PHOTO_BYTES + 1)
        if not data or len(data) > MAX_PHOTO_BYTES:
            raise backend.ValidationError(
                "Each photo must be under 10 MB and not empty.",
                "photo",
            )

        folder.mkdir(parents=True, exist_ok=True)
        (db.UPLOADS_FOLDER / file_path).write_bytes(data)


# ----- pages and photos -----

db.UPLOADS_FOLDER.mkdir(exist_ok=True)
app.mount("/uploads", StaticFiles(directory=db.UPLOADS_FOLDER), name="uploads")
app.mount("/", StaticFiles(directory=FRONTEND_FOLDER, html=True), name="pages")


def main():
    port = int(db.CONFIG["site"]["port"])

    db.ask_for_password()
    backend.SECRET_KEY = db.ask_for_server_key()

    print(f"Lost Pet Tracker running at http://localhost:{port}  (Ctrl+C to stop)")
    uvicorn.run(app, host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
