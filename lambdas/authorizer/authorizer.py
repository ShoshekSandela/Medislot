import os
import logging

import boto3
import jwt
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REGION = os.environ.get("AWS_REGION", "us-east-1")
JWT_SECRET_PARAMETER = os.environ.get(
    "JWT_SECRET_PARAMETER",
    "/medislot/dev/jwt/signing-secret",
)
AUTH_TOKENS_TABLE = os.environ.get(
    "AUTH_TOKENS_TABLE",
    "medislot-dev-auth-tokens",
)

# Load the JWT secret during Lambda initialization (cold start).
ssm = boto3.client("ssm", region_name=REGION)
JWT_SECRET = ssm.get_parameter(
    Name=JWT_SECRET_PARAMETER,
    WithDecryption=True,
)["Parameter"]["Value"]

dynamodb = boto3.resource("dynamodb", region_name=REGION)
token_table = dynamodb.Table(AUTH_TOKENS_TABLE)


def lambda_handler(event, context):
    # This handler expects API Gateway REST API TOKEN-authorizer events.
    auth_header = event.get("authorizationToken", "")
    parts = auth_header.split()

    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise Exception("Unauthorized")

    token = parts[1]

    try:
        claims = jwt.decode(
            token,
            JWT_SECRET,
            algorithms=["HS256"],
            issuer="medislot",
            audience="medislot-api",
            options={"require": ["exp", "iat", "sub", "role", "jti"]},
        )

        user_id = str(claims["sub"])
        role = str(claims["role"])
        token_id = str(claims["jti"])

        if role not in {"patient", "doctor", "admin"}:
            raise jwt.InvalidTokenError("Unsupported role")

        # Logout writes DENYLIST#<jti>; strong consistency gives immediate checks.
        result = token_table.get_item(
            Key={"tokenId": "DENYLIST#" + token_id},
            ConsistentRead=True,
        )
        revoked_record = result.get("Item")
        if revoked_record and revoked_record.get("revoked") is True:
            raise jwt.InvalidTokenError("Token revoked")

        method_arn = event.get("methodArn")
        if not method_arn:
            logger.error("Missing methodArn; configure a REST API TOKEN authorizer")
            raise Exception("Unauthorized")

        return {
            "principalId": user_id,
            "policyDocument": {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Action": "execute-api:Invoke",
                        "Effect": "Allow",
                        "Resource": method_arn,
                    }
                ],
            },
            "context": {
                "userId": user_id,
                "role": role,
                "jti": token_id,
            },
        }

    except jwt.PyJWTError:
        logger.info("Rejected invalid, expired, or revoked JWT")
        raise Exception("Unauthorized")
    except ClientError:
        # Fail closed if revocation status cannot be checked.
        logger.exception("Could not check token revocation in DynamoDB")
        raise Exception("Unauthorized")
