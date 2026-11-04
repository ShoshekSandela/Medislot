import logging, os
from flask import Flask, render_template, request, redirect, url_for, flash, session
from flask_session import Session
from flask_wtf.csrf import CSRFProtect
from .config import Config, FEATURE_FLAGS
from datetime import date,timedelta,datetime
from zoneinfo import ZoneInfo
csrf=CSRFProtect()
def _ist_now(): return datetime.now(ZoneInfo("Asia/Kolkata"))
def _shift(value,days): return (date.fromisoformat(value)+timedelta(days=days)).isoformat()
def create_app(test_config=None):
 app=Flask(__name__,static_url_path="/static"); app.config.from_object(Config)
 if test_config: app.config.update(test_config)
 if not app.config.get("SECRET_KEY"): raise RuntimeError("SECRET_KEY must be set in environment")
 os.makedirs(app.config["SESSION_DIR"],exist_ok=True); app.config["SESSION_FILE_DIR"]=app.config["SESSION_DIR"]
 Session(app); csrf.init_app(app); handler=logging.StreamHandler(); handler.setFormatter(logging.Formatter('%(message)s')); app.logger.handlers=[handler]; app.logger.setLevel(logging.INFO)
 from .blueprints.landing import bp as landing
 from .blueprints.doctor import bp as doctor
 from .blueprints.patient import bp as patient
 app.register_blueprint(landing); app.register_blueprint(doctor); app.register_blueprint(patient)
 from .formatters import time_input,time_display,money,as_bool
 @app.template_filter('to_date_shift')
 def shift_filter(value,days): return _shift(value,int(days))
 @app.context_processor
 def common(): return {"static_base":app.config["STATIC_BASE_URL"],"doctor_name":session.get("doctor_name","Doctor"),"profile_status":session.get("profile_status","—"),"feature_flags":FEATURE_FLAGS,"time_input":time_input,"time_display":time_display,"money":money,"as_bool":as_bool,"today":_ist_now().date().isoformat(),"now_ist":_ist_now().strftime("%Y-%m-%d %H:%M:%S")}
 @app.errorhandler(403)
 def forbidden(e): return render_template("error.html",code=403,message="Doctor access required"),403
 @app.errorhandler(401)
 def unauthorized(e): flash("Session expired, please sign in again.","warning"); return redirect(url_for("doctor.login"))
 @app.errorhandler(404)
 def not_found(e): return render_template("error.html",code=404,message="Page not found"),404
 @app.errorhandler(413)
 def too_large(e): flash("Photo must be 5 MB or smaller.","error"); return redirect(request.referrer or url_for("doctor.profile"))
 @app.after_request
 def headers(resp): resp.headers.setdefault("X-Content-Type-Options","nosniff"); resp.headers.setdefault("Referrer-Policy","strict-origin-when-cross-origin"); resp.headers.setdefault("X-Frame-Options","DENY"); return resp
 return app
