from app.api_client import APIError, request, _safe_json


class Response:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self.payload = payload
        self.text = text

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        if self.payload is None:
            raise ValueError("No JSON body")
        return self.payload


def test_bearer_header_and_correlation_id(app, monkeypatch):
    import app.api_client as api
    seen = {}

    def fake(method, url, **kwargs):
        seen.update(kwargs)
        return Response(200, {"ok": True})

    monkeypatch.setattr(api.requests, "request", fake)
    with app.test_request_context("/doctor/profile"):
        from flask import session
        session["accessToken"] = "secret-token"
        result = request("GET", "/doctors/profile")
    assert result == {"ok": True}
    assert seen["headers"]["Authorization"] == "Bearer secret-token"
    assert seen["headers"]["X-Correlation-Id"]


def test_refresh_once_and_retry(app, monkeypatch):
    import app.api_client as api
    calls = []

    def fake(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if url.endswith("/doctors/profile") and len([x for x in calls if x[1].endswith("/doctors/profile")]) == 1:
            return Response(401, {"message": "Expired"})
        if url.endswith("/auth/refresh"):
            return Response(200, {"accessToken": "new-access", "refreshToken": "new-refresh", "expiresIn": 900})
        return Response(200, {"doctor_id": 7})

    monkeypatch.setattr(api.requests, "request", fake)
    with app.test_request_context("/doctor/profile"):
        from flask import session
        session["accessToken"] = "old-access"
        session["refreshToken"] = "refresh"
        result = request("GET", "/doctors/profile")
        assert session["accessToken"] == "new-access"
    assert result == {"doctor_id": 7}
    assert len(calls) == 3


def test_json_parser_handles_html_and_empty_body():
    html = Response(502, None, "<html>Bad gateway</html>")
    empty = Response(502, None, "")
    assert "Bad gateway" in _safe_json(html)["message"]
    assert _safe_json(empty)["message"] == "Empty response from API"


def test_disabled_endpoint_does_not_call_backend(app, monkeypatch):
    import app.api_client as api
    def should_not_call(*args, **kwargs):
        raise AssertionError("Disabled endpoint must not call API")
    monkeypatch.setattr(api.requests, "request", should_not_call)
    with app.test_request_context("/doctor/appointments"):
        from flask import session
        session["accessToken"] = "test"
        try:
            request("GET", "/doctors/appointments")
        except APIError as exc:
            assert exc.status == 501
        else:
            raise AssertionError("Expected disabled endpoint error")
