"""결과 PNG 보관.

서버 디스크에 쓰고 `/files/<키>` 경로를 돌려준다. main.py 가 그 디렉토리를
StaticFiles 로 마운트하고, routes/stylize.py 가 절대 URL 로 바꿔서 앱에 준다.

⚠️ 가비아 VM 은 디스크가 영속이라 이게 정상 운영 방식이다. 단 Docker 로 띄운다면
   볼륨(-v)을 반드시 마운트할 것. 안 하면 컨테이너를 다시 만드는 순간 등록된
   인형이 전부 사라진다. 기본값 /tmp 도 안 된다 — 리눅스가 주기적으로 비운다.

앱 연동 규약: 여기서 주는 URL 은 **공개·무기한**이다. 만료가 생기면 앱이 이미지를
미리 내려받아 두는 설계가 필요해지므로, 바꾸려면 반드시 팀원과 합의할 것.
"""

import io
import logging
import uuid
from pathlib import Path

from PIL import Image

from server import config

log = logging.getLogger(__name__)


def new_key(suffix: str = "front") -> str:
    return f"{uuid.uuid4().hex[:12]}_{suffix}.png"


def save_png(img: Image.Image, key: str) -> str:
    """PNG 로 저장하고 '/files/...' 경로를 돌려준다."""
    buf = io.BytesIO()
    img.save(buf, format="PNG")

    dest = Path(config.LOCAL_STORAGE_DIR)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / key).write_bytes(buf.getvalue())

    return f"/files/{key}"
