// 코디 체커 — 상품 페이지·이미지 가져오기 전용 중계
//
// 브라우저는 다른 사이트의 HTML·이미지를 직접 읽을 수 없다(CORS, 핫링크 차단).
// 이 워커가 대신 가져와서 CORS 헤더를 붙여 돌려준다. 색 계산과 이미지 처리는
// 전부 브라우저가 Canvas 로 하므로 여기서는 아무것도 가공하지 않는다.

const ALLOWED_ORIGINS = [
  'https://dlthwjd02-domi.github.io',
  'http://localhost:8787',
  'http://127.0.0.1:8787',
  'http://localhost:8080',
  'http://127.0.0.1:8080',
];

const UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 ' +
           '(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36';

const MAX_HTML = 4 * 1024 * 1024;
const MAX_IMAGE = 12 * 1024 * 1024;

// 사설망·로컬 주소로는 나가지 않는다 (열린 중계로 악용되지 않게)
const BLOCKED_HOST = /^(localhost$|127\.|10\.|192\.168\.|169\.254\.|172\.(1[6-9]|2\d|3[01])\.|\[?::1\]?$|.*\.local$|.*\.internal$)/i;

function cors(origin) {
  const ok = ALLOWED_ORIGINS.includes(origin);
  return {
    'Access-Control-Allow-Origin': ok ? origin : ALLOWED_ORIGINS[0],
    'Access-Control-Allow-Methods': 'GET, OPTIONS',
    'Access-Control-Max-Age': '86400',
    'Vary': 'Origin',
  };
}

function bad(message, status, origin) {
  return new Response(JSON.stringify({ error: message }), {
    status,
    headers: { 'Content-Type': 'application/json; charset=utf-8', ...cors(origin) },
  });
}

function allowedCaller(request) {
  const origin = request.headers.get('Origin');
  if (origin) return ALLOWED_ORIGINS.includes(origin);
  // 브라우저가 Origin 을 안 보내는 경우가 있어 Referer 로도 본다
  const referer = request.headers.get('Referer') || '';
  return ALLOWED_ORIGINS.some(o => referer.startsWith(o));
}

function parseTarget(raw) {
  let url;
  try {
    url = new URL(raw);
  } catch {
    return null;
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') return null;
  if (BLOCKED_HOST.test(url.hostname)) return null;
  return url;
}

export default {
  async fetch(request) {
    const origin = request.headers.get('Origin') || '';
    if (request.method === 'OPTIONS') return new Response(null, { headers: cors(origin) });
    if (request.method !== 'GET') return bad('GET 만 받습니다.', 405, origin);

    const here = new URL(request.url);

    if (here.pathname === '/' || here.pathname === '/health') {
      return new Response(JSON.stringify({ ok: true, service: 'coordi-fetch' }), {
        headers: { 'Content-Type': 'application/json', ...cors(origin) },
      });
    }

    if (!allowedCaller(request)) return bad('허용되지 않은 호출입니다.', 403, origin);

    const target = parseTarget(here.searchParams.get('u') || '');
    if (!target) return bad('가져올 주소가 올바르지 않아요.', 400, origin);

    const isImage = here.pathname === '/img';
    if (here.pathname !== '/page' && !isImage) return bad('없는 경로입니다.', 404, origin);

    const headers = {
      'User-Agent': UA,
      'Accept-Language': 'ko-KR,ko;q=0.9,en;q=0.8',
      // AVIF 는 브라우저 지원이 갈려서 요청하지 않는다 (Canvas 로 읽어야 한다)
      'Accept': isImage
        ? 'image/jpeg,image/png,image/webp,image/*;q=0.8'
        : 'text/html,application/xhtml+xml,*/*;q=0.8',
    };
    const referer = here.searchParams.get('r');
    if (referer) {
      const ref = parseTarget(referer);
      if (ref) headers['Referer'] = ref.toString();   // 핫링크 차단 통과용
    }

    let upstream;
    try {
      upstream = await fetch(target.toString(), {
        headers,
        redirect: 'follow',
        cf: { cacheTtl: isImage ? 604800 : 900, cacheEverything: true },
      });
    } catch {
      return bad('주소를 못 찾았어요. 사이트 주소가 맞는지 확인해 주세요.', 502, origin);
    }

    if (!upstream.ok) {
      const code = upstream.status;
      const message = code === 404
        ? '그 주소에는 상품이 없어요 (404). 주소를 다시 확인해 주세요.'
        : [401, 403, 405, 429].includes(code)
          ? `이 사이트가 자동 수집을 막고 있어요 (${code}). 상품 이미지 주소를 직접 넣거나 색을 직접 지정해 주세요.`
          : `사이트가 오류를 냈어요 (${code}).`;
      return bad(message, 200, origin);   // 앱이 문구를 그대로 보여주도록 200 으로
    }

    const type = upstream.headers.get('Content-Type') || '';
    const size = Number(upstream.headers.get('Content-Length') || 0);
    const cap = isImage ? MAX_IMAGE : MAX_HTML;
    if (size && size > cap) return bad('파일이 너무 커요.', 200, origin);

    if (isImage && !type.startsWith('image/')) {
      return bad('이미지가 아니에요.', 200, origin);
    }

    const body = await upstream.arrayBuffer();
    if (body.byteLength > cap) return bad('파일이 너무 커요.', 200, origin);

    return new Response(body, {
      headers: {
        'Content-Type': isImage ? type : 'text/plain; charset=utf-8',
        'X-Final-Url': upstream.url,          // 리다이렉트된 최종 주소 (상대경로 계산용)
        'Access-Control-Expose-Headers': 'X-Final-Url',
        'Cache-Control': isImage ? 'public, max-age=604800' : 'public, max-age=900',
        ...cors(origin),
      },
    });
  },
};
