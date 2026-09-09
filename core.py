from __future__ import annotations

import json
import html as html_lib
import math
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlparse, unquote
from urllib.request import Request, urlopen
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None


ROOT = Path(__file__).resolve().parent
APP_VERSION = "vercel-1.0.2"
HTTP_TIMEOUT = 12

CONTACT = os.getenv("YEOBAEK_CONTACT", "").strip()
APP_USER_AGENT = os.getenv("YEOBAEK_USER_AGENT", "Yeobaek-Vercel/1.0.2").strip()
if CONTACT:
    APP_USER_AGENT += f" ({CONTACT})"

_cache: dict[str, tuple[float, Any]] = {}
_cache_lock = threading.Lock()


LOCAL_CONFIG_PATH = ROOT / "config.local.json"
CONFIG_SOURCE = "environment" if any(os.getenv(k) for k in ("KTO_SERVICE_KEY","KMA_SERVICE_KEY","KAKAO_REST_API_KEY","GEMINI_API_KEY")) else "local"


def load_local_config():
    """Load API keys only from this version folder.

    This intentionally matches the v20 behavior:
    <current Yeobaek folder>/config.local.json
    """
    if not LOCAL_CONFIG_PATH.exists():
        return {}
    try:
        data = json.loads(LOCAL_CONFIG_PATH.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}



def clean_env_secret(value, name=None):
    """Normalize values pasted into Vercel Environment Variables.

    Accepts:
      raw-value
      "raw-value"
      'raw-value'
      NAME=raw-value

    This never logs or returns the secret to the browser.
    """
    text=str(value or "").replace("\r","").strip()
    if not text:
        return ""

    if name and text.startswith(name+"="):
        text=text[len(name)+1:].strip()

    # A copied .env line may be quoted.
    if len(text)>=2 and text[0]==text[-1] and text[0] in {"'", '"'}:
        text=text[1:-1].strip()

    return text


def normalize_service_key(value):
    text=clean_env_secret(value)
    if not text or "여기에" in text or "PASTE" in text.upper():
        return ""
    # Keep one decoded canonical form; urllib.urlencode performs one encoding.
    try:
        return unquote(text)
    except Exception:
        return text


_LOCAL_CONFIG = load_local_config()
KTO_SERVICE_KEY_RAW = clean_env_secret(
    os.getenv("KTO_SERVICE_KEY")
    or _LOCAL_CONFIG.get("kto_service_key")
    or "",
    "KTO_SERVICE_KEY",
)
KTO_SERVICE_KEY = normalize_service_key(KTO_SERVICE_KEY_RAW)
KTO_KEY_MODE = clean_env_secret(
    os.getenv("KTO_KEY_MODE")
    or _LOCAL_CONFIG.get("kto_key_mode")
    or "auto",
    "KTO_KEY_MODE",
)
KTO_BASE_URL = "https://apis.data.go.kr/B551011/KorService2"
KTO_MOBILE_OS = "ETC"
KTO_MOBILE_APP = "Yeobaek"

# 기상청 단기예보 조회서비스. 공공데이터포털의 일반 인증키는 계정 단위로
# 동일하게 보이는 경우가 많아서 별도 키가 없으면 KTO 키를 재사용해 시도한다.
KMA_SERVICE_KEY_RAW = clean_env_secret(
    os.getenv("KMA_SERVICE_KEY")
    or _LOCAL_CONFIG.get("kma_service_key")
    or KTO_SERVICE_KEY_RAW
    or "",
    "KMA_SERVICE_KEY",
)
KMA_SERVICE_KEY = normalize_service_key(KMA_SERVICE_KEY_RAW)
KMA_KEY_MODE = str(
    os.getenv("KMA_KEY_MODE")
    or _LOCAL_CONFIG.get("kma_key_mode")
    or "decoded_https"
).strip()
KMA_BASE_URL = "https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0"
KMA_BASE_URL_HTTP = "http://apis.data.go.kr/1360000/VilageFcstInfoService_2.0"

KAKAO_REST_API_KEY = clean_env_secret(
    os.getenv("KAKAO_REST_API_KEY")
    or _LOCAL_CONFIG.get("kakao_rest_api_key")
    or "",
    "KAKAO_REST_API_KEY",
)
KAKAO_TRANSIT_URL = "https://dapi.kakao.com/v2/routing/publictraffic"

# Free-tier conversational AI (Google Gemini Developer API).
GEMINI_API_KEY = clean_env_secret(
    os.getenv("GEMINI_API_KEY")
    or _LOCAL_CONFIG.get("gemini_api_key")
    or "",
    "GEMINI_API_KEY",
)
_GEMINI_MODEL_CONFIGURED = str(
    os.getenv("GEMINI_MODEL")
    or _LOCAL_CONFIG.get("gemini_model")
    or "gemini-3.6-flash"
).strip()

# Auto-migrate the previous default for users who copy an old config.local.json.
GEMINI_MODEL = (
    "gemini-3.6-flash"
    if _GEMINI_MODEL_CONFIGURED == "gemini-2.5-flash"
    else (_GEMINI_MODEL_CONFIGURED or "gemini-3.6-flash")
)
GEMINI_INTERACTIONS_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"


def kma_status():
    return {"configured":bool(KMA_SERVICE_KEY),"source":"기상청 단기예보 조회서비스","base_url":KMA_BASE_URL,"requires_own_key":False,"reuses_tourapi_key":not bool(_LOCAL_CONFIG.get("kma_service_key")),"key_mode":KMA_KEY_MODE}


def kakao_transit_status():
    return {
        "configured": bool(KAKAO_REST_API_KEY),
        "source": "카카오맵 대중교통 경로 REST API",
    }


def credential_status():
    return {
        "kto_configured": bool(KTO_SERVICE_KEY),
        "kma_configured": bool(KMA_SERVICE_KEY),
        "kakao_configured": bool(KAKAO_REST_API_KEY),
        "ai_configured": bool(GEMINI_API_KEY),
    }


def tourapi_status():
    return {
        "configured": bool(KTO_SERVICE_KEY),
        "source": "한국관광공사 국문 관광정보 서비스 (KorService2)",
        "base_url": KTO_BASE_URL,
        "config_source": CONFIG_SOURCE,
    }


def ai_status():
    return {
        "configured": bool(GEMINI_API_KEY),
        "provider": "Google Gemini",
        "model": GEMINI_MODEL,
        "free_tier_supported": True,
    }


def _gemini_extract_text(data):
    """Extract model text from Gemini Interactions API output."""
    if not isinstance(data, dict):
        return ""

    texts = []
    for step in data.get("steps") or []:
        if not isinstance(step, dict) or step.get("type") != "model_output":
            continue
        for part in step.get("content") or []:
            if not isinstance(part, dict) or part.get("type") != "text":
                continue
            text = clean_text(part.get("text"))
            if text:
                texts.append(text)

    # Compatibility fallback if Google returns a legacy-shaped wrapper.
    if not texts:
        for candidate in data.get("candidates") or []:
            content = (candidate or {}).get("content") or {}
            for part in content.get("parts") or []:
                text = clean_text((part or {}).get("text"))
                if text:
                    texts.append(text)

    return "\n".join(texts).strip()


def gemini_generate_text(message, history=None, context=None, max_output_tokens=700):
    if not GEMINI_API_KEY:
        raise RuntimeError("AI 연결 설정이 필요해.")

    message = clean_text(message)
    if not message:
        raise ValueError("AI에게 물어볼 내용을 입력해줘.")
    message = message[:1800]

    safe_context = context if isinstance(context, dict) else {}
    context_text = json.dumps(
        safe_context,
        ensure_ascii=False,
        separators=(",", ":"),
    )[:18000]

    history_lines = []
    for item in (history or [])[-8:]:
        if not isinstance(item, dict):
            continue
        role = "사용자" if item.get("role") == "user" else "AI"
        text = clean_text(item.get("text") or item.get("content"))[:1200]
        if text:
            history_lines.append(f"{role}: {text}")

    prompt = ["[현재 여행 데이터]", context_text]
    if history_lines:
        prompt += ["", "[최근 대화]"] + history_lines
    prompt += ["", "[사용자 질문]", message]

    system_text = (
        "너는 여행 서비스 '여백(旅Back)'의 AI Travel Agent야. "
        "사용자가 제공한 현재 여행 상황과 여백 추천엔진의 검증된 데이터만 근거로 답해. "
        "장소, 영업시간, 이동시간, 혼잡도, 반려동물 가능 여부를 절대 지어내지 마. "
        "정보가 없으면 '확인 필요'라고 말해. "
        "추천 후보가 제공되면 후보 안에서 비교하고, 다음 약속에 늦지 않는 것을 최우선으로 해. "
        "답변은 자연스러운 한국어로 짧고 실용적으로 2~6문장 정도 작성해. "
        "필요하면 시간·날씨·취향·혼잡 근거를 함께 설명해."
    )

    payload = {
        "model": GEMINI_MODEL,
        "input": "\n".join(prompt),
        "system_instruction": system_text,
        "store": False,
        "generation_config": {
            "max_output_tokens": int(max(128, min(int(max_output_tokens), 1000))),
            "thinking_level": "low",
        },
    }

    req = Request(
        GEMINI_INTERACTIONS_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": GEMINI_API_KEY,
            "User-Agent": APP_USER_AGENT,
        },
        method="POST",
    )

    try:
        with urlopen(req, timeout=HTTP_TIMEOUT + 8) as response:
            data = json.loads(response.read().decode("utf-8"))
    except HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8", "replace"))
            msg = ((detail.get("error") or {}).get("message") or "AI API 요청 실패")
        except Exception:
            msg = "AI API 요청 실패"
        raise RuntimeError(msg) from e
    except URLError as e:
        raise RuntimeError("AI 서비스에 연결할 수 없어.") from e

    if data.get("status") == "failed":
        raise RuntimeError("AI 응답 생성에 실패했어.")

    text = _gemini_extract_text(data)
    if not text:
        raise RuntimeError("AI 응답을 받지 못했어.")
    return text[:5000]


def ai_chat_payload(body):
    message=clean_text((body or {}).get("message"))
    history=(body or {}).get("history") or []
    context=(body or {}).get("context") or {}
    text=gemini_generate_text(message,history,context)
    return {
        "ok":True,
        "reply":text,
        "provider":"Google Gemini",
        "model":GEMINI_MODEL,
    }


def cache_get(key: str):
    with _cache_lock:
        item = _cache.get(key)
        if not item:
            return None
        expires, value = item
        if expires < time.time():
            _cache.pop(key, None)
            return None
        return value


def cache_set(key: str, value: Any, ttl: int):
    with _cache_lock:
        _cache[key] = (time.time() + ttl, value)


class RateLimiter:
    def __init__(self, interval: float):
        self.interval = interval
        self.lock = threading.Lock()
        self.last = 0.0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            delay = self.interval - (now - self.last)
            if delay > 0:
                time.sleep(delay)
            self.last = time.monotonic()


nominatim_limiter = RateLimiter(1.05)
photon_limiter = RateLimiter(0.35)
kto_limiter = RateLimiter(0.12)
kma_limiter = RateLimiter(0.12)
kakao_limiter = RateLimiter(0.08)
osrm_limiter = RateLimiter(0.8)


def safe_float(value, lo=None, hi=None):
    x = float(value)
    if lo is not None and x < lo:
        raise ValueError
    if hi is not None and x > hi:
        raise ValueError
    return x


