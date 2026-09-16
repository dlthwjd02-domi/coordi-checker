#!/usr/bin/env python3
"""코디 체커 - 상품 URL을 넣으면 이미지/색을 뽑아주는 로컬 서버."""
import datetime as dt
import hashlib
import html as html_mod
import io
import json
import os
import re
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from PIL import Image

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(ROOT, "cache")
PORT = int(os.environ.get("PORT", 8787))

# 새로 내려받은 폴더에는 cache/ 가 없다 (git 추적 제외). 없으면 만든다.
os.makedirs(CACHE, exist_ok=True)

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
}


GEO_API = "https://geocoding-api.open-meteo.com/v1/search"
ARCHIVE_API = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_API = "https://api.open-meteo.com/v1/forecast"


# ---------------------------------------------------------------- 지역 / 기후
# 한국 도시는 "경주시", "강릉시" 로 등록돼 있어서 "경주" 로는 안 잡히고 기차역만 나온다.
KO_SUFFIXES = ["", "시", "군", "구", "특별자치시", "특별자치도"]
# 지명으로 인정할 종류. AIRP(공항)·RSTN(역) 같은 건 제외한다.
PLACE_CODES = ("PPL", "ADM", "ISL", "ISLS", "AREA", "RGN")
# OSM 결과는 인구수가 없어서, 종류별로 대략의 가중치를 줘서 순위를 맞춘다
OSM_WEIGHT = {"city": 3_000_000, "municipality": 1_000_000, "town": 300_000,
              "county": 200_000, "state": 150_000, "province": 150_000, "region": 120_000,
              "island": 80_000, "borough": 80_000, "archipelago": 60_000,
              "suburb": 50_000, "village": 30_000, "hamlet": 10_000}
NOMINATIM = "https://nominatim.openstreetmap.org/search"
OSM_UA = {"User-Agent": "coordi-checker/1.0 (personal outfit planning tool)",
          "Accept-Language": "ko"}


# 한글 지명이 한국어 색인에 없고 로마자로만 있는 경우가 있다 (통영 → Tongyeong).
CHO = ["g","kk","n","d","tt","r","m","b","pp","s","ss","","j","jj","ch","k","t","p","h"]
JUNG = ["a","ae","ya","yae","eo","e","yeo","ye","o","wa","wae","oe","yo","u","wo","we",
        "wi","yu","eu","ui","i"]
JONG = ["","k","k","k","n","n","n","t","l","k","m","p","l","l","p","l","m","p","p","t",
        "t","ng","t","t","k","t","p","t"]


