import os
import json
import re
import time
import uuid
import hashlib
import secrets
import logging
import hmac
from datetime import datetime, timedelta, timezone
import boto3
import bcrypt
import jwt
import pymysql
from botocore.exceptions import ClientError
logger = logging.getLogger()
logger.setLevel(logging.INFO)
REGION = os.environ.get("AWS_REGION", "us-east-1")
DB_SECRET_ARN = os.environ["DB_SECRET_ARN"]
DB_HOST = os.environ["DB_HOST"]
DB_NAME = os.environ.get("DB_NAME", "medislot")
JWT_SECRET_PARAMETER = os.environ.get(
    "JWT_SECRET_PARAMETER", "/medislot/dev/jwt/signing-secret"
)
AUTH_TOKENS_TABLE = os.environ.get(
    "AUTH_TOKENS_TABLE", "medislot-dev-auth-tokens"
)
# Set this environment variable only while initializing the database.
DB_INIT_TOKEN = os.environ.get("DB_INIT_TOKEN", "")
SCHEMA_FILE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "database", "schema.sql"))
ACCESS_TOKEN_MINUTES = int(os.environ.get("ACCESS_TOKEN_MINUTES", "15"))
REFRESH_TOKEN_DAYS = int(os.environ.get("REFRESH_TOKEN_DAYS", "7"))
ssm = boto3.client("ssm", region_name=REGION)
secrets_manager = boto3.client("secretsmanager", region_name=REGION)
dynamodb = boto3.resource("dynamodb", region_name=REGION)
token_table = dynamodb.Table(AUTH_TOKENS_TABLE)
# Read secrets during Lambda initialization (cold start).
JWT_SECRET = ssm.get_parameter(
    Name=JWT_SECRET_PARAMETER, WithDecryption=True
)["Parameter"]["Value"]
DB_SECRET = json.loads(
    secrets_manager.get_secret_value(SecretId=DB_SECRET_ARN)["SecretString"]
)


def response(status, body):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
        },
        "body": json.dumps(body, default=str),
    }


def connect_db():
    return pymysql.connect(
        host=DB_HOST,
        user=DB_SECRET.get("username") or DB_SECRET.get("user"),
        password=DB_SECRET["password"],
        database=DB_NAME,
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=5,
        read_timeout=10,
        write_timeout=10,
        autocommit=False,
    )


def initialize_database(event):
    """Run the bundled schema once, protected by a temporary secret token."""
    if not DB_INIT_TOKEN:
        return response(404, {"message": "Route not available"})
    headers = event.get("headers") or {}
    supplied_token = headers.get("x-db-init-token") or headers.get("X-DB-Init-Token") or ""
    if not hmac.compare_digest(str(supplied_token), DB_INIT_TOKEN):
        return response(403, {"message": "Forbidden"})
    if not os.path.isfile(SCHEMA_FILE):
        logger.error("Database schema file was not found at %s", SCHEMA_FILE)
        return response(500, {"message": "Schema file is missing from the Lambda package"})
    connection = None
    try:
        with open(SCHEMA_FILE, "r", encoding="utf-8") as schema_file:
            schema_sql = schema_file.read()
        # Connect without selecting a database so CREATE DATABASE can run
        # even when the medislot database has not been created yet.
        connection = pymysql.connect(
            host=DB_HOST,
            user=DB_SECRET.get("username") or DB_SECRET.get("user"),
            password=DB_SECRET["password"],
            port=int(os.environ.get("DB_PORT", "3306")),
            connect_timeout=5,
            read_timeout=30,
            write_timeout=30,
            autocommit=True,
        )
        statements = [part.strip() for part in schema_sql.split(";") if part.strip()]
        with connection.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)
        # Confirm the expected tables exist in the configured database.
        with connection.cursor() as cursor:
            cursor.execute(f"USE `{DB_NAME}`")
            cursor.execute("SHOW TABLES")
            existing = {row[0] if not isinstance(row, dict) else next(iter(row.values()))
                        for row in cursor.fetchall()}
        expected = {
            "hospitals", "doctors", "patients", "specialisations",
            "doctor_specialisations", "doctor_hospitals", "doctor_blackouts",
            "doctor_availability", "consultation_types", "appointments", "refund_ledger"
        }
        missing = sorted(expected - existing)
        if missing:
            logger.error("Schema initialization missing tables: %s", missing)
            return response(500, {"message": "Schema executed but some tables are missing", "missingTables": missing})
        logger.info("MediSlot schema initialization completed successfully")
        return response(200, {"message": "Database schema initialized successfully", "tables": sorted(existing)})
    except Exception:
        logger.exception("Database schema initialization failed")
        return response(500, {"message": "Database schema initialization failed; check CloudWatch logs"})
    finally:
        if connection:
            connection.close()


