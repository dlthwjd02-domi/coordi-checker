/* 코디 체커 엔진 — 서버 없이 브라우저에서 돌린다.
 *
 * 상품 페이지와 이미지만 Cloudflare Worker 가 대신 가져오고(CORS·핫링크 때문),
 * 색 추출·패턴 판별·배경 제거는 전부 Canvas 로 여기서 한다.
 * 파이썬 서버(server.py)와 같은 결과가 나오도록 같은 값·같은 순서로 옮겼다. */

const RELAY = 'https://coordi-fetch.domii.workers.dev';

/* ---------------------------------------------------------------- 가져오기 */
async function relayPage(url){
  const res = await fetch(`${RELAY}/page?u=${encodeURIComponent(url)}`);
  const type = res.headers.get('Content-Type') || '';
  if (type.includes('application/json')) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.error || '불러오지 못했어요.');
  }
  if (!res.ok) throw new Error('불러오지 못했어요.');
  return { html: await res.text(), finalUrl: res.headers.get('X-Final-Url') || url };
}

const relayImg = (url, referer) =>
  `${RELAY}/img?u=${encodeURIComponent(url)}` + (referer ? `&r=${encodeURIComponent(referer)}` : '');

function loadImage(url, referer){
  return new Promise((ok, fail) => {
    const img = new Image();
    if (!url.startsWith('data:')) img.crossOrigin = 'anonymous';
    img.onload = () => ok(img);
    img.onerror = () => fail(new Error('이미지를 불러오지 못했어요.'));
    img.src = url.startsWith('data:') ? url : relayImg(url, referer);   // 내 사진은 그대로
  });
}

/* ---------------------------------------------------------------- 캔버스 */
// 배경·워터마크를 피하려고 가운데 위주로 자른다 (파이썬 crop_center 와 같은 비율)
function cropCenter(img, maxSide){
  const w = img.naturalWidth, h = img.naturalHeight;
  const sx = Math.round(w * 0.18), sy = Math.round(h * 0.12);
  const sw = Math.round(w * 0.82) - sx, sh = Math.round(h * 0.92) - sy;
  let tw = sw, th = sh;
  if (maxSide && (tw > maxSide || th > maxSide)) {
    const k = Math.min(maxSide / tw, maxSide / th);
    tw = Math.max(1, Math.round(tw * k));
    th = Math.max(1, Math.round(th * k));
  }
  return drawTo(img, sx, sy, sw, sh, tw, th);
}

function drawTo(img, sx, sy, sw, sh, tw, th){
  const cv = document.createElement('canvas');
  cv.width = tw; cv.height = th;
  const ctx = cv.getContext('2d', { willReadFrequently: true });
  ctx.fillStyle = '#fff';                      // 투명 PNG 는 흰 배경에 얹는다
  ctx.fillRect(0, 0, tw, th);
  ctx.drawImage(img, sx, sy, sw, sh, 0, 0, tw, th);
  return cv;
}

const pixelsOf = cv => {
  const d = cv.getContext('2d', { willReadFrequently: true })
    .getImageData(0, 0, cv.width, cv.height).data;
  const out = [];
  for (let i = 0; i < d.length; i += 4) out.push([d[i], d[i + 1], d[i + 2]]);
  return out;
};

const hex2 = v => v.toString(16).padStart(2, '0');
const toHex = (r, g, b) => '#' + hex2(Math.round(r)) + hex2(Math.round(g)) + hex2(Math.round(b));