def romanize(text):
    """국어의 로마자 표기법(간이). 음운 변화는 반영하지 않는다."""
    out = []
    for ch in text:
        code = ord(ch) - 0xAC00
        if 0 <= code < 11172:
            out.append(CHO[code // 588] + JUNG[(code % 588) // 28] + JONG[code % 28])
        else:
            out.append(ch)
    return "".join(out)


def _om_places(name):
    try:
        res = requests.get(GEO_API, params={"name": name, "count": 10, "language": "ko"},
                           headers=HEADERS, timeout=12)
        res.raise_for_status()
        return res.json().get("results", []) or []
    except Exception:
        return []


def _osm_places(query):
    try:
        res = requests.get(NOMINATIM, params={
            "q": query, "format": "jsonv2", "limit": 10,
            "accept-language": "ko", "addressdetails": 1,
        }, headers=OSM_UA, timeout=10)
        res.raise_for_status()
        return res.json() or []
    except Exception:
        return []


def search_places(q):
    q = (q or "").strip()
    if not q:
        return []

    # "대한민국, 경주" / "캐나다 밴쿠버" 처럼 나라를 같이 적는 경우
    parts = [p.strip() for p in re.split(r"[,/·]| {2,}", q) if p.strip()]
    hint, target = (parts[0], parts[-1]) if len(parts) >= 2 else ("", q)

    found, seen = [], {}

    def add(item):
        # 좌표가 겹치거나 같은 나라의 같은 이름이면 한 곳으로 본다
        keys = [(round(item["lat"], 2), round(item["lon"], 2)),
                (item["name"].lower(), item["country"])]
        prev = next((seen[k] for k in keys if k in seen), None)
        if prev:
            prev["pop"] = max(prev["pop"], item["pop"])       # 더 센 쪽 점수를 따른다
            if (item["name"].lower() == target.lower()
                    and prev["name"].lower() != target.lower()):
                prev["name"] = item["name"]                   # 적은 대로 맞는 이름을 쓴다
            if not prev["where"]:
                prev["where"] = item["where"]
            for k in keys:
                seen.setdefault(k, prev)
            return
        for k in keys:
            seen[k] = item
        found.append(item)

    # 접미사를 붙인 질의를 한꺼번에 던진다 (순차로 하면 한 검색에 3초씩 걸린다)
    hangul = bool(re.search(r"[가-힣]", target))
    if hangul:
        roman = romanize(target)
        queries = [target + sfx for sfx in KO_SUFFIXES] + [roman, roman + "-si"]
    else:
        queries = [target]
    # 두 곳을 한꺼번에 조회한다. Open-Meteo 는 접두어 매칭이라 놓치는 게 많고,
    # OSM 은 자유 입력에 강한 대신 역·학교 같은 것도 섞여 나온다.
    with ThreadPoolExecutor(max_workers=len(queries) + 1) as pool:
        om_jobs = [pool.submit(_om_places, qq) for qq in queries]
        osm_job = pool.submit(_osm_places, f"{hint}, {target}" if hint else target)

        for job in om_jobs:                      # 한국어 이름이 나오는 쪽을 먼저 담는다
            for r in job.result():
                if not (r.get("feature_code") or "").startswith(PLACE_CODES):
                    continue
                where = " · ".join(x for x in (r.get("country"), r.get("admin1"))
                                   if x and x != r["name"])
                add({"name": r["name"], "where": where, "country": r.get("country") or "",
                     "lat": round(r["latitude"], 4), "lon": round(r["longitude"], 4),
                     "pop": r.get("population") or 0})

        for r in osm_job.result():
            kind = r.get("addresstype") or r.get("type")
            if kind not in OSM_WEIGHT:
                continue                         # 역·학교·식당 등은 버린다
            addr = r.get("address") or {}
            name = r.get("name") or (r.get("display_name") or "").split(",")[0]
            region = addr.get("state") or addr.get("province") or addr.get("county") or ""
            where = " · ".join(x for x in (addr.get("country"), region) if x and x != name)
            add({"name": name, "where": where, "country": addr.get("country") or "",
                 "lat": round(float(r["lat"]), 4), "lon": round(float(r["lon"]), 4),
                 "pop": OSM_WEIGHT[kind]})

    def score(item):
        s = item["pop"]
        if hint and hint in (item["country"] + " " + item["where"]):
            s += 5_000_000                       # 사용자가 적은 나라를 위로
        lowered = item["name"].lower()
        if lowered == target.lower() or item["name"].startswith(target):
            s += 100_000                         # 적은 이름과 그대로 맞는 것
        if hangul and lowered == romanize(target).lower():
            s += 100_000                         # 로마자로 찾은 같은 이름
        return -s

    found.sort(key=score)
    # 로마자로 찾았으면 사용자가 적은 한글로 보여준다 (Tongyeong → 통영)
    if hangul:
        roman_low = romanize(target).lower()
        for item in found:
            if item["name"].lower() == roman_low:
                item["name"] = target
    for item in found:
        item.pop("pop", None)
        item.pop("src", None)
    return found[:8]


def climate(lat, lon, month):
    """최근 3년 같은 달의 실제 관측치를 평균내서 평년값 대신 쓴다."""
    key = "clim_%.2f_%.2f_%02d.json" % (lat, lon, month)
    path = os.path.join(CACHE, key)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    year = dt.date.today().year
    try:
        res = requests.get(ARCHIVE_API, params={
            "latitude": lat, "longitude": lon,
            "start_date": f"{year - 3}-01-01", "end_date": f"{year - 1}-12-31",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,snowfall_sum",
            "timezone": "auto",
        }, timeout=30)
        res.raise_for_status()
    except requests.exceptions.RequestException:
        # 기온은 Open-Meteo 에서 가져오므로, 쇼핑몰 오류처럼 보이지 않게 따로 안내한다
        raise RuntimeError("기온 자료 서버가 지금 응답하지 않아요. 잠시 뒤 다시 해보세요.")
    d = res.json().get("daily", {})
    times = d.get("time") or []

    hi, lo, rain, snow = [], [], 0, 0
    seen_years = set()
    for i, day in enumerate(times):
        if int(day[5:7]) != month:
            continue
        seen_years.add(day[:4])
        for src, bucket in ((d.get("temperature_2m_max"), hi), (d.get("temperature_2m_min"), lo)):
            if src and src[i] is not None:
                bucket.append(src[i])
        pr = (d.get("precipitation_sum") or [None] * len(times))[i]
        sn = (d.get("snowfall_sum") or [None] * len(times))[i]
        if pr and pr >= 1:
            rain += 1
        if sn and sn >= 0.1:
            snow += 1

    if not hi or not lo:
        raise RuntimeError("이 지역의 기후 자료를 못 찾았어요.")

    years = max(len(seen_years), 1)
    data = {
        "month": month,
        "tmax": round(statistics.mean(hi), 1),
        "tmin": round(statistics.mean(lo), 1),
        "tmean": round((statistics.mean(hi) + statistics.mean(lo)) / 2, 1),
        "swing": round(statistics.mean(hi) - statistics.mean(lo), 1),
        "rain_days": round(rain / years),
        "snow_days": round(snow / years),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return data


def forecast(lat, lon):
    res = requests.get(FORECAST_API, params={
        "latitude": lat, "longitude": lon,
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "forecast_days": 7, "timezone": "auto",
    }, timeout=15)
    res.raise_for_status()
    d = res.json().get("daily", {})
    return [{
        "date": t,
        "max": (d.get("temperature_2m_max") or [])[i],
        "min": (d.get("temperature_2m_min") or [])[i],
        "rain": (d.get("precipitation_probability_max") or [None] * 7)[i],
    } for i, t in enumerate(d.get("time") or [])]


# ---------------------------------------------------------------- 상품 페이지 파싱
def parse_metas(html):
    out = {}
    for tag in re.findall(r"<meta\s[^>]*>", html, re.I):
        key = re.search(r"(?:property|name|itemprop)\s*=\s*[\"']([^\"']+)[\"']", tag, re.I)
        val = re.search(r"content\s*=\s*[\"']([^\"']*)[\"']", tag, re.I)
        if key and val:
            out.setdefault(key.group(1).strip().lower(), val.group(1).strip())
    return out


def pick_image(metas, html, base):
    for k in ("og:image:secure_url", "og:image", "twitter:image", "twitter:image:src", "image"):
        if metas.get(k):
            return urljoin(base, metas[k])
    # JSON-LD 안의 image 필드
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S | re.I):
        m = re.search(r'"image"\s*:\s*(?:\[\s*)?"([^"]+)"', block)
        if m:
            return urljoin(base, m.group(1))
    # 최후: 가장 큰 것처럼 보이는 img
    m = re.search(r'<img[^>]+src=[\"\']([^\"\']+\.(?:jpe?g|png|webp)[^\"\']*)[\"\']', html, re.I)
    return urljoin(base, m.group(1)) if m else None


IMG_URL_RE = re.compile(r'https?://[^\s"\'<>\\]+?\.(?:jpe?g|png|webp|avif)(?:\?[^\s"\'<>\\]*)?', re.I)
IMG_EXT_RE = re.compile(r"\.(?:jpe?g|png|webp|avif)(?:$|\?)", re.I)
JUNK_RE = re.compile(
    r'(logo|icon|sprite|favicon|banner|btn|button|blank|dummy|placeholder|spacer|'
    r'kakao|naver|facebook|twitter|payco|toss|badge|coupon|delivery|flag_|flag_shapes|'
    r'/common/img/|footer|header|_nav|arrow|star|avatar|profile|emoji|qr|app_?down|'
    r'1x1|pixel)', re.I)
TRACKER_RE = re.compile(
    r'(facebook\.com|google-analytics|googletagmanager|doubleclick|criteo|'
    r'/tr\?|/collect|/pixel|/log\?|analytics)', re.I)
GALLERY_HINT = re.compile(
    r'(xzoom|gallery|상품\s*이미지|product.?imag|goods.?imag|prd.?img|detail.?imag)', re.I)
COLOR_HINT = re.compile(r'(chip|color|colour|option|swatch)', re.I)
CHIP_RE = re.compile(r'(chip|swatch)', re.I)


def collect_images(html, base):
    """상품 페이지의 이미지를 모으면서 태그의 class/alt 도 힌트로 같이 챙긴다."""
    hints, order = {}, []

    def add(u, hint="", strict=False):
        if not u or u.startswith("data:"):
            return
        u = urljoin(base, u.strip())
        if TRACKER_RE.search(u) or JUNK_RE.search(urlparse(u).path):
            return
        if (strict or not hint) and not IMG_EXT_RE.search(u):
            return
        if u in hints:
            if hint and not hints[u]:
                hints[u] = hint
            return
        hints[u] = hint
        order.append(u)

    for tag in re.findall(r"<img\s[^>]*>", html, re.I):
        hint = " ".join(re.findall(
            r'(?:class|alt|id|title)\s*=\s*["\']([^"\']*)["\']', tag, re.I))
        for attr in ("data-original", "data-src", "data-lazy", "data-echo", "src"):
            m = re.search(attr + r'\s*=\s*["\']([^"\']+)["\']', tag, re.I)
            if m:
                add(m.group(1), hint)
        m = re.search(r'srcset\s*=\s*["\']([^"\']+)["\']', tag, re.I)
        if m:                                       # srcset 중 가장 큰 것
            best, best_w = None, -1
            for part in m.group(1).split(","):
                bits = part.strip().split()
                if bits:
                    w = int(re.sub(r"\D", "", bits[-1]) or 0)
                    if w > best_w:
                        best, best_w = bits[0], w
            add(best, hint)

    for u in IMG_URL_RE.findall(html):              # JSON 덩어리 안의 URL
        add(u, "", strict=True)

    return [{"url": u, "hint": hints[u]} for u in order][:80]


def shape_key(u):
    """URL에서 숫자·해시 같은 식별자를 지워 '모양'만 남긴다.
    같은 갤러리 줄의 이미지는 색상만 달라도 모양이 같아진다."""
    parsed = urlparse(u)
    segs = []
    for seg in parsed.path.split("/"):
        stem, dot, ext = seg.rpartition(".")
        if dot and looks_like_id(stem):
            segs.append("#." + ext.lower())      # 해시 파일명
        elif looks_like_id(seg):
            segs.append("#")                     # 색상별로 바뀌는 해시/상품 id
        else:
            segs.append(re.sub(r"\d+", "#", seg))
    return domain_of(u) + "/".join(segs)


def looks_like_id(seg):
    return len(seg) >= 8 and bool(re.search(r"\d", seg)) and bool(re.fullmatch(r"[0-9A-Za-z_\-]+", seg))


def domain_of(u):
    """cdn.imweb.me 와 cdn-optimized.imweb.me 를 같은 곳으로 본다."""
    host = urlparse(u).netloc.split(":")[0]
    return ".".join(host.split(".")[-2:])


def canon(u):
    """쿼리스트링만 다른 같은 이미지를 한 장으로 본다."""
    parsed = urlparse(u)
    return parsed.netloc + parsed.path


def pick_color_set(items, main, limit=16):
    """메인 사진 아래 썸네일 줄(다른 색상)만 골라낸다. items = [{url, hint}]"""
    if not items:
        return []

    # 쿼리만 다른 같은 이미지는 하나로 합친다. 이때 힌트는 잃지 않게 이어붙인다.
    merged = {}
    for it in items:
        key = canon(it["url"])
        if key in merged:
            merged[key]["hint"] = (merged[key]["hint"] + " " + it["hint"]).strip()
        else:
            merged[key] = dict(it)
    uniq = list(merged.values())

    site = domain_of(main) if main else None
    groups = {}
    for it in uniq:
        if site and domain_of(it["url"]) != site:
            continue
        # 사이트 전체 이미지가 같은 경로 모양을 쓰는 경우가 있어,
        # 갤러리 표시가 붙은 것은 같은 모양이라도 따로 묶는다.
        tag = "|G" if GALLERY_HINT.search(it["hint"]) else ""
        groups.setdefault(shape_key(it["url"]) + tag, []).append(it)

    best, best_score = None, -1
    for key, group in groups.items():
        if not 2 <= len(group) <= 30:
            continue
        score = len(group)
        if COLOR_HINT.search(key):
            score += 100                                   # 색상 칩 경로
        if key.endswith("|G"):
            score += 60                                    # 갤러리 썸네일 줄
        if main and any(g["url"] == main for g in group):
            score += 20
        if score > best_score:
            best, best_score = group, score

    urls = ([main] if main else []) + [g["url"] for g in (best or uniq)]
    out = []
    for u in urls:
        if u and canon(u) not in {canon(x) for x in out}:
            out.append(u)
    return out[:limit]


def clean_title(raw):
    """"… - 사이즈 & 후기 | 무신사" 처럼 붙는 사이트 꼬리와 HTML 기호를 정리한다."""
    text = re.sub(r"\s+", " ", html_mod.unescape(raw or "")).strip()
    bar = text.rfind(" | ")
    if bar > 8:
        text = text[:bar]
    text = re.sub(r"\s*[-–]\s*(사이즈[^|]*|리뷰|후기)[^|]*$", "", text).strip()
    return text


def pick_title(metas, html):
    for k in ("og:title", "twitter:title", "name"):
        if metas.get(k):
            return clean_title(metas[k])
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    return clean_title(m.group(1)) if m else ""


def pick_price(metas, html):
    raw = ""
    for k in ("product:price:amount", "og:price:amount", "price"):
        if metas.get(k):
            raw = metas[k]
            break
    if not raw:
        m = re.search(r'"price"\s*:\s*"?([0-9][0-9,.]*)"?', html)
        raw = m.group(1) if m else ""
    raw = re.sub(r"[^0-9.]", "", raw).rstrip(".")
    return raw if raw and float(raw or 0) > 0 else ""


def pick_currency(metas, html):
    """해외 사이트 가격을 원화로 잘못 적지 않으려면 통화를 같이 봐야 한다."""
    for k in ("product:price:currency", "og:price:currency", "pricecurrency"):
        if metas.get(k):
            return metas[k].strip().upper()[:3]
    m = re.search(r'"priceCurrency"\s*:\s*"([A-Za-z]{3})"', html)
    return m.group(1).upper() if m else ""


def decode(res):
    """서버가 charset을 안 주면 requests가 latin-1로 잘못 읽어서 한글이 깨진다."""
    charset = None
    m = re.search(r"charset=([\w-]+)", res.headers.get("Content-Type", ""), re.I)
    if m:
        charset = m.group(1)
    if not charset:
        m = re.search(rb"charset=[\"\']?([\w-]+)", res.content[:4096], re.I)
        if m:
            charset = m.group(1).decode("ascii", "ignore")
    try:
        return res.content.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return res.content.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- 색 추출
def open_rgb(raw):
    img = Image.open(io.BytesIO(raw))
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(bg, img)
    return img.convert("RGB")


def crop_center(img):
    """배경·워터마크를 피하려고 가운데 위주로 자른다."""
    w, h = img.size
    return img.crop((int(w * 0.18), int(h * 0.12), int(w * 0.82), int(h * 0.92)))


def dominant_colors(raw, n=5):
    img = crop_center(open_rgb(raw))
    img.thumbnail((140, 140))
    px = list(img.getdata())

    def is_bg(p):
        r, g, b = p
        if r > 238 and g > 238 and b > 238:      # 스튜디오 흰 배경
            return True
        if max(p) - min(p) < 10 and r > 225:      # 밝은 회색 배경
            return True
        return False

    kept = [p for p in px if not is_bg(p)]
    if len(kept) < 40:
        kept = px

    strip = Image.new("RGB", (len(kept), 1))
    strip.putdata(kept)
    q = strip.quantize(colors=max(n, 4), method=Image.Quantize.MEDIANCUT)
    pal = q.getpalette() or []
    counts = sorted(q.getcolors() or [], reverse=True)

    total = sum(c for c, _ in counts) or 1
    out = []
    for cnt, idx in counts[:n]:
        r, g, b = pal[idx * 3: idx * 3 + 3]
        out.append({"hex": "#%02x%02x%02x" % (r, g, b), "ratio": round(cnt / total, 3)})
    return out


# ---------------------------------------------------------------- 이미지 캐시
def grab(url, referer=None):
    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer
    r = requests.get(url, headers=headers, timeout=15)
    r.raise_for_status()
    return r.content


def store(raw, url, maxpx=900, prefix=""):
    name = prefix + hashlib.sha1((url + str(maxpx)).encode()).hexdigest()[:16] + ".png"
    path = os.path.join(CACHE, name)
    if not os.path.exists(path):
        im = Image.open(io.BytesIO(raw))
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGB")
        im.thumbnail((maxpx, maxpx))
        im.save(path, "PNG")
    return name, path


def annotate_colors(urls, referer, gap=14):
    """후보마다 대표색을 뽑는다. (전체, 화면에 보여줄 목록) 을 돌려준다.

    색이 거의 같은 것(같은 색 다른 각도)은 화면에서 한 장만 보여주지만,
    칩을 고를 때는 전체를 써야 한다. 대표 사진과 같은 컬러웨이의 칩은 색이 비슷해서
    걸러지는데, 그게 바로 우리가 찾는 칩이다.
    """
    def one(u):
        try:
            raw = grab(u, referer)
            store(raw, u, maxpx=260, prefix="t_")      # 썸네일 미리 캐시
            return {"url": u, "hex": dominant_colors(raw)[0]["hex"]}
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        got = [r for r in pool.map(one, urls) if r]

    kept, rgbs = [], []
    for item in got:
        h = item["hex"]
        rgb = (int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16))
        near = any(sum((a - b) ** 2 for a, b in zip(rgb, prev)) ** 0.5 <= gap for prev in rgbs)
        if near:
            continue
        kept.append(item)
        rgbs.append(rgb)
    return got, kept


# ---------------------------------------------------------------- 패턴 판별
def analyze_pattern(raw):
    """무지 / 스트라이프 / 그래픽 / 올오버 패턴을 구분한다.

    스트라이프는 한 축으로는 밝기가 크게 오르내리는데 그 축과 직각인 줄 안에서는
    밝기가 거의 일정하다는 성질을 쓴다. 프린트는 상위 색 비중이 낮은 걸로 잡는다.
    """
    img = crop_center(open_rgb(raw))
    gray = img.convert("L").resize((96, 96))
    px = list(gray.getdata())
    rows = [px[i * 96:(i + 1) * 96] for i in range(96)]
    cols = [[rows[r][c] for r in range(96)] for c in range(96)]

    def line_ratio(lines):
        means = [statistics.mean(l) for l in lines]
        within = statistics.mean(statistics.pvariance(l) for l in lines)
        return statistics.pvariance(means) / (within + 1)

    ratio_h = line_ratio(rows)          # 가로줄무늬
    ratio_v = line_ratio(cols)          # 세로줄무늬

    quant = img.resize((96, 96)).quantize(colors=6, method=Image.Quantize.MEDIANCUT)
    counts = sorted((c for c, _ in (quant.getcolors() or [(1, 0)])), reverse=True)
    total = sum(counts) or 1
    top1 = counts[0] / total
    big = sum(1 for c in counts if c / total >= 0.12)

    stripe = max(ratio_h, ratio_v)
    axis = "h" if ratio_h >= ratio_v else "v"
    return {"kind": "stripe" if stripe >= 3 else "solid",
            "axis": axis if stripe >= 3 else None,
            "top1": round(top1, 2), "colors": big, "stripe": round(stripe, 2)}


def code_tokens(u):
    """파일명의 숫자 토큰. 유니클로 krgoods_69_482279 와 goods_69_482279_chip 처럼
    같은 컬러웨이는 같은 코드를 공유한다."""
    return set(re.findall(r"\d{2,}", os.path.basename(urlparse(u).path)))


def garment_view(candidates, main_url, referer, main_hex):
    """색상 칩이 있으면 그것으로 옷의 실제 색과 패턴을 본다.
    모델컷은 피부·배경·그림자가 섞여서 대표색이 배경으로 잡히고 줄무늬도 묻힌다."""
    chips = [c for c in candidates if CHIP_RE.search(urlparse(c["url"]).path)]
    if not chips:
        return None

    want = code_tokens(main_url or "")
    base = tuple(int(main_hex[i:i + 2], 16) for i in (1, 3, 5)) if main_hex else None

    def rank(c):
        shared = len(want & code_tokens(c["url"]))
        gap = 0
        if base:
            rgb = tuple(int(c["hex"][i:i + 2], 16) for i in (1, 3, 5))
            gap = sum((a - b) ** 2 for a, b in zip(rgb, base))
        return (-shared, gap)          # 컬러웨이 코드가 먼저, 그다음 색 거리

    for chip in sorted(chips, key=rank):
        try:
            raw = grab(chip["url"], referer)
            found = analyze_pattern(raw)
            found["from"] = "chip"
            return {"pattern": found, "colors": dominant_colors(raw), "chip": chip["url"]}
        except Exception:
            continue
    return None


def clean_title(raw):
    """"… - 사이즈 & 후기 | 무신사" 처럼 붙는 사이트 꼬리와 HTML 기호를 정리한다."""
    text = re.sub(r"\s+", " ", html_mod.unescape(raw or "")).strip()
    bar = text.rfind(" | ")
    if bar > 8:
        text = text[:bar]
    text = re.sub(r"\s*[-–]\s*(사이즈[^|]*|리뷰|후기)[^|]*$", "", text).strip()
    return text


def pick_title(metas, html):
    for k in ("og:title", "twitter:title", "name"):
        if metas.get(k):
            return clean_title(metas[k])
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    return clean_title(m.group(1)) if m else ""


def pick_price(metas, html):
    raw = ""
    for k in ("product:price:amount", "og:price:amount", "price"):
        if metas.get(k):
            raw = metas[k]
            break
    if not raw:
        m = re.search(r'"price"\s*:\s*"?([0-9][0-9,.]*)"?', html)
        raw = m.group(1) if m else ""
    raw = re.sub(r"[^0-9.]", "", raw).rstrip(".")
    return raw if raw and float(raw or 0) > 0 else ""


def pick_currency(metas, html):
    """해외 사이트 가격을 원화로 잘못 적지 않으려면 통화를 같이 봐야 한다."""
    for k in ("product:price:currency", "og:price:currency", "pricecurrency"):
        if metas.get(k):
            return metas[k].strip().upper()[:3]
    m = re.search(r'"priceCurrency"\s*:\s*"([A-Za-z]{3})"', html)
    return m.group(1).upper() if m else ""


def decode(res):
    """서버가 charset을 안 주면 requests가 latin-1로 잘못 읽어서 한글이 깨진다."""
    charset = None
    m = re.search(r"charset=([\w-]+)", res.headers.get("Content-Type", ""), re.I)
    if m:
        charset = m.group(1)
    if not charset:
        m = re.search(rb"charset=[\"\']?([\w-]+)", res.content[:4096], re.I)
        if m:
            charset = m.group(1).decode("ascii", "ignore")
    try:
        return res.content.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return res.content.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- 색 추출
def open_rgb(raw):
    img = Image.open(io.BytesIO(raw))
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(bg, img)
    return img.convert("RGB")


def crop_center(img):
    """배경·워터마크를 피하려고 가운데 위주로 자른다."""
    w, h = img.size
    return img.crop((int(w * 0.18), int(h * 0.12), int(w * 0.82), int(h * 0.92)))


def dominant_colors(raw, n=5):
    img = crop_center(open_rgb(raw))
    img.thumbnail((140, 140))
    px = list(img.getdata())

    def is_bg(p):
        r, g, b = p
        if r > 238 and g > 238 and b > 238:      # 스튜디오 흰 배경
            return True
        if max(p) - min(p) < 10 and r > 225:      # 밝은 회색 배경
            return True
        return False

    kept = [p for p in px if not is_bg(p)]
    if len(kept) < 40:
        kept = px

    strip = Image.new("RGB", (len(kept), 1))
    strip.putdata(kept)
    q = strip.quantize(colors=max(n, 4), method=Image.Quantize.MEDIANCUT)
    pal = q.getpalette() or []
    counts = sorted(q.getcolors() or [], reverse=True)

    total = sum(c for c, _ in counts) or 1
    out = []
    for cnt, idx in counts[:n]:
        r, g, b = pal[idx * 3: idx * 3 + 3]
        out.append({"hex": "#%02x%02x%02x" % (r, g, b), "ratio": round(cnt / total, 3)})
    return out


# ---------------------------------------------------------------- 이미지 캐시
def grab(url, referer=None):
    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer
    r = requests.get(url, headers=headers, timeout=15)
    r.raise_for_status()
    return r.content


def store(raw, url, maxpx=900, prefix=""):
    name = prefix + hashlib.sha1((url + str(maxpx)).encode()).hexdigest()[:16] + ".png"
    path = os.path.join(CACHE, name)
    if not os.path.exists(path):
        im = Image.open(io.BytesIO(raw))
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGB")
        im.thumbnail((maxpx, maxpx))
        im.save(path, "PNG")
    return name, path


def annotate_colors(urls, referer, gap=14):
    """후보마다 대표색을 뽑는다. (전체, 화면에 보여줄 목록) 을 돌려준다.

    색이 거의 같은 것(같은 색 다른 각도)은 화면에서 한 장만 보여주지만,
    칩을 고를 때는 전체를 써야 한다. 대표 사진과 같은 컬러웨이의 칩은 색이 비슷해서
    걸러지는데, 그게 바로 우리가 찾는 칩이다.
    """
    def one(u):
        try:
            raw = grab(u, referer)
            store(raw, u, maxpx=260, prefix="t_")      # 썸네일 미리 캐시
            return {"url": u, "hex": dominant_colors(raw)[0]["hex"]}
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        got = [r for r in pool.map(one, urls) if r]

    kept, rgbs = [], []
    for item in got:
        h = item["hex"]
        rgb = (int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16))
        near = any(sum((a - b) ** 2 for a, b in zip(rgb, prev)) ** 0.5 <= gap for prev in rgbs)
        if near:
            continue
        kept.append(item)
        rgbs.append(rgb)
    return got, kept