def parse_body(event):
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        import base64
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        raise ValueError("Request body must be valid JSON")
    if not isinstance(data, dict):
        raise ValueError("Request body must be a JSON object")
    return data


def password_error(password):
    if not isinstance(password, str) or len(password) < 12 or len(password) > 72:
        return "Password must be 12–72 characters long"
    if not re.search(r"[a-z]", password):
        return "Password must contain a lowercase letter"
    if not re.search(r"[A-Z]", password):
        return "Password must contain an uppercase letter"
    if not re.search(r"\d", password):
        return "Password must contain a digit"
    if not re.search(r"[^A-Za-z0-9]", password):
        return "Password must contain a special character"
    return None


def hash_password(password):
    # bcrypt only processes up to 72 bytes; reject longer passwords above.
    return bcrypt.hashpw(
        password.encode("utf-8"), bcrypt.gensalt(rounds=12)
    ).decode("utf-8")


def verify_password(password, stored_hash):
    try:
        return bcrypt.checkpw(password.encode("utf-8"), stored_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def sha256_hex(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def new_access_token(user_id, role):
    now = datetime.now(timezone.utc)
    jti = str(uuid.uuid4())
    payload = {
        "sub": str(user_id),
        "profileId": str(user_id),
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=ACCESS_TOKEN_MINUTES)).timestamp()),
        "jti": jti,
        "iss": "medislot",
        "aud": "medislot-api",
    }
    token = jwt.encode(payload, JWT_SECRET, algorithm="HS256")
    return token, jti, payload["exp"]


def create_refresh_token(user_id, role):
    raw_token = secrets.token_urlsafe(48)
    now = int(time.time())
    expiry = now + REFRESH_TOKEN_DAYS * 24 * 60 * 60
    token_table.put_item(
        Item={
            "tokenId": "REFRESH#" + sha256_hex(raw_token),
            "tokenType": "REFRESH",
            "profileId": str(user_id),
            "role": role,
            "createdAt": now,
            "expiresAt": expiry,
            "revoked": False,
        },
        ConditionExpression="attribute_not_exists(tokenId)",
    )
    return raw_token


def bearer_token(event):
    headers = event.get("headers") or {}
    auth = headers.get("Authorization") or headers.get("authorization") or ""
    parts = auth.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1]


def decode_access_token(token):
    return jwt.decode(
        token,
        JWT_SECRET,
        algorithms=["HS256"],
        issuer="medislot",
        audience="medislot-api",
        options={"require": ["exp", "iat", "sub", "role", "jti"]},
    )


