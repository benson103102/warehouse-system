<!-- 下面這段 --- 之間的設定是給 Hugging Face Spaces 讀的（告訴它用 Docker、對外埠 8000）。
     部署到 Render 不受影響，Render 不看 README。 -->
---
title: 儲位配置與揀貨策略優化系統
emoji: 📦
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 8000
pinned: false
---

# 儲位配置與揀貨策略優化系統 — 前後端分離版

這是把「儲位配置與揀貨策略優化系統.html」（原本 809KB、所有運算都寫在瀏覽器端 JS 裡的
單檔原型）重構成「前端 + 後端 + API」架構的示範專案。完整的架構說明、原函式對照表、
為什麼這樣拆、哪些地方刻意簡化，請看 [`docs/架構與移植對照.md`](docs/架構與移植對照.md)——
**強烈建議先看那份文件再動手改程式**，會省下很多來回摸索的時間。

## 目錄結構

```
warehouse-system/
├── backend/                 # FastAPI 後端（真正做運算的地方）
│   ├── app/
│   │   ├── main.py          # 應用進入點，啟動指令見下方
│   │   ├── state.py         # session 狀態（落地保存版：多人隔離、重啟不遺失）
│   │   ├── store.py         # 落地保存層（SQLite 中繼資料 + 磁碟存 DataFrame/上傳檔）
│   │   ├── jsonsafe.py      # pandas/numpy → JSON 安全轉換
│   │   ├── deps.py          # 共用依賴注入
│   │   ├── routers/         # 8 組 API 端點（ingest/clean/abc/copick/forecast/batch/storage/kpi）
│   │   └── services/
│   │       ├── cleaning_core.py   # 資料清洗（改寫自你們團隊原本的 P零售物流data_清洗腳本.py）
│   │       ├── analytics.py       # ABC／共同揀取／預測／批次／儲位／KPI（移植自原前端 JS）
│   │       └── zones_config.json  # 儲位分區資料表（換算自原前端的倉庫平面座標）
│   ├── tests/
│   │   └── starlette_mirror.py    # 本機驗證用的替代伺服器，不是要交付的程式，見文件說明
│   └── requirements.txt
├── frontend/
│   └── index.html           # 示範前端：只呼叫後端 API、畫表格與圖表，不做任何運算
├── data/                    # ⚠️ 執行後才會自動產生：使用者資料落地保存於此（已被 .gitignore）
│   ├── warehouse.db         # SQLite：session 狀態 + 每次上傳的檔案紀錄
│   ├── sessions/{id}/       # 各使用者解析後的原始資料與清洗結果（.pkl）
│   └── uploads/{id}/        # 各使用者上傳的原始檔，原封不動各留一份
└── docs/
    └── 架構與移植對照.md      # 完整教學文件（從這裡開始看）
```

## 多人使用與資料保存

狀態已從「進程內記憶體字典」升級為**落地保存**，達成三件事：

- **帳號登入**：使用者用 email＋密碼註冊／登入（`/api/auth/*`），登入後拿到 token，之後每次
  請求帶 `Authorization: Bearer <token>`。密碼以 PBKDF2 雜湊存放、絕不明文（見 `security.py`）。
- **多人隔離**：資料歸屬綁定「登入帳號」，後端以使用者 id 當 key 存取資料，彼此互不干擾。
  （不再是前端自帶字串，所以知道別人的什麼 id 也存取不到別人的資料。）
- **重啟不遺失**：解析後的原始資料、清洗結果都存到 `backend/data/`，伺服器重開後第一次被
  存取時自動載回（延遲載入，平時不佔記憶體）。使用者不必重新上傳、不必重跑清洗。
- **記錄每個人的檔案**：每次上傳都會把原始檔位元組存一份到 `data/uploads/`，並在 SQLite
  的 `uploads` 表登記檔名、大小、列數、時間、狀態。用 `GET /api/ingest/uploads` 可查詢
  自己的上傳歷史。

想清空全部資料重來，直接刪掉 `backend/data/` 這個資料夾即可（伺服器會在下次啟動時重建）。
資料存放的絕對路徑可用環境變數 `WAREHOUSE_DATA_DIR` 覆寫（例如部署時指到掛載的資料磁碟）。

> 仍可再強化的部分：把上傳檔改存**物件儲存**（S3／R2）而非本機磁碟（要多台水平擴充時才需要）、
> token 加上有效期限與定期清理。單台部署 + 持久磁碟的現況已足以正式給人使用。

## 部署到 Render

本專案已內含 [`Dockerfile`](Dockerfile) 與 [`render.yaml`](render.yaml)，後端會**連同前端一起送出**
（同源，免處理 CORS）。使用者只要打開你的服務網址，就會看到登入畫面。

**步驟**

