# 部署到 Google Cloud Run（GitHub + Cloud Run）

專案已經內建 `Dockerfile`（後端 FastAPI + 前端 index.html 一起打包，同源送出），且完全相容
Cloud Run（監聽 `0.0.0.0`、吃 `$PORT` 環境變數）。`render.yaml` 是 Render 專用設定，Cloud Run
不會讀它，可以留著不用管。

## 0. 前置準備

1. 安裝 [gcloud CLI](https://cloud.google.com/sdk/docs/install)，然後登入並選定專案：
   ```bash
   gcloud auth login
   gcloud config set project YOUR_PROJECT_ID
   ```
2. 確認這個 GCP 專案已啟用帳單（Cloud Run 有免費額度，但仍需綁定信用卡的專案才能用）。
3. 啟用需要的 API：
   ```bash
   gcloud services enable run.googleapis.com \
     cloudbuild.googleapis.com \
     artifactregistry.googleapis.com \
     storage.googleapis.com \
     iamcredentials.googleapis.com
   ```

## 1. 推上 GitHub

專案已經設定好遠端 `origin`（`github.com/benson103102/warehouse-system`）。推上去之前先確認
`P零售物流data.xlsx`（約90MB，真實業務資料）不會被帶進 repo —— `.gitignore` 目前沒排除它，
建議先加一行：

```bash
cd warehouse-system
echo "P零售物流data.xlsx" >> .gitignore
git add .
git commit -m "chore: 部署前清理未使用端點、排除大型原始資料檔"
git push origin main
```

> 如果這個檔案先前已經 commit 過，加 `.gitignore` 沒用（歷史紀錄裡還在），要另外用
> `git filter-repo` 或 `git rm --cached` 處理；不確定的話跟我說，我可以幫你檢查。

## 2. 部署到 Cloud Run（用原始碼直接建置）

在 repo 根目錄（`Dockerfile` 所在位置）執行：

```bash
gcloud run deploy warehouse-system \
  --source . \
  --region asia-east1 \
  --allow-unauthenticated \
  --memory 2Gi \
  --cpu 2 \
  --timeout 3600 \
  --no-cpu-throttling \
  --min-instances 1 \
  --max-instances 1 \
  --set-env-vars WAREHOUSE_DATA_DIR=/data
```

參數說明（跟這個專案的特性有關，不是隨便抄的預設值）：

- **`--no-cpu-throttling`（重要）**：Cloud Run 預設「只有在處理請求時才配給 CPU」，回應送出後
  CPU 幾乎被砍到 0。但這個後端的 `/api/ingest/upload` 是**先回應、再丟到背景執行緒解析檔案**
  （大檔案要跑 3~4 分鐘），沒開這個參數的話背景解析會被嚴重卡住甚至跑不完。**這是 Cloud Run
  和 Render 最大的差異，Render 沒有這個限制。**
- **`--min-instances 1 --max-instances 1`**：這個後端用本機 SQLite + 磁碟檔案存帳號/資料
  （跟 Render 版一樣），只能單一實例、且要保持活著資料才不會不一致。`min-instances 1` 讓它
  不會縮到 0（縮到 0 = 資料全部消失、下次還要冷啟動），代價是即使沒人用也會有小額常駐費用。
- **`--memory 2Gi --cpu 2`**：處理百萬列 Excel、算 ABC/預測等運算頗吃資源，免費額度的預設規格
  容易 OOM 或跑很慢，抓寬一點。資料量沒那麼大的話可以先試 `1Gi` / `1` 再視情況調高。
- **`--timeout 3600`**：Cloud Run 單一請求最長可設 60 分鐘，防止使用者網路慢、直接上傳大檔案
  時中途被斷線。

部署完成後，指令會印出服務網址（例如 `https://warehouse-system-xxxxx.asia-east1.run.app`），
打開它、**先註冊一個帳號**即可開始使用。

> ⚠️ **關於資料保存的重要提醒**：跟 Render 的持久磁碟不同，Cloud Run 的本機磁碟**不是真正
> 持久化的**——即使設了 `min-instances 1`，Google 仍可能因為維護、升版等原因回收重建實例，
> 屆時 `/data` 會被清空（帳號、上傳檔案、清洗結果全部消失）。這個設定**適合demo/小規模正式
> 使用**，但如果要長期穩定給很多人用，建議之後把 `backend/data/` 換成外部持久儲存（例如
> SQLite 換成 Cloud SQL、檔案換成掛載 GCS FUSE volume），需要的話我可以再幫你加。

## 3. （選用）大檔案直傳 GCS，繞過 Cloud Run 32MB 請求上限

專案已經內建這個機制（`ingest.py` 的 `/upload_url` + `/upload_from_gcs`），只差設定：

```bash
# 建一個 bucket 專門放暫存上傳檔
gsutil mb -l asia-east1 gs://YOUR_BUCKET_NAME

# 讓 Cloud Run 服務帳號能讀寫這個 bucket、也能簽發直傳網址
SA=$(gcloud run services describe warehouse-system --region asia-east1 \
  --format='value(spec.template.spec.serviceAccountName)')
gsutil iam ch serviceAccount:${SA}:roles/storage.objectAdmin gs://YOUR_BUCKET_NAME
gcloud iam service-accounts add-iam-policy-binding ${SA} \
  --member="serviceAccount:${SA}" --role="roles/iam.serviceAccountTokenCreator"

# 允許瀏覽器直接 PUT 檔案到這個 bucket（換成你實際的 Cloud Run 網址）
cat > /tmp/cors.json <<'EOF'
[{"origin": ["https://warehouse-system-xxxxx.asia-east1.run.app"],
  "method": ["PUT"], "responseHeader": ["Content-Type"], "maxAgeSeconds": 3600}]
EOF
gsutil cors set /tmp/cors.json gs://YOUR_BUCKET_NAME

# 告訴後端這個 bucket 名稱
gcloud run services update warehouse-system --region asia-east1 \
  --set-env-vars WAREHOUSE_DATA_DIR=/data,WAREHOUSE_UPLOAD_BUCKET=YOUR_BUCKET_NAME
```

沒設定這段也沒關係：`/upload_url` 會回 501，前端會自動退回一般上傳路徑，小於 32MB 的檔案
完全不受影響。

## 4. （選用）改程式碼後自動重新部署（CI/CD）

Cloud Run 本身不像 Render 有內建的 `autoDeploy`，最簡單的做法是在 Cloud Console 設定
「持續部署」：**Cloud Run → 建立服務 → 「持續部署自 Repository」→ 連接 GitHub → 選這個
repo 與 `main` 分支 → 選 `Dockerfile`**，Google 會自動建立 Cloud Build 觸發條件，之後每次
`git push` 都會自動重新建置＋部署。

沒設定的話，改完程式碼要重新跑一次第 2 步的 `gcloud run deploy --source .` 才會生效。

## 5. 驗證

- 打開服務網址，確認能看到登入畫面、註冊帳號、上傳「內建範例資料」跑一輪流程。
- `https://你的服務網址/api/health` 應回 `{"status":"ok"}`。
- `https://你的服務網址/docs` 可以看 Swagger、直接試打每支 API。