def register(event, role=None):
    try:
        data = parse_body(event)
    except ValueError as exc:
        return response(400, {"message": str(exc)})

    role_value = role if role is not None else data.get("role", "")
    if not isinstance(role_value, str):
        return response(400, {"message": "role must be DOCTOR or PATIENT"})
    role = role_value.strip().lower()
    if role not in {"doctor", "patient"}:
        return response(400, {"message": "role must be DOCTOR or PATIENT"})

    email = data.get("email")
    password = data.get("password")
    first_name = data.get("first_name", data.get("firstName"))
    last_name = data.get("last_name", data.get("lastName", ""))

    if not isinstance(email, str) or not email.strip() or len(email.strip()) > 255:
        return response(400, {"message": "A valid email is required"})
    email = email.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return response(400, {"message": "Email format is invalid"})
    if not isinstance(first_name, str) or not first_name.strip() or len(first_name.strip()) > 100:
        return response(400, {"message": "first_name is required (max 100 characters)"})
    if not isinstance(last_name, str) or len(last_name) > 100:
        return response(400, {"message": "last_name must be a string (max 100 characters)"})
    problem = password_error(password)
    if problem:
        return response(400, {"message": problem})

    password_hash = hash_password(password)
    first_name = first_name.strip()
    last_name = last_name.strip() or None

    try:
        with connect_db() as conn:
            with conn.cursor() as cur:
                if role == "doctor":
                    # Both doctor email and registration number must be unique.
                    registration_number = data.get("registration_number")
                    if not isinstance(registration_number, str) or not registration_number.strip():
                        return response(400, {
                            "message": "Doctor registration number is required."
                        })
                    registration_number = registration_number.strip()
                    if len(registration_number) > 100:
                        return response(400, {
                            "message": "Doctor registration number must not exceed 100 characters."
                        })

                    cur.execute(
                        "SELECT doctor_id FROM doctors WHERE email = %s LIMIT 1",
                        (email,)
                    )
                    if cur.fetchone():
                        return response(409, {
                            "message": "A doctor account with this email already exists. "
                        })

                    cur.execute(
                        "SELECT doctor_id FROM doctors WHERE registration_number = %s LIMIT 1",
                        (registration_number,)
                    )
                    if cur.fetchone():
                        return response(409, {
                            "message": "This doctor registration number is already registered."
                        })

                    try:
                        experience = int(data.get("experience_years") or 0)
                    except (TypeError, ValueError):
                        return response(400, {"message": "experience_years must be an integer"})
                    if not 0 <= experience <= 100:
                        return response(400, {"message": "experience_years must be between 0 and 100"})

                    cur.execute(
                        """
                        INSERT INTO doctors
                          (registration_number, first_name, last_name, email, password_hash,
                           qualification, experience_years, profile_status)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, 'ACTIVE')
                        """,
                        (
                            registration_number,
                            first_name,
                            last_name or "",
                            email,
                            password_hash,
                            data.get("qualification"),
                            experience
                        ),
                    )
                    profile_id = int(cur.lastrowid)

                    hospital_id = data.get("hospital_id")
                    if hospital_id not in (None, ""):
                        if not str(hospital_id).isdigit() or int(hospital_id) <= 0:
                            conn.rollback()
                            return response(400, {"message": "hospital_id must be a positive integer"})
                        cur.execute(
                            "INSERT INTO doctor_hospitals (doctor_id, hospital_id, is_active) VALUES (%s, %s, TRUE)",
                            (profile_id, int(hospital_id)),
                        )

                    account_status = "ACTIVE"
                else:
                    cur.execute(
                        "SELECT patient_id FROM patients WHERE email = %s LIMIT 1",
                        (email,)
                    )
                    if cur.fetchone():
                        return response(409, {
                            "message": "A patient account with this email already exists. Please log in or use another email."
                        })

                    gender = data.get("gender")
                    if isinstance(gender, str):
                        gender = gender.strip().lower()
                    if gender not in (None, "", "male", "female", "other", "prefer_not_to_say"):
                        return response(400, {"message": "Invalid gender"})
                    dob = data.get("date_of_birth")
                    if dob:
                        try:
                            parsed_dob = datetime.strptime(str(dob), "%Y-%m-%d").date()
                            if parsed_dob > datetime.utcnow().date():
                                return response(400, {"message": "date_of_birth cannot be in the future"})
                        except ValueError:
                            return response(400, {"message": "date_of_birth must be YYYY-MM-DD"})
                    phone = data.get("phone")
                    if phone is not None:
                        if not isinstance(phone, str) or len(phone.strip()) > 20:
                            return response(400, {"message": "phone must be a string up to 20 characters"})
                        phone = phone.strip() or None
                    cur.execute(
                        """
                        INSERT INTO patients
                          (first_name, last_name, email, password_hash, date_of_birth, gender, phone)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """,
                        (first_name, last_name, email, password_hash, dob or None, gender or None, phone),
                    )
                    profile_id = int(cur.lastrowid)
                    account_status = "ACTIVE"

            conn.commit()

        return response(201, {
            "message": f"{role.capitalize()} registration successful",
            "profileId": profile_id,
            "role": role,
            "status": account_status,
        })
    except pymysql.err.IntegrityError as exc:
        code_number = exc.args[0] if exc.args else None
        logger.info("Registration conflict: %s", exc.args)
        if code_number == 1062:
            duplicate_detail = str(exc.args[1] if len(exc.args) > 1 else "").lower()
            if role == "doctor" and "registration" in duplicate_detail:
                return response(409, {
                    "message": "This doctor registration number is already registered. "
                })
            if role == "patient":
                message = "A patient account with this email already exists. Please log in or use another email."
            else:
                message = "A doctor account with this email already exists. Please log in or use another email."
            return response(409, {"message": message})
        if code_number == 1452:
            return response(400, {"message": "The selected hospital does not exist. Please provide a valid hospital_id."})
        return response(400, {"message": "Registration details conflict with an existing record. Please check the information and try again."})
    except Exception:
        logger.exception("Registration failed")
        return response(500, {"message": "Internal server error"})