# ---------------------------------------------------------------- 패턴 판별
def analyze_pattern(raw):
    """무지 / 스트라이프 / 그래픽 / 올오버 패턴을 구분한다.

    스트라이프는 한 축으로는 밝기가 크게 오르내리는데 그 축과 직각인 줄 안에서는
    밝기가 거의 일정하다는 성질을 쓴다. 프린트는 상위 색 비중이 낮은 걸로 잡는다.
    """
    img = crop_center(open_rgb(raw))
    gray = img.convert("L").resize((96, 96))
    px = list(gray.getdata())
    rows = [px[i * 96:(i + 1) * 96] for i in range(96)]
    cols = [[rows[r][c] for r in range(96)] for c in range(96)]

    def line_ratio(lines):
        means = [statistics.mean(l) for l in lines]
        within = statistics.mean(statistics.pvariance(l) for l in lines)
        return statistics.pvariance(means) / (within + 1)

    ratio_h = line_ratio(rows)          # 가로줄무늬
    ratio_v = line_ratio(cols)          # 세로줄무늬

    quant = img.resize((96, 96)).quantize(colors=6, method=Image.Quantize.MEDIANCUT)
    counts = sorted((c for c, _ in (quant.getcolors() or [(1, 0)])), reverse=True)
    total = sum(counts) or 1
    top1 = counts[0] / total
    big = sum(1 for c in counts if c / total >= 0.12)

    stripe = max(ratio_h, ratio_v)
    axis = "h" if ratio_h >= ratio_v else "v"
    return {"kind": "stripe" if stripe >= 3 else "solid",
            "axis": axis if stripe >= 3 else None,
            "top1": round(top1, 2), "colors": big, "stripe": round(stripe, 2)}


