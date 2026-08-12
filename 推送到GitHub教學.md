# 把程式碼推送（push）到 GitHub 教學

我檢查了你資料夾裡的 `.git` 設定，發現這個專案**已經做過 `git init` 並且本機已經
有至少一次 commit**，而且已經設定好要推去哪裡：

```
https://github.com/benson103102/warehouse-system.git
```

所以你不用重頭設定，只差「登入驗證＋按下 push」這一步。這份教學全部在**你自己的
電腦**上操作（跟之前 Cloud Run 教學在瀏覽器 Cloud Shell 裡操作不一樣，這次是在
你電腦的一個叫「Git Bash」的黑色視窗裡打指令）。

---

## Part 0：確認你電腦有裝 Git

1. 按 Windows 鍵，打字搜尋「**Git Bash**」。
   - 如果搜得到、點開後跳出一個黑底視窗，代表你已經裝過 Git，直接跳到 Part 1。
   - 如果搜不到，代表還沒裝，往下看。
2. 瀏覽器打開 **https://git-scm.com/downloads/win**，下載「64-bit Git for
   Windows Setup」，下載完點兩下執行安裝。
3. 安裝過程一路「Next」用預設值就好，不用改任何設定，最後按「Install」→「Finish」。
4. 裝完再按 Windows 鍵搜尋「Git Bash」，這次應該找得到了，點開它。

**之後這份教學裡「打指令」都是指在這個 Git Bash 黑色視窗裡打。**

---

## Part 1：先去 GitHub 網站確認/建立這個 repository

因為你的專案已經設定好要推去
`github.com/benson103102/warehouse-system`，但**這個空間本身要先在 GitHub 網站
上存在**，才推得進去。

1. 瀏覽器打開 **https://github.com/benson103102/warehouse-system**（把
   `benson103102` 換成你自己的 GitHub 帳號，如果這就是你的帳號就直接開這個網址）。
2. 看畫面出現什麼：
   - **如果看到一個正常的 repository 頁面**（有檔案列表之類的）：代表已經建好了，
     直接跳到 Part 2。
   - **如果看到「404 - Page not found」**：代表這個 repository 還沒建立，往下看
     步驟 3。
3. 打開 **https://github.com/new**（如果還沒登入會先要你登入/註冊 GitHub 帳號）。
4. **Repository name** 填 `warehouse-system`（要跟本機設定的名字一模一樣，這樣才
   對得上）。
5. 選 **Private**（私人，別人看不到）或 **Public**（公開）都可以，看你需求，
   內部系統建議選 **Private**。
6. **⚠️ 最重要的一步**：下面「Initialize this repository with」那幾個選項
   （README、.gitignore、license）**全部不要勾**，保持空白狀態就好。如果你手滑
   勾了，會導致等一下 push 失敗（因為 GitHub 上會有你本機沒有的檔案，衝突）。
7. 按綠色的「**Create repository**」按鈕。
8. 建好後會看到一個「Quick setup」的頁面，上面顯示的網址（例如
   `https://github.com/benson103102/warehouse-system.git`）跟你本機設定的要一樣
   ——如果不一樣，把畫面上這個正確網址記下來，等一下 Part 4 會用到。

---

## Part 2：設定 Git 的身份資訊（只需要做一次）

在 Git Bash 打（把名字跟 email 換成你自己的，email 建議跟你 GitHub 帳號註冊時用
的同一個）：

```bash
git config --global user.name "你的名字"
git config --global user.email "你的email@example.com"
```

---

## Part 3：打開專案資料夾

在 Git Bash 裡打 `cd` 加上你資料夾的路徑，把它切換到專案所在的位置：

```bash
cd /c/Users/user/Downloads/warehouse-system
```

> 小提醒：Git Bash 裡的路徑寫法跟 Windows 平常看到的不一樣——`C:\Users\user\...`
> 要寫成 `/c/Users/user/...`（開頭的 `C:` 換成 `/c`，反斜線 `\` 換成正斜線 `/`）。

打完按 Enter，可以打 `ls` 確認一下，應該要看到 `Dockerfile`、`backend`、
`frontend`、`README.md` 這些東西，代表位置對了。

---

## Part 4：正式推送（push）

### 4-1　確認遠端網址設定正確

```bash
git remote -v
```

會印出目前設定的網址，應該要看到 `origin` 這行對應到
`https://github.com/benson103102/warehouse-system.git`（或 Part 1 步驟 8 你記下
的那個正確網址）。**如果網址不對**，用這行改成正確的（把網址換成你的）：

