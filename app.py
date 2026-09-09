from __future__ import annotations

import json
import sys
from urllib.parse import urlparse

from flask import Flask, jsonify, render_template, request

import core

app = Flask(__name__, template_folder="templates")


def json_response(payload, status=200):
    response = jsonify(payload)
    response.status_code = status
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.after_request
def add_headers(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")


@app.route("/api/health", methods=["GET"])
def health():
    return json_response({
        "ok": True,
        "service": "yeobaek",
        "version": core.APP_VERSION,
        "deployment": "vercel",
        "tourapi_configured": bool(core.KTO_SERVICE_KEY),
        "kma_configured": bool(core.KMA_SERVICE_KEY),
        "kakao_transit_configured": bool(core.KAKAO_REST_API_KEY),
        "ai_configured": bool(core.GEMINI_API_KEY),
    })


@app.route("/api/geocode", methods=["GET"])
def geocode():
    try:
        text=(request.args.get("q") or "").strip()
        if len(text)<2:
            return json_response({"ok":False,"message":"장소명을 2글자 이상 입력해줘."},400)

        lat=lon=None
        try:
            if request.args.get("lat") is not None:
                lat=float(request.args.get("lat"))
            if request.args.get("lon") is not None:
                lon=float(request.args.get("lon"))
        except Exception:
            lat=lon=None

        results,provider,errors=core.geocode_search(text,lat,lon)
        if results:
            return json_response({
                "ok":True,
                "results":results,
                "provider":provider,
                "fallback":provider=="Photon",
            })
        if errors:
            return json_response({
                "ok":False,
                "message":"장소 검색이 잠시 원활하지 않아. 지도에서 직접 선택하거나 잠시 후 다시 시도해줘."
            },502)
        return json_response({"ok":True,"results":[]})
    except Exception as e:
        sys.stderr.write(f"[Yeobaek/Vercel] geocode: {e}\n")
        return json_response({"ok":False,"message":"장소 검색 중 문제가 생겼어."},500)


@app.route("/api/reverse", methods=["GET"])
def reverse():
    try:
        lat=core.safe_float(request.args.get("lat"),-90,90)
        lon=core.safe_float(request.args.get("lon"),-180,180)
        return json_response({"ok":True,**core.reverse_geocode(lat,lon)})
    except Exception:
        return json_response({"ok":False,"message":"현재 위치를 확인하지 못했어."},400)


@app.route("/api/weather", methods=["GET"])
def weather():
    try:
        lat=core.safe_float(request.args.get("lat"),-90,90)
        lon=core.safe_float(request.args.get("lon"),-180,180)
        horizon=int(request.args.get("minutes") or 360)
        return json_response(core.weather_at(lat,lon,horizon))
    except Exception as e:
        sys.stderr.write(f"[Yeobaek/Vercel] weather: {e}\n")
        return json_response({"ok":False,"message":"날씨 정보를 불러오지 못했어."},500)


@app.route("/api/kma-status", methods=["GET"])
def kma_status():
    return json_response({"ok":True,**core.kma_status()})


@app.route("/api/crowd", methods=["GET"])
def crowd():
    area=(request.args.get("area") or "").strip()
    return json_response(core.crowd_payload(area))


@app.route("/api/tourapi-status", methods=["GET"])
def tourapi_status():
    return json_response({"ok":True,**core.tourapi_status()})


@app.route("/api/credential-status", methods=["GET"])
def credential_status():
    return json_response({"ok":True,**core.credential_status()})


@app.route("/api/kto-extra-status", methods=["GET"])
def kto_extra_status():
    return json_response({"ok":True,**core.kto_extra_service_state()})


@app.route("/api/transit-status", methods=["GET"])
def transit_status():
    return json_response({"ok":True,**core.kakao_transit_status()})


@app.route("/api/ai-status", methods=["GET"])
def ai_status():
    return json_response({"ok":True,**core.ai_status()})


@app.route("/api/place-details", methods=["GET"])
def place_details():
    try:
        content_id=(request.args.get("contentid") or "").strip()
        content_type=(request.args.get("contenttypeid") or "").strip()
        name=(request.args.get("name") or "장소").strip() or "장소"
        image_url=request.args.get("image_url")
        address=request.args.get("address")
        include_pet=(request.args.get("pet") or "0")=="1"
        return json_response(
            core.kto_place_detail_payload(
                content_id,
                content_type,
                name,
                image_url,
                address,
                include_pet,
            )
        )
    except Exception as e:
        sys.stderr.write(f"[Yeobaek/Vercel] place-details: {e}\n")
        return json_response({"ok":False,"message":"상세정보를 불러오지 못했어."},500)


@app.route("/api/recommend", methods=["POST"])
def recommend():
    try:
        body=request.get_json(silent=True) or {}
        try:
            return json_response(core.build_recommendations(body))
        except (KeyError,ValueError,TypeError):
            return json_response({
                "ok":False,
                "message":"현재 위치, 다음 약속 위치, 남은 시간을 확인해줘."
            },400)
    except Exception as e:
        sys.stderr.write(f"[Yeobaek/Vercel] recommend: {e}\n")
        return json_response({"ok":False,"message":"추천을 만드는 중 문제가 생겼어."},500)


@app.route("/api/route", methods=["POST"])
def route():
    try:
        body=request.get_json(silent=True) or {}
        points=body.get("points") or []
        if not 2<=len(points)<=4:
            return json_response({"ok":False,"message":"경로 좌표가 올바르지 않아."},400)
        return json_response(core.route_payload(points,body.get("transport","도보")))
    except Exception as e:
        sys.stderr.write(f"[Yeobaek/Vercel] route: {e}\n")
        return json_response({"ok":False,"message":"경로를 계산하지 못했어."},500)


@app.route("/api/ai-chat", methods=["POST"])
def ai_chat():
    if not core.GEMINI_API_KEY:
        return json_response({
            "ok":False,
            "message":"AI 연결 설정이 필요해."
        },503)

    try:
        body=request.get_json(silent=True) or {}
        try:
            return json_response(core.ai_chat_payload(body))
        except ValueError as e:
            return json_response({"ok":False,"message":str(e)},400)
        except Exception as e:
            sys.stderr.write(f"[Yeobaek AI/Vercel] {e}\n")
            return json_response({
                "ok":False,
                "message":"AI가 잠시 응답하지 못했어. 잠시 후 다시 시도해줘."
            },502)
    except Exception as e:
        sys.stderr.write(f"[Yeobaek/Vercel] ai-chat: {e}\n")
        return json_response({"ok":False,"message":"AI 요청을 처리하지 못했어."},500)


@app.errorhandler(404)
def not_found(_):
    return json_response({"ok":False,"message":"없는 주소야."},404)


# Vercel auto-detects this top-level Flask WSGI application.