def login(event):
    try:
        data = parse_body(event)
    except ValueError as exc:
        return response(400, {"message": str(exc)})

    email = data.get("email")
    password = data.get("password")

    if not isinstance(email, str) or not email.strip():
        return response(400, {"message": "Valid email is required"})

    if not isinstance(password, str) or not password:
        return response(400, {"message": "Password is required"})

    email = email.strip().lower()

    try:
        with connect_db() as conn:
            with conn.cursor() as cur:

                # Find the account in the patients table.
                cur.execute(
                    """
                    SELECT patient_id AS profile_id,
                           email,
                           password_hash,
                           account_status
                    FROM patients
                    WHERE email = %s
                    LIMIT 1
                    """,
                    (email,)
                )
                patient = cur.fetchone()

                # Find the account in the doctors table.
                cur.execute(
                    """
                    SELECT doctor_id AS profile_id,
                           email,
                           password_hash,
                           profile_status
                    FROM doctors
                    WHERE email = %s
                    LIMIT 1
                    """,
                    (email,)
                )
                doctor = cur.fetchone()

        # An email must belong to only one account type.
        if patient and doctor:
            return response(
                409,
                {"message": "Email exists in both patient and doctor accounts"}
            )

        if not patient and not doctor:
            return response(401, {"message": "Invalid email or password"})

        if patient:
            account = patient
            role = "patient"
            if str(account.get("account_status", "ACTIVE")).upper() != "ACTIVE":
                return response(403, {"message": "Patient account is deactivated"})
        else:
            account = doctor
            role = "doctor"

        # Verify the password.
        if not verify_password(password, account["password_hash"]):
            return response(401, {"message": "Invalid email or password"})

        # Doctors must be approved before login.
        if role == "doctor":
            if str(account["profile_status"]).upper() != "ACTIVE":
                return response(
                    403,
                    {"message": "Doctor profile is pending approval or inactive"}
                )

        # Use the numeric patient_id or doctor_id.
        profile_id = int(account["profile_id"])

        # Generate access and refresh tokens.
        access_token, _, _ = new_access_token(profile_id, role)
        refresh_token = create_refresh_token(profile_id, role)

        return response(
            200,
            {
                "message": "Login successful",
                "accessToken": access_token,
                "tokenType": "Bearer",
                "expiresIn": ACCESS_TOKEN_MINUTES * 60,
                "refreshToken": refresh_token,
                "profileId": profile_id,
                "role": role
            }
        )

    except Exception:
        logger.exception("Login failed")
        return response(500, {"message": "Internal server error"})

