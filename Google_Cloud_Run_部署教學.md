# 儲位配置與揀貨策略優化系統 —— 部署到 Google Cloud Run 教學

這份教學假設你完全沒用過 Google Cloud，每一步都會講「要在哪裡點、在哪裡打指令」。
整個過程完全不需要在自己電腦上安裝任何東西（不用裝 Docker、不用裝 gcloud），
全部透過瀏覽器裡的「Cloud Shell」完成。

看完 Part 1～4，你的系統就會有一個網址可以打開了（大約 15～20 分鐘）。
Part 5 是「資料要不要保留」的重要說明，強烈建議看完再決定要不要正式使用。

---

## 開始之前你需要什麼

- 一個 Google 帳號（一般的 Gmail 帳號就可以）
- 一張信用卡（Google Cloud 要求綁信用卡才能啟用帳務，**但有免費額度**，這個系統
  這種小流量用法，一個月的費用通常是幾十元台幣以內、甚至 $0，細節見 Part 6）
- 你的專案程式碼（就是 `warehouse-system舊版` 這個資料夾）

---

## Part 1：建立 Google Cloud 專案

1. 瀏覽器打開 **https://console.cloud.google.com**，用你的 Google 帳號登入。
2. 第一次登入會問你要不要開通免費試用（通常會送 $300 額度、90 天），照畫面指示
   填信用卡資料、按「啟用」／「Start free」即可，這步不會馬上扣款。
3. 登入後畫面最上方、Google Cloud 標誌旁邊，有一個下拉選單（可能寫著「選取專案」
   或已有的專案名稱），點下去。
4. 跳出的視窗右上角點「**新增專案**」（New Project）。
5. 專案名稱填 `warehouse-system`（名稱只是給你自己看的，隨意），**專案 ID**
   下面會自動產生一串（例如 `warehouse-system-123456`）——**這串 ID 之後會用到，
   建議先複製記下來**。
6. 按「建立」（Create），等幾秒它建好，然後在最上方那個下拉選單裡**切換到剛建好
   的這個專案**（很重要，不然接下來的指令會跑錯專案）。

---

## Part 2：開啟 Cloud Shell（雲端終端機，不用裝任何東西）

1. 畫面**最上方右側**，有一排小圖示，找一個長得像「`>_`」的圖示（滑鼠移過去會
   顯示「Activate Cloud Shell」），點它。
2. 畫面下方會跳出一個黑底的終端機視窗，跑一下初始化，等它出現一個像
   `你的帳號@cloudshell:~ ($)` 的提示字元就可以打指令了。
3. **之後這份教學裡所有「打指令」的步驟，都是在這個黑色視窗裡打**、打完按 Enter。
4. 先確認專案有選對，貼上這行（把 `你的專案ID` 換成 Part 1 記下的那串）：

   ```bash
   gcloud config set project 你的專案ID
   ```

   例如：`gcloud config set project warehouse-system-123456`

---

## Part 3：把你的程式碼放進 Cloud Shell

Cloud Shell 有自己的一塊硬碟（跟你電腦是分開的），要先把 `warehouse-system舊版`
資料夾的內容放進去。兩種方法選一種：

### 方法 A：如果你的程式碼已經在 GitHub 上（README 有提過要 push 到 GitHub 才能部署到 Render，如果你之前做過那一步，程式碼應該已經在 GitHub）

在 Cloud Shell 打：

```bash
git clone 你的repo網址
cd 你的repo資料夾名稱
```

（repo 網址就是你 GitHub 上那個專案頁面網址加 `.git`，例如
`https://github.com/你的帳號/warehouse-system.git`）

### 方法 B：直接把資料夾上傳（如果程式碼只在你自己電腦上）

1. 在 Cloud Shell 視窗右上角，有個「⋮」（更多選項）或「上傳」的圖示，點開會看到
   「**上傳**」（Upload）。
2. Cloud Shell 一次只能上傳檔案、不能整包資料夾，最簡單的做法是：先在你電腦上把
   `warehouse-system舊版` 整個資料夾**壓縮成一個 zip 檔**（在資料夾上按右鍵 →
   「傳送到」→「壓縮的資料夾」，或用你慣用的壓縮工具）。
