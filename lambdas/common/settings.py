"""Business thresholds. Read from SSM Parameter Store when USE_SSM_SETTINGS=1
(deployed Lambdas), otherwise from MEDISLOT_<NAME> environment variables, and
finally from the documented default. Nothing business-related is hardcoded at
the call sites."""

import os

_cache = {}


def _env_name(name):
    return "MEDISLOT_" + name.upper().replace("/", "_").replace("-", "_")


def get_setting(name, default, cast=int):
    override = os.environ.get(_env_name(name))
    if override is not None:
        return cast(override)
    if os.environ.get("USE_SSM_SETTINGS") == "1":
        if name not in _cache:
            import boto3
            environment = os.environ.get("ENVIRONMENT", "dev")
            client = boto3.client("ssm", region_name=os.environ.get("AWS_REGION", "ap-southeast-2"))
            value = client.get_parameter(Name=f"/medislot/{environment}/{name}")["Parameter"]["Value"]
            _cache[name] = cast(value)
        return _cache[name]
    return default
