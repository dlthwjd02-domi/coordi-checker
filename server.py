#!/usr/bin/env python3
"""코디 체커 - 상품 URL을 넣으면 이미지/색을 뽑아주는 로컬 서버."""
import datetime as dt
import hashlib
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
def search_places(q):
    res = requests.get(GEO_API, params={"name": q, "count": 8, "language": "ko"}, timeout=12)
    res.raise_for_status()
    out = []
    for r in res.json().get("results", []):
        parts = [r.get("country"), r.get("admin1")]
        out.append({
            "name": r["name"],
            "where": " · ".join([p for p in parts if p and p != r["name"]]),
            "country": r.get("country") or "",
            "lat": round(r["latitude"], 4),
            "lon": round(r["longitude"], 4),
        })
    return out


def climate(lat, lon, month):
    """최근 3년 같은 달의 실제 관측치를 평균내서 평년값 대신 쓴다."""
    key = "clim_%.2f_%.2f_%02d.json" % (lat, lon, month)
    path = os.path.join(CACHE, key)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    year = dt.date.today().year
    res = requests.get(ARCHIVE_API, params={
        "latitude": lat, "longitude": lon,
        "start_date": f"{year - 3}-01-01", "end_date": f"{year - 1}-12-31",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,snowfall_sum",
        "timezone": "auto",
    }, timeout=30)
    res.raise_for_status()
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
COLOR_HINT = re.compile(r'(chip|color|colour|option|swatch|_sub|/sub/)', re.I)


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


def pick_title(metas, html):
    for k in ("og:title", "twitter:title", "name"):
        if metas.get(k):
            return metas[k]
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


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
    """후보마다 대표색을 뽑고, 색이 거의 같은 것(같은 색 다른 각도)은 한 장만 남긴다."""
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
    return kept


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
    if stripe >= 2.0 and top1 < 0.8:
        kind = "stripe"
        axis = "h" if ratio_h >= ratio_v else "v"
    elif top1 < 0.5 or big >= 4:
        kind, axis = "print", None
    elif top1 < 0.78:
        kind, axis = "graphic", None
    else:
        kind, axis = "solid", None

    return {"kind": kind, "axis": axis, "top1": round(top1, 2),
            "colors": big, "stripe": round(stripe, 2)}


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

    candidates, every = [], []
    if ctype.startswith("image/"):
        img_url, title, price = res.url, os.path.basename(urlparse(res.url).path), ""
        raw = res.content
    else:
        res.raise_for_status()
        html = decode(res)
        metas = parse_metas(html)
        img_url = pick_image(metas, html, res.url)
        title = pick_title(metas, html)
        price = pick_price(metas, html)
        every = collect_images(html, res.url)
        urls = [e["url"] for e in every]
        if img_url and img_url not in urls:
            every.insert(0, {"url": img_url, "hint": "og:image"})
        if not every:
            raise RuntimeError("상품 이미지를 못 찾았어요. 색을 직접 지정해 주세요.")
        img_url = img_url or every[0]["url"]
        candidates = annotate_colors(pick_color_set(every, img_url), res.url)
        raw = grab(img_url, res.url)

    name, _ = store(raw, img_url)
    return {
        "url": url,
        "referer": res.url,
        "site": urlparse(url).netloc.replace("www.", ""),
        "title": title[:120],
        "price": price,
        "image": "/cache/" + name,
        "source": img_url,
        "colors": dominant_colors(raw),
        "pattern": analyze_pattern(raw),
        "candidates": candidates,
        "all_candidates": [e["url"] for e in every],
    }


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            qs = parse_qs(parsed.query)
            one = lambda k, d="": (qs.get(k) or [d])[0]
            try:
                if parsed.path == "/api/places":
                    return self._json(200, {"results": search_places(one("q"))})
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
                    raw = grab(one("u"), one("r") or None)
                    name, _ = store(raw, one("u"))
                    return self._json(200, {
                        "image": "/cache/" + name,
                        "source": one("u"),
                        "colors": dominant_colors(raw),
                        "pattern": analyze_pattern(raw),
                    })
                if parsed.path == "/api/climate":
                    lat, lon = float(one("lat")), float(one("lon"))
                    month = int(one("month") or dt.date.today().month)
                    payload = {"climate": climate(lat, lon, month)}
                    if one("forecast") == "1":
                        try:
                            payload["forecast"] = forecast(lat, lon)
                        except Exception:
                            payload["forecast"] = []
                    return self._json(200, payload)
            except Exception as exc:
                return self._json(200, {"error": str(exc) or exc.__class__.__name__})
            return self.send_error(404)
        if parsed.path in ("/", "/index.html"):
            self.path = "/static/index.html"
        return super().do_GET()

    def do_POST(self):
        if self.path != "/api/product":
            return self.send_error(404)
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            data = fetch_product((body.get("url") or "").strip())
            self._json(200, data)
        except Exception as exc:  # 사용자에게 그대로 보여준다
            self._json(200, {"error": str(exc) or exc.__class__.__name__})

    def _json(self, code, obj):
        payload = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        if "--quiet" not in sys.argv:
            sys.stderr.write("· " + fmt % args + "\n")


if __name__ == "__main__":
    print(f"\n  코디 체커 → http://localhost:{PORT}\n  (끄려면 Ctrl+C)\n")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