3. 點「上傳」，選那個 zip 檔，等它傳完（傳完會出現在 Cloud Shell 的家目錄）。
4. 在 Cloud Shell 打指令解壓縮（把 `檔名.zip` 換成你實際的檔名）：

   ```bash
   unzip 檔名.zip -d warehouse-system
   cd warehouse-system
   ```

5. 確認檔案有進來，打 `ls`，應該要看到 `Dockerfile`、`backend`、`frontend` 這些
   東西。

---

## Part 4：一鍵部署到 Cloud Run

### 4-1　啟用需要的服務（只需要做一次）

在 Cloud Shell 打（這步會問你「要啟用計費帳戶嗎」之類的提示，照著按確認即可）：

```bash
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com
```

這會等個 30 秒到 1 分鐘，跑完沒有紅字錯誤訊息就是成功了。

### 4-2　部署

**確認你現在的所在目錄是專案的最上層**（打 `ls` 應該要看到 `Dockerfile`），然後
打這一行指令：

```bash
gcloud run deploy warehouse-system \
  --source . \
  --region asia-east1 \
  --allow-unauthenticated \
  --set-env-vars WAREHOUSE_DATA_DIR=/data \
  --max-instances 1 \
  --memory 1Gi
```

指令打完會問你幾個問題，照下面回答：

- 如果問 `Allow unauthenticated invocations`：因為指令裡已經帶了
  `--allow-unauthenticated`，通常不會再問；如果有問就選 `y`（表示任何人都能打開
  這個網址，因為系統本身有自己的帳號登入機制，不需要再加一層 Google 的存取限制）。
- 如果問要不要建立 Artifact Registry repository：選 `y`。

接著它會開始**用你的程式碼建置容器映像**（跑 Cloud Build，讀你的 `Dockerfile`），
這步大約 3～6 分鐘，會看到一堆安裝套件的 log（`pip install...`）跑過去，正常。

建置完成後會自動部署，最後會印出類似這樣的一行：

```
Service URL: https://warehouse-system-xxxxxxxxxx.asia-east1.run.app
```

**這個網址就是你的系統網址**，複製貼到瀏覽器打開，應該會看到登入畫面（第一次
要先「註冊」一個帳號，見 README「快速開始」第 3 點）。

> `--region asia-east1` 是彰化的資料中心，離台灣最近、速度最快，不建議改。
> `--max-instances 1` 這個很重要、**請不要拿掉**，原因見下面 Part 5。

---

## Part 5：資料會不會不見？（很重要，請務必看完再正式使用）

這個系統的資料（帳號、上傳的檔案、清洗結果）是存在**容器裡的本機磁碟**
（`WAREHOUSE_DATA_DIR=/data`），用 SQLite。這件事在 Cloud Run 上有兩個坑：

1. **Cloud Run 的容器磁碟預設是「用完即丟」**：容器被關掉（例如閒置一段時間、
   或你重新部署新版本）之後再打開，是全新的容器、磁碟是空的——**所有帳號和上傳
   的資料都會消失**。這跟 README 裡提到 Render 免費方案的限制是同一個道理。
2. **如果同時有兩個容器實例在跑，兩邊的磁碟是各自獨立、互不相通的**——例如你剛
   好在 A 實例註冊帳號、清洗完資料，下一次打開網頁卻被導去 B 實例，會發現「我的
   帳號不見了」。這就是為什麼上面的部署指令加了 `--max-instances 1`（強制最多
   只能有一個實例同時運作，避免這個問題，跟 README 裡「有掛 disk 的服務只能單一
   實例，這正好符合 SQLite 的用法」是同一個考量）。

**結論：**

- **如果只是想先試玩、demo 給人看**：上面 Part 4 的指令已經夠用，`--max-instances 1`
  可以避免「資料分裂在兩台」的問題，但「閒置太久或重新部署後資料會清空」這個限制
  還在，跟 Render 免費方案的行為基本一樣。
- **如果要正式給人長期使用、資料不能不見**：需要把 `/data` 這個目錄「掛載」到一個
  真正持久的儲存空間，而不是容器自己的磁碟。往下看「Part 5-1：加上持久儲存」。

### Part 5-1：加上持久儲存（讓資料不會消失）

