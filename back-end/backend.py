import hashlib
import hmac
import math
import os
import re
import secrets
import time
from datetime import date, datetime, timedelta, timezone

import database as db


SECRET_SALT = os.getenv("SECRET_SALT", "dev-only-salt")
SITE_ADDRESS = os.getenv("BASE_URL", "http://localhost:8000")
OUTBOX_FOLDER = db.BACKEND_FOLDER / "dev-outbox"

SESSION_MINUTES = 30
MAX_WRONG_GUESSES = 5

EMAIL_PATTERN = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
PHONE_PATTERN = re.compile(r"\+?[0-9]{7,15}")
PHOTO_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".heic")
VISIBILITY_CHOICES = ("public", "email_only")


# ----- validation -----

class ValidationError(Exception):
    """an input was not accepted. `field` names the form field it came from."""

    def __init__(self, message, field=None):
        super().__init__(message)
        self.message = message
        self.field = field


def check_text(value, field, label, max_length, required=True):
    """return the trimmed text, or None when it is empty and not required."""
    is_empty = not isinstance(value, str) or not value.strip()

    if is_empty:
        if required:
            raise ValidationError(f"{label} is required.", field)
        return None

    text = value.strip()
    if len(text) > max_length:
        raise ValidationError(f"{label} can be at most {max_length} characters.", field)

    return text


def check_number(value, field, label, lowest, highest, required=True):
    """return the value as a number between lowest and highest, inclusive."""
    if value is None or value == "":
        if required:
            raise ValidationError(f"{label} is required.", field)
        return None

    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{label} must be a number.", field) from None

    if not math.isfinite(number) or not lowest <= number <= highest:
        raise ValidationError(f"{label} must be between {lowest} and {highest}.", field)

    return number


# ----- pet reports -----

class PetReport:
    """a report being filled in. each method checks its input before storing it."""

    post_type = None

    def __init__(self):
        self.title = None
        self.species = None
        self.breed = None
        self.description = None
        self.weight = None
        self.sex = None
        self.latitude = None
        self.longitude = None
        self.lost_or_found_at = None
        self.colors = []
        self.markings = []
        self.photos = []

    def set_pet_info(self, title, species, breed=None):
        self.title = check_text(title, "report_title", "A report title", 100)
        self.species = check_text(species, "species", "The species", 50).lower()
        self.breed = check_text(breed, "breed", "The breed", 100, required=False)

    def set_location(self, latitude, longitude):
        self.latitude = check_number(latitude, "location", "Latitude", -90, 90)
        self.longitude = check_number(longitude, "location", "Longitude", -180, 180)

    def set_pet_details(self, weight=None, sex=None):
        weight = check_number(weight, "weight", "Weight in kg", 0, 1000, required=False)
        if weight == 0:
            raise ValidationError("Weight must be greater than 0 kg.", "weight")

        sex = check_text(sex, "sex", "Sex", 7, required=False)
        if sex is not None:
            sex = sex.lower()
            if sex not in ("male", "female", "unknown"):
                raise ValidationError("Sex must be male, female or unknown.", "sex")

        self.weight = weight
        self.sex = sex

    def set_last_seen_time(self, when, now=None):
        """accepts a datetime or ISO text. a time with no zone is taken as local time."""
        if isinstance(when, str):
            when = parse_date(when)

        if not isinstance(when, datetime):
            raise ValidationError("The date and time are required.", "last_seen_at")

        when = when.astimezone()
        now = now or datetime.now(timezone.utc)
        if when > now:
            raise ValidationError(
                "The date and time cannot be in the future.",
                "last_seen_at",
            )

        self.lost_or_found_at = when

    def add_color(self, color):
        color = check_text(color, "colors", "A color", 30).lower()
        if color in self.colors:
            raise ValidationError(f"The color '{color}' is listed twice.", "colors")

        self.colors.append(color)

    def add_marking(self, marking):
        marking = check_text(marking, "markings", "A marking", 255)
        if marking in self.markings:
            raise ValidationError(f"The marking '{marking}' is listed twice.", "markings")

        self.markings.append(marking)

    def upload_photo(self, file_path):
        file_path = check_text(file_path, "photo", "A photo", 500)
        if not file_path.lower().endswith(PHOTO_EXTENSIONS):
            raise ValidationError("Photos must be JPG, PNG, WEBP or HEIC images.", "photo")

        self.photos.append(file_path)