```bash
git remote set-url origin https://github.com/benson103102/warehouse-system.git
```

### 4-2　把目前的檔案變化加入這次要送出的內容

```bash
git add .
```

（這行是「把資料夾裡所有新增/修改過的檔案都標記成準備要送出」，`.` 代表「目前
資料夾底下全部」）

### 4-3　建立一筆提交紀錄（commit）

```bash
git commit -m "初次推送到 GitHub"
```

`-m` 後面雙引號裡的文字是這次修改的說明，可以自己改，例如
`"新增儲位配置精細版與批次地理分群功能"`。

> 如果這行印出 `nothing to commit, working tree clean`，代表你本機其實已經有
> commit 過、沒有新的變化要送出，直接跳到 4-4 即可。

### 4-4　推送到 GitHub

```bash
git push -u origin main
```

**這一步第一次執行時會跳出驗證畫面**，依你電腦的情況會是下面兩種之一：

- **彈出一個瀏覽器視窗**，要你登入 GitHub 帳號、按「Authorize」授權——這是最常見
  的情況（Git for Windows 內建的 Git Credential Manager 會自動處理），照著登入
  跟按確認就好，登入完成後回到 Git Bash 應該就會繼續跑完。
- **在 Git Bash 裡直接問你帳號密碼**：Username 填你的 GitHub 帳號名稱；
  **Password 這欄不能填你平常登入網站的密碼**，GitHub 現在只接受填「Personal
  Access Token」，取得方式見下面「Part 5：如果被要求輸入 Token」。

跑完沒有紅字錯誤、看到類似
`branch 'main' set up to track 'origin/main'` 這樣的訊息，就代表推送成功了。
回到瀏覽器重新整理 `https://github.com/benson103102/warehouse-system`，應該就
能看到你的檔案都上去了。

---

## Part 5：如果被要求輸入 Token（Personal Access Token）

如果 Part 4-4 卡在要你輸入密碼、且不是跳瀏覽器視窗那種，代表你需要一組
Token 來代替密碼：

1. 瀏覽器打開 **https://github.com/settings/tokens/new**（要先登入 GitHub）。
2. **Note** 隨便填一個名稱，例如 `warehouse-system-push`。
3. **Expiration**（有效期限）選你方便的，例如 90 天，或選 No expiration
   （永久有效，較方便但安全性較低，自行取捨）。
4. **Select scopes** 勾選最上面的 **repo**（整組會自動全勾，這樣就有推送權限）。
5. 拉到最下面按「**Generate token**」。
6. 畫面會出現一長串綠色的字（例如 `ghp_xxxxxxxxxxxxxxxxxxxx`）——
   **這是唯一一次會顯示，離開頁面就再也看不到了，先複製起來存好**（例如貼到記
   事本，但不要傳給別人、不要上傳到任何地方）。
7. 回到 Git Bash，剛剛卡住要你輸密碼的地方，**貼上這串 token 當密碼**（帳號欄
   還是填你的 GitHub 帳號名稱），按 Enter。

之後 Git 通常會把這組驗證資訊記住（存在 Windows 認證管理員），下次 push 大多不會
再問。

---

## 常見錯誤

**`remote: Repository not found.`**
代表 Part 1 的 repository 還沒建好，或網址打錯／帳號名稱不對，回頭檢查
`git remote -v` 印出的網址是否跟 GitHub 網站上這個 repo 的網址一致。

**`Support for password authentication was removed`**
就是 Part 5 說的狀況，密碼欄要填 Personal Access Token，不能填登入密碼。

**`Updates were rejected because the remote contains work that you do not have locally`**
代表 GitHub 上已經有內容（例如 Part 1 不小心勾了自動產生 README），而你本機沒
同步過去。最簡單的解法（**會覆蓋掉 GitHub 上現有的內容，換成你本機的版本**，
如果你確定本機版本才是對的就可以用）：

```bash
git push -u origin main --force
```

**之後每次改完程式碼，想再推一次更新，只要重複 Part 4-2～4-4 這三行就好**
（`git add .` → `git commit -m "說明這次改了什麼"` → `git push`），不用重新設定
Part 1～3。這也是之後要更新 Cloud Run 上的系統時，Cloud Shell 那邊
`git pull`／重新 `git clone` 拿到最新程式碼的前提。