def pattern_for(candidates, fallback_raw, referer, main_hex):
    """패턴은 색상 칩으로 본다. 모델컷은 피부·배경·그림자 때문에 줄무늬가 묻힌다.
    칩이 여러 개면 지금 보고 있는 색과 가장 가까운 칩을 쓴다."""
    chips = [c for c in candidates if CHIP_RE.search(urlparse(c["url"]).path)]
    if chips and main_hex:
        want = tuple(int(main_hex[i:i + 2], 16) for i in (1, 3, 5))

        def gap(c):
            rgb = tuple(int(c["hex"][i:i + 2], 16) for i in (1, 3, 5))
            return sum((a - b) ** 2 for a, b in zip(rgb, want))

        for chip in sorted(chips, key=gap):
            try:
                chip_raw = grab(chip["url"], referer)
                found = analyze_pattern(chip_raw)
                found["from"] = "chip"
                # 패턴 옷의 실제 색 구성. 모델컷 팔레트는 피부·배경이 섞여 못 쓴다.
                found["palette"] = dominant_colors(chip_raw)
                return found
            except Exception:
                continue

    found = analyze_pattern(fallback_raw)
    found["from"] = "photo"          # 믿을 수 없으니 자동 태그에 쓰지 않는다
    return found


# ---------------------------------------------------------------- 배경 제거 (컷아웃)
def cutout(raw, tol=26, limit=560):
    """스튜디오 단색 배경을 테두리에서부터 지워 투명 PNG로 만든다.
    배경이 단색이 아니면 손대지 않고 원본을 돌려준다."""
    img = open_rgb(raw)
    img.thumbnail((limit, limit))
    w, h = img.size
    px = img.load()

    edge = [px[x, 0] for x in range(0, w, 4)] + [px[x, h - 1] for x in range(0, w, 4)] \
         + [px[0, y] for y in range(0, h, 4)] + [px[w - 1, y] for y in range(0, h, 4)]

    # 모델 발이나 옷이 화면 끝에 닿는 경우가 많아 평균이 아니라 최빈색을 배경으로 본다.
    # 버킷만으로는 같은 회색이 경계에서 쪼개지므로, 씨드를 잡고 허용범위로 다시 모은다.
    buckets = {}
    for c in edge:
        buckets.setdefault(tuple(v // 32 for v in c), []).append(c)
    seed = max(buckets.values(), key=len)
    base = tuple(round(statistics.mean(c[i] for c in seed)) for i in range(3))
    same = [c for c in edge if max(abs(c[i] - base[i]) for i in range(3)) <= tol]
    if len(same) / len(edge) < 0.55:     # 테두리가 한 색으로 안 모인다 = 배경 있는 사진
        return img, False
    base = tuple(round(statistics.mean(c[i] for c in same)) for i in range(3))

    def near(c):
        return all(abs(c[i] - base[i]) <= tol for i in range(3))

    # 테두리에서 시작해 배경색으로 이어진 영역만 지운다 (옷 안쪽 흰색은 남는다)
    mask = bytearray(w * h)
    stack = [(x, 0) for x in range(w)] + [(x, h - 1) for x in range(w)] \
          + [(0, y) for y in range(h)] + [(w - 1, y) for y in range(h)]
    while stack:
        x, y = stack.pop()
        i = y * w + x
        if mask[i] or not near(px[x, y]):
            continue
        mask[i] = 1
        if x > 0:     stack.append((x - 1, y))
        if x < w - 1: stack.append((x + 1, y))
        if y > 0:     stack.append((x, y - 1))
        if y < h - 1: stack.append((x, y + 1))

    out = img.convert("RGBA")
    alpha = Image.frombytes("L", (w, h), bytes(255 - m * 255 for m in mask))
    out.putalpha(alpha)
    box = out.getbbox()
    if box:
        out = out.crop(box)
    return out, True


# ---------------------------------------------------------------- 요청 처리
def fetch_product(url):
    if not re.match(r"^https?://", url):
        url = "https://" + url
    sess = requests.Session()
    res = sess.get(url, headers=HEADERS, timeout=15, allow_redirects=True)
    ctype = res.headers.get("Content-Type", "")

    candidates, every, every_shot, referer = [], [], [], None
    if ctype.startswith("image/"):
        img_url, title, price, currency = res.url, os.path.basename(urlparse(res.url).path), "", ""
        raw = res.content
    else:
        res.raise_for_status()
        html = decode(res)
        metas = parse_metas(html)
        img_url = pick_image(metas, html, res.url)
        title = pick_title(metas, html)
        price = pick_price(metas, html)
        currency = pick_currency(metas, html)
        referer = res.url
        every = collect_images(html, res.url)
        urls = [e["url"] for e in every]
        if img_url and img_url not in urls:
            every.insert(0, {"url": img_url, "hint": "og:image"})
        if not every:
            raise RuntimeError("상품 이미지를 못 찾았어요. 색을 직접 지정해 주세요.")
        img_url = img_url or every[0]["url"]
        every_shot, candidates = annotate_colors(pick_color_set(every, img_url), res.url)
        raw = grab(img_url, res.url)

    name, _ = store(raw, img_url)
    colors = dominant_colors(raw)
    view = garment_view(every_shot, img_url, referer, colors[0]["hex"] if colors else None)
    if view:
        colors, pattern, color_from = view["colors"], view["pattern"], "chip"
    else:
        pattern = analyze_pattern(raw)
        pattern["from"] = "photo"
        color_from = "photo"
    return {
        "url": url,
        "referer": res.url,
        "site": urlparse(url).netloc.replace("www.", ""),
        "title": title[:120],
        "price": price,
        "currency": currency,
        "image": "/cache/" + name,
        "source": img_url,
        "colors": colors,
        "pattern": pattern,
        "color_from": color_from,
        "candidates": candidates,
        "all_candidates": [e["url"] for e in every],
    }


def friendly_error(exc):
    """requests 스택트레이스를 그대로 보여주면 읽을 수 없어서 한국어로 바꿔준다."""
    if isinstance(exc, RuntimeError):
        return str(exc)
    if isinstance(exc, requests.exceptions.Timeout):
        return "사이트가 응답하지 않아요. 잠시 뒤 다시 해보세요."
    if isinstance(exc, requests.exceptions.HTTPError):
        code = exc.response.status_code if exc.response is not None else 0
        if code in (401, 403, 405, 429):
            return (f"이 사이트가 자동 수집을 막고 있어요 ({code}). "
                    "상품 이미지 주소를 직접 넣거나 색을 직접 지정해 주세요.")
        if code == 404:
            return "그 주소에는 상품이 없어요 (404). 주소를 다시 확인해 주세요."
        return f"사이트가 오류를 냈어요 ({code})."
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "주소를 못 찾았어요. 사이트 주소가 맞는지 확인해 주세요."
    text = str(exc)
    if "Invalid URL" in text or "No host supplied" in text:
        return "주소 형식이 이상해요. https:// 로 시작하는 상품 주소를 넣어 주세요."
    if isinstance(exc, (ValueError, TypeError)):
        return "입력값이 올바르지 않아요."
    return "불러오지 못했어요. 상품 이미지 주소를 직접 넣어 보세요."


def trim_cache(limit=600, keep=400):
    """개인용이라 캐시를 안 지우면 계속 쌓인다. 오래된 이미지부터 정리한다."""
    try:
        files = [os.path.join(CACHE, f) for f in os.listdir(CACHE) if f.endswith((".png", ".meta"))]
        if len(files) <= limit:
            return
        files.sort(key=lambda f: os.path.getmtime(f))
        for f in files[:len(files) - keep]:
            try:
                os.remove(f)
            except OSError:
                pass
    except OSError:
        pass


# 웹 화면(GitHub Pages)이 이 서버를 통해 가져올 수 있게 허용한다.
# 쇼핑몰 차단은 IP 기준이라, 내 맥에서 가져오면 무신사 같은 곳도 그냥 된다.
WEB_ORIGINS = ("https://dlthwjd02-domi.github.io", "http://localhost:8787",
               "http://127.0.0.1:8787", "http://localhost:8080", "http://127.0.0.1:8080")


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def _allow_origin(self):
        origin = self.headers.get("Origin")
        return origin if origin in WEB_ORIGINS else None

    def do_OPTIONS(self):
        """크롬은 https 페이지가 사설망(내 맥) 으로 요청할 때 먼저 프리플라이트를 보낸다.
        Access-Control-Allow-Private-Network 를 줘야 본 요청이 통과한다."""
        origin = self._allow_origin()
        self.send_response(204)
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "content-type")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _relay(self, target, referer, as_image):
        """웹 화면 대신 이 맥이 가져온다 (워커는 쇼핑몰 IP 차단에 걸린다)."""
        if not re.match(r"^https?://", target or ""):
            raise RuntimeError("가져올 주소가 올바르지 않아요.")
        raw = grab(target, referer or None)
        blob = raw if as_image else None
        origin = self._allow_origin()
        self.send_response(200)
        if as_image:
            self.send_header("Content-Type", "image/png" if raw[:4] == b"\x89PNG" else "image/jpeg")
        else:
            self.send_header("Content-Type", "text/plain; charset=utf-8")
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Access-Control-Expose-Headers", "X-Final-Url")
        self.send_header("X-Final-Url", target)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            qs = parse_qs(parsed.query)
            one = lambda k, d="": (qs.get(k) or [d])[0]
            try:
                if parsed.path == "/api/places":
                    return self._json(200, {"results": search_places(one("q"))})
                if parsed.path == "/api/health":
                    return self._json(200, {"ok": True, "relay": True, "service": "coordi-local"})
                if parsed.path == "/api/page":
                    return self._relay(one("u"), None, False)
                if parsed.path == "/api/img":
                    return self._relay(one("u"), one("r"), True)
                if parsed.path == "/api/thumb":
                    raw = grab(one("u"), one("r") or None)
                    _, path = store(raw, one("u"), maxpx=260, prefix="t_")
                    with open(path, "rb") as f:
                        blob = f.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Content-Length", str(len(blob)))
                    self.send_header("Cache-Control", "max-age=86400")
                    self.end_headers()
                    self.wfile.write(blob)
                    return
                if parsed.path == "/api/cutout":
                    src = one("u")
                    name = "c_" + hashlib.sha1(src.encode()).hexdigest()[:16] + ".png"
                    path = os.path.join(CACHE, name)
                    if not os.path.exists(path):
                        img, removed = cutout(grab(src, one("r") or None))
                        img.save(path, "PNG")
                        with open(path + ".meta", "w") as f:
                            f.write("1" if removed else "0")
                    with open(path + ".meta") as f:
                        removed = f.read() == "1"
                    return self._json(200, {"image": "/cache/" + name, "removed": removed})
                if parsed.path == "/api/image":
                    src_u = one("u")
                    raw = grab(src_u, one("r") or None)
                    name, _ = store(raw, src_u)
                    found = analyze_pattern(raw)
                    found["from"] = "chip" if CHIP_RE.search(urlparse(src_u).path) else "photo"
                    return self._json(200, {
                        "image": "/cache/" + name,
                        "source": src_u,
                        "colors": dominant_colors(raw),
                        "pattern": found,
                    })
                if parsed.path == "/api/climate":
                    try:
                        lat, lon = float(one("lat")), float(one("lon"))
                        month = int(one("month") or dt.date.today().month)
                    except ValueError:
                        raise RuntimeError("여행지를 다시 골라 주세요.")
                    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                        raise RuntimeError("좌표 범위를 벗어났어요.")
                    if not 1 <= month <= 12:
                        raise RuntimeError("월은 1~12 사이여야 해요.")
                    payload = {"climate": climate(lat, lon, month)}
                    if one("forecast") == "1":
                        try:
                            payload["forecast"] = forecast(lat, lon)
                        except Exception:
                            payload["forecast"] = []
                    return self._json(200, payload)
            except Exception as exc:
                return self._json(200, {"error": friendly_error(exc)})
            return self.send_error(404)
        # 웹 앱(docs/)을 그대로 서비스한다. 같은 출처라 브라우저 권한 문제가 없고,
        # 가져오기는 /api/page·/api/img 를 쓰므로 무신사 같은 곳도 그냥 된다.
        if parsed.path in ("/", "/index.html"):
            self.path = "/docs/index.html"
        elif not parsed.path.startswith(("/cache/", "/docs/", "/static/")):
            self.path = "/docs" + self.path
        return super().do_GET()

    def do_POST(self):
        if self.path != "/api/product":
            return self.send_error(404)
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            data = fetch_product((body.get("url") or "").strip())
            self._json(200, data)
        except Exception as exc:
            self._json(200, {"error": friendly_error(exc)})

    def _json(self, code, obj):
        payload = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        origin = self._allow_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        if "--quiet" not in sys.argv:
            sys.stderr.write("· " + fmt % args + "\n")


if __name__ == "__main__":
    trim_cache()
    print(f"\n  코디 체커 → http://localhost:{PORT}\n  (끄려면 Ctrl+C)\n")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