class LostPetReport(PetReport):
    post_type = "lost"


class FoundPetReport(PetReport):
    post_type = "found"


def parse_date(text):
    try:
        return datetime.fromisoformat(text.strip())
    except ValueError:
        return None


def submit_report(conn, account_id, post_type, report):
    """store a report for an account. it starts as 'unverified'. returns the PostID."""
    if post_type not in ("lost", "found") or post_type != report.post_type:
        raise ValidationError("The report type must be 'lost' or 'found'.", "post_type")

    account_exists = db.query(
        conn,
        "SELECT 1 FROM Account WHERE AccountID = %s",
        (account_id,),
        one=True,
    )
    if not account_exists:
        raise ValidationError("The account does not exist.")

    already_has_report = db.query(
        conn,
        "SELECT 1 FROM Post WHERE AccountID = %s",
        (account_id,),
        one=True,
    )
    if already_has_report:
        raise ValidationError("This account already has a report.")

    required = {
        "report_title": report.title,
        "location": report.latitude,
        "last_seen_at": report.lost_or_found_at,
        "photo": report.photos,
    }
    missing = [field for field, value in required.items() if value in (None, [])]
    if missing:
        raise ValidationError("Still needed: " + ", ".join(missing) + ".", missing[0])

    post = db.insert(
        conn,
        "Post",
        AccountID=account_id,
        PostType=post_type,
        Title=report.title,
        Description=report.description,
        Species=report.species,
        Breed=report.breed,
        Weight=report.weight,
        Sex=report.sex,
        Latitude=report.latitude,
        Longitude=report.longitude,
        LostOrFoundAt=report.lost_or_found_at,
    )
    post_id = post["postid"]

    for color in report.colors:
        db.insert(conn, "PetColor", PostID=post_id, Color=color)

    for marking in report.markings:
        db.insert(conn, "PetMarking", PostID=post_id, Marking=marking)

    for file_path in report.photos:
        db.insert(conn, "Photo", PostID=post_id, FilePath=file_path)

    return post_id


def create_report(conn, report, email, email_visibility, phone="", phone_visibility="email_only"):
    """create an account with its contacts and report, then email the first code.

    returns the new account number.
    """
    settings = db.settings(conn)
    if not settings["canpost"]:
        raise ValidationError("New reports are switched off right now.")

    account_number = str(secrets.randbelow(9_000_000_000) + 1_000_000_000)
    account_id = create_account(
        conn,
        hash_account_number(account_number),
        settings["settingsid"],
    )

    email_contact_id = add_email(conn, account_id, email, email_visibility)
    set_two_factor_email(conn, account_id, email_contact_id)

    if phone and phone.strip():
        add_phone(conn, account_id, phone, phone_visibility)

    submit_report(conn, account_id, report.post_type, report)
    email_new_code(conn, account_id, account_number, first_time=True)

    return account_number


# ----- accounts -----

def keyed_hash(*parts):
    """HMAC-SHA256 of the parts with the server's salt.

    account numbers, codes and sessions are only ever stored or compared in this form.
    """
    text = ":".join(str(part) for part in parts)
    return hmac.new(SECRET_SALT.encode(), text.encode(), hashlib.sha256).hexdigest()


def hash_account_number(account_number):
    digits_only = re.sub(r"\D", "", account_number or "")
    return keyed_hash("account", digits_only)


