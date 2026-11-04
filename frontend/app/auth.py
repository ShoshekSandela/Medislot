from functools import wraps
from flask import session, redirect, url_for, flash, abort

def login_session(payload):
    session.clear(); session["accessToken"]=payload["accessToken"]; session["refreshToken"]=payload["refreshToken"]
    session["profileId"]=payload.get("profileId"); session["role"]=payload.get("role")
    from time import time
    session["expiresAt"]=int(time())+int(payload.get("expiresIn",900))

def doctor_required(fn):
    @wraps(fn)
    def wrapped(*args,**kwargs):
        if not session.get("accessToken") or session.get("role")!="doctor":
            flash("Please sign in with a doctor account.","warning"); return redirect(url_for("landing.index"))
        return fn(*args,**kwargs)
    return wrapped

def clear_session(): session.clear()
