"""Deterministic local mock. Never used unless MOCK_API=1."""
from datetime import date, timedelta
from werkzeug.datastructures import Headers

DOCTOR={"doctor_id":7,"registration_number":"REG-2026-001","first_name":"Aarav","last_name":"Sharma","qualification":"MBBS, MD","experience_years":8,"profile_photo_url":"","profile_status":"ACTIVE","created_at":"2026-01-01 09:00:00","updated_at":"2026-11-01 09:00:00"}
TYPES=[{"consultation_type_id":1,"type_name":"General Consultation","duration_minutes":15,"capacity":1,"description":"Standard consultation","is_active":1,"consultation_fee":"500.00"},{"consultation_type_id":2,"type_name":"Health Camp Session","duration_minutes":60,"capacity":5,"description":"Group health camp","is_active":1,"consultation_fee":"250.00"}]
AVAIL=[{"availability_id":1,"doctor_id":7,"hospital_id":1,"consultation_type_id":1,"day_of_week":1,"start_time":"9:00:00","end_time":"12:00:00","timezone":"Asia/Kolkata","effective_from":date.today().isoformat(),"effective_until":None,"is_active":1}]
BLACK=[]
def dispatch(method,path,params=None,json=None,headers=None):
    params=params or {}; data=json or {}; status=200; out={"message":"OK"}
    if path=="/auth/login" and method=="POST":
        if data.get("email")=="doctor@medislot.local" and data.get("password")=="DoctorPass123!": out={"accessToken":"mock-access","tokenType":"Bearer","expiresIn":900,"refreshToken":"mock-refresh","profileId":7,"role":"doctor"}
        else: status=401; out={"message":"Invalid email or password"}
    elif path=="/auth/register" and method=="POST": status=201; out={"message":"Doctor registered","profileId":7,"role":"doctor","status":"PENDING"}
    elif path=="/auth/refresh": out={"accessToken":"mock-access-2","expiresIn":900,"refreshToken":"mock-refresh","profileId":7,"role":"doctor"}
    elif path=="/auth/logout": out={"message":"Logout successful"}
    elif path=="/doctors/profile" and method=="GET": out=DOCTOR.copy()
    elif path=="/doctors/profile" and method=="PUT": DOCTOR.update(data); out={"message":"Profile updated"}
    elif path=="/doctors/photo": out={"uploadUrl":"mock://upload","objectKey":"doctor/7/profile.webp","contentType":data.get("contentType"),"expiresIn":300}
    elif path=="/doctors/consultation-types" and method=="GET": out=TYPES
    elif path=="/doctors/consultation-types" and method=="POST": status=201; out={"consultation_type_id":3}
    elif path.startswith("/doctors/consultation-types/"):
        ident=int(path.rsplit("/",1)[-1]); item=next((x for x in TYPES if x["consultation_type_id"]==ident),None)
        if not item: status=404; out={"message":"Consultation type not found"}
        elif method=="DELETE": item["is_active"]=0; out={"message":"Deactivated"}
        else: item.update(data); out={"message":"Updated"}
    elif path=="/doctors/availability" and method=="GET": out=AVAIL.copy()
    elif path=="/doctors/availability" and method=="POST": status=201; out={"availability_id":2}
    elif path.startswith("/doctors/availability/"):
        ident=int(path.rsplit("/",1)[-1]); item=next((x for x in AVAIL if x["availability_id"]==ident),None)
        if not item: status=404; out={"message":"Availability not found"}
        elif method=="DELETE": AVAIL.remove(item); out={"message":"Deleted"}
        else: item.update(data); out={"message":"Updated"}
    elif path=="/doctors/blackouts" and method=="GET": out=BLACK.copy()
    elif path=="/doctors/blackouts" and method=="POST": status=201; out={"blackout_id":len(BLACK)+1}
    elif path.startswith("/doctors/blackouts/"):
        ident=int(path.rsplit("/",1)[-1]); item=next((x for x in BLACK if x["blackout_id"]==ident),None)
        if not item: status=404; out={"message":"Blackout not found"}
        else: BLACK.remove(item); out={"message":"Deleted"}
    elif path=="/doctors/schedule":
        d=params.get("date",date.today().isoformat()); out={"date":d,"timezone":"Asia/Kolkata","slots":[],"appointments":[]}
    elif path=="/doctors/appointments": status=404; out={"message":"Not implemented"}
    else: status=404; out={"message":"Route not found"}
    return status,out