def find_account(conn, account_number):
    """the account row for the number a user typed, or None."""
    return db.query(
        conn,
        "SELECT * FROM Account WHERE AccountNumHash = %s",
        (hash_account_number(account_number),),
        one=True,
    )


def create_account(conn, account_num_hash, settings_id):
    """create the account for a new report. returns the AccountID."""
    check_text(account_num_hash, None, "An account number hash", 255)

    settings_exist = db.query(
        conn,
        "SELECT 1 FROM Settings WHERE SettingsID = %s",
        (settings_id,),
        one=True,
    )
    if not settings_exist:
        raise ValidationError("The settings record does not exist.")

    hash_in_use = db.query(
        conn,
        "SELECT 1 FROM Account WHERE AccountNumHash = %s",
        (account_num_hash,),
        one=True,
    )
    if hash_in_use:
        raise ValidationError("That account number is already in use.")

    account = db.insert(
        conn,
        "Account",
        SettingsID=settings_id,
        AccountNumHash=account_num_hash,
    )
    return account["accountid"]


def add_email(conn, account_id, email_address, visibility):
    """attach an email address to an account. returns the ContactID."""
    email_address = check_text(email_address, "email_address", "An email address", 255)
    if not EMAIL_PATTERN.fullmatch(email_address):
        raise ValidationError(
            "Enter an email address like name@example.com.",
            "email_address",
        )

    return add_contact(conn, account_id, visibility, "Email", EmailAddress=email_address)


def add_phone(conn, account_id, phone_number, visibility):
    """attach a phone number to an account. returns the ContactID."""
    phone_number = check_text(phone_number, "phone_number", "A phone number", 30)
    digits = re.sub(r"[\s().-]", "", phone_number)
    if not PHONE_PATTERN.fullmatch(digits):
        raise ValidationError("Enter a phone number with 7 to 15 digits.", "phone_number")

    return add_contact(conn, account_id, visibility, "Phone", PhoneNumber=digits)


def add_contact(conn, account_id, visibility, table, **details):
    """shared by add_email and add_phone."""
    if visibility not in VISIBILITY_CHOICES:
        raise ValidationError(
            "Visibility must be 'public' or 'email_only'.",
            "email_visibility",
        )

    contact = db.insert(conn, "ContactMethod", AccountID=account_id, Visibility=visibility)
    contact_id = contact["contactid"]

    db.insert(conn, table, AccountID=account_id, ContactID=contact_id, **details)
    return contact_id


def set_two_factor_email(conn, account_id, contact_id):
    """choose which of the account's emails receives verification codes."""
    is_email_of_account = db.query(
        conn,
        "SELECT 1 FROM Email WHERE AccountID = %s AND ContactID = %s",
        (account_id, contact_id),
        one=True,
    )
    if not is_email_of_account:
        raise ValidationError(
            "Codes can only be sent to one of the account's email addresses."
        )

    if db.two_factor_email(conn, account_id):
        raise ValidationError("This account already has a verification email.")

    db.insert(conn, "UseFor2FA", AccountID=account_id, ContactID=contact_id)


# ----- verification codes -----

failed_attempts = {}


def hash_code(account_id, code):
    return keyed_hash("code", account_id, (code or "").strip())


def issue_auth_code(conn, account_id, code_hash, used_at=None):
    """store the hash of a code that is about to be emailed. returns the CodeID."""
    check_text(code_hash, None, "A code hash", 255)

    code = db.insert(
        conn,
        "AuthCode",
        AccountID=account_id,
        CodeHash=code_hash,
        UsedAt=used_at,
    )
    return code["codeid"]


