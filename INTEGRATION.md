# 共識 Accord：主網站串接規格

主網站技術尚未確認時，可先依此中立 API 合約開發。這份實作在本機 Python API 中完成；若之後移植 Worker／D1，保留相同 API 合約即可。尚未連接任何實際主網站。

## 身分與資料來源

主網站是員工 ID、工號、姓名、部門、啟用狀態與管理員權限的唯一來源。投票系統保存同步快取，以供資格驗證、操作及歷史查詢；不設第二套帳密，也不接受瀏覽器傳來的姓名／部門／admin 作為權限依據。

員工以主網站不變的 `id` 識別。工號可以改，`id` 不可隨姓名、部門或工號改變。活動發布時凍結投票及各選項名單；日後調動部門不回溯改變已發布活動資格。員工停用後不得繼續使用既有工作階段。同步權限降級會立即套用至既有工作階段。

目前僅支援單一主網站。正式模式應使用獨立空白資料庫，避免混入示範活動。

## 啟動模式

`demo`：保留目前示範入口，不啟用主網站交換與同步 API。

`host`：拒絕 `X-Demo-User`，前端移除角色切換；所有操作都驗證投票系統簽發的工作階段。

伺服器環境變數（見 `.env.example`，程式不會自動讀取 .env；由部署平台或啟動環境注入）：

| 變數 | 用途 |
| --- | --- |
| `ACCORD_AUTH_MODE` | `demo` 或 `host` |
| `ACCORD_HOST_ORIGIN` | 主網站確切來源，如 `https://staff.example.com`，不含路徑 |
| `ACCORD_SSO_ISSUER` | 主網站固定簽發者，如 `company-main` |
| `ACCORD_SSO_AUDIENCE` | 預設 `accord` |
| `ACCORD_SSO_SECRET` | 主網站與投票 API 共用的登入簽章密鑰，至少 32 字元 |
| `ACCORD_DIRECTORY_TOKEN` | 名冊伺服器 API 專用密鑰，至少 32 字元，與登入密鑰不同 |
| `ACCORD_DIRECTORY_URL` | 選填，主網站名冊 GET API；設定後在登入時拉取最新名冊 |
| `VOTE_DB` | 獨立正式資料庫路徑 |

兩個密鑰都只存在伺服器，不可放入網頁 JavaScript、Git、網址或傳給員工。主網站來源需 HTTPS；本機 localhost／127.0.0.1 開發例外。不要直接以目前無登入保護的示範模式收集正式員工資料。

## 一、同步名冊

### 主網站主動推送

`POST /api/integration/roster`

伺服器送出 `Authorization: Bearer <ACCORD_DIRECTORY_TOKEN>` 與 `Content-Type: application/json`：

```json
{
  "revision": 1,
  "employees": [
    {"id":"employee-immutable-id","employeeNumber":"E001","name":"範例員工","department":"業務部","admin":false,"active":true},
    {"id":"admin-immutable-id","employeeNumber":"A001","name":"範例主管","department":"人資部","admin":true,"active":true}
  ]
}
```

這是完整名冊快照：缺席員工會停用、保留歷史，而非刪除。`admin`／`active` 必須為布林值。`id` 與 `employeeNumber` 不可重複。部門來自各員工的 `department` 欄位，不另外使用示範部門清單。`revision` 每次名冊更新遞增；舊版本或同版本不同內容回 409。相同版本及內容重試不會重複寫入。空陣列表示全員停用，主網站需審慎產生完整快照。

### 投票系統讀取主網站名冊

設定 `ACCORD_DIRECTORY_URL` 後，登入交換會先驗證票券簽章，再以伺服器 GET 拉取名冊。主網站 GET API 接收相同名冊專用 Bearer 密鑰，回應上述 JSON。也可由主網站伺服器呼叫 `POST /api/integration/sync`（body `{}`，相同 Bearer 密鑰）立即觸發拉取。未設定 URL 時採推送方式。

名冊 API 不允許重新導向。取得失敗回 503，不假裝名冊已更新。停用或權限異動若要立即生效，主網站應推送最新快照或觸發 sync；僅依登入時拉取，不會自動得知兩次同步之間的異動。

