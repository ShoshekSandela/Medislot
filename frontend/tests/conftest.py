import pytest
from app import create_app


class FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = str(self._payload)

    def json(self):
        return self._payload


@pytest.fixture
def app(tmp_path, monkeypatch):
    import app.api_client as api

    def fake_request(method, url, **kwargs):
        from urllib.parse import urlparse, parse_qs
        path = urlparse(url).path
        body = kwargs.get("json") or {}
        if path.endswith("/auth/login") and method == "POST":
            role = "patient" if body.get("email") == "patient@example.com" else "doctor"
            return FakeResponse(200, {
                "accessToken": "test-access", "refreshToken": "test-refresh",
                "expiresIn": 900, "profileId": 17 if role == "patient" else 7, "role": role
            })
        if path.endswith("/auth/register") and method == "POST":
            return FakeResponse(201, {"message": "Patient registration successful", "profileId": 17, "role": "patient"})
        if path.endswith("/patients") and method == "GET":
            return FakeResponse(200, {"patient": {"patient_id": 17, "first_name": "Priya", "last_name": "Test", "email": "patient@example.com"}})
        if path.endswith("/patients") and method == "PUT":
            return FakeResponse(200, {"message": "Updated"})
        if path.endswith("/doctors") and method == "GET":
            return FakeResponse(200, {"doctors": [], "pagination": {"page": 1, "page_size": 10, "total": 0, "total_pages": 0}})
        if path.endswith("/doctors/9") and method == "GET":
            return FakeResponse(200, {"doctor_id": 9, "first_name": "Test", "hospitals": [], "specialisations": [], "consultation_types": []})
        if path.endswith("/doctors/9/slots") and method == "GET":
            return FakeResponse(200, {"doctor_id": 9, "slots": [], "total_slots": 0})
        if path.endswith("/auth/refresh"):
            return FakeResponse(200, {
                "accessToken": "test-access-new", "refreshToken": "test-refresh",
                "expiresIn": 900, "profileId": 7, "role": "doctor"
            })
        if path.endswith("/auth/logout"):
            return FakeResponse(200, {"message": "Logout successful"})
        if path.endswith("/doctors/profile") and method == "GET":
            return FakeResponse(200, {
                "doctor_id": 7, "registration_number": "REG-7",
                "first_name": "Aarav", "last_name": "Sharma",
                "qualification": "MBBS", "experience_years": 5,
                "profile_photo_url": "", "profile_status": "ACTIVE"
            })
        if path.endswith("/doctors/profile") and method == "PUT":
            return FakeResponse(200, {"message": "Updated"})
        if path.endswith("/doctors/consultation-types"):
            if method == "GET":
                return FakeResponse(200, [{
                    "consultation_type_id": 1, "type_name": "General Consultation",
                    "duration_minutes": 15, "capacity": 1, "description": "",
                    "is_active": 1, "consultation_fee": "500.00"
                }])
            return FakeResponse(201, {"consultation_type_id": 2})
        if path.endswith("/doctors/availability"):
            if method == "GET":
                return FakeResponse(200, [])
            return FakeResponse(201, {"availability_id": 1})
        if path.endswith("/doctors/blackouts"):
            if method == "GET":
                return FakeResponse(200, [])
            return FakeResponse(201, {"blackout_id": 1})
        if path.endswith("/doctors/schedule"):
            date_value = parse_qs(urlparse(url).query).get("date", ["2026-11-05"])[0]
            return FakeResponse(200, {
                "date": date_value, "timezone": "Asia/Kolkata",
                "slots": [], "appointments": []
            })
        return FakeResponse(404, {"message": "Route not found"})

    monkeypatch.setattr(api.requests, "request", fake_request)
    return create_app({
        "TESTING": True,
        "SECRET_KEY": "test-secret",
        "API_BASE_URL": "https://api.test/dev",
        "APP_ENV": "test",
        "SESSION_DIR": str(tmp_path),
        "WTF_CSRF_ENABLED": False,
    })


@pytest.fixture
def client(app):
    return app.test_client()