def email_new_code(conn, account_id, account_number, first_time=False):
    """make a 6-digit code from account number, system time and salt, then email it."""
    seed = keyed_hash("seed", account_number, time.time_ns(), secrets.token_hex(4))
    code = str(int(seed, 16) % 1_000_000).zfill(6)
    issue_auth_code(conn, account_id, hash_code(account_id, code))

    lifetime = db.settings(conn)["authcodelifetime"]
    minutes = int(lifetime.total_seconds() // 60)

    body = (
        f"Your Lost Pet Tracker code: {code}\n"
        f"It works once and expires in {minutes} minutes.\n"
    )
    if first_time:
        body += (
            f"\nYour account number: {account_number}\n"
            "Keep it. It is the only way to view your report later, at "
            f"{SITE_ADDRESS}/my-reports.html\n"
        )

    recipient = db.two_factor_email(conn, account_id)
    send_email(recipient["emailaddress"], "Your Lost Pet Tracker code", body)


def verify_auth_code(conn, account_id, code):
    """return True if `code` is the account's newest code, unused and not expired.

    a correct code is used up, marks the email as verified and makes an
    unverified report active. five wrong guesses retire the code.
    """
    newest_code = db.query(
        conn,
        """
        SELECT AuthCode.*,
               now() - AuthCode.CreatedAt <= Settings.AuthCodeLifetime AS fresh
        FROM AuthCode
        JOIN Account USING (AccountID)
        JOIN Settings USING (SettingsID)
        WHERE AuthCode.AccountID = %s
        ORDER BY AuthCode.CreatedAt DESC, AuthCode.CodeID DESC
        LIMIT 1
        """,
        (account_id,),
        one=True,
    )

    if newest_code is None:
        return False
    if newest_code["usedat"] is not None or not newest_code["fresh"]:
        return False

    code_id = newest_code["codeid"]
    is_correct = hmac.compare_digest(newest_code["codehash"], hash_code(account_id, code))

    if not is_correct:
        attempts = failed_attempts.get((account_id, code_id), 0) + 1
        failed_attempts[(account_id, code_id)] = attempts
        if attempts >= MAX_WRONG_GUESSES:
            mark_code_used(conn, account_id, code_id)
        return False

    mark_code_used(conn, account_id, code_id)
    activate_account(conn, account_id)
    return True


def mark_code_used(conn, account_id, code_id):
    db.query(
        conn,
        "UPDATE AuthCode SET UsedAt = now() WHERE AccountID = %s AND CodeID = %s",
        (account_id, code_id),
    )


def activate_account(conn, account_id):
    """mark the email verified, make the report public and record the activity."""
    db.query(
        conn,
        """
        UPDATE Email
        SET VerifiedAt = COALESCE(Email.VerifiedAt, now())
        FROM UseFor2FA
        WHERE UseFor2FA.AccountID = Email.AccountID
          AND UseFor2FA.ContactID = Email.ContactID
          AND Email.AccountID = %s
        """,
        (account_id,),
    )
    db.query(
        conn,
        "UPDATE Post SET Status = 'active' WHERE AccountID = %s AND Status = 'unverified'",
        (account_id,),
    )
    db.query(
        conn,
        "UPDATE Account SET LastActiveAt = now() WHERE AccountID = %s",
        (account_id,),
    )


# ----- sign-in -----

def make_session(account_id):
    """the value of the sign-in cookie: account id, expiry time and a signature."""
    expires = int(time.time()) + SESSION_MINUTES * 60
    signature = keyed_hash("session", account_id, expires)
    return f"{account_id}.{expires}.{signature}"


def read_session(cookie):
    """return the account id from a genuine, unexpired cookie. otherwise None."""
    try:
        account_id, expires, signature = (cookie or "").split(".")
        expected = keyed_hash("session", account_id, expires)

        is_genuine = hmac.compare_digest(signature, expected)
        is_current = int(expires) > time.time()

        if is_genuine and is_current:
            return int(account_id)
    except ValueError:
        pass

    return None


# ----- email (simulated) -----

def send_email(to, subject, body):
    """print the email and save it as a text file in dev-outbox/.

    returns True when the email was saved. real delivery is not connected yet.
    """
    message = f"To: {to}\nSubject: {subject}\n\n{body}\n"
    print("\n----- email (not really sent) -----\n" + message, flush=True)

    timestamp = time.strftime("%Y%m%d-%H%M%S")
    unique_part = f"{time.time_ns() % 1_000_000:06d}"
    safe_address = re.sub(r"[^\w@.-]", "_", to)
    file_name = f"{timestamp}-{unique_part}-{safe_address}.txt"

    try:
        OUTBOX_FOLDER.mkdir(exist_ok=True)
        (OUTBOX_FOLDER / file_name).write_text(message, encoding="utf-8")
    except OSError:
        return False

    return True


# ----- locations (simulated) -----

KNOWN_PLACES = {
    "atlanta": (33.7490, -84.3880),
    "piedmont park": (33.7851, -84.3738),
    "grant park": (33.7396, -84.3724),
    "decatur": (33.7748, -84.2963),
    "baltimore": (39.2904, -76.6122),
    "towson": (39.4015, -76.6019),
}


def locate(text):
    """return (latitude, longitude) for "lat, lng" or a known place name, or None.

    google maps is not connected yet.
    """
    text = (text or "").strip().lower()

    coordinates = re.fullmatch(r"(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)", text)
    if coordinates:
        return float(coordinates[1]), float(coordinates[2])

    longest_names_first = sorted(KNOWN_PLACES, key=len, reverse=True)
    for name in longest_names_first:
        if name in text:
            return KNOWN_PLACES[name]

    return None


# ----- matching -----

def run_matching(conn, account_id):
    """compare the account's active report with reports of the other type.

    pairs that score at or above the threshold are saved in the match table,
    and both posters are emailed once.
    """
    own_post = db.query(
        conn,
        """
        SELECT PostID FROM Post
        WHERE AccountID = %s AND Status = 'active' AND ResolvedAt IS NULL
        """,
        (account_id,),
        one=True,
    )
    if own_post is None:
        return

    settings = db.settings(conn)
    own_report = db.load_report(conn, own_post["postid"])

    candidates = db.query(
        conn,
        """
        SELECT PostID FROM Post
        WHERE PostType <> %s
          AND Status = 'active'
          AND ResolvedAt IS NULL
          AND lower(Species) = %s
        """,
        (own_report["posttype"], own_report["species"].lower()),
    )

    for candidate in candidates:
        other_report = db.load_report(conn, candidate["postid"])

        if own_report["posttype"] == "lost":
            lost, found = own_report, other_report
        else:
            lost, found = other_report, own_report

        time_apart = found["lostorfoundat"] - lost["lostorfoundat"]
        days_apart = time_apart.total_seconds() / 86400
        km = distance_km(lost, found)
        sigma = search_radius_km(settings, days_apart)

        found_before_lost = days_apart < -1
        too_far_apart = km > 3 * sigma
        if found_before_lost or too_far_apart:
            continue

        overall_score, attribute_score, explanation = score_match(
            lost, found, km, sigma, settings
        )
        pair = (lost["postid"], found["postid"])

        if overall_score < settings["overallscorethreshold"]:
            db.query(conn, "DELETE FROM Match WHERE LostID = %s AND FoundID = %s", pair)
            continue

        saved = db.query(
            conn,
            """
            INSERT INTO Match (LostID, FoundID, AttributeScore, Score, Explanation)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (LostID, FoundID) DO UPDATE
                SET AttributeScore = EXCLUDED.AttributeScore,
                    Score = EXCLUDED.Score,
                    Explanation = EXCLUDED.Explanation
            RETURNING Notified
            """,
            (*pair, attribute_score, overall_score, explanation),
            one=True,
        )

        if not saved["notified"]:
            both_emailed = notify(conn, lost, found, overall_score, explanation)
            if both_emailed:
                db.query(
                    conn,
                    "UPDATE Match SET Notified = true WHERE LostID = %s AND FoundID = %s",
                    pair,
                )


def search_radius_km(settings, days_apart):
    """the radius grows the longer a pet has been missing, up to a cap."""
    days_missing = max(days_apart, 0)
    radius = settings["basesigma"] + settings["radiussigmagrowth"] * days_missing
    return min(radius, settings["radiussigmacap"])


def distance_km(report_a, report_b):
    """great-circle (haversine) distance between two reports."""
    latitude_a = math.radians(report_a["latitude"])
    latitude_b = math.radians(report_b["latitude"])
    latitude_gap = latitude_b - latitude_a
    longitude_gap = math.radians(report_b["longitude"] - report_a["longitude"])

    half_chord = (
        math.sin(latitude_gap / 2) ** 2
        + math.cos(latitude_a) * math.cos(latitude_b) * math.sin(longitude_gap / 2) ** 2
    )
    earth_radius_km = 6371
    return 2 * earth_radius_km * math.asin(min(1.0, math.sqrt(half_chord)))


def score_match(lost, found, km, sigma, settings):
    """compare a lost report with a found report.

    returns (overall score, attribute score, explanation). scores run from 0 to 1.
    photo and text similarity will join the overall score when the AI models
    are connected.
    """
    reasons = [
        f"Both reports are for a {lost['species']}.",
        f"The locations are {km:.1f} km apart.",
    ]
    attribute_scores = []

    comparisons = [
        compare_sets("breed", words(lost["breed"]), words(found["breed"])),
        compare_sets("colors", set(lost["colors"]), set(found["colors"])),
        compare_sets(
            "marking words",
            words(" ".join(lost["markings"])),
            words(" ".join(found["markings"])),
        ),
    ]
    for comparison in comparisons:
        if comparison is not None:
            similarity, reason = comparison
            attribute_scores.append(similarity)
            reasons.append(reason)

    both_sexes_known = lost["sex"] in ("male", "female") and found["sex"] in ("male", "female")
    if both_sexes_known:
        same_sex = lost["sex"] == found["sex"]
        attribute_scores.append(1.0 if same_sex else 0.0)
        reasons.append("Same sex." if same_sex else "The sex differs.")

    if lost["weight"] and found["weight"]:
        difference = abs(lost["weight"] - found["weight"])
        heavier = max(lost["weight"], found["weight"])
        attribute_scores.append(1 - difference / heavier)
        reasons.append(f"Weights: {lost['weight']:g} kg and {found['weight']:g} kg.")

    attribute_score = None
    if attribute_scores:
        attribute_score = sum(attribute_scores) / len(attribute_scores)

    distance_score = math.exp(-km ** 2 / (2 * sigma ** 2))

    weighted_scores = [(settings["matchradiusweight"], distance_score)]
    if attribute_score is not None:
        weighted_scores.append((settings["attributescoreweight"], attribute_score))

    total_weight = sum(weight for weight, score in weighted_scores)
    weighted_sum = sum(weight * score for weight, score in weighted_scores)
    overall_score = weighted_sum / total_weight

    return overall_score, attribute_score, " ".join(reasons)


def compare_sets(label, lost_items, found_items):
    """return (similarity from 0 to 1, sentence), or None if either set is empty."""
    if not lost_items or not found_items:
        return None

    shared = lost_items & found_items
    similarity = len(shared) / len(lost_items | found_items)

    if shared:
        reason = f"Shared {label}: {', '.join(sorted(shared))}."
    else:
        reason = f"No {label} in common."

    return similarity, reason


def words(text):
    """the words of three or more characters in a text, in lower case."""
    return set(re.findall(r"[a-z0-9]{3,}", (text or "").lower()))


def notify(conn, lost, found, overall_score, explanation):
    """email both posters about a match. returns True only if both emails were sent."""
    lost_poster_emailed = send_match_email(conn, lost, found, overall_score, explanation)
    found_poster_emailed = send_match_email(conn, found, lost, overall_score, explanation)
    return lost_poster_emailed and found_poster_emailed


def send_match_email(conn, own_report, other_report, overall_score, explanation):
    recipient = db.two_factor_email(conn, own_report["accountid"])
    if recipient is None:
        return False

    other_contacts = db.contacts(conn, other_report["accountid"])
    contact_lines = "\n".join(
        f"  {contact['type']}: {contact['value']}" for contact in other_contacts
    )
    percent = round(overall_score * 100)

    body = (
        f"A {other_report['posttype']}-pet report may match your report "
        f"\"{own_report['title']}\".\n\n"
        f"Possible match: \"{other_report['title']}\" ({percent}% match)\n\n"
        f"Why it was suggested:\n{explanation}\n\n"
        f"The other poster's contact details:\n{contact_lines}\n\n"
        f"Compare the two reports at {SITE_ADDRESS}/matches.html\n"
        "This is only a suggestion. You decide whether it is the same animal.\n"
    )
    return send_email(recipient["emailaddress"], "Possible match for your pet report", body)


def my_page(conn, account_id):
    """the signed-in poster's report, contact details and matches, best match first."""
    own_post = db.query(
        conn,
        "SELECT PostID FROM Post WHERE AccountID = %s",
        (account_id,),
        one=True,
    )
    if own_post is None:
        return {}

    report = db.load_report(conn, own_post["postid"])

    if report["posttype"] == "lost":
        own_column, other_column = "LostID", "FoundID"
    else:
        own_column, other_column = "FoundID", "LostID"

    match_rows = db.query(
        conn,
        f"""
        SELECT {other_column} AS otherpostid, Score, Explanation
        FROM Match
        WHERE {own_column} = %s
        ORDER BY Score DESC
        """,
        (report["postid"],),
    )

    matches = []
    for row in match_rows:
        other_report = db.load_report(conn, row["otherpostid"])
        other_contacts = db.contacts(conn, other_report["accountid"])
        public_contacts = [c for c in other_contacts if c["visibility"] == "public"]

        matches.append({
            "score": row["score"],
            "explanation": row["explanation"],
            "report": other_report,
            "contacts": public_contacts,
        })

    return {
        "report": report,
        "contacts": db.contacts(conn, account_id),
        "matches": matches,
    }


# ----- browsing reports -----

MAX_BROWSE_RESULTS = 100
DEFAULT_RADIUS_KM = 25


def like_pattern(text):
    """a 'contains' pattern for ILIKE, with the wildcard characters in the text escaped."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def parse_day(text, field, label):
    """the date in `text` (yyyy-mm-dd), or None when it is empty."""
    text = check_text(text, field, label, 10, required=False)
    if text is None:
        return None

    try:
        return date.fromisoformat(text)
    except ValueError:
        raise ValidationError(f"{label} must be a date like 2026-11-16.", field) from None


def local_midnight(day):
    """the start of `day` in the server's local time, like set_last_seen_time assumes."""
    return datetime.combine(day, datetime.min.time()).astimezone()


def browse_reports(conn, filters):
    """the public reports that match the browse page's filters, as {"reports": [...]}.

    `filters` maps filter names to text (for example request.args). empty filters are
    ignored. only active, unresolved reports are shown. with a location the nearest
    reports come first, otherwise the newest.
    """
    conditions = ["Post.Status = 'active'", "Post.ResolvedAt IS NULL"]
    values = []

    post_type = check_text(filters.get("post_type"), "post_type", "The report status", 10, required=False)
    if post_type is not None:
        post_type = post_type.lower()
        if post_type not in ("lost", "found"):
            raise ValidationError("The report status must be 'lost' or 'found'.", "post_type")
        conditions.append("Post.PostType = %s")
        values.append(post_type)

    species = check_text(filters.get("species"), "species", "The species", 50, required=False)
    if species is not None:
        conditions.append("lower(Post.Species) = %s")
        values.append(species.lower())

    breed = check_text(filters.get("breed"), "breed", "The breed", 100, required=False)
    if breed is not None:
        conditions.append("Post.Breed ILIKE %s")
        values.append(like_pattern(breed))

    sex = check_text(filters.get("sex"), "sex", "Sex", 7, required=False)
    if sex is not None:
        sex = sex.lower()
        if sex not in ("male", "female", "unknown"):
            raise ValidationError("Sex must be male, female or unknown.", "sex")
        conditions.append("Post.Sex = %s")
        values.append(sex)

    # "tan, white" and "tan and white" both mean the colors tan and white. a report
    # matches when it has any of them, and "brown" also matches "dark brown".
    color_text = check_text(filters.get("colors"), "colors", "Colors", 200, required=False)
    if color_text is not None:
        names = [name.strip().lower() for name in re.split(r",|&|/|\band\b", color_text)]
        names = [name for name in names if name][:10]
        if names:
            tests = " OR ".join("PetColor.Color ILIKE %s" for _ in names)
            conditions.append(
                "EXISTS (SELECT 1 FROM PetColor "
                f"WHERE PetColor.PostID = Post.PostID AND ({tests}))"
            )
            values.extend(like_pattern(name) for name in names)

    lightest = check_number(filters.get("min_weight"), "min_weight", "The minimum weight", 0, 1000, required=False)
    heaviest = check_number(filters.get("max_weight"), "max_weight", "The maximum weight", 0, 1000, required=False)
    if lightest is not None and heaviest is not None and lightest > heaviest:
        raise ValidationError("The minimum weight must not be more than the maximum weight.", "min_weight")
    if lightest is not None:
        conditions.append("Post.Weight >= %s")
        values.append(lightest)
    if heaviest is not None:
        conditions.append("Post.Weight <= %s")
        values.append(heaviest)

    first_day = parse_day(filters.get("date_from"), "date_from", "The start date")
    last_day = parse_day(filters.get("date_to"), "date_to", "The end date")
    if first_day is not None and last_day is not None and first_day > last_day:
        raise ValidationError("The start date must be on or before the end date.", "date_from")
    if first_day is not None:
        conditions.append("Post.LostOrFoundAt >= %s")
        values.append(local_midnight(first_day))
    if last_day is not None:
        # the end date counts as a whole day, so stop at the start of the next one.
        conditions.append("Post.LostOrFoundAt < %s")
        values.append(local_midnight(last_day + timedelta(days=1)))

    origin = None
    radius = None
    place = check_text(filters.get("location"), "location", "The location", 200, required=False)
    if place is not None:
        point = locate(place)
        if point is None:
            raise ValidationError(
                "We could not find that place. Try a city name or coordinates like 33.75, -84.39.",
                "location",
            )
        origin = {"latitude": point[0], "longitude": point[1]}

        radius = check_number(filters.get("radius_km"), "radius_km", "The search distance", 0.1, 1000, required=False)
        if radius is None:
            radius = DEFAULT_RADIUS_KM

    rows = db.query(
        conn,
        f"""
        SELECT Post.PostID, Post.Latitude, Post.Longitude
        FROM Post
        WHERE {" AND ".join(conditions)}
        ORDER BY Post.LostOrFoundAt DESC, Post.PostID DESC
        """,
        tuple(values),
    )

    if origin is not None:
        nearby = []
        for row in rows:
            spot = {"latitude": float(row["latitude"]), "longitude": float(row["longitude"])}
            km = distance_km(origin, spot)
            if km <= radius:
                nearby.append((km, row))

        # sort is stable, so reports the same distance away stay newest first.
        nearby.sort(key=lambda pair: pair[0])
        rows = [row for km, row in nearby]

    reports = []
    for row in rows[:MAX_BROWSE_RESULTS]:
        report = dict(db.load_report(conn, row["postid"]))
        contacts = db.contacts(conn, report.pop("accountid"))
        report["contacts"] = [c for c in contacts if c["visibility"] == "public"]
        reports.append(report)

    return {"reports": reports}
