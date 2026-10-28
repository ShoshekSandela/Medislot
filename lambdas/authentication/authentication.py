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

            "users", "hospitals", "doctors", "patients",

            "specialisations", "doctor_specialisations",

            "consultation_types", "appointments"

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

        "sub": user_id,

        "userId": user_id,

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

            "userId": user_id,

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
 
 
def register(event, role):

    data = parse_body(event)

    email = data.get("email")

    password = data.get("password")

    first_name = data.get("first_name", data.get("firstName"))

    last_name = data.get("last_name", data.get("lastName", ""))
 
    if not isinstance(email, str) or not email.strip() or len(email) > 255:

        return response(400, {"message": "A valid email is required"})

    email = email.strip().lower()

    if not isinstance(first_name, str) or not first_name.strip() or len(first_name) > 100:

        return response(400, {"message": "first_name is required (max 100 characters)"})

    if not isinstance(last_name, str) or len(last_name) > 100:

        return response(400, {"message": "last_name must be at most 100 characters"})

    problem = password_error(password)

    if problem:

        return response(400, {"message": problem})
 
    # Validate role-specific fields BEFORE touching the database

    registration_number = None

    experience_years = 0

    consultation_fee = 0

    gender = None

    if role == "doctor":

        registration_number = data.get("registration_number")

        if not isinstance(registration_number, str) or not registration_number.strip():

            return response(400, {"message": "registration_number is required"})

        registration_number = registration_number.strip()

        if len(registration_number) > 100:

            return response(400, {"message": "registration_number must be at most 100 characters"})

        try:

            experience_years = int(data.get("experience_years") or 0)

            consultation_fee = float(data.get("consultation_fee") or 0)

        except (TypeError, ValueError):

            return response(400, {"message": "experience_years and consultation_fee must be numbers"})

        if experience_years < 0 or consultation_fee < 0:

            return response(400, {"message": "experience_years and consultation_fee cannot be negative"})

    else:

        gender = data.get("gender")

        allowed_gender = {"male", "female", "other", "prefer_not_to_say", None, ""}

        if gender not in allowed_gender:

            return response(400, {"message": "Invalid gender"})
 
    user_id = str(uuid.uuid4())

    profile_id = str(uuid.uuid4())

    password_hash = hash_password(password)
 
    try:

        with connect_db() as conn:

            with conn.cursor() as cur:

                cur.execute(

                    "INSERT INTO users (user_id, email, password_hash, role, status) "

                    "VALUES (%s, %s, %s, %s, 'active')",

                    (user_id, email, password_hash, role),

                )

                if role == "patient":

                    phone = data.get("phone")

                    dob = data.get("date_of_birth")

                    cur.execute(

                        "INSERT INTO patients "

                        "(patient_id, user_id, first_name, last_name, phone, date_of_birth, gender) "

                        "VALUES (%s, %s, %s, %s, %s, %s, %s)",

                        (profile_id, user_id, first_name.strip(), last_name.strip() or None,

                         phone, dob or None, gender or None),

                    )

                else:

                    # Doctor registration is pending until the profile is reviewed.

                    # hospital_id is no longer collected at registration.

                    cur.execute(

                        "INSERT INTO doctors "

                        "(doctor_id, user_id, registration_number, first_name, "

                        "last_name, qualification, experience_years, consultation_fee, profile_status) "

                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending')",

                        (profile_id, user_id, registration_number,

                         first_name.strip(), last_name.strip() or "", data.get("qualification"),

                         experience_years, consultation_fee),

                    )

            conn.commit()

        return response(201, {

            "message": f"{role.capitalize()} registration successful",

            "userId": user_id,

            "profileId": profile_id,

            "status": "pending" if role == "doctor" else "active",

        })

    except pymysql.err.IntegrityError as exc:

        code = exc.args[0] if exc.args else None

        logger.info("Registration conflict: %s", exc.args)

        if code == 1062:

            return response(409, {"message": "Email or registration number already exists"})

        return response(400, {"message": "Invalid or conflicting registration data"})

    except Exception:

        logger.exception("Registration failed")

        return response(500, {"message": "Internal server error"})
 
 
def login(event):

    data = parse_body(event)

    email = data.get("email")

    password = data.get("password")

    if not isinstance(email, str) or not isinstance(password, str):

        return response(400, {"message": "email and password are required"})
 
    try:

        with connect_db() as conn:

            with conn.cursor() as cur:

                cur.execute(

                    "SELECT user_id, email, password_hash, role, status "

                    "FROM users WHERE email = %s",

                    (email.strip().lower(),),

                )

                user = cur.fetchone()

        if not user or not verify_password(password, user["password_hash"]):

            return response(401, {"message": "Invalid email or password"})

        if user["status"] != "active":

            return response(403, {"message": "Account is not active"})

        if user["role"] == "doctor":

            with connect_db() as conn:

                with conn.cursor() as cur:

                    cur.execute(

                        "SELECT profile_status FROM doctors WHERE user_id = %s",

                        (user["user_id"],),

                    )

                    profile = cur.fetchone()

            if not profile or profile["profile_status"] != "active":

                return response(403, {"message": "Doctor profile is awaiting approval or inactive"})
 
        access, jti, access_exp = new_access_token(user["user_id"], user["role"])

        refresh = create_refresh_token(user["user_id"], user["role"])

        return response(200, {

            "accessToken": access,

            "tokenType": "Bearer",

            "expiresIn": ACCESS_TOKEN_MINUTES * 60,

            "refreshToken": refresh,

        })

    except Exception:

        logger.exception("Login failed")

        return response(500, {"message": "Internal server error"})
 
 
