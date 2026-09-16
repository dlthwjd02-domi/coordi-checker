#!/bin/zsh
# 더블클릭하면 코디 체커가 열립니다.
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "파이썬3이 없어요."
  echo "터미널에서  xcode-select --install  을 실행해 설치한 뒤 다시 열어 주세요."
  read -r "?엔터를 누르면 창이 닫힙니다."
  exit 1
fi

# 이미 켜져 있으면 두 번 띄우지 않고 브라우저만 연다
if curl -s -o /dev/null -m 2 http://localhost:8787/ 2>/dev/null; then
  echo "이미 켜져 있어요. 브라우저만 엽니다."
  open http://localhost:8787
  exit 0
fi

# 처음 실행이면 이 폴더 안에만 파이썬 꾸러미를 깐다 (맥 전체 설정은 건드리지 않음)
if [ ! -x .venv/bin/python ]; then
  echo "처음 실행이라 준비를 좀 할게요. 1~2분 걸립니다..."
  python3 -m venv .venv || { echo "준비 실패"; read -r "?엔터"; exit 1; }
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt || { echo "꾸러미 설치 실패"; read -r "?엔터"; exit 1; }
  echo "준비 끝!"
fi

.venv/bin/python server.py &
SERVER=$!
for _ in {1..40}; do
  curl -s -o /dev/null -m 1 http://localhost:8787/ 2>/dev/null && break
  sleep 0.25
done
open http://localhost:8787
echo ""
echo "  끄려면 이 창을 닫거나 Ctrl+C 를 누르세요."
wait $SERVER