/* ---------------------------------------------------------------- 색 추출 */
// Pillow 의 MEDIANCUT 과 같은 방식: 폭이 가장 넓은 축을 중앙값에서 쪼갠다
function medianCut(pixels, want){
  if (!pixels.length) return [];
  let boxes = [pixels.slice()];
  const spread = box => {
    const lo = [255, 255, 255], hi = [0, 0, 0];
    for (const p of box) for (let c = 0; c < 3; c++) {
      if (p[c] < lo[c]) lo[c] = p[c];
      if (p[c] > hi[c]) hi[c] = p[c];
    }
    return [hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]];
  };
  while (boxes.length < want) {
    let pick = -1, widest = -1, axis = 0;
    boxes.forEach((box, i) => {
      if (box.length < 2) return;
      const s = spread(box);
      const m = Math.max(s[0], s[1], s[2]);
      if (m > widest) { widest = m; pick = i; axis = s.indexOf(m); }
    });
    if (pick < 0 || widest <= 0) break;
    const box = boxes[pick];
    box.sort((a, b) => a[axis] - b[axis]);
    const mid = box.length >> 1;
    boxes.splice(pick, 1, box.slice(0, mid), box.slice(mid));
  }
  // 상자를 중앙값에서 쪼개면 상자마다 픽셀 수가 비슷해져서 비중이 무의미해진다.
  // 팔레트를 만든 뒤 모든 픽셀을 가장 가까운 색에 배정해 실제 비중을 센다.
  let centers = boxes.filter(b => b.length).map(box => {
    let r = 0, g = 0, b = 0;
    for (const p of box) { r += p[0]; g += p[1]; b += p[2]; }
    return [r / box.length, g / box.length, b / box.length];
  });

  // 청바지처럼 그라데이션이 있으면 중앙값 분할만으로는 어느 덩어리가 최다인지 뒤집힌다.
  // 몇 번 다시 배정해 중심을 수렴시키면 결과가 안정된다.
  const assign = () => {
    const sums = centers.map(() => [0, 0, 0, 0]);
    for (const p of pixels) {
      let pick = 0, near = Infinity;
      for (let i = 0; i < centers.length; i++) {
        const q = centers[i];
        const d = (p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2 + (p[2] - q[2]) ** 2;
        if (d < near) { near = d; pick = i; }
      }
      const s = sums[pick];
      s[0] += p[0]; s[1] += p[1]; s[2] += p[2]; s[3]++;
    }
    return sums;
  };
  let sums = assign();
  for (let round = 0; round < 4; round++) {
    centers = centers.map((q, i) =>
      sums[i][3] ? [sums[i][0] / sums[i][3], sums[i][1] / sums[i][3], sums[i][2] / sums[i][3]] : q);
    sums = assign();
  }

  const total = pixels.length;
  return centers
    .map((q, i) => ({ hex: toHex(q[0], q[1], q[2]),
                      ratio: Math.round(sums[i][3] / total * 1000) / 1000 }))
    .filter(c => c.ratio > 0)
    .sort((a, b) => b.ratio - a.ratio);
}

const isStudioBg = p =>
  (p[0] > 238 && p[1] > 238 && p[2] > 238) ||
  (Math.max(p[0], p[1], p[2]) - Math.min(p[0], p[1], p[2]) < 10 && p[0] > 225);

function dominantColors(img, n = 5){
  const px = pixelsOf(cropCenter(img, 140));
  let kept = px.filter(p => !isStudioBg(p));
  if (kept.length < 40) kept = px;             // 흰 옷을 흰 배경에서 찍은 경우
  return medianCut(kept, Math.max(n, 4)).slice(0, n);
}

/* ---------------------------------------------------------------- 패턴 판별 */
function analyzePattern(img){
  const w = img.naturalWidth, h = img.naturalHeight;
  const sx = Math.round(w * 0.18), sy = Math.round(h * 0.12);
  const cv = drawTo(img, sx, sy, Math.round(w * 0.82) - sx, Math.round(h * 0.92) - sy, 96, 96);
  const px = pixelsOf(cv);
  const gray = px.map(p => 0.299 * p[0] + 0.587 * p[1] + 0.114 * p[2]);

  const rows = [], cols = [];
  for (let y = 0; y < 96; y++) rows.push(gray.slice(y * 96, y * 96 + 96));
  for (let x = 0; x < 96; x++) {
    const col = [];
    for (let y = 0; y < 96; y++) col.push(gray[y * 96 + x]);
    cols.push(col);
  }
  const mean = a => a.reduce((s, v) => s + v, 0) / a.length;
  const pvar = a => { const m = mean(a); return mean(a.map(v => (v - m) ** 2)); };
  // 한 축으로는 밝기가 크게 오르내리는데 직각인 줄 안에서는 일정하면 줄무늬다
  const lineRatio = lines => pvar(lines.map(mean)) / (mean(lines.map(pvar)) + 1);

  const ratioH = lineRatio(rows), ratioV = lineRatio(cols);
  const stripe = Math.max(ratioH, ratioV);
  const quant = medianCut(px, 6);
  const top1 = quant.length ? quant[0].ratio : 1;
  const big = quant.filter(c => c.ratio >= 0.12).length;

  return {
    kind: stripe >= 3 ? 'stripe' : 'solid',
    axis: stripe >= 3 ? (ratioH >= ratioV ? 'h' : 'v') : null,
    top1: Math.round(top1 * 100) / 100,
    colors: big,
    stripe: Math.round(stripe * 100) / 100,
  };
}