1. 把整個專案推上 GitHub（`backend/.gitignore` 已排除 `.venv/` 與 `data/`，不會把本機資料帶上去）。
2. 到 [Render](https://render.com) → **New +** →「**Blueprint**」→ 選這個 repo，Render 會讀 `render.yaml` 自動建好服務。
3. 部署完成後打開服務網址（例如 `https://warehouse-system.onrender.com`），**先註冊一個帳號**即可開始使用。

**⚠️ 目前預設是免費方案（純試玩）**：`render.yaml` 用 `plan: free`，**沒有持久磁碟**。免費方案硬碟
用完即丟——每次重新部署、或服務閒置約 15 分鐘自動休眠再被喚醒時，`data/` 都會清空（帳號、上傳的
檔案都會消失），而且冷啟動要等數十秒。試玩夠用，正式給人用就不行。
要讓資料真正保留：把 `render.yaml` 的 `plan` 改成 `starter`（約 $7/月），並加回 `disk:` 設定
（檔案裡有註解範例）掛一顆 1GB disk 到 `/data`。有掛 disk 的服務只能單一實例，這正好符合 SQLite 的用法。

**改了程式碼會即時更新嗎？** 不會即時，但**半自動**：`render.yaml` 開了 `autoDeploy`，所以你每次
`git push` 到 GitHub，Render 會自動重新 build＋上線，約 1～3 分鐘後生效（不是像本機 `--reload` 那樣一存檔就變）。
使用者「輸入的資料」則是即時記錄的，跟改程式碼無關。

**環境變數**（`render.yaml` 已設好，這裡供參考）
- `WAREHOUSE_DATA_DIR=/data`：資料落地目錄，指向掛載的持久磁碟。
- `WAREHOUSE_CORS_ORIGINS`：僅在「把前端拆成獨立網域」時才需要，填你的前端網域白名單（逗號分隔）。
- `PORT`：Render 自動注入，`Dockerfile` 會自動採用，不必手動設。

## 快速開始

**1. 啟動後端**

```bash
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

> `requirements.txt` 裡的 `python-calamine` 建議一定要裝成功——實測你們的真實資料
> 「訂單資料」工作表有 100 萬列以上，用 openpyxl 讀要 3~4 分鐘，裝了 calamine 後
> `ingest.py` 會自動優先用它（Rust 實作、快很多）。裝不起來也沒關係，程式會自動退回
> openpyxl（就是比較慢），不會整支壞掉。細節見 [`docs/架構與移植對照.md`](docs/架構與移植對照.md) 第 7 節。

啟動後打開 `http://127.0.0.1:8000/docs`，會看到 Swagger 互動式 API 文件，每一支 API 都可以
直接在網頁上試打、看真正的回傳 JSON——這是學習「後端 API 長什麼樣子」最快的方式。

**2. 打開前端**

兩種方式都可以：
- 直接用瀏覽器打開 `frontend/index.html`（會自動連 `http://127.0.0.1:8000`）；或
- 直接開 `http://127.0.0.1:8000/`——後端會把前端一起送出（跟部署後的體驗一致）。

第一次使用要**先註冊一個帳號**（email＋密碼），登入後才會進到主畫面。之後資料都綁定這個帳號。

**3. 操作順序**

依左側選單：① 資料匯入（上傳 .xlsx/.csv，或點「改用內建範例資料」快速體驗；上傳只會
匯入資料本身，**不會**自動清洗——匯入跟清洗是刻意分開的兩個步驟）→ ② 資料清洗（進到
這個頁面後按一次「執行清洗」才會真的清洗）→ ③~⑦ 其餘頁面即可查詢，調整參數會即時
重新呼叫 API。

> 上傳真實的大檔案（例如百萬列等級的訂單資料）時，畫面會顯示「背景解析中…已等待 N 秒」，
> 這是正常的——後端在背景執行緒解析檔案，可能要等好幾分鐘，不是卡死，可以先切去其他
> 頁面，等等再回來看。

## 這份程式碼是怎麼驗證過的

開發這份程式碼的環境本身連不上 PyPI（連 `pip install fastapi` 都被網路政策擋掉），
所以沒辦法直接在那個環境裡跑起真正的 FastAPI 伺服器做端對端測試。應變做法：

1. 業務邏輯層（`cleaning_core.py`／`analytics.py`）本身不依賴任何 Web 框架，直接用你們的
   內建範例資料（2,629 筆訂單）跑過一次完整流程並人工核對數字合理性。
2. 用該環境剛好有預裝的 Starlette（FastAPI 底層用的框架）刻了一個路徑／參數／回傳格式
   完全一致的最簡版本（`backend/tests/starlette_mirror.py`），實際發 HTTP 請求（含檔案上傳）
   測過一輪全部端點。
3. 把示範前端接上這個替代伺服器，用瀏覽器自動化工具實際點過全部 7 個畫面、截圖確認畫面
   正確渲染、瀏覽器主控台沒有 JS 錯誤。

你在自己電腦上第一次執行時，建議照上面「快速開始」跑一遍、對照 Swagger 文件確認每支
API 回傳合理，比較放心。