def refresh(event):

    """

    Validate the existing refresh token and issue a new access token.
 
    Refresh tokens are stable for their original 7-day lifetime:

    - Do not revoke or rotate the refresh token during refresh.

    - Do not update expiresAt during refresh.

    - Return the exact same refresh token supplied by the client.

    """

    data = parse_body(event)

    raw = data.get("refreshToken")

    if not isinstance(raw, str) or not raw:

        return response(400, {"message": "refreshToken is required"})
 
    token_id = "REFRESH#" + sha256_hex(raw)

    now = int(time.time())
 
    try:

        result = token_table.get_item(

            Key={"tokenId": token_id},

            ConsistentRead=True,

        )

        record = result.get("Item")
 
        if (

            not record

            or record.get("tokenType") != "REFRESH"

            or record.get("revoked") is True

            or int(record.get("expiresAt", 0)) <= now

        ):

            return response(401, {"message": "Invalid or expired refresh token"})
 
        # Issue a fresh short-lived access token, while keeping the original

        # refresh token and its original expiresAt unchanged.

        access, jti, access_exp = new_access_token(

            str(record["userId"]),

            str(record["role"]),

        )
 
        return response(200, {

            "accessToken": access,

            "tokenType": "Bearer",

            "expiresIn": ACCESS_TOKEN_MINUTES * 60,

            "refreshToken": raw,

            "refreshTokenExpiresAt": int(record["expiresAt"]),

        })
 
    except ClientError:

        logger.exception("Refresh token lookup failed")

        return response(500, {"message": "Internal server error"})

    except Exception:

        logger.exception("Refresh failed")

        return response(500, {"message": "Internal server error"})
 
 
def logout(event):

    token = bearer_token(event)

    if not token:

        return response(401, {"message": "Bearer access token is required"})

    try:

        claims = decode_access_token(token)

        now = int(time.time())

        jti = claims["jti"]

        expiry = int(claims["exp"])

        # Immediate revocation is enforced by the Authorizer's strongly consistent lookup.

        token_table.put_item(

            Item={

                "tokenId": "DENYLIST#" + jti,

                "tokenType": "DENYLIST",

                "userId": claims["sub"],

                "createdAt": now,

                "expiresAt": expiry,

                "revoked": True,

            },

        )

        data = parse_body(event)

        refresh_raw = data.get("refreshToken")

        if isinstance(refresh_raw, str) and refresh_raw:

            refresh_id = "REFRESH#" + sha256_hex(refresh_raw)

            token_table.update_item(

                Key={"tokenId": refresh_id},

                UpdateExpression="SET revoked = :true, revokedAt = :now",

                ConditionExpression="attribute_exists(tokenId)",

                ExpressionAttributeValues={":true": True, ":now": now},

            )

        return response(200, {"message": "Logged out"})

    except jwt.ExpiredSignatureError:

        return response(401, {"message": "Access token has expired"})

    except jwt.InvalidTokenError:

        return response(401, {"message": "Invalid access token"})

    except ClientError:

        logger.exception("Logout token revocation failed")

        return response(500, {"message": "Internal server error"})

    except Exception:

        logger.exception("Logout failed")

        return response(500, {"message": "Internal server error"})
 
 
def lambda_handler(event, context):

    path = (event.get("path") or event.get("rawPath") or "").rstrip("/")

    method = event.get("httpMethod") or event.get("requestContext", {}).get("http", {}).get("method", "")

    method = method.upper()

    # Support the current Postman paths and the earlier route names.

    # API Gateway REST API Lambda proxy events include the full path, without the stage.

    routes = {

        ("/auth/initialize-database", "POST"): lambda: initialize_database(event),

        ("/auth/patients/register", "POST"): lambda: register(event, "patient"),

        ("/auth/doctors/register", "POST"): lambda: register(event, "doctor"),

        ("/auth/register/patient", "POST"): lambda: register(event, "patient"),

        ("/auth/register/doctor", "POST"): lambda: register(event, "doctor"),

        ("/auth/login", "POST"): lambda: login(event),

        ("/auth/refresh", "POST"): lambda: refresh(event),

        ("/auth/logout", "POST"): lambda: logout(event),

    }

    handler = routes.get((path, method))

    if not handler:

        return response(404, {"message": "Route not found"})

    return handler()
 #