def refresh_session(event):
    """
    Validate the existing refresh token and issue a new access token.
    The refresh token itself is NOT rotated; it remains valid until its
    original seven-day expiresAt time or until logout/revocation.
    """
    try:
        data = parse_body(event)
    except ValueError as exc:
        return response(400, {"message": str(exc)})

    refresh_token = data.get("refreshToken")
    if not isinstance(refresh_token, str) or not refresh_token.strip():
        return response(400, {"message": "refreshToken is required"})

    refresh_token = refresh_token.strip()
    token_id = "REFRESH#" + sha256_hex(refresh_token)

    try:
        result = token_table.get_item(
            Key={"tokenId": token_id},
            ConsistentRead=True
        )
        item = result.get("Item")

        if not item or item.get("tokenType") != "REFRESH":
            return response(401, {"message": "Invalid refresh token"})

        now = int(time.time())
        expires_at = int(item.get("expiresAt", 0))

        if item.get("revoked") is True or expires_at <= now:
            return response(401, {"message": "Refresh token is expired or revoked"})

        profile_id = item.get("profileId")
        role = str(item.get("role", "")).strip().lower()

        if not profile_id or role not in {"patient", "doctor"}:
            logger.error("Refresh token has invalid profile or role")
            return response(401, {"message": "Invalid refresh token"})

        # Confirm that the account still exists and that a doctor is still active.
        with connect_db() as conn:
            with conn.cursor() as cur:
                if role == "patient":
                    cur.execute(
                        "SELECT patient_id, account_status FROM patients WHERE patient_id = %s LIMIT 1",
                        (int(profile_id),)
                    )
                else:
                    cur.execute(
                        """
                        SELECT doctor_id, profile_status
                        FROM doctors
                        WHERE doctor_id = %s
                        LIMIT 1
                        """,
                        (int(profile_id),)
                    )

                account = cur.fetchone()

        if not account:
            return response(401, {"message": "Account no longer exists"})

        if role == "doctor" and str(account.get("profile_status", "")).upper() != "ACTIVE":
            return response(403, {"message": "Doctor profile is pending approval or inactive"})
        if role == "patient" and str(account.get("account_status", "ACTIVE")).upper() != "ACTIVE":
            return response(403, {"message": "Patient account is deactivated"})

        access_token, _, _ = new_access_token(int(profile_id), role)

        return response(200, {
            "message": "Access token refreshed successfully",
            "accessToken": access_token,
            "tokenType": "Bearer",
            "expiresIn": ACCESS_TOKEN_MINUTES * 60,
            "refreshToken": refresh_token,
            "profileId": int(profile_id),
            "role": role,
            "refreshTokenExpiresAt": expires_at
        })

    except Exception:
        logger.exception("Refresh token validation failed")
        return response(500, {"message": "Internal server error"})


def logout(event):
    """
    Revoke a refresh token only when it belongs to the same account as the
    access token supplied in the Authorization: Bearer header.
    """
    try:
        data = parse_body(event)
    except ValueError as exc:
        return response(400, {"message": str(exc)})

    refresh_token = data.get("refreshToken")
    if not isinstance(refresh_token, str) or not refresh_token.strip():
        return response(400, {"message": "refreshToken is required"})

    access_token = bearer_token(event)
    if not access_token:
        return response(401, {"message": "A valid Bearer access token is required"})

    try:
        # Verify access-token signature, issuer, audience and expiry.
        try:
            access_claims = decode_access_token(access_token)
        except jwt.ExpiredSignatureError:
            return response(401, {"message": "Access token has expired"})
        except jwt.InvalidTokenError:
            return response(401, {"message": "Invalid access token"})

        access_profile_id = str(
            access_claims.get("profileId") or access_claims.get("sub") or ""
        )
        access_role = str(access_claims.get("role", "")).strip().lower()

        if not access_profile_id or access_role not in {"patient", "doctor"}:
            return response(401, {"message": "Invalid access token"})

        token_id = "REFRESH#" + sha256_hex(refresh_token.strip())
        result = token_table.get_item(
            Key={"tokenId": token_id},
            ConsistentRead=True
        )
        item = result.get("Item")

        if not item or item.get("tokenType") != "REFRESH":
            return response(401, {"message": "Invalid refresh token"})

        now = int(time.time())
        expires_at = int(item.get("expiresAt", 0))
        if expires_at <= now:
            return response(401, {"message": "Refresh token has expired"})

        refresh_profile_id = str(item.get("profileId", ""))
        refresh_role = str(item.get("role", "")).strip().lower()

        # Critical binding check: access and refresh tokens must belong to
        # exactly the same profile ID AND role.
        if (
            not refresh_profile_id
            or refresh_role not in {"patient", "doctor"}
            or access_profile_id != refresh_profile_id
            or access_role != refresh_role
        ):
            return response(401, {
                "message": "Access token and refresh token do not belong to the same account"
            })

        if item.get("revoked") is True:
            return response(200, {"message": "Logout successful"})

        token_table.update_item(
            Key={"tokenId": token_id},
            UpdateExpression="SET revoked = :revoked, revokedAt = :revoked_at",
            ConditionExpression=(
                "attribute_exists(tokenId) AND tokenType = :token_type "
                "AND profileId = :profile_id AND #role = :role "
                "AND expiresAt > :now AND revoked = :not_revoked"
            ),
            ExpressionAttributeNames={"#role": "role"},
            ExpressionAttributeValues={
                ":revoked": True,
                ":revoked_at": now,
                ":token_type": "REFRESH",
                ":profile_id": refresh_profile_id,
                ":role": refresh_role,
                ":now": now,
                ":not_revoked": False
            }
        )

        return response(200, {"message": "Logout successful"})

    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code")
        if error_code == "ConditionalCheckFailedException":
            return response(401, {"message": "Refresh token is invalid, expired, or already revoked"})
        logger.exception("Logout DynamoDB operation failed")
        return response(500, {"message": "Internal server error"})
    except Exception:
        logger.exception("Logout failed")
        return response(500, {"message": "Internal server error"})


