# 儲位配置與揀貨策略優化系統 — 正式部署用容器映像
# 後端（FastAPI）連同前端 index.html 一起打包，由同一個服務送出（同源、免處理 CORS）。
# 適用 Render（runtime: docker）、也可直接在任何有 Docker 的雲主機跑。

FROM python:3.12-slim

# 讓 log 即時輸出、不寫 .pyc
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# 先只複製需求檔並安裝，利用 Docker 快取：日後只改程式不改套件時，不必重裝依賴。
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

# 再複製程式碼（後端 + 前端）
COPY backend ./backend
COPY frontend ./frontend

# 資料落地目錄：正式部署時把這個路徑掛到「持久磁碟」，資料才不會在重新部署時消失。
# 前端目錄：讓後端知道去哪裡找 index.html 一起送出。
ENV WAREHOUSE_DATA_DIR=/data \
    WAREHOUSE_FRONTEND_DIR=/app/frontend
# chmod 777：Hugging Face Spaces 的容器以非 root 使用者（UID 1000）執行，
# 若不開放寫入權限，後端建立 sessions／uploads／SQLite 時會 Permission denied。
RUN mkdir -p /data && chmod 777 /data

WORKDIR /app/backend

# Render／多數平台會用環境變數 $PORT 指定對外埠；本機直接跑則預設 8000。
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
