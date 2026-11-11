from app.formatters import time_input, time_display, money, full_day_range
from app.blueprints.doctor import password_errors


def test_formatters():
    assert time_input("9:00:00") == "09:00"
    assert time_display("9:00:00") == "9:00 AM"
    assert money("1250") == "₹1,250.00"
    assert full_day_range("2026-11-05", "2026-11-06") == (
        "2026-11-05 00:00:00", "2026-11-06 23:59:59"
    )


def test_password():
    assert not password_errors("StrongPassword123!")


def test_landing_and_patient(client):
    assert client.get("/").status_code == 200
    response = client.get("/patient/login")
    assert response.status_code == 200
    assert b"Patient sign in" in response.data
    assert client.get("/patient/register").status_code == 200


def test_patient_login_dashboard_and_search(client):
    response = client.post("/patient/login", data={
        "email": "patient@example.com", "password": "StrongPassword123!"
    })
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/patient/dashboard")
    assert client.get("/patient/dashboard").status_code == 200
    assert client.get("/patient/doctors?name=Test&city=Hyderabad").status_code == 200


def test_patient_cannot_access_doctor_workspace(client):
    with client.session_transaction() as sess:
        sess["role"] = "patient"
        sess["accessToken"] = "patient-token"
    response = client.get("/doctor/dashboard")
    assert response.status_code == 302


def test_login_and_protected_pages(client):
    response = client.post(
        "/doctor/login",
        data={"email": "doctor@example.com", "password": "StrongPassword123!"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    for path in [
        "/doctor/dashboard", "/doctor/profile", "/doctor/consultation-types",
        "/doctor/availability", "/doctor/blackouts", "/doctor/schedule",
        "/doctor/appointments",
    ]:
        assert client.get(path).status_code == 200


def test_anonymous_guard_redirects_to_landing(client):
    response = client.get("/doctor/profile")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/")


def test_patient_session_cannot_access_doctor(client):
    with client.session_transaction() as sess:
        sess["role"] = "patient"
        sess["accessToken"] = "patient-token"
    response = client.get("/doctor/profile")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/")


def test_schedule_week_and_dashboard_render(client):
    client.post(
        "/doctor/login",
        data={"email": "doctor@example.com", "password": "StrongPassword123!"},
    )
    assert client.get("/doctor/dashboard").status_code == 200
    assert client.get("/doctor/schedule?view=week").status_code == 200