/* ---------------------------------------------------------------- 배경 제거 */
function cutout(img, tol = 26, limit = 560){
  const scale = Math.min(1, limit / Math.max(img.naturalWidth, img.naturalHeight));
  const w = Math.max(1, Math.round(img.naturalWidth * scale));
  const h = Math.max(1, Math.round(img.naturalHeight * scale));
  const cv = drawTo(img, 0, 0, img.naturalWidth, img.naturalHeight, w, h);
  const ctx = cv.getContext('2d', { willReadFrequently: true });
  const id = ctx.getImageData(0, 0, w, h);
  const d = id.data;
  const at = (x, y) => { const i = (y * w + x) * 4; return [d[i], d[i + 1], d[i + 2]]; };

  const edge = [];
  for (let x = 0; x < w; x += 4) { edge.push(at(x, 0)); edge.push(at(x, h - 1)); }
  for (let y = 0; y < h; y += 4) { edge.push(at(0, y)); edge.push(at(w - 1, y)); }

  // 모델 발이 화면 끝에 닿는 사진이 많아 평균이 아니라 최빈색을 배경으로 본다.
  // 버킷만으로는 같은 회색이 경계에서 쪼개지므로 씨드를 잡고 허용범위로 다시 모은다.
  const buckets = new Map();
  for (const c of edge) {
    const key = `${c[0] >> 5},${c[1] >> 5},${c[2] >> 5}`;
    if (!buckets.has(key)) buckets.set(key, []);
    buckets.get(key).push(c);
  }
  let seed = [];
  for (const group of buckets.values()) if (group.length > seed.length) seed = group;
  const avg = list => [0, 1, 2].map(c => Math.round(list.reduce((s, p) => s + p[c], 0) / list.length));
  let base = avg(seed);
  const near = c => Math.max(Math.abs(c[0] - base[0]), Math.abs(c[1] - base[1]), Math.abs(c[2] - base[2])) <= tol;
  const same = edge.filter(near);
  if (same.length / edge.length < 0.55) return { canvas: cv, removed: false };
  base = avg(same);

  // 테두리에서 시작해 배경색으로 이어진 영역만 지운다 (옷 안쪽 흰색은 남는다)
  const mask = new Uint8Array(w * h);
  const stack = [];
  for (let x = 0; x < w; x++) { stack.push(x, 0, x, h - 1); }
  for (let y = 0; y < h; y++) { stack.push(0, y, w - 1, y); }
  while (stack.length) {
    const y = stack.pop(), x = stack.pop();
    const i = y * w + x;
    if (mask[i] || !near(at(x, y))) continue;
    mask[i] = 1;
    if (x > 0) stack.push(x - 1, y);
    if (x < w - 1) stack.push(x + 1, y);
    if (y > 0) stack.push(x, y - 1);
    if (y < h - 1) stack.push(x, y + 1);
  }

  let minX = w, minY = h, maxX = -1, maxY = -1;
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
    const i = y * w + x;
    if (mask[i]) { d[i * 4 + 3] = 0; continue; }
    if (x < minX) minX = x;
    if (x > maxX) maxX = x;
    if (y < minY) minY = y;
    if (y > maxY) maxY = y;
  }
  ctx.putImageData(id, 0, 0);
  if (maxX < 0) return { canvas: cv, removed: true };

  const out = document.createElement('canvas');
  out.width = maxX - minX + 1; out.height = maxY - minY + 1;
  out.getContext('2d').drawImage(cv, minX, minY, out.width, out.height, 0, 0, out.width, out.height);
  return { canvas: out, removed: true };
}