## 二、由主網站登入

主網站驗證自己的員工登入後，在伺服器產生短效 HS256 JWT，header 固定：

```json
{"alg":"HS256","typ":"JWT"}
```

payload 範例：

```json
{"iss":"company-main","aud":"accord","sub":"employee-immutable-id","iat":1805600000,"exp":1805600120,"jti":"每次唯一且至少16字元的隨機值"}
```

上列時間是格式示例，實際必須使用當下 Unix 秒數。有效期最長 300 秒，建議 120 秒；`jti` 只能交換一次。票券只帶不變 ID，不需放姓名、部門或管理員權限，這些以已驗證名冊為準。

`POST /api/auth/exchange`，JSON `{"token":"簽章JWT"}`。成功回傳 `sessionToken`、`expiresAt` 與 `user`。工作階段有效 1 小時，之後由主網站產生新票券重新交換。每次操作以 `Authorization: Bearer <sessionToken>` 驗證，拒絕過期、已登出或停用帳號。資料庫只保存 sessionToken 的 SHA-256 摘要。

`POST /api/auth/logout`，body `{}`，帶上述工作階段 Bearer，即撤銷該工作階段。主網站登出時也應呼叫此端點（或在嵌入頁中執行登出），不能只關閉 iframe。

## 三、嵌入主網站畫面

建議 iframe + `postMessage`：主網站傳入短效票券，投票頁自行交換登入，不必依賴跨站 Cookie。投票頁把工作階段存在該分頁的 sessionStorage，不放網址。

主網站瀏覽器範例（`/internal/accord-ticket` 是主網站需實作的伺服器路由，使用既有登入驗證，呼叫簽章範例產生票券；不可在瀏覽器持有簽章密鑰）：

```html
<iframe id="accord" src="https://vote.example.com/" title="共識 Accord" style="width:100%;height:90vh;border:0"></iframe>
<script>
const voteOrigin = 'https://vote.example.com';
const frame = document.getElementById('accord');
window.addEventListener('message', async (event) => {
  if (event.origin !== voteOrigin || event.source !== frame.contentWindow) return;
  if (event.data?.type !== 'accord:ready') return;
  const response = await fetch('/internal/accord-ticket', {method:'POST',credentials:'same-origin'});
  if (!response.ok) return;
  const {token} = await response.json();
  frame.contentWindow.postMessage({type:'accord:login',token}, voteOrigin);
});
</script>
```

主網站的票券端點應沿用其既有 CSRF 防護。前端僅接受 `ACCORD_HOST_ORIGIN` 且來自父視窗／開啟視窗的訊息，不接受 `*`。完成會回傳 `accord:authenticated`。直接在新分頁開啟可由主網站 popup opener 使用相同訊息協定；尚未提供獨立 OAuth redirect 流程。

## API 與錯誤

| API | 呼叫者／用途 |
| --- | --- |
| `GET /api/integration/config` | 公開模式與主網站來源，不含密鑰 |
| `POST /api/integration/roster` | 主網站伺服器推送名冊 |
| `POST /api/integration/sync` | 主網站伺服器觸發名冊拉取 |
| `POST /api/auth/exchange` | 以短效票券交換工作階段 |
| `POST /api/auth/logout` | 撤銷目前工作階段 |
| `GET /api/state` | 已登入使用者資料與可見活動；一般員工不取得完整名冊 |
| 既有投票 API | 沿用同一 Bearer 工作階段及既有防重複／版本檢查 |

401：登入／服務驗證失敗；403：帳號停用或權限不足；409：名冊／草稿版本衝突；503：無法取得主網站名冊。

主網站與投票 API 的直接跨來源瀏覽器呼叫不開放 CORS；名冊走伺服器對伺服器，iframe 內 API 走同來源。

## 交接時只需補齊

主網站網址、取得目前登入員工 ID 的方法、完整員工名冊來源，以及主網站哪個權限對應 `admin=true`。不需要把主網站員工密碼傳給投票系統。