def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2-lat1), math.radians(lon2-lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*r*math.asin(math.sqrt(a))


def approx_minutes(lat1, lon1, lat2, lon2, mode="walk"):
    straight = haversine_m(lat1, lon1, lat2, lon2)
    network = straight * (1.22 if mode == "walk" else 1.30)
    if mode == "car":
        return max(3.0, network / 350.0)
    if mode == "transit":
        return max(8.0, network / 260.0 + 6.0)
    return max(1.0, network / 75.0)


def fetch_bytes(url: str, params=None, method="GET", data=None, headers=None, timeout=HTTP_TIMEOUT):
    if params:
        url += ("&" if "?" in url else "?") + urlencode(params, doseq=True)
    hdrs = {
        "User-Agent": APP_USER_AGENT,
        "Accept": "application/json, text/plain;q=0.9, */*;q=0.2",
        "Accept-Language": "ko,en;q=0.8",
        "Connection": "close",
    }
    if headers:
        hdrs.update(headers)
    payload = data
    if isinstance(data, dict):
        payload = urlencode(data).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
    req = Request(url, data=payload, headers=hdrs, method=method)
    try:
        with urlopen(req, timeout=timeout) as res:
            return res.read(), res.headers.get("content-type", "")
    except HTTPError as e:
        body = e.read(200).decode("utf-8", "replace").replace("\n", " ")
        raise RuntimeError(f"HTTP {e.code}: {body[:160]}") from e
    except URLError as e:
        raise RuntimeError(f"network: {e.reason}") from e


def fetch_json(url: str, params=None, method="GET", data=None, headers=None, timeout=HTTP_TIMEOUT):
    raw, _ = fetch_bytes(url, params=params, method=method, data=data, headers=headers, timeout=timeout)
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as e:
        snippet = raw[:160].decode("utf-8", "replace").replace("\n", " ")
        raise RuntimeError(f"JSON이 아닌 응답: {snippet}") from e


def nominatim(path, params, ttl=86400):
    key = "nom:" + path + ":" + urlencode(params)
    if (v := cache_get(key)) is not None:
        return v
    nominatim_limiter.wait()
    v = fetch_json("https://nominatim.openstreetmap.org" + path, params=params)
    cache_set(key, v, ttl)
    return v


def photon(path, params, ttl=86400):
    key = "pho:" + path + ":" + urlencode(params)
    if (v := cache_get(key)) is not None:
        return v
    photon_limiter.wait()
    v = fetch_json("https://photon.komoot.io" + path, params=params)
    cache_set(key, v, ttl)
    return v


def photon_display(props):
    values = []
    for k in ("name", "street", "district", "city", "county", "state", "postcode", "country"):
        x = props.get(k)
        if x and str(x) not in values:
            values.append(str(x))
    return ", ".join(values) or "장소 정보"


def compact_address(parts):
    out = []
    for value in parts:
        if not value:
            continue
        text = str(value).strip()
        if not text or text in {"대한민국", "Republic of Korea", "South Korea"}:
            continue
        if text not in out:
            out.append(text)
    return " ".join(out)


def nominatim_short_address(address):
    return compact_address([
        address.get("province") or address.get("state"),
        address.get("city") or address.get("town") or address.get("county"),
        address.get("borough") or address.get("city_district"),
        address.get("suburb") or address.get("quarter") or address.get("neighbourhood"),
        address.get("road") or address.get("pedestrian"),
        address.get("house_number"),
    ])


def photon_short_address(props):
    return compact_address([
        props.get("state"),
        props.get("city") or props.get("county"),
        props.get("district"),
        props.get("street"),
        props.get("housenumber"),
    ])


def geocode_search(q, bias_lat=None, bias_lon=None):
    errors = []
    try:
        data = nominatim("/search", {
            "q": q, "format": "jsonv2", "limit": 6, "countrycodes": "kr",
            "addressdetails": 1, "accept-language": "ko"
        })
        out = []
        if isinstance(data, list):
            for item in data[:6]:
                try:
                    addr = item.get("address") or {}
                    out.append({
                        "name": item.get("name") or item.get("display_name", "").split(",")[0],
                        "display_name": item.get("display_name", ""),
                        "short_address": nominatim_short_address(addr) or item.get("name") or item.get("display_name", "").split(",")[0],
                        "lat": float(item["lat"]), "lon": float(item["lon"]),
                        "type": item.get("type", ""), "provider": "Nominatim"
                    })
                except Exception:
                    pass
        if out:
            return out, "Nominatim", errors
    except Exception as e:
        errors.append("Nominatim: " + str(e))

    try:
        params = {"q": q, "limit": 8, "lang": "ko", "bbox": "124.5,33.0,132.0,38.9"}
        if bias_lat is not None and bias_lon is not None:
            params.update({"lat": bias_lat, "lon": bias_lon, "zoom": 12})
        data = photon("/api", params)
        out = []
        for f in (data.get("features") or []):
            p = f.get("properties") or {}
            cc = str(p.get("countrycode") or "").upper()
            if cc and cc not in {"KR", "KOR"}:
                continue
            coords = (f.get("geometry") or {}).get("coordinates") or []
            if len(coords) < 2:
                continue
            out.append({
                "name": str(p.get("name") or p.get("street") or p.get("city") or q),
                "display_name": photon_display(p),
                "short_address": photon_short_address(p) or photon_display(p),
                "lat": float(coords[1]), "lon": float(coords[0]),
                "type": p.get("osm_value") or "", "provider": "Photon"
            })
        return out[:6], "Photon", errors
    except Exception as e:
        errors.append("Photon: " + str(e))
        return [], None, errors


def reverse_geocode(lat, lon):
    errors = []
    try:
        data = nominatim("/reverse", {
            "lat": f"{lat:.6f}", "lon": f"{lon:.6f}", "format": "jsonv2",
            "zoom": 18, "addressdetails": 1, "accept-language": "ko"
        }, ttl=21600)
        if isinstance(data, dict):
            a = data.get("address") or {}
            name = (data.get("name") or a.get("suburb") or a.get("quarter") or
                    a.get("neighbourhood") or a.get("road") or a.get("city") or "현재 위치")
            short_address = nominatim_short_address(a) or name
            return {"name": name, "display_name": data.get("display_name") or name,
                    "short_address": short_address,
                    "lat": lat, "lon": lon, "provider": "Nominatim"}
    except Exception as e:
        errors.append("Nominatim: " + str(e))
    try:
        data = photon("/reverse", {"lat": f"{lat:.6f}", "lon": f"{lon:.6f}", "limit": 1, "lang": "ko"}, ttl=21600)
        fs = data.get("features") or []
        if fs:
            p = fs[0].get("properties") or {}
            return {"name": str(p.get("name") or p.get("street") or p.get("district") or p.get("city") or "현재 위치"),
                    "display_name": photon_display(p),
                    "short_address": photon_short_address(p) or photon_display(p),
                    "lat": lat, "lon": lon, "provider": "Photon", "fallback": True}
    except Exception as e:
        errors.append("Photon: " + str(e))
    return {"name": "지도에서 선택한 위치", "display_name": "주소를 불러오지 못했지만 이 좌표는 사용할 수 있어.",
            "short_address": "주소 확인 불가",
            "lat": lat, "lon": lon, "provider": "좌표", "fallback": True, "detail": " | ".join(errors)}


KMA_SKY = {1:"맑음",3:"구름 많음",4:"흐림"}
KMA_PTY = {0:"없음",1:"비",2:"비/눈",3:"눈",4:"소나기",5:"빗방울",6:"빗방울/눈날림",7:"눈날림"}


def kma_grid(lat, lon):
    """기상청 5km 격자 변환 (Lambert Conformal Conic)."""
    RE=6371.00877; GRID=5.0; SLAT1=30.0; SLAT2=60.0; OLON=126.0; OLAT=38.0; XO=43; YO=136
    DEGRAD=math.pi/180.0
    re=RE/GRID; slat1=SLAT1*DEGRAD; slat2=SLAT2*DEGRAD; olon=OLON*DEGRAD; olat=OLAT*DEGRAD
    sn=math.tan(math.pi*0.25+slat2*0.5)/math.tan(math.pi*0.25+slat1*0.5)
    sn=math.log(math.cos(slat1)/math.cos(slat2))/math.log(sn)
    sf=math.tan(math.pi*0.25+slat1*0.5); sf=math.pow(sf,sn)*math.cos(slat1)/sn
    ro=math.tan(math.pi*0.25+olat*0.5); ro=re*sf/math.pow(ro,sn)
    ra=math.tan(math.pi*0.25+lat*DEGRAD*0.5); ra=re*sf/math.pow(ra,sn)
    theta=lon*DEGRAD-olon
    if theta>math.pi: theta-=2.0*math.pi
    if theta<-math.pi: theta+=2.0*math.pi
    theta*=sn
    x=ra*math.sin(theta)+XO
    y=ro-ra*math.cos(theta)+YO
    return int(x+0.5), int(y+0.5)


def _kma_request_url(endpoint, params, mode=None):
    """Build KMA request in the exact key form selected by setup diagnostics.

    Modes:
      decoded_https  - decoded key, urllib encodes exactly once
      encoded_https  - user's encoded key appended verbatim
      decoded_http   - same as decoded_https but official HTTP base
      encoded_http   - encoded key appended verbatim over HTTP
      capital_https  - legacy/case-diagnostic ServiceKey parameter
    """
    mode = mode or KMA_KEY_MODE or "decoded_https"
    base = KMA_BASE_URL_HTTP if mode.endswith("_http") else KMA_BASE_URL
    others = {"dataType":"JSON","numOfRows":1000,"pageNo":1}
    others.update(params or {})

    if mode.startswith("encoded_"):
        raw = KMA_SERVICE_KEY_RAW or ""
        # Encoding key must already contain %xx sequences. If a decoded key
        # was supplied, safely quote it once instead of exposing '+' as space.
        if "%" not in raw:
            raw = quote(KMA_SERVICE_KEY, safe="")
        query = "serviceKey=" + raw
        if others:
            query += "&" + urlencode(others, doseq=True)
        return f"{base}/{endpoint}?{query}"

    key_name = "ServiceKey" if mode == "capital_https" else "serviceKey"
    full = {key_name: KMA_SERVICE_KEY}
    full.update(others)
    return f"{base}/{endpoint}?" + urlencode(full, doseq=True)


def _kma_decode_response(raw):
    text = raw.decode("utf-8","replace")
    try:
        data = json.loads(text)
    except Exception as e:
        msg = re.search(
            r"<(?:returnAuthMsg|errMsg|resultMsg)>(.*?)</(?:returnAuthMsg|errMsg|resultMsg)>",
            text,re.I|re.S
        )
        reason = clean_text(msg.group(1)) if msg else text[:180]
        raise RuntimeError(f"기상청 API 오류: {reason}") from e

    service_error = data.get("OpenAPI_ServiceResponse") if isinstance(data,dict) else None
    if isinstance(service_error,dict):
        cmm = service_error.get("cmmMsgHeader") or {}
        err = cmm.get("errMsg") or cmm.get("returnAuthMsg") or "인증 오류"
        auth = cmm.get("returnAuthMsg")
        code = cmm.get("returnReasonCode")
        detail = str(err)
        if auth and auth not in detail:
            detail += f" / {auth}"
        if code:
            detail += f" ({code})"
        raise RuntimeError(f"기상청 API 인증 오류: {detail}")

    response = data.get("response") or {}
    header = response.get("header") or {}
    code = str(header.get("resultCode") or "")
    # 03 NO_DATA means the key/permission worked; caller may handle no data.
    if code and code not in {"00","03"}:
        raise RuntimeError(
            f"기상청 API 오류: {header.get('resultMsg') or '요청 실패'} ({code})"
        )
    return data


def kma_probe(endpoint, params, mode):
    """One uncached KMA probe. Returns data if authentication reached service."""
    url = _kma_request_url(endpoint, params, mode)
    raw, _ = fetch_bytes(url, params=None, timeout=HTTP_TIMEOUT)
    return _kma_decode_response(raw)



_kma_mode_lock = threading.Lock()
_kma_mode_resolved = False


def resolve_kma_key_mode(probe_params=None):
    """Find a working KMA serviceKey transport without changing the key.

    Uses the SAME data.go.kr account key as TourAPI by default.
    Only the URL representation changes.
    """
    global KMA_KEY_MODE, _kma_mode_resolved

    if not KMA_SERVICE_KEY:
        raise RuntimeError("공공데이터포털 인증키가 설정되지 않았어.")

    with _kma_mode_lock:
        if _kma_mode_resolved:
            return KMA_KEY_MODE

        params = probe_params
        if not params:
            nx, ny = kma_grid(37.5665,126.9780)
            bd, bt = _kma_vilage_base(datetime.now(KST))
            params = {
                "base_date": bd,
                "base_time": bt,
                "nx": nx,
                "ny": ny,
                "numOfRows": 20,
                "pageNo": 1,
            }

        # Stored mode first, then all safe representations.
        modes = []
        for mode in (
            KMA_KEY_MODE,
            "decoded_https",
            "encoded_https",
            "decoded_http",
            "encoded_http",
            "capital_https",
        ):
            if mode and mode not in modes:
                modes.append(mode)

        errors = []
        for mode in modes:
            try:
                kma_probe("getVilageFcst", params, mode)
                KMA_KEY_MODE = mode
                _kma_mode_resolved = True
                return mode
            except Exception as e:
                errors.append(f"{mode}: {e}")

        raise RuntimeError(
            "기상청 키 전달 방식 자동진단 실패: "
            + " | ".join(errors[:5])
        )



def kma_fetch(endpoint, params, ttl=300):
    if not KMA_SERVICE_KEY:
        raise RuntimeError("기상청 단기예보 인증키가 설정되지 않았어.")

    # If setup_weather.bat has not confirmed a mode yet, diagnose using
    # a VALID getVilageFcst request of its own. Do not pass parameters from
    # getUltraSrtNcst/getUltraSrtFcst because their base_time rules differ.
    if not _kma_mode_resolved and not _LOCAL_CONFIG.get("kma_key_mode"):
        resolve_kma_key_mode()

    cache_params = {"mode":KMA_KEY_MODE}
    cache_params.update(params or {})
    key = "kma:" + endpoint + ":" + urlencode(
        sorted((str(k),str(v)) for k,v in cache_params.items())
    )
    if (v:=cache_get(key)) is not None:
        return v

    kma_limiter.wait()
    url = _kma_request_url(endpoint, params, KMA_KEY_MODE)
    raw, _ = fetch_bytes(url, params=None, timeout=HTTP_TIMEOUT)
    data = _kma_decode_response(raw)
    cache_set(key,data,ttl)
    return data


def kma_items(data):
    body=((data or {}).get("response") or {}).get("body") or {}
    items=body.get("items") or {}
    if isinstance(items,dict): items=items.get("item") or []
    if isinstance(items,dict): return [items]
    return items if isinstance(items,list) else []


def _kma_ultra_base(now):
    # 초단기예보: 매시 30분 발표, 약 15분 후 제공. 20분 여유를 둔다.
    d=now-timedelta(minutes=20)
    if d.minute<30:
        d=d-timedelta(hours=1)
    return d.strftime("%Y%m%d"), d.strftime("%H")+"30"


def _kma_ncst_base(now):
    # 초단기실황: 정시 관측, 약 40분 후 제공.
    d=now-timedelta(minutes=45)
    return d.strftime("%Y%m%d"), d.strftime("%H")+"00"


def _kma_ncst_base_candidates(now, count=4):
    first_date, first_time=_kma_ncst_base(now)
    first=datetime.strptime(first_date+first_time,"%Y%m%d%H%M").replace(tzinfo=KST)
    return [
        ((first-timedelta(hours=i)).strftime("%Y%m%d"),
         (first-timedelta(hours=i)).strftime("%H%M"))
        for i in range(max(1,int(count)))
    ]


def _kma_ultra_base_candidates(now, count=4):
    first_date, first_time=_kma_ultra_base(now)
    first=datetime.strptime(first_date+first_time,"%Y%m%d%H%M").replace(tzinfo=KST)
    return [
        ((first-timedelta(hours=i)).strftime("%Y%m%d"),
         (first-timedelta(hours=i)).strftime("%H%M"))
        for i in range(max(1,int(count)))
    ]


def _kma_items_with_base_retry(endpoint, candidates, nx, ny, ttl=300):
    errors=[]
    for bd,bt in candidates:
        try:
            data=kma_fetch(
                endpoint,
                {"base_date":bd,"base_time":bt,"nx":nx,"ny":ny},
                ttl=ttl,
            )
            items=kma_items(data)
            if items:
                return items,bd,bt,errors
            errors.append(f"{bd} {bt}: no data")
        except Exception as e:
            errors.append(f"{bd} {bt}: {e}")
    return [],None,None,errors


def _kma_vilage_base(now):
    # 단기예보 발표시각 02/05/08/11/14/17/20/23시, 약 10분 후 제공.
    d=now-timedelta(minutes=15)
    bases=[2,5,8,11,14,17,20,23]
    eligible=[h for h in bases if h<=d.hour]
    if eligible: h=max(eligible)
    else:
        d=d-timedelta(days=1); h=23
    return d.strftime("%Y%m%d"), f"{h:02d}00"


def _kma_vilage_base_candidates(now, count=5):
    """Newest-to-older valid village forecast publication times.

    Data publication can occasionally lag behind the nominal base time.
    Trying several previous official base slots makes the weather display
    resilient without inventing weather data.
    """
    bases=[2,5,8,11,14,17,20,23]
    cursor=now-timedelta(minutes=20)
    out=[]
    while len(out)<count:
        eligible=[h for h in bases if h<=cursor.hour]
        if eligible:
            h=max(eligible)
            dt=cursor.replace(hour=h,minute=0,second=0,microsecond=0)
        else:
            cursor=cursor-timedelta(days=1)
            dt=cursor.replace(hour=23,minute=0,second=0,microsecond=0)
        pair=(dt.strftime("%Y%m%d"),dt.strftime("%H%M"))
        if pair not in out:
            out.append(pair)
        cursor=dt-timedelta(minutes=1)
    return out


def _kma_village_forecast_with_retry(now,nx,ny):
    errors=[]
    for bd,bt in _kma_vilage_base_candidates(now,5):
        try:
            data=kma_fetch(
                "getVilageFcst",
                {"base_date":bd,"base_time":bt,"nx":nx,"ny":ny},
                ttl=600
            )
            items=kma_items(data)
            if items:
                return _group_kma_fcst(items),bd,bt,errors
            errors.append(f"{bd} {bt}: no data")
        except Exception as e:
            errors.append(f"{bd} {bt}: {e}")
    return {},None,None,errors


def _group_kma_fcst(items):
    grouped={}
    for row in items:
        date=clean_text(row.get("fcstDate")); tm=clean_text(row.get("fcstTime"))
        cat=clean_text(row.get("category")); val=row.get("fcstValue")
        if not (date and tm and cat): continue
        grouped.setdefault((date,tm),{})[cat]=val
    return grouped


def _safe_num(v, default=None):
    try: return float(str(v).replace(",","").strip())
    except Exception: return default


def weather_at(lat, lon, horizon_minutes=360):
    nx,ny=kma_grid(lat,lon)
    horizon=max(60,min(int(horizon_minutes or 360),720))
    key=f"weather-kma:{nx}:{ny}:{horizon//60}"
    if (v:=cache_get(key)) is not None: return v
    if not KMA_SERVICE_KEY:
        return {"ok":False,"provider":"기상청","summary":"기상청 키 필요","rainy":False,"detail":"kma_key_missing"}
    now=datetime.now(KST)
    errors=[]; current={}; forecast={}
    ncst_items,ncst_bd,ncst_bt,ncst_errors=_kma_items_with_base_retry(
        "getUltraSrtNcst",_kma_ncst_base_candidates(now,4),nx,ny,ttl=300
    )
    if ncst_items:
        current={
            clean_text(x.get("category")):x.get("obsrValue")
            for x in ncst_items
        }
    elif ncst_errors:
        errors.append("실황: "+" | ".join(ncst_errors[-2:]))

    ultra_items,ultra_bd,ultra_bt,ultra_errors=_kma_items_with_base_retry(
        "getUltraSrtFcst",_kma_ultra_base_candidates(now,4),nx,ny,ttl=300
    )
    if ultra_items:
        forecast=_group_kma_fcst(ultra_items)
    elif ultra_errors:
        errors.append("초단기: "+" | ".join(ultra_errors[-2:]))
    # 단기예보는 여러 공식 발표시각을 순서대로 재시도한다.
    village,used_bd,used_bt,village_errors=_kma_village_forecast_with_retry(now,nx,ny)
    if village:
        for k,vals in village.items():
            forecast.setdefault(k,{}).update({
                c:v for c,v in vals.items() if c not in forecast.get(k,{})
            })
    elif village_errors:
        errors.append("단기: "+" | ".join(village_errors[-3:]))
    if not current and not forecast:
        return {
            "ok":False,
            "provider":"기상청",
            "summary":None,
            "rainy":False,
            "grid":{"nx":nx,"ny":ny},
            "reason":"weather_unavailable",
            "errors":errors[-4:] if errors else None,
        }
    cutoff=now+timedelta(minutes=horizon)
    hourly=[]
    for (date,tm),vals in sorted(forecast.items()):
        try: dt=datetime.strptime(date+tm,"%Y%m%d%H%M").replace(tzinfo=KST)
        except Exception: continue
        if dt<now-timedelta(minutes=30) or dt>cutoff: continue
        pty=int(_safe_num(vals.get("PTY"),0) or 0); sky=int(_safe_num(vals.get("SKY"),0) or 0)
        pop=_safe_num(vals.get("POP"),None); temp=_safe_num(vals.get("T1H"),None)
        if temp is None: temp=_safe_num(vals.get("TMP"),None)
        hourly.append({"time":dt.strftime("%H:%M"),"datetime":dt.isoformat(),"temperature":temp,"sky":sky,"pty":pty,"pop":pop,"rain":clean_text(vals.get("RN1") or vals.get("PCP")),"summary":KMA_PTY.get(pty) if pty else KMA_SKY.get(sky,"날씨")})
    temp=_safe_num(current.get("T1H"),None)
    if temp is None and hourly: temp=hourly[0].get("temperature")
    current_pty=int(_safe_num(current.get("PTY"),0) or 0)
    max_pop=max([x["pop"] for x in hourly if x.get("pop") is not None] or [0])
    pty_values=[x.get("pty",0) for x in hourly]
    rainy=current_pty>0 or any(x>0 for x in pty_values) or max_pop>=50
    if current_pty>0: summary=KMA_PTY.get(current_pty,"강수")
    elif hourly: summary=hourly[0].get("summary") or "날씨"
    else: summary="날씨 확인"
    v={"ok":True,"provider":"기상청 단기예보","temperature":temp,"humidity":_safe_num(current.get("REH"),None),"wind_speed":_safe_num(current.get("WSD"),None),"summary":summary,"rainy":rainy,"next_rain_probability":round(max_pop),"current_precip_type":KMA_PTY.get(current_pty,"없음"),"hourly":hourly[:12],
       "grid":{"nx":nx,"ny":ny},
       "bases":{
           "nowcast":{"date":ncst_bd,"time":ncst_bt},
           "ultra":{"date":ultra_bd,"time":ultra_bt},
           "village":{"date":used_bd,"time":used_bt},
       },
       "errors":errors or None}
    cache_set(key,v,300); return v


KTO_CATEGORY_MAP = {
    "A02060100": ("museum", "박물관", True, 45),
    "A02060200": ("history", "기념관", True, 40),
    "A02060300": ("arts", "전시관", True, 40),
    "A02060500": ("gallery", "미술관/화랑", True, 45),
    "A02060600": ("theatre", "공연장", True, 50),
    "A05020900": ("cafe", "카페/전통찻집", True, 35),
}


_kto_mode_lock = threading.Lock()
_kto_mode_resolved = False


def _kto_request_url(endpoint, params, mode=None):
    mode=(mode or KTO_KEY_MODE or "auto").strip()
    others={
        "MobileOS":KTO_MOBILE_OS,
        "MobileApp":KTO_MOBILE_APP,
        "_type":"json",
    }
    others.update(params or {})

    if mode=="encoded_https":
        raw=KTO_SERVICE_KEY_RAW
        # If a decoding key was supplied, encode it exactly once.
        if "%" not in raw:
            raw=quote(KTO_SERVICE_KEY,safe="")
        query="serviceKey="+raw
        if others:
            query+="&"+urlencode(others,doseq=True)
        return f"{KTO_BASE_URL}/{endpoint}?{query}"

    full={"serviceKey":KTO_SERVICE_KEY}
    full.update(others)
    return f"{KTO_BASE_URL}/{endpoint}?"+urlencode(full,doseq=True)


def _kto_decode_response(raw):
    text=raw.decode("utf-8","replace")
    try:
        data=json.loads(text)
    except Exception as e:
        auth=re.search(
            r"<(?:returnAuthMsg|errMsg)>(.*?)</(?:returnAuthMsg|errMsg)>",
            text,re.I|re.S
        )
        code=re.search(
            r"<(?:returnReasonCode|resultCode)>(.*?)</(?:returnReasonCode|resultCode)>",
            text,re.I|re.S
        )
        if auth:
            msg=html_lib.unescape(
                re.sub(r"<[^>]+>","",auth.group(1))
            ).strip()
            raise RuntimeError(
                "TourAPI 인증 오류: "+msg+
                (f" ({code.group(1).strip()})" if code else "")
            ) from e
        raise RuntimeError(
            "TourAPI 응답을 읽지 못했어. 인증키와 활용신청 상태를 확인해줘."
        ) from e

    # Gateway auth errors may arrive outside response/header.
    gateway=data.get("OpenAPI_ServiceResponse") if isinstance(data,dict) else None
    if isinstance(gateway,dict):
        cmm=gateway.get("cmmMsgHeader") or {}
        msg=cmm.get("returnAuthMsg") or cmm.get("errMsg") or "인증 오류"
        raise RuntimeError(f"TourAPI 인증 오류: {msg}")

    response=data.get("response") if isinstance(data,dict) else None
    if not isinstance(response,dict):
        raise RuntimeError("TourAPI 응답 형식이 예상과 달라.")

    header=response.get("header") or {}
    result_code=str(header.get("resultCode") or "")
    if result_code and result_code!="0000":
        raise RuntimeError(
            f"TourAPI 오류: {header.get('resultMsg') or '요청 실패'} ({result_code})"
        )
    return data


def kto_probe(endpoint, params, mode):
    url=_kto_request_url(endpoint,params,mode)
    raw,_=fetch_bytes(url,params=None,timeout=HTTP_TIMEOUT)
    return _kto_decode_response(raw)


def resolve_kto_key_mode(probe_params=None):
    global KTO_KEY_MODE,_kto_mode_resolved

    if not KTO_SERVICE_KEY:
        raise RuntimeError("한국관광공사 TourAPI 인증키가 설정되지 않았어.")

    with _kto_mode_lock:
        if _kto_mode_resolved and KTO_KEY_MODE!="auto":
            return KTO_KEY_MODE

        params=probe_params or {
            "mapX":"126.9780",
            "mapY":"37.5665",
            "radius":"1000",
            "arrange":"E",
            "numOfRows":"1",
            "pageNo":"1",
        }

        modes=[]
        for mode in (KTO_KEY_MODE,"decoded_https","encoded_https"):
            if mode and mode!="auto" and mode not in modes:
                modes.append(mode)
        for mode in ("decoded_https","encoded_https"):
            if mode not in modes:
                modes.append(mode)

        errors=[]
        for mode in modes:
            try:
                data=kto_probe("locationBasedList2",params,mode)
                KTO_KEY_MODE=mode
                _kto_mode_resolved=True
                return mode
            except Exception as e:
                errors.append(f"{mode}: {e}")

        raise RuntimeError(
            "TourAPI 연결 실패: "+" | ".join(errors[:2])
        )


def kto_fetch_json(endpoint, params, ttl=600):
    if not KTO_SERVICE_KEY:
        raise RuntimeError("한국관광공사 TourAPI 인증키가 아직 설정되지 않았어.")

    if KTO_KEY_MODE=="auto" or not _kto_mode_resolved:
        resolve_kto_key_mode()

    key="kto:"+endpoint+":"+urlencode(
        sorted((str(k),str(v)) for k,v in (params or {}).items())
    )+":"+KTO_KEY_MODE
    if (v:=cache_get(key)) is not None:
        return v

    kto_limiter.wait()
    url=_kto_request_url(endpoint,params,KTO_KEY_MODE)
    raw,_=fetch_bytes(url,params=None,timeout=HTTP_TIMEOUT)
    data=_kto_decode_response(raw)
    cache_set(key,data,ttl)
    return data


def kto_items(data):
    try:
        body = (data.get("response") or {}).get("body") or {}
        items = body.get("items") or {}
        if isinstance(items, dict):
            items = items.get("item") or []
        if isinstance(items, dict):
            return [items]
        return items if isinstance(items, list) else []
    except Exception:
        return []


KTO_GATEWAY_ROOT = "https://apis.data.go.kr/B551011"

# Separate KTO OpenAPI products may require a separate "활용신청".
# If an optional service rejects the key, disable it for this server session so
# one missing approval never slows or breaks the main recommendation flow.
_aux_disabled: dict[str, str] = {}
_aux_lock = threading.Lock()


def _disable_aux(service, reason):
    with _aux_lock:
        _aux_disabled[service] = str(reason)[:240]


def kto_aux_fetch_json(service, operation, params=None, ttl=1800):
    if not KTO_SERVICE_KEY:
        raise RuntimeError("한국관광공사 인증키가 설정되지 않았어.")
    with _aux_lock:
        disabled = _aux_disabled.get(service)
    if disabled:
        raise RuntimeError(f"{service} 비활성: {disabled}")

    full = {
        "serviceKey": KTO_SERVICE_KEY,
        "MobileOS": KTO_MOBILE_OS,
        "MobileApp": KTO_MOBILE_APP,
        "_type": "json",
    }
    full.update(params or {})
    cache_key = (
        f"ktoaux:{service}:{operation}:"
        + urlencode(sorted((str(k), str(v)) for k, v in full.items() if k != "serviceKey"))
    )
    if (value := cache_get(cache_key)) is not None:
        return value

    try:
        kto_limiter.wait()
        raw, _ = fetch_bytes(
            f"{KTO_GATEWAY_ROOT}/{service}/{operation}",
            params=full,
            timeout=HTTP_TIMEOUT,
        )
        text = raw.decode("utf-8", "replace")
        try:
            data = json.loads(text)
        except Exception as e:
            # data.go.kr errors sometimes arrive as XML.
            msg_match = re.search(
                r"<(?:returnAuthMsg|errMsg|resultMsg)>(.*?)</(?:returnAuthMsg|errMsg|resultMsg)>",
                text, re.I | re.S
            )
            msg = clean_text(msg_match.group(1)) if msg_match else "JSON이 아닌 응답"
            if any(token in msg.lower() for token in ("service key", "인증", "등록", "승인", "access")):
                _disable_aux(service, msg)
            raise RuntimeError(f"{service}/{operation}: {msg}") from e

        response = data.get("response") if isinstance(data, dict) else None
        if isinstance(response, dict):
            header = response.get("header") or {}
            result_code = str(header.get("resultCode") or "")
            if result_code and result_code != "0000":
                msg = str(header.get("resultMsg") or "요청 실패")
                if result_code not in {"03"}:
                    # 03/no-data should not disable a service.
                    if any(x in msg.lower() for x in ("key", "auth", "승인", "등록", "access")):
                        _disable_aux(service, msg)
                raise RuntimeError(f"{service}/{operation}: {msg} ({result_code})")
        cache_set(cache_key, data, ttl)
        return data
    except Exception as e:
        message = str(e)
        if any(x in message.lower() for x in ("http 401", "http 403", "service key", "인증", "승인되지", "등록되지")):
            _disable_aux(service, message)
        raise


def kto_aux_items(data):
    """Normalize common data.go.kr response forms."""
    if not isinstance(data, dict):
        return []
    response = data.get("response")
    if isinstance(response, dict):
        body = response.get("body") or {}
        items = body.get("items") or {}
        if isinstance(items, dict):
            items = items.get("item") or []
        if isinstance(items, dict):
            return [items]
        if isinstance(items, list):
            return items
    # A few KTO gateways use direct body/item shapes.
    body = data.get("body") or {}
    items = body.get("items") if isinstance(body, dict) else None
    if isinstance(items, dict):
        items = items.get("item") or []
    if isinstance(items, dict):
        return [items]
    return items if isinstance(items, list) else []


def kto_extra_service_state():
    with _aux_lock:
        disabled = dict(_aux_disabled)
    return {
        "disabled": disabled,
        "services": {
            "barrier_free": "KorWithService2",
            "photo_gallery": "PhotoGalleryService1",
            "audio_guide": "Odii",
            "visitor_bigdata": "DataLabService",
            "durunubi": "Durunubi",
            "concentration": "TatsCnctrRateService",
            "related_attractions": "TarRlteTarService1",
            "wellness": "WellnessTursmService",
            "ecotour": "GreenTourService1",
            "regional_diversity": "AreaTarDivService",
            "regional_demand": "AreaTarDemDsService",
            "regional_resource_demand": "AreaTarResDemService",
        },
    }


def https_url(value):
    text = str(value or "").strip()
    if text.startswith("http://"):
        return "https://" + text[len("http://"):]
    return text or None


def clean_text(value):
    text = html_lib.unescape(str(value or ""))
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_homepage(value):
    text = html_lib.unescape(str(value or ""))
    m = re.search(r'href=["\']([^"\']+)', text, re.I)
    url = m.group(1).strip() if m else clean_text(text).strip()
    if url.startswith("www."):
        url = "https://" + url
    return https_url(url) if url.startswith(("http://", "https://")) else None


def google_place_search_url(name, address=None):
    query = " ".join(x for x in (str(name or "").strip(), str(address or "").strip()) if x).strip()
    return "https://www.google.com/maps/search/?api=1&query=" + quote(query or str(name or "장소"), safe="")


def normalized_place_family(name):
    text = re.sub(r"[^0-9A-Za-z가-힣]", "", str(name or "").lower())
    for suffix in ("점", "지점", "본점", "분점", "센터", "관", "박물관", "미술관", "카페"):
        if text.endswith(suffix) and len(text) > len(suffix) + 2:
            text = text[:-len(suffix)]
    return text


def kto_classify(item):
    cat3 = str(item.get("cat3") or "").strip()
    content_type = str(item.get("contenttypeid") or item.get("contentTypeId") or "").strip()
    title = str(item.get("title") or "")
    if cat3 in KTO_CATEGORY_MAP:
        return KTO_CATEGORY_MAP[cat3]
    if content_type == "12" and cat3.startswith("A0201"):
        return "history", "역사관광지", False, 45
    # Defensive fallback for incomplete classification rows.
    if "박물관" in title:
        return "museum", "박물관", True, 45
    if any(x in title for x in ("미술관", "화랑", "갤러리")):
        return "gallery", "미술관/화랑", True, 45
    if "전시관" in title:
        return "arts", "전시관", True, 40
    if any(x in title for x in ("공연장", "극장", "아트홀", "예술의전당")):
        return "theatre", "공연장", True, 50
    if content_type == "39" and any(x in title.lower() for x in ("카페", "커피", "coffee", "베이커리", "다방", "찻집")):
        return "cafe", "카페/전통찻집", True, 35
    if content_type == "15":
        return "festival", "축제·공연·행사", False, 60
    return "other", "관광정보", False, 35


def content_types_for_preferences(cultures):
    cultures = set(cultures or [])
    types = set()
    if cultures & {"박물관", "미술관", "전시", "공연"}: types.add("14")
    if "역사문화" in cultures: types.update({"12","14"})
    if "카페" in cultures: types.add("39")
    if not types and "축제행사" not in cultures: types.update({"12","14"})
    return sorted(types)


def parse_kto_places(items, o_lat, o_lon):
    out = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            lat = float(item.get("mapy")); lon = float(item.get("mapx"))
        except Exception:
            continue
        name = str(item.get("title") or "").strip()
        if not name:
            continue
        content_id = str(item.get("contentid") or "").strip()
        key = content_id or (name, round(lat, 5), round(lon, 5))
        if key in seen:
            continue
        seen.add(key)
        category, label, indoor, dwell = kto_classify(item)
        address = " ".join(x for x in (str(item.get("addr1") or "").strip(), str(item.get("addr2") or "").strip()) if x).strip()
        try:
            distance = round(float(item.get("dist")))
        except Exception:
            distance = round(haversine_m(o_lat, o_lon, lat, lon))
        out.append({
            "name": name, "lat": lat, "lon": lon,
            "category": category, "category_label": label, "indoor": indoor,
            "visit_minutes": dwell, "distance_m": distance,
            "opening_hours": None, "website": None,
            "description": f"한국관광공사에 등록된 {label} 관광정보야. 상세 설명을 확인하는 중이야.",
            "image_url": https_url(item.get("firstimage") or item.get("firstimage2")),
            "address": address or None,
            "map_url": google_place_search_url(name, address),
            "contentid": content_id,
            "contenttypeid": str(item.get("contenttypeid") or ""),
            "cat1": item.get("cat1"), "cat2": item.get("cat2"), "cat3": item.get("cat3"),
            "areacode": item.get("areacode") or item.get("areaCode"),
            "sigungucode": item.get("sigungucode") or item.get("sigunguCode"),
            "ldong_regn_cd": item.get("lDongRegnCd") or item.get("ldongregncd"),
            "ldong_signgu_cd": item.get("lDongSignguCd") or item.get("ldongsigngucd"),
            "ldong_rd_cd": item.get("lDongRdCd") or item.get("ldongrdcd"),
            "modifiedtime": item.get("modifiedtime"),
            "source": "한국관광공사 TourAPI",
            "tel": item.get("tel"),
            "copyright_div_cd": item.get("cpyrhtDivCd"),
        })
    return out


def kto_nearby_places(lat, lon, radius, cultures):
    radius = max(300, min(int(radius), 20000))
    all_items = []
    providers = []
    errors = []
    for content_type in content_types_for_preferences(cultures):
        try:
            data = kto_fetch_json("locationBasedList2", {
                "numOfRows": 80,
                "pageNo": 1,
                "arrange": "E",
                "mapX": f"{lon:.7f}",
                "mapY": f"{lat:.7f}",
                "radius": radius,
                "contentTypeId": content_type,
            }, ttl=600)
            all_items.extend(kto_items(data))
            providers.append(content_type)
        except Exception as e:
            errors.append(f"contentTypeId={content_type}: {e}")
    if not all_items and errors:
        raise RuntimeError(" | ".join(errors))
    return parse_kto_places(all_items, lat, lon), providers, errors


def kto_common_detail(content_id):
    data = kto_fetch_json("detailCommon2", {
        "numOfRows": 10,
        "pageNo": 1,
        "contentId": content_id,
    }, ttl=86400)
    items = kto_items(data)
    return items[0] if items else {}


def kto_intro_detail(content_id, content_type_id):
    if not content_type_id:
        return {}
    data = kto_fetch_json("detailIntro2", {
        "numOfRows": 10, "pageNo": 1, "contentId": content_id, "contentTypeId": content_type_id,
    }, ttl=86400)
    items = kto_items(data)
    return items[0] if items else {}


def kto_image_detail(content_id):
    data = kto_fetch_json("detailImage2", {
        "numOfRows": 20,
        "pageNo": 1,
        "contentId": content_id,
        "imageYN": "Y",
    }, ttl=86400)
    return kto_items(data)


def kto_repeat_detail(content_id, content_type_id):
    if not content_id or not content_type_id:
        return []
    data = kto_fetch_json("detailInfo2", {
        "numOfRows": 30,
        "pageNo": 1,
        "contentId": content_id,
        "contentTypeId": content_type_id,
    }, ttl=86400)
    return kto_items(data)


def kto_pet_detail(content_id):
    if not content_id:
        return {}
    data = kto_fetch_json("detailPetTour2", {
        "numOfRows": 10,
        "pageNo": 1,
        "contentId": content_id,
    }, ttl=86400)
    items = kto_items(data)
    return items[0] if items else {}


def kto_area_codes(area_code=None):
    params = {"numOfRows": 100, "pageNo": 1}
    if area_code:
        params["areaCode"] = area_code
    return kto_items(kto_fetch_json("areaCode2", params, ttl=7*86400))






def kto_area_list(area_code, content_type_id=None, rows=100):
    params = {
        "numOfRows": min(max(int(rows),1),200),
        "pageNo":1, "arrange":"A", "areaCode":str(area_code),
    }
    if content_type_id: params["contentTypeId"] = str(content_type_id)
    return kto_items(kto_fetch_json("areaBasedList2", params, ttl=1800))


def kto_keyword_search(keyword, content_type_id=None, rows=50):
    params={"numOfRows":min(max(int(rows),1),100),"pageNo":1,"arrange":"A","keyword":keyword}
    if content_type_id: params["contentTypeId"] = str(content_type_id)
    return kto_items(kto_fetch_json("searchKeyword2", params, ttl=1800))


def kto_festival_search(start_date, end_date=None, area_code=None, rows=100):
    params={"numOfRows":min(max(int(rows),1),200),"pageNo":1,"arrange":"A","eventStartDate":start_date}
    if end_date: params["eventEndDate"] = end_date
    if area_code: params["areaCode"] = str(area_code)
    return kto_items(kto_fetch_json("searchFestival2", params, ttl=900))







def _previous_complete_month(offset=1):
    now = datetime.now(KST)
    year, month = now.year, now.month - offset
    while month <= 0:
        month += 12
        year -= 1
    return f"{year:04d}{month:02d}"


def _tourapi_region_codes(place):
    """Old TourAPI area/sigungu codes: required by related-attraction API."""
    return clean_text(place.get("areacode")) or None, clean_text(place.get("sigungucode")) or None


def _legal_region_codes(place):
    """Legal/admin region codes: used by concentration/DataLab products."""
    return clean_text(place.get("ldong_regn_cd")) or None, clean_text(place.get("ldong_signgu_cd")) or None




def kto_barrier_free_detail(content_id):
    if not content_id:
        return {}
    data = kto_aux_fetch_json(
        "KorWithService2", "detailWithTour2",
        {"contentId": content_id, "numOfRows": 10, "pageNo": 1},
        ttl=86400,
    )
    items = kto_aux_items(data)
    return items[0] if items else {}


ACCESS_KEY_LABELS = {
    "parking": "장애인 주차",
    "route": "무장애 이동경로",
    "publictransport": "대중교통 접근",
    "ticketoffice": "매표소 접근",
    "promotion": "점자/안내",
    "wheelchair": "휠체어",
    "exit": "출입구",
    "elevator": "엘리베이터",
    "restroom": "장애인 화장실",
    "auditorium": "관람석",
    "room": "객실",
    "handicapetc": "기타 무장애",
    "braileblock": "점자블록",
    "helpdog": "보조견",
    "guidehuman": "안내요원",
    "audioguide": "오디오가이드",
    "bigprint": "큰활자",
    "brailepromotion": "점자홍보물",
    "guidesystem": "안내시스템",
    "blindhandicapetc": "시각장애 기타",
    "signguide": "수어안내",
    "videoguide": "영상안내",
    "hearingroom": "청각지원",
    "hearinghandicapetc": "청각장애 기타",
    "stroller": "유모차",
    "lactationroom": "수유실",
    "babysparechair": "유아의자",
    "infantsfamilyetc": "영유아가족 기타",
}


def summarize_accessibility(row):
    if not row:
        return {"available": False, "tags": [], "details": []}
    tags, details = [], []
    for key, label in ACCESS_KEY_LABELS.items():
        value = clean_text(row.get(key))
        if not value:
            continue
        lower = value.lower()
        # API fields are descriptive text; only call it a positive tag when the
        # text is not an explicit "없음/불가".
        negative = any(x in lower for x in ("없음", "불가", "미제공", "해당없음", "없다"))
        details.append(f"{label}: {value}")
        if not negative and label not in tags:
            tags.append(label)
    return {
        "available": bool(tags or details),
        "tags": tags[:8],
        "details": details[:10],
    }


def kto_photo_gallery_search(keyword, rows=8):
    if not keyword: return []
    # Current KTO manual calls this galSearchKeyword. Older examples sometimes
    # used keyword, so retry only when the canonical parameter yields no rows.
    for params in (
        {"galSearchKeyword":keyword,"numOfRows":rows,"pageNo":1},
        {"keyword":keyword,"numOfRows":rows,"pageNo":1},
    ):
        try:
            items=kto_aux_items(kto_aux_fetch_json("PhotoGalleryService1","gallerySearchList1",params,ttl=86400))
            if items: return items
        except Exception:
            if "galSearchKeyword" not in params: raise
    return []


def normalize_photo_gallery(items):
    out = []
    for row in items or []:
        if not isinstance(row, dict):
            continue
        url = https_url(
            row.get("galWebImageUrl")
            or row.get("galWebImageUrl2")
            or row.get("galWebImageUrl1")
        )
        if not url:
            continue
        out.append({
            "url": url,
            "title": clean_text(row.get("galTitle")),
            "location": clean_text(row.get("galPhotographyLocation")),
            "photographer": clean_text(row.get("galPhotographer")),
            "keywords": clean_text(row.get("galSearchKeyword")),
            "source": "한국관광공사 관광사진 정보",
        })
    return out[:8]


def kto_odii_story_search(keyword, rows=5):
    if not keyword:
        return []
    data = kto_aux_fetch_json(
        "Odii", "storySearchList",
        {"keyword": keyword, "numOfRows": rows, "pageNo": 1},
        ttl=86400,
    )
    return kto_aux_items(data)


def normalize_audio_guides(items):
    out=[]
    for row in items or []:
        if not isinstance(row,dict): continue
        title=first_nonempty(row,("title","storyTitle","storyNm","audioTitle","name"))
        url=first_nonempty(row,("mp3Path","mp3path","audioUrl","audioFileUrl","mp3Url","playUrl","url"))
        text=first_nonempty(row,("script","story","storyText","storyCont","contents","description"))
        image=first_nonempty(row,("imagePath","imagepath","imageUrl","image"))
        if not (title or url or text): continue
        out.append({"story_id":first_nonempty(row,("storyId","storyid")),"title":title or "오디오 해설","theme_name":first_nonempty(row,("themeName","themename")),"url":https_url(url),"image_url":https_url(image),"description":text,"source":"한국관광공사 오디(Odii)"})
    return out[:4]


def kto_related_attractions(place, rows=30):
    name=clean_text(place.get("name")); area,signgu=_tourapi_region_codes(place)
    if not name or not area or not signgu: return []
    for offset in (1,2,3,6,12):
        try:
            items=kto_aux_items(kto_aux_fetch_json("TarRlteTarService1","searchKeyword1",{"keyword":name,"baseYm":_previous_complete_month(offset),"areaCd":area,"signguCd":signgu,"numOfRows":rows,"pageNo":1},ttl=86400))
            if items: return items
        except Exception as e:
            if "비활성" in str(e): break
    return []


def normalize_related_items(items, source_name=None):
    source_norm=normalized_place_family(source_name); out=[]
    for row in items or []:
        if not isinstance(row,dict): continue
        name=clean_text(row.get("rlteTatsNm"))
        if not name or (source_norm and normalized_place_family(name)==source_norm): continue
        try: rank=int(float(row.get("rlteRank")))
        except Exception: rank=999
        out.append({"name":name,"rank":rank,"category_large":clean_text(row.get("rlteCtgryLclsNm")),"category_mid":clean_text(row.get("rlteCtgryMclsNm")),"category_small":clean_text(row.get("rlteCtgrySclsNm")),"base_ym":clean_text(row.get("baseYm")),"source":"한국관광공사 연관 관광지"})
    out.sort(key=lambda x:x["rank"]); return out[:30]




def related_pair_rank(a,b):
    bf=normalized_place_family(b.get("name")); af=normalized_place_family(a.get("name")); ranks=[]
    for item in a.get("related_items") or []:
        n=normalized_place_family(item.get("name"))
        if n and bf and (n==bf or (len(n)>=4 and (n in bf or bf in n))): ranks.append(item.get("rank",999))
    for item in b.get("related_items") or []:
        n=normalized_place_family(item.get("name"))
        if n and af and (n==af or (len(n)>=4 and (n in af or af in n))): ranks.append(item.get("rank",999))
    return min(ranks) if ranks else None


def kto_concentration_for_place(place):
    name = clean_text(place.get("name"))
    area, signgu = _legal_region_codes(place)
    if not name or not area or not signgu:
        return None
    params = {
        "areaCd": area,
        "signguCd": signgu,
        "tAtsNm": name,
        "numOfRows": 100,
        "pageNo": 1,
    }
    data = kto_aux_fetch_json(
        "TatsCnctrRateService", "tatsCnctrRatedList", params, ttl=1800
    )
    items = kto_aux_items(data)
    if not items:
        return None

    target = datetime.now(KST).strftime("%Y%m%d")
    best = None
    for row in items:
        if not isinstance(row, dict):
            continue
        raw_rate = row.get("cnctrRate")
        try:
            rate = float(str(raw_rate).replace("%", "").strip())
        except Exception:
            continue
        date = clean_text(row.get("baseYmd"))
        candidate = {
            "rate": rate,
            "date": date or None,
            "area": clean_text(row.get("areaNm")),
            "sigungu": clean_text(row.get("signguNm")),
            "name": clean_text(row.get("tAtsNm")) or name,
            "source": "한국관광공사 관광지 집중률 예측",
        }
        if date == target:
            return candidate
        if best is None or (date and date < target and date > (best.get("date") or "")):
            best = candidate
    return best


def concentration_label(rate):
    try:
        rate = float(rate)
    except Exception:
        return None
    if rate < 35:
        return "한산"
    if rate < 60:
        return "보통"
    if rate < 80:
        return "주의"
    return "혼잡"


def kto_regional_visitors(place):
    area,signgu=_legal_region_codes(place)
    if not area: return None
    for offset in (1,2,3):
        base_ym=_previous_complete_month(offset)
        operations=[]
        if signgu: operations.append(("locgoRegnVisitrDDList",{"baseYm":base_ym,"areaCd":area,"signguCd":signgu,"numOfRows":1000,"pageNo":1}))
        operations.append(("metcoRegnVisitrDDList",{"baseYm":base_ym,"areaCd":area,"numOfRows":1000,"pageNo":1}))
        for operation,params in operations:
            try:
                items=kto_aux_items(kto_aux_fetch_json("DataLabService",operation,params,ttl=3600))
                if not items: continue
                rows=[]
                for row in items:
                    if not isinstance(row,dict): continue
                    count=None
                    for k in ("visitrCnt","visitorCnt","visitorCount","visitrNum","visitrCount"):
                        if row.get(k) not in (None,""):
                            try: count=float(str(row.get(k)).replace(",","")); break
                            except Exception: pass
                    if count is None: continue
                    rows.append({"date":clean_text(row.get("baseYmd") or row.get("baseDate")),"day":clean_text(row.get("daywkDivNm") or row.get("daywkName")),"division":clean_text(row.get("touDivNm") or row.get("tourDivisionName")),"count":count})
                if not rows: continue
                counts=[r["count"] for r in rows]
                return {"available":True,"operation":operation,"base_ym":base_ym,"rows":rows[-14:],"average":round(sum(counts)/len(counts),1),"total":round(sum(counts),1),"source":"한국관광공사 빅데이터 지역별 방문자수","note":"최근 지역 방문 흐름 참고용이며 실시간 혼잡도가 아님"}
            except Exception as e:
                if "비활성" in str(e): return None
    return None


def kto_durunubi_courses(rows=100):
    data = kto_aux_fetch_json(
        "Durunubi", "courseList",
        {"numOfRows": min(rows, 300), "pageNo": 1},
        ttl=86400,
    )
    return kto_aux_items(data)


def nearby_durunubi_summary(lat, lon, radius=15000, address_hint=None):
    try: items=kto_durunubi_courses(300)
    except Exception: return None
    hint=clean_text(address_hint); candidates=[]
    for row in items:
        if not isinstance(row,dict): continue
        sigun=first_nonempty(row,("sigun","sigunNm","sigungu","region"))
        name=first_nonempty(row,("crsKorNm","courseName","crsNm","name","title")) or "두루누비 코스"
        if hint and sigun and clean_text(sigun) not in hint and normalize_area_name(sigun) not in normalize_area_name(hint): continue
        candidates.append({"name":name,"sigun":sigun,"distance":first_nonempty(row,("crsDstnc","distance")),"required_time":first_nonempty(row,("crsTotlRqrmHour","requiredTime","required_time")),"level":first_nonempty(row,("crsLevel","level")),"gpx_path":https_url(first_nonempty(row,("gpxPath","gpx_path"))),"source":"한국관광공사 두루누비"})
    return candidates[:3] or None








def enrich_kto_extra_recommendation_data(top, companion, walk, remaining, address_hint=None):
    used=set(); errors=[]; visitor_by_region={}
    for p in top[:4]:
        try:
            rows=kto_related_attractions(p); p["related_items"]=normalize_related_items(rows,p.get("name")); p["related_names"]=[x["name"] for x in p["related_items"]]
            if p["related_items"]: used.add("관광지별 연관 관광지")
        except Exception as e: errors.append(str(e))
        try:
            c=kto_concentration_for_place(p)
            if c: c["label"]=concentration_label(c.get("rate")); p["concentration"]=c; used.add("관광지 집중률 예측")
        except Exception as e: errors.append(str(e))
        try:
            if companion in {"가족","아이 동반"} or len(top)<=3:
                info=summarize_accessibility(kto_barrier_free_detail(p.get("contentid")))
                if info.get("available"): p["accessibility"]=info; used.add("무장애 여행 정보")
        except Exception as e: errors.append(str(e))
        try:
            reg=_legal_region_codes(p)
            if reg[0]:
                if reg not in visitor_by_region: visitor_by_region[reg]=kto_regional_visitors(p)
                if visitor_by_region.get(reg): p["regional_visitors"]=visitor_by_region[reg]; used.add("빅데이터 지역별 방문자수")
        except Exception as e: errors.append(str(e))
    # Relative recent visitor-flow level across candidate regions. This affects score,
    # but is never presented as live crowding.
    avgs=[float(p["regional_visitors"]["average"]) for p in top if (p.get("regional_visitors") or {}).get("average") is not None]
    if avgs:
        lo,hi=min(avgs),max(avgs)
        for p in top:
            rv=p.get("regional_visitors") or {}; val=rv.get("average")
            if val is None: continue
            if hi<=lo: level="보통"
            else:
                ratio=(float(val)-lo)/(hi-lo)
                level="낮음" if ratio<0.34 else ("높음" if ratio>0.66 else "보통")
            rv["relative_level"]=level
    durunubi=None
    if walk=="많이" and remaining>=120:
        try:
            durunubi=nearby_durunubi_summary(top[0]["lat"],top[0]["lon"],address_hint=address_hint) if top else None
            if durunubi: used.add("두루누비")
        except Exception as e: errors.append(str(e))
    return {"used":sorted(used),"visitor_context":next((p.get("regional_visitors") for p in top if p.get("regional_visitors")),None),"durunubi":durunubi,"errors":errors[:8]}




def first_nonempty(mapping, keys):
    for key in keys:
        value = clean_text(mapping.get(key))
        if value:
            return value
    return None


def kto_place_detail_payload(content_id, content_type_id=None, fallback_name="장소", fallback_image=None, fallback_address=None, include_pet=False):
    if not content_id:
        return {"ok":True,"name":fallback_name,"description":"한국관광공사 상세 콘텐츠 ID가 없는 장소야.","image_url":fallback_image,"image_urls":[fallback_image] if fallback_image else [],"homepage_url":None,"opening_hours":None,"repeat_info":[],"pet_info":None,"map_url":google_place_search_url(fallback_name,fallback_address),"source":"한국관광공사 TourAPI","endpoints_used":[]}
    key=f"kto-detail-v25:{content_id}:{content_type_id}:{int(bool(include_pet))}"
    if (v:=cache_get(key)) is not None: return v
    common={}; intro={}; repeat=[]; images=[]; pet={}; errors=[]; endpoints=[]
    calls=[("detailCommon2",lambda:kto_common_detail(content_id)),("detailIntro2",lambda:kto_intro_detail(content_id,content_type_id)),("detailInfo2",lambda:kto_repeat_detail(content_id,content_type_id)),("detailImage2",lambda:kto_image_detail(content_id))]
    for name,getter in calls:
        try:
            value=getter(); endpoints.append(name)
            if name=="detailCommon2": common=value
            elif name=="detailIntro2": intro=value
            elif name=="detailInfo2": repeat=value
            else: images=value
        except Exception as e: errors.append(f"{name}: {e}")
    if include_pet:
        try: pet=kto_pet_detail(content_id); endpoints.append("detailPetTour2")
        except Exception as e: errors.append("detailPetTour2: "+str(e))
    name=clean_text(common.get("title")) or fallback_name
    address=" ".join(x for x in (clean_text(common.get("addr1")),clean_text(common.get("addr2"))) if x).strip() or fallback_address
    urls=[]
    for raw in (common.get("firstimage"),common.get("firstimage2"),fallback_image):
        u=https_url(raw)
        if u and u not in urls: urls.append(u)
    for it in images:
        u=https_url(it.get("originimgurl") or it.get("smallimageurl"))
        if u and u not in urls: urls.append(u)
    photo_gallery=[]
    if len(urls) < 3:
        try:
            photo_gallery=normalize_photo_gallery(kto_photo_gallery_search(name,8))
            for photo in photo_gallery:
                u=photo.get("url")
                if u and u not in urls:
                    urls.append(u)
            if photo_gallery:
                endpoints.append("PhotoGalleryService1/gallerySearchList1")
        except Exception as e:
            errors.append("관광사진: "+str(e))

    audio_guides=[]
    if str(content_type_id or "") in {"12","14","15"}:
        try:
            audio_guides=normalize_audio_guides(kto_odii_story_search(name,5))
            if audio_guides:
                endpoints.append("Odii/storySearchList")
        except Exception as e:
            errors.append("오디오가이드: "+str(e))

    accessibility=None
    try:
        barrier=kto_barrier_free_detail(content_id)
        accessibility=summarize_accessibility(barrier)
        if accessibility.get("available"):
            endpoints.append("KorWithService2/detailWithTour2")
    except Exception as e:
        errors.append("무장애: "+str(e))

    result={
        "ok":True,
        "name":name,
        "description":clean_text(common.get("overview")) or f"{name}은(는) 한국관광공사 국문 관광정보에 등록된 장소야.",
        "image_url":urls[0] if urls else None,
        "image_urls":urls[:10],
        "photo_gallery":photo_gallery,
        "homepage_url":extract_homepage(common.get("homepage")),
        "opening_hours":first_nonempty(intro,("usetimeculture","opentimefood","usetime","usetimeleports","playtime")),
        "rest_date":first_nonempty(intro,("restdateculture","restdatefood","restdate","restdateleports")),
        "spendtime":first_nonempty(intro,("spendtime","spendtimefestival","usetimefestival")),
        "fee":first_nonempty(intro,("usefee","usefeeleports","eventplace")),
        "parking":first_nonempty(intro,("parkingculture","parkingfood","parking","parkingleports")),
        "tel":clean_text(common.get("tel")) or clean_text(intro.get("infocenterculture")) or clean_text(intro.get("infocenter")),
        "address":address,
        "repeat_info":flatten_repeat_info(repeat),
        "pet_info":summarize_pet_info(pet),
        "accessibility":accessibility,
        "audio_guides":audio_guides,
        "map_url":google_place_search_url(name,address),
        "source":"한국관광공사 TourAPI+",
        "endpoints_used":endpoints,
        "errors":errors or None
    }
    cache_set(key,result,86400); return result


def _build_kst_timezone():
    """Return Korea time without requiring the optional tzdata package."""
    if ZoneInfo is not None:
        try:
            return ZoneInfo("Asia/Seoul")
        except Exception:
            pass
    return timezone(timedelta(hours=9), name="KST")


KST = _build_kst_timezone()
WEEKDAY_KO = ["월요일","화요일","수요일","목요일","금요일","토요일","일요일"]


def parse_duration_minutes(text, fallback=None):
    value = clean_text(text)
    if not value:
        return fallback
    hour = re.search(r"(\d+(?:\.\d+)?)\s*시간", value)
    minute = re.search(r"(\d+)\s*분", value)
    total = 0
    if hour:
        try:
            total += int(float(hour.group(1)) * 60)
        except Exception:
            pass
    if minute:
        total += int(minute.group(1))
    if total:
        return max(10, min(total, 240))
    return fallback


def operating_info_for_place(place):
    """Fetch only TourAPI intro data needed for visit-time checks."""
    content_id = str(place.get("contentid") or "").strip()
    content_type = str(place.get("contenttypeid") or "").strip()
    if not content_id or not content_type:
        return {
            "opening_hours": place.get("opening_hours"),
            "rest_date": None,
            "spendtime": None,
            "source": "한국관광공사 TourAPI",
        }
    key = f"operating:{content_id}:{content_type}"
    if (v := cache_get(key)) is not None:
        return v
    try:
        intro = kto_intro_detail(content_id, content_type)
        result = {
            "opening_hours": first_nonempty(
                intro,
                ("usetimeculture","opentimefood","usetime","usetimeleports","playtime")
            ),
            "rest_date": first_nonempty(
                intro,
                ("restdateculture","restdatefood","restdate","restdateleports")
            ),
            "spendtime": first_nonempty(
                intro,
                ("spendtime","spendtimefestival","usetimefestival")
            ),
            "source": "한국관광공사 TourAPI",
        }
    except Exception as e:
        result = {
            "opening_hours": place.get("opening_hours"),
            "rest_date": None,
            "spendtime": None,
            "error": str(e),
            "source": "한국관광공사 TourAPI",
        }
    cache_set(key, result, 86400)
    return result


def _month_in_range(month, start, end):
    if start <= end:
        return start <= month <= end
    return month >= start or month <= end


def _opening_relevant_text(opening, visit_dt):
    text = clean_text(opening)
    if not text:
        return ""
    lines = [x.strip() for x in re.split(r"[\n\r]+", text) if x.strip()]
    month = visit_dt.month
    weekday = visit_dt.weekday()

    seasonal = []
    for line in lines:
        m = re.search(r"(\d{1,2})\s*[~\-–]\s*(\d{1,2})\s*월", line)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if 1 <= a <= 12 and 1 <= b <= 12 and _month_in_range(month, a, b):
                seasonal.append(line)
    if seasonal:
        lines = seasonal

    day_specific = []
    for line in lines:
        if weekday < 5 and "평일" in line:
            day_specific.append(line)
        elif weekday >= 5 and any(k in line for k in ("주말","토·일","토일")):
            day_specific.append(line)
        elif weekday == 5 and "토요일" in line:
            day_specific.append(line)
        elif weekday == 6 and "일요일" in line:
            day_specific.append(line)
    if day_specific:
        lines = day_specific

    return "\n".join(lines) if lines else text


def _is_rest_day(rest_text, visit_dt):
    text = clean_text(rest_text)
    if not text:
        return False, None
    compact = re.sub(r"\s+", "", text)
    if "연중무휴" in compact or "없음" == compact:
        return False, None

    weekday = WEEKDAY_KO[visit_dt.weekday()]
    short = weekday[0]  # 월/화/...
    patterns = [
        weekday,
        f"매주{weekday}",
        f"{short}요일",
    ]
    if any(p in compact for p in patterns):
        # TourAPI restdate fields are explicitly "쉬는날", so weekday mention here
        # is generally a closure rule.
        return True, f"휴무 정보: {text}"

    # Common fixed-date closures
    if visit_dt.month == 1 and visit_dt.day == 1 and "1월1일" in compact:
        return True, f"휴무 정보: {text}"

    return False, None


def evaluate_opening_status(opening, rest_date, visit_dt, dwell_minutes=30):
    visit_dt = visit_dt.astimezone(KST)
    closed_day, rest_reason = _is_rest_day(rest_date, visit_dt)
    planned = visit_dt.strftime("%H:%M")

    if closed_day:
        return {
            "status": "closed",
            "label": f"{planned} 방문예정 · 휴무",
            "reason": rest_reason,
            "planned_time": planned,
        }

    relevant = _opening_relevant_text(opening, visit_dt)
    ranges = []
    for h1, m1, h2, m2 in re.findall(
        r"(?<!\d)(\d{1,2})\s*:\s*(\d{2})\s*(?:~|\-|–|∼|〜)\s*(\d{1,2})\s*:\s*(\d{2})",
        relevant
    ):
        try:
            start = int(h1) * 60 + int(m1)
            end = int(h2) * 60 + int(m2)
            if 0 <= start < 24*60 and 0 <= end < 24*60:
                ranges.append((start, end))
        except Exception:
            pass

    if not ranges:
        return {
            "status": "unknown",
            "label": f"{planned} 방문예정 · 운영시간 확인 필요",
            "reason": opening or "운영시간 정보 없음",
            "planned_time": planned,
        }

    current = visit_dt.hour * 60 + visit_dt.minute
    visit_end = current + int(dwell_minutes or 0)

    for start, end in ranges:
        if end < start:  # overnight
            is_open = current >= start or current <= end
            enough = True
        else:
            is_open = start <= current < end
            enough = visit_end <= end
        if is_open:
            if enough:
                return {
                    "status": "open",
                    "label": f"{planned} 방문예정 · 영업중",
                    "reason": relevant,
                    "planned_time": planned,
                }
            return {
                "status": "closing_soon",
                "label": f"{planned} 방문예정 · 마감 임박",
                "reason": f"운영시간 {relevant}",
                "planned_time": planned,
            }

    return {
        "status": "closed",
        "label": f"{planned} 방문예정 · 영업 종료",
        "reason": f"운영시간 {relevant}",
        "planned_time": planned,
    }


def enrich_operating(place):
    info = operating_info_for_place(place)
    place["opening_hours"] = info.get("opening_hours")
    place["rest_date"] = info.get("rest_date")
    place["spendtime"] = info.get("spendtime")
    parsed = parse_duration_minutes(info.get("spendtime"), None)
    if parsed:
        place["visit_minutes"] = parsed
    return place


def kakao_map_to_url(place):
    """Kakao Map destination link usable on mobile/desktop."""
    name=clean_text(place.get("name")) or "도착지"
    lat=float(place["lat"]); lon=float(place["lon"])
    return f"https://map.kakao.com/link/to/{quote(name)},{lat:.7f},{lon:.7f}"


def kakao_transit_leg(a, b):
    if not KAKAO_REST_API_KEY:
        raise RuntimeError("카카오 REST API 키가 설정되지 않았어.")

    lat1, lon1 = float(a["lat"]), float(a["lon"])
    lat2, lon2 = float(b["lat"]), float(b["lon"])
    key = f"kakao-transit:{lat1:.5f}:{lon1:.5f}:{lat2:.5f}:{lon2:.5f}"
    if (v := cache_get(key)) is not None:
        return v

    params = {
        "start_x": f"{lon1:.7f}",
        "start_y": f"{lat1:.7f}",
        "end_x": f"{lon2:.7f}",
        "end_y": f"{lat2:.7f}",
        "s_name": str(a.get("name") or "출발")[:40],
        "e_name": str(b.get("name") or "도착")[:40],
        "input_coord": "WGS84",
        "output_coord": "WGS84",
    }
    kakao_limiter.wait()
    raw = fetch_json(
        KAKAO_TRANSIT_URL,
        params=params,
        headers={"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"},
    )
    if raw.get("status") != "OK" or not raw.get("routes"):
        raise RuntimeError(f"카카오 대중교통 경로 없음: {raw.get('status') or 'unknown'}")

    route = raw["routes"][0]
    props = route.get("properties") or {}
    steps_out = []
    geometry_points = []

    for step in route.get("steps") or []:
        sp = step.get("properties") or {}
        stype = sp.get("type") or "WALKING"
        guidance = clean_text(sp.get("guidance"))
        vehicles = sp.get("vehicles") or []
        vehicle_names = [str(x.get("name")) for x in vehicles if x.get("name")]
        stops = sp.get("stops") or []
        stop_names = [str(x.get("name")) for x in stops if x.get("name")]
        path = step.get("path") or {}
        points = path.get("points") or []
        for point in points:
            if isinstance(point, list) and len(point) >= 2:
                geometry_points.append([float(point[0]), float(point[1])])

        summary = guidance
        if not summary:
            if stype == "WALKING":
                summary = "도보 이동"
            elif vehicle_names:
                summary = f"{'/'.join(vehicle_names[:2])} 이용"
            else:
                summary = "대중교통 이동"

        steps_out.append({
            "type": stype,
            "guidance": summary,
            "minutes": max(1, round((sp.get("time") or 0) / 60)),
            "distance_m": int(sp.get("distance") or 0),
            "vehicles": vehicle_names[:3],
            "start_stop": stop_names[0] if stop_names else None,
            "end_stop": stop_names[-1] if stop_names else None,
        })

    landing = (
        (raw.get("properties") or {}).get("landingURL")
        or (route.get("properties") or {}).get("landingURL")
        or kakao_map_to_url(b)
    )
    result = {
        "actual": True,
        "provider": "Kakao",
        "minutes": max(1, math.ceil((props.get("totalTime") or 0) / 60)),
        "distance_m": int(props.get("totalDistance") or 0),
        "transfers": int(props.get("transfers") or 0),
        "fare": int(((props.get("fare") or {}).get("value") or 0)),
        "route_type": props.get("type"),
        "steps": steps_out,
        "landing_url": landing,
        "start_name": clean_text(a.get("name")) or "출발",
        "end_name": clean_text(b.get("name")) or "도착",
        "geometry": {
            "type": "LineString",
            "coordinates": geometry_points or [[lon1, lat1], [lon2, lat2]],
        },
    }
    cache_set(key, result, 180)
    return result


def travel_leg(a, b, transport):
    mode, mode_label = transport_mode(transport)
    if transport == "대중교통":
        try:
            route = kakao_transit_leg(a, b)
            return {
                "minutes": route["minutes"],
                "quality": "actual",
                "label": "카카오 대중교통",
                "route": route,
            }
        except Exception as e:
            return {
                "minutes": approx_minutes(a["lat"],a["lon"],b["lat"],b["lon"],"transit"),
                "quality": "estimate",
                "label": "대중교통(예상)",
                "route": None,
                "error": str(e),
            }

    if mode in {"walk","car"}:
        try:
            data = osrm_route(
                [(float(a["lat"]),float(a["lon"])),(float(b["lat"]),float(b["lon"]))],
                mode
            )
            r = data["routes"][0]
            return {
                "minutes": max(1, r["duration"]/60),
                "quality": "actual",
                "label": mode_label,
                "route": None,
            }
        except Exception:
            pass

    return {
        "minutes": approx_minutes(a["lat"],a["lon"],b["lat"],b["lon"],mode),
        "quality": "estimate",
        "label": mode_label + ("(예상)" if "예상" not in mode_label else ""),
        "route": None,
    }


def transit_route_status():
    return {
        "configured": bool(KAKAO_REST_API_KEY),
        "provider": "카카오맵 대중교통 경로 API" if KAKAO_REST_API_KEY else None,
        "fallback": not bool(KAKAO_REST_API_KEY),
    }


def osrm_base(mode):
    return "https://routing.openstreetmap.de/" + ("routed-car" if mode=="car" else "routed-foot")


def osrm_table(coords, mode="walk"):
    c=";".join(f"{lon:.6f},{lat:.6f}" for lat,lon in coords)
    url=f"{osrm_base(mode)}/table/v1/driving/{c}"
    key=f"table:{mode}:{c}"
    if (v:=cache_get(key)) is not None: return v
    osrm_limiter.wait()
    v=fetch_json(url,params={"annotations":"duration,distance"})
    if v.get("code")!="Ok": raise RuntimeError(v.get("message") or "OSRM table failed")
    cache_set(key,v,600); return v


def osrm_route(coords, mode="walk"):
    c=";".join(f"{lon:.6f},{lat:.6f}" for lat,lon in coords)
    url=f"{osrm_base(mode)}/route/v1/driving/{c}"
    key=f"route:{mode}:{c}"
    if (v:=cache_get(key)) is not None: return v
    osrm_limiter.wait()
    v=fetch_json(url,params={"overview":"full","geometries":"geojson","steps":"false"})
    if v.get("code")!="Ok" or not v.get("routes"): raise RuntimeError(v.get("message") or "OSRM route failed")
    cache_set(key,v,300); return v


PREF_MAP={"박물관":{"museum"},"미술관":{"gallery"},"전시":{"arts"},"카페":{"cafe"},"공연":{"theatre"},"역사문화":{"history"},"축제행사":{"festival"}}


def reserve_meal_minutes(meal_time, remaining):
    if not meal_time or meal_time=="상관없음": return 0
    try:
        hh,mm=(int(x) for x in meal_time.split(":",1))
        now=datetime.now(KST)
        target=now.replace(hour=hh,minute=mm,second=0,microsecond=0)
        until=(target-now).total_seconds()/60
        return 40 if 0<=until<=remaining else 0
    except Exception:
        return 0


def transport_mode(transport):
    if transport=="택시·차량": return "car","차량"
    if transport=="대중교통": return "transit",("카카오 대중교통" if KAKAO_REST_API_KEY else "대중교통(예상)")
    if transport=="상황에 따라": return "walk","도보"
    return "walk","도보"


def preference_categories(cultures):
    desired=set()
    for c in cultures or []:
        desired |= PREF_MAP.get(c,set())
    return desired


def strict_preference_filter(pois, desired):
    # If the user explicitly chose categories, never leak unrelated categories
    # (e.g. cafe when only museum/gallery are selected).
    if not desired:
        return [p for p in pois if p.get("category") != "food"]
    return [p for p in pois if p.get("category") in desired]


def normalize_area_name(text):
    text=clean_text(text)
    aliases={"서울특별시":"서울","부산광역시":"부산","대구광역시":"대구","인천광역시":"인천","광주광역시":"광주","대전광역시":"대전","울산광역시":"울산","세종특별자치시":"세종","경기도":"경기","강원특별자치도":"강원","충청북도":"충북","충청남도":"충남","전북특별자치도":"전북","전라북도":"전북","전라남도":"전남","경상북도":"경북","경상남도":"경남","제주특별자치도":"제주"}
    for full,short in aliases.items():
        if full in text: return short
    return text


def infer_tourapi_area_code(*texts):
    combined=" ".join(clean_text(x) for x in texts if x); short=normalize_area_name(combined)
    try: codes=kto_area_codes()
    except Exception: return None
    for item in codes:
        name=clean_text(item.get("name")); code=clean_text(item.get("code"))
        if name and code and (name in combined or normalize_area_name(name) in short): return code
    return None


CULTURE_KEYWORDS={"박물관":["박물관"],"미술관":["미술관","갤러리"],"전시":["전시관"],"카페":["카페"],"공연":["공연장"],"역사문화":["기념관","역사"],"축제행사":["축제"]}


def filter_places_by_radius(items,lat,lon,radius):
    return [p for p in parse_kto_places(items,lat,lon) if haversine_m(lat,lon,p["lat"],p["lon"])<=radius]


def kto_area_fallback_places(lat,lon,radius,cultures,area_code):
    if not area_code: return [],[],[]
    items=[]; used=[]; errors=[]
    for ctype in content_types_for_preferences(cultures):
        try:
            items.extend(kto_area_list(area_code,ctype,100)); used.append(f"areaBasedList2:{ctype}")
        except Exception as e: errors.append(f"areaBasedList2/{ctype}: {e}")
    return filter_places_by_radius(items,lat,lon,radius*1.35),used,errors


def kto_keyword_fallback_places(lat,lon,radius,cultures):
    items=[]; used=[]; errors=[]; keywords=[]
    for culture in cultures or []:
        for kw in CULTURE_KEYWORDS.get(culture,[]):
            if kw not in keywords: keywords.append(kw)
    for kw in keywords[:3]:
        try:
            items.extend(kto_keyword_search(kw,None,50)); used.append(f"searchKeyword2:{kw}")
        except Exception as e: errors.append(f"searchKeyword2/{kw}: {e}")
    return filter_places_by_radius(items,lat,lon,radius*1.5),used,errors


def kto_active_festival_places(lat,lon,radius,area_code=None):
    now=datetime.now(KST).date(); start=(now-timedelta(days=45)).strftime("%Y%m%d"); end=(now+timedelta(days=30)).strftime("%Y%m%d"); today=now.strftime("%Y%m%d")
    raw=kto_festival_search(start,end,area_code,100); active=[]
    for it in raw:
        s=clean_text(it.get("eventstartdate")); e=clean_text(it.get("eventenddate"))
        if s and e and not (s<=today<=e): continue
        active.append(it)
    places=filter_places_by_radius(active,lat,lon,radius*1.5)
    for p in places:
        p["category"]="festival"; p["category_label"]="축제·공연·행사"; p["visit_minutes"]=max(45,int(p.get("visit_minutes") or 60))
    return places


def flatten_repeat_info(items):
    out=[]
    for row in items or []:
        pieces=[]
        for key in ("infoname","infotext","subname","subdetailoverview","subdetailalt","menu","treatmenu"):
            val=clean_text((row or {}).get(key))
            if val and val not in pieces: pieces.append(val)
        if pieces: out.append(" · ".join(pieces))
    return out[:8]


def summarize_pet_info(pet):
    if not pet: return None
    pieces=[]
    for key in ("acmpyTypeCd","relaAcdntRiskMtr","etcAcmpyInfo","relaPosesFclty","acmpyPsblCpam","acmpyNeedMtr","petTursmInfo"):
        val=clean_text(pet.get(key))
        if val: pieces.append(val)
    if not pieces:
        for value in pet.values():
            val=clean_text(value)
            if val and val not in pieces: pieces.append(val)
    return " · ".join(pieces[:5]) if pieces else None


def tour_search_radius(walk, transport, remaining):
    """Search radius is a discovery radius, not a walking distance limit.

    The old version used walking preference only, so a user who chose
    '가볍게 + 대중교통 + 3시간' still searched just 900 m.
    """
    remaining = max(20, min(int(remaining), 720))

    if transport == "대중교통":
        # 3h -> about 8.1 km, capped at 12 km.
        return int(min(12000, max(4500, remaining * 45)))

    if transport == "택시·차량":
        return int(min(18000, max(6500, remaining * 60)))

    if transport == "상황에 따라":
        return int(min(7000, max(2500, remaining * 28)))

    base = {"가볍게": 1500, "보통": 2500, "많이": 4000}.get(walk, 2500)
    time_cap = max(base, min(5000, remaining * 18))
    return int(time_cap)


def merge_places(*groups):
    out = []
    seen = set()
    for group in groups:
        for p in group or []:
            key = p.get("contentid") or (
                p.get("name"),
                round(float(p.get("lat") or 0), 5),
                round(float(p.get("lon") or 0), 5),
            )
            if key in seen:
                continue
            seen.add(key)
            out.append(p)
    return out


def search_tourapi_candidates(origin,dest,radius,cultures,desired,remaining):
    all_places=[]; errors=[]; successful_calls=0; searched=[]; endpoints_used=[]
    area_code=infer_tourapi_area_code(origin.get("displayName"),origin.get("name"),dest.get("displayName"),dest.get("name"))
    def merge(label,places,labels,errs=None):
        nonlocal all_places,successful_calls,endpoints_used
        all_places=merge_places(all_places,places or []); successful_calls+=len(labels or []); endpoints_used.extend(labels or [])
        if errs: errors.extend(errs)
        searched.append({"label":label,"raw_count":len(places or []),"api_ok":bool(labels)})
    def loc(label,lat,lon,r):
        try:
            places,providers,errs=kto_nearby_places(lat,lon,r,cultures); merge(label,places,[f"locationBasedList2:{x}" for x in providers],errs)
        except Exception as e: errors.append(f"{label}: {e}"); searched.append({"label":label,"raw_count":0,"api_ok":False})
    loc("현재 위치",origin["lat"],origin["lon"],radius)
    if "축제행사" in (cultures or []):
        try: merge("오늘의 축제·행사",kto_active_festival_places(origin["lat"],origin["lon"],radius,area_code),["searchFestival2"])
        except Exception as e: errors.append(f"searchFestival2: {e}")
    matched=strict_preference_filter(all_places,desired)
    if len(matched)<4 and area_code:
        try:
            p,l,e=kto_area_fallback_places(origin["lat"],origin["lon"],radius,cultures,area_code); merge("지역기반 보강",p,l,e)
        except Exception as e: errors.append(f"areaBasedList2: {e}")
        matched=strict_preference_filter(all_places,desired)
    if len(matched)<4:
        try:
            p,l,e=kto_keyword_fallback_places(origin["lat"],origin["lon"],radius,cultures); merge("키워드 보강",p,l,e)
        except Exception as e: errors.append(f"searchKeyword2: {e}")
        matched=strict_preference_filter(all_places,desired)
    # Long gaps need route-aware discovery even when the current-location query
    # already returned many raw candidates. Otherwise a Monday closure can turn
    # "8 candidates" into only 1 usable recommendation without searching farther.
    if remaining>=150:
        loc("다음 약속 주변",dest["lat"],dest["lon"],min(radius,8000))
        matched=strict_preference_filter(all_places,desired)
        loc(
            "이동 동선 중간",
            (origin["lat"]+dest["lat"])/2,
            (origin["lon"]+dest["lon"])/2,
            min(radius,6500)
        )
        matched=strict_preference_filter(all_places,desired)
    elif len(matched)<6 and remaining>=75:
        loc("다음 약속 주변",dest["lat"],dest["lon"],min(radius,8000))
        matched=strict_preference_filter(all_places,desired)

    return {"all_places":all_places,"matched_places":matched,"api_ok":successful_calls>0,"successful_calls":successful_calls,"errors":errors,"searched":searched,"radius_m":radius,"area_code":area_code,"endpoints_used":sorted(set(endpoints_used))}


def safe_fallback(origin, dest, remaining, meal_reserve, mode, mode_label, reason=None):
    direct=approx_minutes(origin["lat"],origin["lon"],dest["lat"],dest["lon"], mode)
    buffer=remaining-direct-meal_reserve
    if buffer < 15:
        return []
    dwell=max(0,min(25,int(buffer-15)))
    return [{
        "kind":"single","name":"다음 약속 장소로 미리 이동",
        "lat":dest["lat"],"lon":dest["lon"],"category":"safe_plan","category_label":"안전 플랜",
        "indoor":False,"visit_minutes":dwell,"distance_m":round(haversine_m(origin["lat"],origin["lon"],dest["lat"],dest["lon"])),
        "walk_to_minutes":max(1,round(direct)),"walk_to_destination_minutes":0,
        "walk_to_distance_m":0,"destination_distance_m":0,"total_minutes":round(direct+dwell),
        "buffer_minutes":round(remaining-direct-dwell-meal_reserve),"feasible":True,"score":1,
        "reason":reason or "한국관광공사 관광정보를 불러오지 못해도 다음 약속에는 늦지 않는 안전 플랜이야",
        "route_quality":"estimate","travel_mode_label":mode_label,"fallback_plan":True,
        "opening_hours":None,"description":"다음 일정 장소로 먼저 이동한 뒤 주변에서 쉬는 가장 안전한 선택이야.",
        "image_url":None,"contentid":None,"contenttypeid":None,"source":"안전 플랜",
        "map_url":google_place_search_url(dest.get("name") or "다음 약속 장소", dest.get("displayName"))
    }]




PAIR_MIN_VISIT = {
    "museum":25,
    "gallery":25,
    "arts":20,
    "history":25,
    "theatre":30,
    "cafe":20,
    "festival":30,
    "park":25,
    "attraction":25,
    "other":20,
}

PAIR_MAX_VISIT = {
    "museum":75,
    "gallery":65,
    "arts":60,
    "history":65,
    "theatre":60,
    "cafe":40,
    "festival":90,
    "park":60,
    "attraction":60,
    "other":50,
}


def compact_visit_floor(place):
    preferred=max(10,int(place.get("visit_minutes") or 30))
    floor=PAIR_MIN_VISIT.get(place.get("category"),20)
    return max(10,min(preferred,floor))


def pair_visit_cap(place):
    preferred=max(10,int(place.get("visit_minutes") or 30))
    return max(preferred,PAIR_MAX_VISIT.get(place.get("category"),preferred))


def _spread_minutes(current, caps, extra):
    """Distribute extra minutes proportionally without exceeding caps."""
    cur=list(current)
    extra=max(0,int(extra))
    while extra>0:
        deficits=[max(0,int(caps[i])-int(cur[i])) for i in range(len(cur))]
        total=sum(deficits)
        if total<=0:
            break
        progressed=False
        for i,d in enumerate(deficits):
            if d<=0 or extra<=0:
                continue
            add=max(1,min(d,round(extra*d/total)))
            add=min(add,extra)
            cur[i]+=add
            extra-=add
            progressed=True
        if not progressed:
            break
    return cur


def allocate_pair_visit_minutes(a,b,travel_total,remaining,meal_reserve):
    """Fit two stops while preserving 15–25 min appointment safety margin.

    Start from a compact '핵심 관람' duration, then use spare time to approach
    each place's preferred duration and finally a sensible soft maximum.
    """
    minimum=[compact_visit_floor(a),compact_visit_floor(b)]
    preferred=[
        max(minimum[0],int(a.get("visit_minutes") or 30)),
        max(minimum[1],int(b.get("visit_minutes") or 30)),
    ]
    maximum=[pair_visit_cap(a),pair_visit_cap(b)]

    # Aim for ~20 min. If needed, allow down to the hard 15-min safety buffer.
    target_buffer=20
    budget=int(remaining-meal_reserve-travel_total-target_buffer)
    if budget < sum(minimum):
        target_buffer=15
        budget=int(remaining-meal_reserve-travel_total-target_buffer)
    if budget < sum(minimum):
        return None

    allocated=list(minimum)
    spare=budget-sum(allocated)

    # First restore normal preferred dwell.
    allocated=_spread_minutes(allocated,preferred,spare)
    spare=budget-sum(allocated)

    # If a lot of time is still left, allow a fuller visit instead of
    # leaving 40+ minutes unused before the appointment.
    if spare>0:
        allocated=_spread_minutes(allocated,maximum,spare)

    return int(allocated[0]),int(allocated[1])



def build_pair_itineraries(top, origin, dest, remaining, meal_reserve, transport, rainy, now_dt):
    if remaining-meal_reserve < 90 or len(top) < 2:
        return []

    pairs = []
    # Keep route API usage bounded while giving the itinerary builder
    # enough alternatives to fill a meaningful gap.
    limit = min(len(top), 5)

    origin_legs = {}
    dest_legs = {}
    for i in range(limit):
        p = top[i]
        origin_legs[i] = travel_leg(origin, p, transport)
        dest_legs[i] = travel_leg(p, dest, transport)

    for ai in range(limit):
        for bi in range(limit):
            if ai == bi:
                continue
            a, b = top[ai], top[bi]

            same_category = a.get("category") == b.get("category")
            # Repeating cafes was the original bad case. Cultural venues can
            # reasonably form a themed course when time/route/related-data fit.
            if same_category and a.get("category") == "cafe":
                continue

            af = normalized_place_family(a.get("name"))
            bf = normalized_place_family(b.get("name"))
            if af and bf and (af == bf or (len(af)>=5 and len(bf)>=5 and (af in bf or bf in af))):
                continue

            l1 = origin_legs[ai]
            l2 = travel_leg(a, b, transport)
            l3 = dest_legs[bi]

            travel_total=l1["minutes"]+l2["minutes"]+l3["minutes"]
            allocation=allocate_pair_visit_minutes(
                a,b,travel_total,remaining,meal_reserve
            )
            if not allocation:
                continue
            va,vb=allocation

            preferred_a=int(a.get("visit_minutes") or 30)
            preferred_b=int(b.get("visit_minutes") or 30)

            arrival_a = now_dt + timedelta(minutes=l1["minutes"])
            status_a = evaluate_opening_status(
                a.get("opening_hours"), a.get("rest_date"), arrival_a, va
            )
            if status_a["status"] == "closed":
                continue

            arrival_b = arrival_a + timedelta(minutes=va + l2["minutes"])
            status_b = evaluate_opening_status(
                b.get("opening_hours"), b.get("rest_date"), arrival_b, vb
            )
            if status_b["status"] == "closed":
                continue

            total = travel_total + va + vb
            buffer = remaining - total - meal_reserve
            if buffer < 15:
                continue

            score = 100
            if same_category:
                score -= 6
            if rainy and a.get("indoor") and b.get("indoor"):
                score += 7
            if 15 <= buffer <= 25:
                score += 16
            elif buffer <= 35:
                score += 8
            elif buffer > 35:
                score -= 5
            if l2["minutes"] <= 20:
                score += 6
            related_rank=related_pair_rank(a,b)
            related_match=related_rank is not None
            if related_rank is not None:
                score += 20 if related_rank<=5 else (16 if related_rank<=10 else (10 if related_rank<=20 else 6))
            if status_a["status"] == "closing_soon" or status_b["status"] == "closing_soon":
                score -= 8

            reasons = ["남는 시간을 활용해서 2곳 코스로 묶었어"]
            if va<preferred_a or vb<preferred_b:
                reasons.append("각 장소는 핵심만 짧게 둘러보는 일정")
            if same_category:
                reasons.append("같은 문화 테마지만 서로 다른 장소로 구성")
            else:
                reasons.append("서로 다른 분위기로 골랐어")
            if related_match:
                reasons.append(f"관광공사 연관관광지 {related_rank}위 조합")
            if l1["quality"] == l2["quality"] == l3["quality"] == "actual" and transport == "대중교통":
                reasons.append("카카오 대중교통 실제 경로시간 반영")
            if rainy and a.get("indoor") and b.get("indoor"):
                reasons.append("둘 다 실내")
            reasons.append(f"약속 전 {round(buffer)}분 여유")

            stops = []
            for p, st, arr, actual_visit, preferred_visit in (
                (a,status_a,arrival_a,va,preferred_a),
                (b,status_b,arrival_b,vb,preferred_b),
            ):
                item = {k:p.get(k) for k in (
                    "name","lat","lon","category","category_label","indoor",
                    "opening_hours","rest_date","website","description","image_url","map_url",
                    "address","contentid","contenttypeid","source","tel",
                    "concentration","accessibility","related_names","related_items","regional_visitors","pet_info",
                    "areacode","sigungucode","ldong_regn_cd","ldong_signgu_cd"
                )}
                item["visit_minutes"] = int(actual_visit)
                item["recommended_visit_minutes"] = int(preferred_visit)
                item["compact_visit"] = int(actual_visit) < int(preferred_visit)
                item["opening_status"] = st
                item["planned_visit_at"] = arr.astimezone(KST).isoformat()
                stops.append(item)

            pairs.append({
                "kind":"itinerary",
                "name":f"{a['name']} → {b['name']}",
                "category":"itinerary",
                "category_label":"2곳 코스",
                "stops":stops,
                "visit_minutes":va+vb,
                "walk_to_minutes":max(1,round(l1["minutes"])),
                "between_minutes":max(1,round(l2["minutes"])),
                "walk_to_destination_minutes":max(1,round(l3["minutes"])),
                "total_minutes":round(total),
                "buffer_minutes":round(buffer),
                "feasible":True,
                "score":round(score),
                "reason":" · ".join(reasons[:4]),
                "route_quality":"actual" if l1["quality"]==l2["quality"]==l3["quality"]=="actual" else "estimate",
                "travel_mode_label":l1["label"] if transport=="대중교통" else transport_mode(transport)[1],
                "fallback_plan":False,
                "opening_hours":None,
                "map_url":None,
                "transit_legs":[
                    {"label":"지금 → 1번", **l1},
                    {"label":"1번 → 2번", **l2},
                    {"label":"2번 → 약속", **l3},
                ] if transport=="대중교통" else None,
            })

    best={}
    for r in pairs:
        key=tuple(sorted(s["name"] for s in r["stops"]))
        if key not in best or r["score"]>best[key]["score"] or (
            r["score"]==best[key]["score"] and r["total_minutes"]<best[key]["total_minutes"]
        ):
            best[key]=r

    out=list(best.values())
    out.sort(key=lambda r:(-r["score"],r["total_minutes"],-r["buffer_minutes"]))
    return out[:3]


def build_recommendations(body):
    origin=body["origin"]; dest=body["destination"]
    o_lat=safe_float(origin["lat"],-90,90); o_lon=safe_float(origin["lon"],-180,180)
    d_lat=safe_float(dest["lat"],-90,90); d_lon=safe_float(dest["lon"],-180,180)
    remaining=int(body.get("remaining_minutes",60))
    if not 20<=remaining<=720: raise ValueError("remaining")
    prefs=body.get("preferences") or {}
    walk=prefs.get("walk","보통"); cultures=prefs.get("culture") or []
    transport=prefs.get("transport","도보"); meal_time=prefs.get("mealTime","상관없음"); companion=prefs.get("companion","혼자")
    try: stroll=max(20,min(90,int(prefs.get("strollMinutes",40))))
    except Exception: stroll=40
    meal_reserve=reserve_meal_minutes(meal_time,remaining)
    mode,mode_label=transport_mode(transport)
    desired=preference_categories(cultures)

    weather=weather_at(o_lat,o_lon,min(remaining,360))

    origin_point={"lat":o_lat,"lon":o_lon,"name":origin.get("name"),"displayName":origin.get("displayName")}
    dest_point={"lat":d_lat,"lon":d_lon,"name":dest.get("name"),"displayName":dest.get("displayName")}
    radius=tour_search_radius(walk,transport,remaining)

    search=search_tourapi_candidates(
        origin_point,
        dest_point,
        radius,
        cultures,
        desired,
        remaining,
    )
    all_pois=search["all_places"]
    pois=search["matched_places"]
    poi_error=" | ".join(search["errors"]) if search["errors"] else None

    tourapi_status={
        "api_ok": search["api_ok"],
        "search_radius_m": radius,
        "searched": search["searched"],
        "raw_count": len(all_pois),
        "matched_count": len(pois),
        "selected_preferences": cultures,
        "transport": transport,
        "companion": companion,
        "area_code": search.get("area_code"),
        "endpoints_used": search.get("endpoints_used") or [],
    }

    if not pois:
        if search["api_ok"] and not all_pois:
            reason=(
                f"지금 동선 주변에서 조건에 맞는 관광지를 찾기 어려웠어. "
                f"현재 이동수단({transport}) 기준 최대 약 {radius/1000:.1f}km까지 확인했어."
            )
        elif search["api_ok"] and all_pois and desired:
            chosen=" · ".join(cultures)
            reason=(
                f"관광정보에서 후보 {len(all_pois)}개는 찾았지만 "
                f"선택한 취향({chosen}) 분류에 맞는 장소가 없었어. 다른 종류 장소는 섞지 않았어."
            )
        else:
            reason="관광정보를 잠시 불러오지 못해 다음 약속에 늦지 않는 안전한 동선으로 안내할게."
            if poi_error:
                sys.stderr.write(f"[Yeobaek TourAPI] {poi_error}\n")

        fb=safe_fallback(
            origin_point,dest_point,remaining,meal_reserve,mode,mode_label,reason
        )
        return {
            "ok":True,
            "recommendations":fb,
            "weather":weather,
            "radius_m":round(radius),
            "meal_reserve_minutes":meal_reserve,
            "meal_time":meal_time if meal_reserve else None,
            "degraded":not search["api_ok"],
            "strict_preference":bool(desired),
            "itinerary_count":0,
            "message":None if fb else "지금은 바로 다음 약속 장소로 이동하는 게 안전해.",
            "provider_warning":reason,
            "tourapi_status":tourapi_status,
            "kto_extra_status":kto_extra_service_state(),
            "transit_status":transit_route_status(),
        }

    rainy=bool(weather.get("rainy"))
    pre=[]
    for p in pois:
        a=approx_minutes(o_lat,o_lon,p["lat"],p["lon"],mode)
        b=approx_minutes(p["lat"],p["lon"],d_lat,d_lon,mode)
        visit=stroll if p["category"] in {"park","attraction"} else p["visit_minutes"]
        p["visit_minutes"]=visit

        # Keep candidates that can work either as a normal single visit OR
        # as a shorter stop inside a two-place itinerary.
        compact=compact_visit_floor(p)
        if a+compact+b+meal_reserve+15 <= remaining+10:
            pre.append((a+compact+b,p))
    pre.sort(key=lambda x:x[0])
    top=[p for _,p in pre[:12]]

    # TourAPI detailIntro2 is cached; use it now so opening checks are part
    # of the recommendation itself rather than merely decorative text later.
    for p in top:
        enrich_operating(p)
        if companion=="반려동물":
            try:
                pet=kto_pet_detail(p.get("contentid")); p["pet_info"]=summarize_pet_info(pet); p["pet_checked"]=True
                if "detailPetTour2" not in tourapi_status["endpoints_used"]: tourapi_status["endpoints_used"].append("detailPetTour2")
            except Exception: p["pet_checked"]=False

    kto_extra = enrich_kto_extra_recommendation_data(
        top, companion, walk, remaining,
        " ".join(x for x in (origin.get("displayName"),origin.get("name")) if x)
    ) if top else {
        "used":[],"related_hits":0,"concentration_hits":0,"accessibility_hits":0,
        "visitor_context":None,"durunubi":None,"wellness_count":0,
        "ecotour_count":0,"region_insights":[],"errors":[]
    }
    tourapi_status["extra_used"] = kto_extra.get("used") or []
    tourapi_status["extra_data_count"] = len(tourapi_status["extra_used"])
    tourapi_status["visitor_context"] = kto_extra.get("visitor_context")
    tourapi_status["durunubi"] = kto_extra.get("durunubi")
    tourapi_status["wellness_count"] = kto_extra.get("wellness_count",0)
    tourapi_status["ecotour_count"] = kto_extra.get("ecotour_count",0)
    tourapi_status["region_insights"] = kto_extra.get("region_insights") or []

    if not top:
        reason="선택한 취향 장소는 찾았지만, 다음 약속까지 안전하게 다녀올 시간은 부족해."
        fb=safe_fallback({"lat":o_lat,"lon":o_lon},{"lat":d_lat,"lon":d_lon},remaining,meal_reserve,mode,mode_label,reason)
        return {"ok":True,"recommendations":fb,"weather":weather,"radius_m":round(radius),"strict_preference":bool(desired),
                "itinerary_count":0,"degraded":False,"message":None,"provider_warning":reason,
                "tourapi_status":tourapi_status,
            "kto_extra_status":kto_extra_service_state(),
            "transit_status":transit_route_status()}

    now_dt = datetime.now(KST)

    durations=distances=None
    if mode in {"walk","car"} and top:
        coords=[(o_lat,o_lon)]+[(p["lat"],p["lon"]) for p in top]+[(d_lat,d_lon)]
        try:
            table=osrm_table(coords,mode)
            durations=table.get("durations")
            distances=table.get("distances")
        except Exception:
            pass
    dest_idx=len(top)+1

    singles=[]
    rejection_stats={"closed":0,"time_short":0,"other":0}
    for i,p in enumerate(top,1):
        p_point={**p}
        origin_named={"lat":o_lat,"lon":o_lon,"name":origin.get("name") or "현재 위치"}
        dest_named={"lat":d_lat,"lon":d_lon,"name":dest.get("name") or "다음 약속"}

        if transport=="대중교통":
            leg1_info=travel_leg(origin_named,p_point,transport)
            leg2_info=travel_leg(p_point,dest_named,transport)
            leg1=leg1_info["minutes"]
            leg2=leg2_info["minutes"]
            quality="actual" if leg1_info["quality"]==leg2_info["quality"]=="actual" else "estimate"
            dist1=p.get("distance_m") or 0
            dist2=round(haversine_m(p["lat"],p["lon"],d_lat,d_lon))
            mode_display="카카오 대중교통" if quality=="actual" else "대중교통(예상)"
        elif durations and durations[0][i] is not None and durations[i][dest_idx] is not None:
            leg1=durations[0][i]/60
            leg2=durations[i][dest_idx]/60
            dist1=distances[0][i] if distances else p["distance_m"]
            dist2=distances[i][dest_idx] if distances else None
            quality="actual"
            leg1_info={"minutes":leg1,"quality":"actual","label":mode_label,"route":None}
            leg2_info={"minutes":leg2,"quality":"actual","label":mode_label,"route":None}
            mode_display=mode_label
        else:
            leg1=approx_minutes(o_lat,o_lon,p["lat"],p["lon"],mode)
            leg2=approx_minutes(p["lat"],p["lon"],d_lat,d_lon,mode)
            dist1=p["distance_m"]
            dist2=round(haversine_m(p["lat"],p["lon"],d_lat,d_lon)*1.25)
            quality="estimate"
            leg1_info={"minutes":leg1,"quality":"estimate","label":mode_label,"route":None}
            leg2_info={"minutes":leg2,"quality":"estimate","label":mode_label,"route":None}
            mode_display=mode_label

        visit=int(p.get("visit_minutes") or 30)
        arrival=now_dt + timedelta(minutes=leg1)
        opening_status=evaluate_opening_status(
            p.get("opening_hours"),p.get("rest_date"),arrival,visit
        )

        total=leg1+visit+leg2
        buffer=remaining-total-meal_reserve
        feasible=buffer>=15 and opening_status["status"]!="closed"

        if opening_status["status"]=="closed":
            rejection_stats["closed"]+=1
        elif buffer<15:
            rejection_stats["time_short"]+=1

        score=68
        if desired: score+=20
        if rainy and p["indoor"]: score+=16
        elif rainy and not p["indoor"]: score-=16
        if p["distance_m"]<600: score+=8
        if 15<=buffer<=35: score+=12
        elif buffer>35: score+=6
        if opening_status["status"]=="open": score+=12
        elif opening_status["status"]=="closing_soon": score-=8
        elif opening_status["status"]=="unknown": score-=1
        if quality=="actual" and transport=="대중교통": score+=8

        concentration=p.get("concentration") or {}
        try:
            c_rate=float(concentration.get("rate"))
            if c_rate < 35: score+=7
            elif c_rate >= 80: score-=14
            elif c_rate >= 60: score-=7
        except Exception:
            pass

        regional=p.get("regional_visitors") or {}
        flow=regional.get("relative_level")
        if flow=="낮음": score+=4
        elif flow=="높음": score-=5

        accessibility=p.get("accessibility") or {}
        if companion in {"가족","아이 동반"} and accessibility.get("available"):
            score+=8

        if companion=="반려동물":
            if p.get("pet_info"): score+=10
            elif p.get("pet_checked"): score-=4
        if not feasible: score-=60

        reasons=[]
        if desired: reasons.append("선택한 취향에 맞아")
        if opening_status["status"]=="open": reasons.append(f"{opening_status['planned_time']} 방문 시 영업중")
        elif opening_status["status"]=="closing_soon": reasons.append("도착 시 마감이 가까워")
        if quality=="actual" and transport=="대중교통": reasons.append("카카오 대중교통 실제 경로시간 반영")
        if p.get("concentration"):
            label=(p.get("concentration") or {}).get("label")
            if label: reasons.append(f"관광공사 집중률 {label}")
        flow=(p.get("regional_visitors") or {}).get("relative_level")
        if flow=="낮음": reasons.append("최근 지역 방문 흐름 비교적 여유")
        elif flow=="높음": reasons.append("최근 지역 방문 수요 높음")
        if companion in {"가족","아이 동반"} and (p.get("accessibility") or {}).get("available"):
            reasons.append("무장애·가족 편의정보 확인")
        if companion=="반려동물" and p.get("pet_info"): reasons.append("관광공사 반려동물 동반정보 확인")
        if rainy and p["indoor"]: reasons.append("비 와도 괜찮아")
        if feasible: reasons.append(f"약속 전 {max(0,round(buffer))}분 정도 남아")

        singles.append({
            **p,
            "kind":"single",
            "walk_to_minutes":max(1,round(leg1)),
            "walk_to_destination_minutes":max(0,round(leg2)),
            "walk_to_distance_m":round(dist1 or 0),
            "destination_distance_m":round(dist2 or 0),
            "visit_minutes":visit,
            "total_minutes":round(total),
            "buffer_minutes":round(buffer),
            "feasible":feasible,
            "score":round(score),
            "reason":" · ".join(reasons[:4]) or "동선이 비교적 단순해",
            "route_quality":quality,
            "travel_mode_label":mode_display,
            "fallback_plan":False,
            "opening_status":opening_status,
            "planned_visit_at":arrival.astimezone(KST).isoformat(),
            "transit_legs":[
                {"label":"지금 → 장소", **leg1_info},
                {"label":"장소 → 약속", **leg2_info},
            ] if transport=="대중교통" else None,
        })

    evaluated_single_count=len(singles)
    singles=[r for r in singles if r["feasible"]]
    singles.sort(key=lambda r:(-r["score"],-r["buffer_minutes"],r["total_minutes"]))

    itineraries=build_pair_itineraries(
        top,
        {"lat":o_lat,"lon":o_lon,"name":origin.get("name") or "현재 위치"},
        {"lat":d_lat,"lon":d_lon,"name":dest.get("name") or "다음 약속"},
        remaining,meal_reserve,transport,rainy,now_dt
    )

    large_unused_gap=bool(singles and singles[0].get("buffer_minutes",0)>=30)

    if itineraries and large_unused_gap:
        # A large single-stop buffer means the core product has more "여백"
        # available to turn into an experience. Put courses first.
        recs=itineraries[:3] + singles[:3]
    elif itineraries:
        recs=itineraries[:2] + singles[:4]
    else:
        recs=singles[:5]

    if not recs:
        reason="선택한 취향 장소는 찾았지만, 방문 예정 시간에 영업 중이면서 다음 약속까지 안전하게 갈 수 있는 후보가 없었어."
        recs=safe_fallback(
            {"lat":o_lat,"lon":o_lon},
            {"lat":d_lat,"lon":d_lon},
            remaining,meal_reserve,mode,mode_label,reason
        )

    tourapi_status["matched_count"]=len(pois)
    tourapi_status["usable_count"]=len(top)

    recommendation_diagnostics={
        "matched_candidates":len(pois),
        "pre_feasible_candidates":len(top),
        "evaluated_candidates":evaluated_single_count,
        "single_recommendations":len(singles),
        "itinerary_recommendations":len(itineraries),
        "closed_excluded":rejection_stats["closed"],
        "time_excluded":rejection_stats["time_short"],
        "returned_recommendations":len(recs),
        "single_large_unused_gap":large_unused_gap,
    }

    return {"ok":True,"recommendations":recs,"weather":weather,"radius_m":round(radius),
            "meal_reserve_minutes":meal_reserve,"meal_time":meal_time if meal_reserve else None,
            "degraded":not search["api_ok"],"strict_preference":bool(desired),"itinerary_count":len(itineraries),
            "message":None if recs else "지금은 다음 약속 장소로 바로 이동하는 게 안전해.",
            "recommendation_diagnostics":recommendation_diagnostics,
            "tourapi_status":tourapi_status,
            "kto_extra_status":kto_extra_service_state(),
            "transit_status":transit_route_status()}


def route_payload(points, transport="도보"):
    coords=[{
        "lat":safe_float(p["lat"],-90,90),
        "lon":safe_float(p["lon"],-180,180),
        "name":p.get("name") or f"지점 {i+1}",
    } for i,p in enumerate(points)]

    if transport=="대중교통":
        legs=[]
        geometry=[]
        total_minutes=0
        total_distance=0
        all_actual=True
        for i in range(len(coords)-1):
            a,b=coords[i],coords[i+1]
            leg=travel_leg(a,b,transport)
            total_minutes += leg["minutes"]
            if leg["quality"]!="actual" or not leg.get("route"):
                all_actual=False
                geometry.extend([[a["lon"],a["lat"]],[b["lon"],b["lat"]]])
                legs.append({
                    "label":f"{a['name']} → {b['name']}",
                    **leg,
                })
                continue

            route=leg["route"]
            total_distance += route.get("distance_m") or 0
            pts=(route.get("geometry") or {}).get("coordinates") or []
            if geometry and pts and geometry[-1]==pts[0]:
                geometry.extend(pts[1:])
            else:
                geometry.extend(pts)
            legs.append({
                "label":f"{a['name']} → {b['name']}",
                **leg,
            })

        return {
            "ok":True,
            "duration_minutes":round(total_minutes),
            "distance_m":round(total_distance),
            "geometry":{"type":"LineString","coordinates":geometry},
            "estimated":not all_actual,
            "travel_mode_label":"카카오 대중교통" if all_actual else "대중교통(일부 예상)",
            "transit_legs":legs,
            "provider":"Kakao" if all_actual else "fallback",
            "message":None if all_actual else "일부 구간은 카카오 경로를 못 받아 예상시간으로 표시했어.",
        }

    mode,mode_label=transport_mode(transport)
    if mode in {"walk","car"}:
        try:
            raw_coords=[(p["lat"],p["lon"]) for p in coords]
            data=osrm_route(raw_coords,mode)
            r=data["routes"][0]
            return {
                "ok":True,
                "duration_minutes":round(r["duration"]/60),
                "distance_m":round(r["distance"]),
                "geometry":r["geometry"],
                "estimated":False,
                "travel_mode_label":mode_label,
            }
        except Exception:
            pass

    total_d=0
    total_m=0
    line=[]
    for idx,p in enumerate(coords):
        line.append([p["lon"],p["lat"]])
        if idx:
            a=coords[idx-1]
            d=haversine_m(a["lat"],a["lon"],p["lat"],p["lon"])
            total_d+=d
            total_m+=approx_minutes(a["lat"],a["lon"],p["lat"],p["lon"],mode)
    return {
        "ok":True,
        "duration_minutes":round(total_m),
        "distance_m":round(total_d*1.25),
        "geometry":{"type":"LineString","coordinates":line},
        "estimated":True,
        "travel_mode_label":mode_label,
        "message":"실시간 경로 서버 대신 보수적인 예상 동선을 표시했어.",
    }



def crowd_payload(area):
    key=os.getenv("SEOUL_OPEN_DATA_KEY","").strip()
    if not key: return {"ok":True,"enabled":False,"reason":"api_key_missing"}
    if not area: return {"ok":True,"enabled":False,"reason":"area_missing"}
    ck=f"crowd:{area}"
    if (v:=cache_get(ck)) is not None: return v
    try:
        url=f"http://openapi.seoul.go.kr:8088/{key}/json/citydata/1/5/{quote(area,safe='')}"
        raw=fetch_json(url)
        root=raw.get("SeoulRtd.citydata") or raw
        city=root.get("CITYDATA") if isinstance(root,dict) else None
        if isinstance(city,list): city=city[0] if city else None
        if not isinstance(city,dict): return {"ok":True,"enabled":False,"reason":"unsupported_area"}
        live=city.get("LIVE_PPLTN_STTS") or {}
        if isinstance(live,list): live=live[0] if live else {}
        level=live.get("AREA_CONGEST_LVL") or city.get("AREA_CONGEST_LVL")
        if not level: return {"ok":True,"enabled":False,"reason":"no_crowd_data"}
        v={"ok":True,"enabled":True,"area":city.get("AREA_NM") or area,"level":level,
           "message":live.get("AREA_CONGEST_MSG") or city.get("AREA_CONGEST_MSG"),"source":"서울특별시 실시간 도시데이터"}
        cache_set(ck,v,120); return v
    except Exception:
        return {"ok":True,"enabled":False,"reason":"provider_error"}



def self_test():
    d=haversine_m(37.5665,126.9780,37.5665,128.17)
    assert 100000 < d < 120000, d
    assert approx_minutes(37.57,126.98,37.571,126.981,"walk") > 1
    fb=safe_fallback({"lat":37.57,"lon":126.98},{"lat":37.58,"lon":126.99},90,0,"walk","도보")
    assert fb and fb[0]["feasible"]
    if not KAKAO_REST_API_KEY:
        rp=route_payload([{"lat":37.57,"lon":126.98},{"lat":37.58,"lon":126.99}],"대중교통")
        assert rp["ok"] and rp["estimated"]
    test_open=evaluate_opening_status("09:00 ~ 18:00","화요일",datetime(2026,9,7,13,0,tzinfo=KST),30)
    assert test_open["status"]=="open"
    test_closed=evaluate_opening_status("09:00 ~ 18:00","월요일",datetime(2026,9,7,13,0,tzinfo=KST),30)
    assert test_closed["status"]=="closed"
    sample=[{"category":"museum"},{"category":"gallery"},{"category":"cafe"}]
    assert kto_classify({"cat3":"A02060100","contenttypeid":"14","title":"테스트 박물관"})[0]=="museum"
    assert kto_classify({"cat3":"A02060500","contenttypeid":"14","title":"테스트 미술관"})[0]=="gallery"
    assert kto_classify({"cat3":"A05020900","contenttypeid":"39","title":"테스트 카페"})[0]=="cafe"
    filtered=strict_preference_filter(sample,{"museum","gallery"})
    assert len(filtered)==2 and all(x["category"]!="cafe" for x in filtered)
    assert "37." not in google_place_search_url("서울공예박물관","서울 종로구")
    pair_sample=[
        {"name":"카페A","category":"cafe","lat":37.57,"lon":126.98,"visit_minutes":25,"indoor":True},
        {"name":"카페B","category":"cafe","lat":37.571,"lon":126.981,"visit_minutes":25,"indoor":True},
    ]
    assert build_pair_itineraries(
        pair_sample,
        {"lat":37.56,"lon":126.97,"name":"현재"},
        {"lat":37.58,"lon":126.99,"name":"약속"},
        240,0,"도보",False,datetime(2026,9,7,12,0,tzinfo=KST)
    )==[]
    print("[여백] 내부 self-test OK")