/* ---------------------------------------------------------------- 상품 페이지 읽기 */
const IMG_EXT = /\.(?:jpe?g|png|webp|avif)(?:$|\?)/i;
const IMG_URL_RE = /https?:\/\/[^\s"'<>\\]+?\.(?:jpe?g|png|webp|avif)(?:\?[^\s"'<>\\]*)?/gi;
const JUNK_RE = /(logo|icon|sprite|favicon|banner|btn|button|blank|dummy|placeholder|spacer|kakao|naver|facebook|twitter|payco|toss|badge|coupon|delivery|flag_|flag_shapes|\/common\/img\/|footer|header|_nav|arrow|star|avatar|profile|emoji|qr|app_?down|1x1|pixel)/i;
const TRACKER_RE = /(facebook\.com|google-analytics|googletagmanager|doubleclick|criteo|\/tr\?|\/collect|\/pixel|\/log\?|analytics)/i;
const GALLERY_HINT = /(xzoom|gallery|상품\s*이미지|product.?imag|goods.?imag|prd.?img|detail.?imag)/i;
const COLOR_HINT = /(chip|color|colour|option|swatch)/i;
const CHIP_RE = /(chip|swatch)/i;

function parseMetas(doc){
  const out = {};
  doc.querySelectorAll('meta').forEach(tag => {
    const key = tag.getAttribute('property') || tag.getAttribute('name') || tag.getAttribute('itemprop');
    const val = tag.getAttribute('content');
    if (key && val != null && !(key.toLowerCase() in out)) out[key.toLowerCase()] = val.trim();
  });
  return out;
}

const absolute = (raw, base) => { try { return new URL(raw, base).toString(); } catch { return null; } };

function pickImage(metas, html, base){
  for (const k of ['og:image:secure_url', 'og:image', 'twitter:image', 'twitter:image:src', 'image'])
    if (metas[k]) return absolute(metas[k], base);
  const ld = html.match(/"image"\s*:\s*(?:\[\s*)?"([^"]+)"/);
  if (ld) return absolute(ld[1], base);
  const any = html.match(/<img[^>]+src=["']([^"']+\.(?:jpe?g|png|webp)[^"']*)["']/i);
  return any ? absolute(any[1], base) : null;
}

function pickTitle(metas, doc){
  for (const k of ['og:title', 'twitter:title', 'name']) if (metas[k]) return metas[k];
  return (doc.title || '').replace(/\s+/g, ' ').trim();
}

function pickPrice(metas, html){
  let raw = '';
  for (const k of ['product:price:amount', 'og:price:amount', 'price']) if (metas[k]) { raw = metas[k]; break; }
  if (!raw) { const m = html.match(/"price"\s*:\s*"?([0-9][0-9,.]*)"?/); raw = m ? m[1] : ''; }
  raw = raw.replace(/[^0-9.]/g, '').replace(/\.$/, '');
  return raw && Number(raw) > 0 ? raw : '';
}

function pickCurrency(metas, html){
  for (const k of ['product:price:currency', 'og:price:currency', 'pricecurrency'])
    if (metas[k]) return metas[k].trim().toUpperCase().slice(0, 3);
  const m = html.match(/"priceCurrency"\s*:\s*"([A-Za-z]{3})"/);
  return m ? m[1].toUpperCase() : '';
}

// 지연로딩 속성과 JSON 덩어리까지 훑고, 태그의 class/alt 를 힌트로 챙긴다
function collectImages(doc, html, base){
  const hints = new Map(), order = [];
  const add = (raw, hint, strict) => {
    if (!raw || raw.startsWith('data:')) return;
    const url = absolute(raw.trim(), base);
    if (!url) return;
    if (TRACKER_RE.test(url) || JUNK_RE.test(new URL(url).pathname)) return;
    if ((strict || !hint) && !IMG_EXT.test(url)) return;
    if (hints.has(url)) { if (hint && !hints.get(url)) hints.set(url, hint); return; }
    hints.set(url, hint || ''); order.push(url);
  };

  doc.querySelectorAll('img').forEach(tag => {
    const hint = ['class', 'alt', 'id', 'title'].map(a => tag.getAttribute(a) || '').join(' ');
    for (const attr of ['data-original', 'data-src', 'data-lazy', 'data-echo', 'src'])
      add(tag.getAttribute(attr), hint);
    const set = tag.getAttribute('srcset');
    if (set) {                                  // srcset 중 가장 큰 것
      let best = null, bestW = -1;
      for (const part of set.split(',')) {
        const bits = part.trim().split(/\s+/);
        if (!bits[0]) continue;
        const w = Number((bits[bits.length - 1] || '').replace(/\D/g, '')) || 0;
        if (w > bestW) { bestW = w; best = bits[0]; }
      }
      add(best, hint);
    }
  });

  for (const u of html.match(IMG_URL_RE) || []) add(u, '', true);
  return order.slice(0, 80).map(url => ({ url, hint: hints.get(url) }));
}

/* ---------------------------------------------------------------- 후보 고르기 */
const looksLikeId = seg => seg.length >= 8 && /\d/.test(seg) && /^[0-9A-Za-z_-]+$/.test(seg);

// URL 에서 숫자·해시를 지워 '모양'만 남긴다. 같은 갤러리 줄은 모양이 같아진다.
function shapeKey(u){
  const p = new URL(u);
  const segs = p.pathname.split('/').map(seg => {
    const dot = seg.lastIndexOf('.');
    if (dot > 0 && looksLikeId(seg.slice(0, dot))) return '#.' + seg.slice(dot + 1).toLowerCase();
    if (looksLikeId(seg)) return '#';
    return seg.replace(/\d+/g, '#');
  });
  return domainOf(u) + segs.join('/');
}
const canon = u => { const p = new URL(u); return p.host.split(':')[0] + p.pathname; };
// cdn.imweb.me 와 cdn-optimized.imweb.me 를 같은 곳으로 본다
const domainOf = u => new URL(u).hostname.split('.').slice(-2).join('.');
// 유니클로 krgoods_69_482279 와 goods_69_482279_chip 은 같은 컬러웨이 코드를 쓴다
const codeTokens = u => new Set((new URL(u).pathname.split('/').pop() || '').match(/\d{2,}/g) || []);

function pickColorSet(items, main, limit = 16){
  if (!items.length) return [];
  const merged = new Map();
  for (const it of items) {
    const key = canon(it.url);
    if (merged.has(key)) merged.get(key).hint = (merged.get(key).hint + ' ' + it.hint).trim();
    else merged.set(key, { ...it });
  }
  const uniq = [...merged.values()];
  const site = main ? domainOf(main) : null;

  const groups = new Map();
  for (const it of uniq) {
    if (site && domainOf(it.url) !== site) continue;
    // 사이트 전체 이미지가 같은 경로 모양을 쓰는 경우가 있어, 갤러리 표시가 붙은 것은 따로 묶는다
    const key = shapeKey(it.url) + (GALLERY_HINT.test(it.hint) ? '|G' : '');
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(it);
  }

  let best = null, bestScore = -1;
  for (const [key, group] of groups) {
    if (group.length < 2 || group.length > 30) continue;
    let score = group.length;
    if (COLOR_HINT.test(key)) score += 100;              // 색상 칩 경로
    if (key.endsWith('|G')) score += 60;                 // 갤러리 썸네일 줄
    if (main && group.some(g => g.url === main)) score += 20;
    if (score > bestScore) { bestScore = score; best = group; }
  }

  const out = [];
  for (const u of (main ? [main] : []).concat((best || uniq).map(g => g.url)))
    if (u && !out.some(x => canon(x) === canon(u))) out.push(u);
  return out.slice(0, limit);
}

// 후보마다 대표색을 뽑는다. 화면에 보여줄 목록은 색이 거의 같은 것(같은 색 다른 각도)을
// 한 장만 남기지만, 칩을 고를 때는 전체를 써야 한다. 대표 사진과 같은 컬러웨이의 칩은
// 색이 비슷해서 걸러지는데, 그게 바로 우리가 찾는 칩이다.
async function annotateColors(urls, referer, gap = 14){
  const all = (await Promise.all(urls.map(async url => {
    try { return { url, hex: dominantColors(await loadImage(url, referer))[0].hex }; }
    catch { return null; }
  }))).filter(Boolean);
  const kept = [], rgbs = [];
  for (const item of all) {
    const rgb = [1, 3, 5].map(i => parseInt(item.hex.slice(i, i + 2), 16));
    if (rgbs.some(p => Math.hypot(rgb[0] - p[0], rgb[1] - p[1], rgb[2] - p[2]) <= gap)) continue;
    kept.push(item); rgbs.push(rgb);
  }
  return { all, kept };
}

// 색상 칩이 있으면 그것으로 옷의 실제 색과 패턴을 본다.
// 모델컷은 피부·배경·그림자가 섞여서 대표색이 배경으로 잡히고 줄무늬도 묻힌다.
async function garmentView(candidates, mainUrl, referer, mainHex){
  const chips = candidates.filter(c => CHIP_RE.test(new URL(c.url).pathname));
  if (!chips.length) return null;
  const want = codeTokens(mainUrl || '');
  const base = mainHex ? [1, 3, 5].map(i => parseInt(mainHex.slice(i, i + 2), 16)) : null;
  const rank = c => {
    const shared = [...codeTokens(c.url)].filter(t => want.has(t)).length;
    const rgb = [1, 3, 5].map(i => parseInt(c.hex.slice(i, i + 2), 16));
    const gap = base ? rgb.reduce((s, v, i) => s + (v - base[i]) ** 2, 0) : 0;
    return [-shared, gap];                     // 컬러웨이 코드가 먼저, 그다음 색 거리
  };
  chips.sort((a, b) => { const A = rank(a), B = rank(b); return A[0] - B[0] || A[1] - B[1]; });
  for (const chip of chips) {
    try {
      const img = await loadImage(chip.url, referer);
      const pattern = analyzePattern(img);
      pattern.from = 'chip';
      return { pattern, colors: dominantColors(img), chip: chip.url };
    } catch { /* 다음 칩 */ }
  }
  return null;
}

/* ---------------------------------------------------------------- 상품 불러오기 */
// 앱 공유 링크는 진짜 주소를 쿼리에 담고 있다. 먼저 풀어야 상품 페이지에 닿는다.
const DEEPLINK_HOST = /(onelink\.me|app\.link|page\.link|smart\.link|adj\.st|bit\.ly|naver\.me)$/i;
const DEEPLINK_KEYS = ['af_web_dp', 'af_dp', 'af_r', 'deep_link_value', 'url', 'link', 'u',
                       'target', 'redirect', 'redirect_url'];

function unwrapDeeplink(url){
  let out = url;
  for (let i = 0; i < 3; i++) {                  // 두 번 감싼 링크도 있다
    let u;
    try { u = new URL(out); } catch { return out; }
    if (!DEEPLINK_HOST.test(u.hostname)) return out;
    let next = null;
    for (const key of DEEPLINK_KEYS) {
      const v = u.searchParams.get(key);
      if (v && /^https?:\/\//i.test(v)) { next = v; break; }
    }
    if (!next) return out;
    out = next;
  }
  return out;
}

/* 내 사진 올리기 — 자동 수집을 막는 사이트나 앱에서만 보이는 상품은 이 길이 확실하다 */
async function getLocalPhoto(file){
  if (!file || !file.type || !file.type.startsWith('image/'))
    throw new Error('이미지 파일만 넣어 주세요 (jpg, png, heic 등).');
  const dataUrl = await new Promise((ok, fail) => {
    const reader = new FileReader();
    reader.onload = () => ok(reader.result);
    reader.onerror = () => fail(new Error('파일을 읽지 못했어요.'));
    reader.readAsDataURL(file);
  });
  const img = await loadImage(dataUrl, null);
  const pattern = analyzePattern(img);
  pattern.from = 'photo';
  return {
    url: '', referer: '', site: '내 사진',
    title: (file.name || '사진').replace(/\.[^.]+$/, '').slice(0, 60),
    price: '', currency: '',
    image: dataUrl, source: dataUrl,
    colors: dominantColors(img), pattern, color_from: 'photo',
    candidates: [], all_candidates: [], local: true,
  };
}

async function getProduct(raw){
  let url = (raw || '').trim();
  if (!url) throw new Error('주소 형식이 이상해요. https:// 로 시작하는 상품 주소를 넣어 주세요.');
  if (!/^https?:\/\//i.test(url)) url = 'https://' + url;
  url = unwrapDeeplink(url);

  // 이미지 주소를 직접 넣은 경우
  if (IMG_EXT.test(url)) {
    const img = await loadImage(url, null);
    const pattern = analyzePattern(img);
    pattern.from = 'photo';
    return {
      url, referer: '', site: new URL(url).hostname.replace(/^www\./, ''),
      title: new URL(url).pathname.split('/').pop(), price: '', currency: '',
      image: relayImg(url, null), source: url,
      colors: dominantColors(img), pattern, color_from: 'photo',
      candidates: [], all_candidates: [],
    };
  }

  const { html, finalUrl } = await relayPage(url);
  const doc = new DOMParser().parseFromString(html, 'text/html');
  const metas = parseMetas(doc);
  let main = pickImage(metas, html, finalUrl);

  const every = collectImages(doc, html, finalUrl);
  if (main && !every.some(e => e.url === main)) every.unshift({ url: main, hint: 'og:image' });
  if (!every.length) throw new Error('상품 이미지를 못 찾았어요. 색을 직접 지정해 주세요.');
  main = main || every[0].url;

  const mainImg = await loadImage(main, finalUrl);
  let colors = dominantColors(mainImg);
  let pattern = analyzePattern(mainImg);
  pattern.from = 'photo';
  let colorFrom = 'photo';

  const shots = await annotateColors(pickColorSet(every, main), finalUrl);
  const view = await garmentView(shots.all, main, finalUrl, colors[0] && colors[0].hex);
  if (view) { colors = view.colors; pattern = view.pattern; colorFrom = 'chip'; }

  return {
    url, referer: finalUrl,
    site: new URL(finalUrl).hostname.replace(/^www\./, ''),
    title: pickTitle(metas, doc).slice(0, 120),
    price: pickPrice(metas, html),
    currency: pickCurrency(metas, html),
    image: relayImg(main, finalUrl), source: main,
    colors, pattern, color_from: colorFrom,
    candidates: shots.kept.map(c => ({ url: c.url, hex: c.hex })),
    all_candidates: every.map(e => e.url),
  };
}

async function getImageInfo(url, referer){
  const img = await loadImage(url, referer);
  const pattern = analyzePattern(img);
  pattern.from = CHIP_RE.test(new URL(url).pathname) ? 'chip' : 'photo';
  return { image: relayImg(url, referer), source: url, colors: dominantColors(img), pattern };
}

async function getCutout(url, referer){
  const { canvas, removed } = cutout(await loadImage(url, referer));
  return { image: canvas.toDataURL('image/png'), removed };
}

/* ---------------------------------------------------------------- 지역 검색 */
// 한국 도시는 "경주시" 로 등록돼 있어서 "경주" 로는 안 잡히고 기차역만 나온다
const KO_SUFFIXES = ['', '시', '군', '구', '특별자치시', '특별자치도'];
const PLACE_CODES = ['PPL', 'ADM', 'ISL', 'ISLS', 'AREA', 'RGN'];
const OSM_WEIGHT = { city: 3e6, municipality: 1e6, town: 3e5, county: 2e5, state: 15e4,
  province: 15e4, region: 12e4, island: 8e4, borough: 8e4, archipelago: 6e4,
  suburb: 5e4, village: 3e4, hamlet: 1e4 };

// 한글 지명이 한국어 색인에 없고 로마자로만 있는 경우가 있다 (통영 → Tongyeong)
const CHO = ['g','kk','n','d','tt','r','m','b','pp','s','ss','','j','jj','ch','k','t','p','h'];
const JUNG = ['a','ae','ya','yae','eo','e','yeo','ye','o','wa','wae','oe','yo','u','wo','we',
  'wi','yu','eu','ui','i'];
const JONG = ['','k','k','k','n','n','n','t','l','k','m','p','l','l','p','l','m','p','p','t',
  't','ng','t','t','k','t','p','t'];

function romanize(text){
  let out = '';
  for (const ch of text) {
    const code = ch.charCodeAt(0) - 0xAC00;
    if (code >= 0 && code < 11172)
      out += CHO[Math.floor(code / 588)] + JUNG[Math.floor((code % 588) / 28)] + JONG[code % 28];
    else out += ch;
  }
  return out;
}

async function omPlaces(name){
  try {
    const r = await fetch('https://geocoding-api.open-meteo.com/v1/search?count=10&language=ko&name='
      + encodeURIComponent(name));
    return r.ok ? ((await r.json()).results || []) : [];
  } catch { return []; }
}
async function osmPlaces(query){
  try {
    const r = await fetch('https://nominatim.openstreetmap.org/search?format=jsonv2&limit=10'
      + '&accept-language=ko&addressdetails=1&q=' + encodeURIComponent(query));
    return r.ok ? (await r.json()) : [];
  } catch { return []; }
}

async function searchPlaces(raw){
  const q = (raw || '').trim();
  if (!q) return [];
  // "대한민국, 경주" / "캐나다 밴쿠버" 처럼 나라를 같이 적는 경우
  const parts = q.split(/[,/·]| {2,}/).map(s => s.trim()).filter(Boolean);
  const hint = parts.length >= 2 ? parts[0] : '';
  const target = parts.length >= 2 ? parts[parts.length - 1] : q;

  const found = [], seen = new Map();
  const add = item => {
    const keys = [`${item.lat.toFixed(2)},${item.lon.toFixed(2)}`,
                  `${item.name.toLowerCase()}|${item.country}`];
    const prev = keys.map(k => seen.get(k)).find(Boolean);
    if (prev) {                                // 더 센 쪽 점수를 따른다
      prev.pop = Math.max(prev.pop, item.pop);
      if (item.name.toLowerCase() === target.toLowerCase() &&
          prev.name.toLowerCase() !== target.toLowerCase()) prev.name = item.name;
      if (!prev.where) prev.where = item.where;
      keys.forEach(k => { if (!seen.has(k)) seen.set(k, prev); });
      return;
    }
    keys.forEach(k => seen.set(k, item));
    found.push(item);
  };

  const hangul = /[가-힣]/.test(target);
  const roman = hangul ? romanize(target) : '';
  const queries = hangul
    ? KO_SUFFIXES.map(s => target + s).concat([roman, roman + '-si'])
    : [target];

  const [omBatches, osmRows] = await Promise.all([
    Promise.all(queries.map(omPlaces)),
    osmPlaces(hint ? `${hint}, ${target}` : target),
  ]);

  for (const batch of omBatches) for (const r of batch) {      // 한국어 이름 먼저
    if (!PLACE_CODES.some(c => (r.feature_code || '').startsWith(c))) continue;
    add({ name: r.name, where: [r.country, r.admin1].filter(x => x && x !== r.name).join(' · '),
          country: r.country || '', lat: +r.latitude.toFixed(4), lon: +r.longitude.toFixed(4),
          pop: r.population || 0 });
  }
  for (const r of osmRows) {
    const kind = r.addresstype || r.type;
    if (!(kind in OSM_WEIGHT)) continue;                       // 역·학교·식당은 버린다
    const a = r.address || {};
    const name = r.name || (r.display_name || '').split(',')[0];
    const region = a.state || a.province || a.county || '';
    add({ name, where: [a.country, region].filter(x => x && x !== name).join(' · '),
          country: a.country || '', lat: +Number(r.lat).toFixed(4), lon: +Number(r.lon).toFixed(4),
          pop: OSM_WEIGHT[kind] });
  }

  found.sort((a, b) => score(b) - score(a));
  function score(item){
    let s = item.pop;
    if (hint && (item.country + ' ' + item.where).includes(hint)) s += 5e6;
    const low = item.name.toLowerCase();
    if (low === target.toLowerCase() || item.name.startsWith(target)) s += 1e5;
    if (hangul && low === roman.toLowerCase()) s += 1e5;
    return s;
  }
  // 로마자로 찾았으면 사용자가 적은 한글로 보여준다 (Tongyeong → 통영)
  if (hangul) for (const item of found)
    if (item.name.toLowerCase() === roman.toLowerCase()) item.name = target;
  return found.slice(0, 8).map(({ pop, ...rest }) => rest);
}

/* ---------------------------------------------------------------- 기후 */
const CLIM_KEY = 'coordi-climate';
const climCache = () => { try { return JSON.parse(localStorage.getItem(CLIM_KEY)) || {}; } catch { return {}; } };

async function getClimate(lat, lon, month, withForecast){
  if (!(month >= 1 && month <= 12)) throw new Error('월은 1~12 사이여야 해요.');
  const key = `${lat.toFixed(2)},${lon.toFixed(2)},${month}`;
  const cache = climCache();
  let climate = cache[key];

  if (!climate) {
    const year = new Date().getFullYear();
    let data;
    try {
      const r = await fetch('https://archive-api.open-meteo.com/v1/archive'
        + `?latitude=${lat}&longitude=${lon}&start_date=${year - 3}-01-01&end_date=${year - 1}-12-31`
        + '&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,snowfall_sum&timezone=auto');
      if (!r.ok) throw new Error();
      data = (await r.json()).daily || {};
    } catch {
      throw new Error('기온 자료 서버가 지금 응답하지 않아요. 잠시 뒤 다시 해보세요.');
    }
    const times = data.time || [];
    const hi = [], lo = [], years = new Set();
    let rain = 0, snow = 0;
    times.forEach((day, i) => {
      if (Number(day.slice(5, 7)) !== month) return;
      years.add(day.slice(0, 4));
      const a = (data.temperature_2m_max || [])[i], b = (data.temperature_2m_min || [])[i];
      if (a != null) hi.push(a);
      if (b != null) lo.push(b);
      if ((data.precipitation_sum || [])[i] >= 1) rain++;
      if ((data.snowfall_sum || [])[i] >= 0.1) snow++;
    });
    if (!hi.length || !lo.length) throw new Error('이 지역의 기후 자료를 못 찾았어요.');
    const mean = a => a.reduce((s, v) => s + v, 0) / a.length;
    const n = Math.max(years.size, 1);
    climate = {
      month,
      tmax: +mean(hi).toFixed(1), tmin: +mean(lo).toFixed(1),
      tmean: +((mean(hi) + mean(lo)) / 2).toFixed(1),
      swing: +(mean(hi) - mean(lo)).toFixed(1),
      rain_days: Math.round(rain / n), snow_days: Math.round(snow / n),
    };
    cache[key] = climate;
    try { localStorage.setItem(CLIM_KEY, JSON.stringify(cache)); } catch {}
  }

  let forecast = [];
  if (withForecast) {
    try {
      const r = await fetch('https://api.open-meteo.com/v1/forecast'
        + `?latitude=${lat}&longitude=${lon}&forecast_days=7&timezone=auto`
        + '&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max');
      const d = (await r.json()).daily || {};
      forecast = (d.time || []).map((t, i) => ({
        date: t, max: (d.temperature_2m_max || [])[i], min: (d.temperature_2m_min || [])[i],
        rain: (d.precipitation_probability_max || [])[i],
      }));
    } catch { forecast = []; }
  }
  return { climate, forecast };
}

window.coordi = { getProduct, getLocalPhoto, getImageInfo, getCutout,
                  searchPlaces, getClimate, thumb: relayImg };