Cloud Run 可以直接把一個 **Cloud Storage 儲存桶（bucket）** 掛載成容器裡的一個
資料夾，讀寫起來就像本機磁碟一樣，不需要改你的程式碼。步驟：

1. 建立一個儲存桶（`你的專案ID-warehouse-data` 這串名字全球要唯一，建議照抄、
   前面加你的專案ID 就不會撞名）：

   ```bash
   gcloud storage buckets create gs://你的專案ID-warehouse-data --location=asia-east1
   ```

2. 重新部署一次，這次加上掛載設定：

   ```bash
   gcloud run deploy warehouse-system \
     --source . \
     --region asia-east1 \
     --allow-unauthenticated \
     --set-env-vars WAREHOUSE_DATA_DIR=/data \
     --max-instances 1 \
     --memory 1Gi \
     --add-volume name=data-vol,type=cloud-storage,bucket=你的專案ID-warehouse-data \
     --add-volume-mount volume=data-vol,mount-path=/data
   ```

這樣之後不管重新部署幾次、容器休眠又醒來，`/data` 底下的資料都會保留在這個
儲存桶裡，不會消失。

> 小提醒：SQLite 這種資料庫在「網路掛載的檔案系統」上，遇到**大量同時寫入**時
> 穩定性不如本機磁碟，但這個系統本來就設計成「單一實例、內部使用」（見上面
> `--max-instances 1`），一般幾個人同時操作的情境下沒問題；如果之後真的要給
> 幾十人同時大量使用，建議考慮換成 Cloud SQL（正式的雲端資料庫），這屬於比較
> 大的架構調整，非本教學範圍。

---

## Part 6：以後要怎麼更新／多少錢／怎麼刪掉

### 更新程式碼後要怎麼重新部署

之後你（或我）改了程式碼，想要更新到雲端上，流程是：把改過的檔案放進 Cloud
Shell（同 Part 3 的方法），`cd` 進資料夾，然後重新打一次 Part 4-2（或 Part 5-1，
如果你已經設定了持久儲存）那串 `gcloud run deploy` 指令即可，網址不會變。

### 費用

Cloud Run 是「有人用才收費」（用多少算多少，沒人打開網站幾乎不花錢），而且每個
月有免費額度（約 200 萬次請求、360,000 GB-秒運算是免費的）。像這種內部小型倉儲
系統、使用量不大的話，**一個月費用通常在幾十元台幣以內，很可能是 $0**。

如果不放心，可以設定預算警示（超過你設的金額會 email 通知你）：

1. 左上角「☰」選單 →「帳單」（Billing）。
2. 左側選「預算與提醒」（Budgets & alerts）→「建立預算」，設定例如「每月 200 元
   台幣」，之後接近或超過會收到通知（**不會自動關站**，只是提醒）。

### 不想用了怎麼整個刪掉、確保不再扣錢

最乾脆的方式是直接刪掉整個專案（連同裡面所有資源一起刪除）：

1. 左上角「☰」→「IAM 與管理」→「設定」（Settings）。
2. 確認上面顯示的是你這個 `warehouse-system` 專案，點「關閉」（Shut down）。
3. 依畫面指示輸入專案 ID 確認。

專案關閉後大約 30 天內都不會再產生任何費用，之後會被永久刪除。

---

## 常見問題

**Q：部署指令跑到一半說 `PERMISSION_DENIED` 或要我啟用某個 API？**
照它提示的指令貼上執行，或回到 Part 4-1 重新跑一次 `gcloud services enable`
那行，確認三個 API 都有列在裡面。

**Q：打開網址是空白頁或 502 錯誤？**
先等 1～2 分鐘（容器剛啟動需要暖機時間），還是不行的話打這行看 log 找錯誤訊息：

```bash
gcloud run services logs read warehouse-system --region asia-east1 --limit 50
```

**Q：Cloud Shell 過一段時間沒動作，视窗說連線中斷？**
沒關係，資料不會不見（Cloud Shell 的家目錄本身是持久的），重新整理分頁、
`cd` 回你的專案資料夾繼續就好；如果找不到資料夾，打 `ls` 看看目前在哪裡。

**Q：想把網址換成自己的網域（例如 warehouse.你的公司.com）？**
可以，但需要你有一個網域名稱、且要另外設定 DNS，這部分較進階，需要的話再告訴我，
我可以再補一份教學。
