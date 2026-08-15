FROM python:3.12-slim

# onnxruntime / pillow 가 요구하는 시스템 라이브러리
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ai/ ./ai/
COPY server/ ./server/

# ★ 배경 제거 모델을 빌드 타임에 받아 이미지에 굽는다.
#   이 줄을 빼면 컨테이너가 뜰 때마다 모델을 새로 내려받아 콜드스타트가 길어지고
#   시연 중 첫 요청이 타임아웃난다.
#   U2NET_HOME 을 고정해야 빌드 때 받은 캐시를 런타임에 같은 경로에서 찾는다.
#
#   모델명을 여기 직접 쓰지 않고 config 에서 읽는 이유: 예전에 코드는 isnet 으로
#   바꿨는데 이 줄만 birefnet 으로 남아 있던 적이 있다. 그러면 안 쓰는 모델을
#   굽고 정작 쓰는 모델은 런타임에 받는, 최악의 조합이 된다.
#   ⚠️ 배포 시 REMBG_MODEL 환경변수로 다른 모델을 지정하면 그 모델은 구워져 있지
#      않으므로 첫 요청이 느려진다. 모델을 바꿀 거면 이미지를 다시 빌드할 것.
ENV U2NET_HOME=/opt/models
RUN python -c "from server import config; from rembg import new_session; new_session(config.REMBG_MODEL); print('baked:', config.REMBG_MODEL)"

ENV PYTHONUNBUFFERED=1
# 플랫폼이 PORT 를 주입하면 그걸 쓰고, 없으면 8080.
ENV PORT=8080

# ★ --forwarded-allow-ips 가 nginx 뒤에 둘 때의 핵심이다.
#   routes/stylize.py 가 request.base_url 로 이미지의 절대 URL 을 만든다. uvicorn 은
#   X-Forwarded-Proto 를 읽어 https 로 바로잡아 주는데, **보낸 쪽이 신뢰 목록에 있을
#   때만** 그렇게 한다. 기본 신뢰 대상은 127.0.0.1 뿐이고, 도커 안에서 본 nginx 는
#   브리지 게이트웨이(172.17.0.1 등)라 여기 해당하지 않는다. 그래서 그냥 두면
#   https 로 접속한 앱이 http 이미지 URL 을 받고, 안드로이드가 그걸 차단한다.
#   증상이 "서버는 200 인데 이미지만 안 보인다" 라서 원인 찾기가 매우 어렵다.
#   (실측: 신뢰 목록에서 빠지면 헤더를 보내도 http:// 로 나간다)
#
#   --proxy-headers 는 uvicorn 기본값이 이미 켜짐이지만, 왜 필요한지가 드러나도록
#   같이 적어 둔다.
#
#   '*' 는 컨테이너 포트를 127.0.0.1 에만 바인딩하는 것을 전제로 한다(README ③).
#   외부에 직접 열어두면 아무나 헤더를 위조할 수 있다.
CMD exec uvicorn server.main:app --host 0.0.0.0 --port ${PORT} \
    --proxy-headers --forwarded-allow-ips='*'
