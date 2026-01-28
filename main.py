# main.py
# CloudRAG - 서버 실행 엔트리포인트

import os
import sys

# backend 폴더를 경로에 추가
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backend"))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        reload_dirs=["backend"]
    )