def lambda_handler(event, context):
    """API Gateway entry point for MediSlot authentication."""
    try:
        event = event or {}
        request_context = event.get("requestContext") or {}

        method = (
            event.get("httpMethod")
            or (request_context.get("http") or {}).get("method")
            or ""
        ).upper()

        # For REST API proxy resources, event["resource"] can be the
        # template /auth/{proxy+}; use the actual request path instead.
        path = (
            event.get("path")
            or event.get("rawPath")
            or request_context.get("path")
            or ""
        )
        path = str(path).split("?", 1)[0].rstrip("/").lower()

        # Recover the actual route from pathParameters when necessary.
        if not path or "{proxy+}" in path or path.endswith("/{proxy}"):
            path_parameters = event.get("pathParameters") or {}
            proxy_value = path_parameters.get("proxy")
            if proxy_value:
                path = "/auth/" + str(proxy_value).strip("/").lower()
            elif not path:
                path = str(event.get("resource") or "").split("?", 1)[0].rstrip("/").lower()

        # Remove API Gateway stage prefix, e.g. /dev/auth/refresh.
        stage = str(request_context.get("stage") or "").strip("/").lower()
        if stage and (path == "/" + stage or path.startswith("/" + stage + "/")):
            path = path[len(stage) + 1:]

        path = "/" + path.lstrip("/")

        request_id = getattr(context, "aws_request_id", "unknown")
        logger.info(
            "AUTH REQUEST START request_id=%s method=%s path=%s resource=%s",
            request_id, method, path, event.get("resource")
        )

        if method == "OPTIONS":
            return {
                "statusCode": 200,
                "headers": {
                    "Content-Type": "application/json",
                    "Cache-Control": "no-store",
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Headers": (
                        "Content-Type,Authorization,X-DB-Init-Token"
                    ),
                    "Access-Control-Allow-Methods": "OPTIONS,POST"
                },
                "body": ""
            }

        if method == "POST" and path == "/auth/register":
            return register(event)

        if method == "POST" and path == "/auth/login":
            return login(event)

        if method == "POST" and path == "/auth/refresh":
            return refresh_session(event)

        if method == "POST" and path == "/auth/logout":
            return logout(event)

        if method == "POST" and path == "/auth/init-db":
            return initialize_database(event)

        logger.warning("Authentication route not found request_id=%s method=%s path=%s", request_id, method, path)
        return response(404, {
            "message": "Route not found",
            "method": method,
            "path": path,
            "resource": event.get("resource")
        })

    except Exception:
        logger.exception("Authentication Lambda handler failed request_id=%s", getattr(context, "aws_request_id", "unknown"))
        return response(500, {"message": "Internal server error"})

