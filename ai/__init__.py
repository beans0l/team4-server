"""검증 스크립트 묶음. server 패키지가 여기서 함수를 가져다 쓴다.

각 스크립트는 `if __name__ == "__main__"` 가드가 있어서 import 해도 실행되지 않는다.
무거운 의존성(rembg, google-genai)은 함수 안에서 lazy import 하므로 import 비용도 싸다.
"""
