# Traefik Route API

以 Python / FastAPI 接收路由註冊，產生 Traefik File Provider 的 YAML 或 TOML。支援 Python 3.9+、Linux，可直接執行本機 binary。

服務只負責路由設定；Web Service、DNS、TLS 憑證由部署流程處理。API 回傳 `written` 代表檔案已落盤，不保證 Traefik 已載入或網站已可連線。

## 線上 Swagger 文件

GitHub Pages 網址：[Traefik Route API Swagger](https://obsidiandrake.github.io/Traefik-API/)。

此頁為靜態文件，提供端點、參數、JSON 範例與 OpenAPI 下載；不會連線到你的內網服務。實際呼叫 API 請使用執行中服務的 `http://127.0.0.1:8000/docs`，在 Authorize 輸入 API token。

首次發布需到 repository 的 **Settings → Pages → Build and deployment → Source** 選擇 **GitHub Actions**。之後 main 分支的程式、依賴或文件變更會觸發 `Publish Swagger documentation` workflow，重新產生規格並發布；也可在 Actions 手動執行。若首次 workflow 因尚未啟用 Pages 失敗，啟用後重新執行該 workflow 即可。

本機重新產生規格：

```bash
venv/bin/python scripts/export_openapi.py
venv/bin/python -m http.server 9000 --directory docs
```

開啟 `http://127.0.0.1:9000/`。匯出流程使用獨立設定，不讀取 `.env`、不掃描正式路由目錄，也不連線到 Traefik。Swagger UI 靜態資源使用固定版本的 jsDelivr CDN，需要瀏覽器能連線到該 CDN。

## 本機啟動

Makefile 僅用來產生本機單一執行檔，沿用參考專案的 venv → 安裝依賴 → PyInstaller 流程：

```bash
make
# 產物：dist/traefik-route-api
```

`make` / `make executable` 建置 binary；`make clean` 清除 `build/`、`dist/`；若設定檔放在 `dist/`，清除前請先備份。可用 `make VENV=/path/to/venv` 指定虛擬環境。

打包方式使用 [PyInstaller onefile](https://www.pyinstaller.org/en/stable/usage.html)。執行檔包含 Python 與執行依賴，可於本機離開 venv 執行；不包含 `.env`、token 或 Traefik 設定。產物針對建置主機的 OS／架構，不是跨平台 binary。

先複製 `.env.example` 為執行目錄內的 `.env` 並編輯設定，再直接啟動（bash / tcsh 均適用）：

```bash
./dist/traefik-route-api --check-config
./dist/traefik-route-api --host 127.0.0.1 --port 8000
```

程式預設自動讀取**目前工作目錄**的 `.env`，不搜尋父目錄，也不以 binary 所在目錄判定。可用 `--env-file /absolute/path/.env` 指定另一份檔案。現有環境變數（含空字串）優先於檔案，其次才是程式預設值；若先前使用 tcsh `setenv`，那些值會覆蓋 `.env`。檔案支援 `KEY=value`、引號、空白與註解，不執行 shell 指令或展開 `${VAR}`。預設 `.env` 不存在時沿用環境變數；明確指定的檔案不存在則回報錯誤。`--check-config` 僅驗證環境參數，不檢查目錄權限、Traefik 或 upstream；`--help` 顯示參數。相對設定目錄以執行時的工作目錄為準，本機使用建議指定絕對路徑。

若直接從 Python 原始碼啟動，流程如下：

```bash
venv/bin/python -m pip install -r requirements-dev.txt
cp .env.example .env
```

編輯 `.env`，填入至少 24 字元的隨機 `API_TOKEN`，並設定允許的 upstream：

```bash
venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

在 `.env` 所在工作目錄直接啟動：

```bash
venv/bin/python -m uvicorn route_api.main:create_app --factory --host 127.0.0.1 --port 8000 --workers 1
```

- Swagger UI：`http://127.0.0.1:8000/docs`，按 Authorize 輸入 token。
- OpenAPI：`http://127.0.0.1:8000/openapi.json`。
- 健康檢查：`GET /healthz`。只檢查 API 存活，不檢查 upstream 或 Traefik。
- 預設写入 `./data/config`。確認正式 Traefik 設定後，將 `TRAEFIK_CONFIG_DIR` 改為 `/home/stan/Infra/traefik/config`。

## 註冊與接管規則

相同入口以服務端 entryPoint、正規化 host、正規化 pathPrefix 判定；host 大小寫與尾端句點忽略，路徑尾端 `/` 正規化。host-only 與相同 host 的根路徑視為同一入口。

1. 新入口且名稱未使用：建立，回傳 `201`；不要求新服務已啟動。
2. 相同入口，或更新已存在名稱：對**原設定**的每個 upstream 執行 GET，使用原設定的 `probePath`。
3. 任何 upstream 回覆 HTTP（含 3xx/4xx/5xx）：回傳 `409 existing_service_responding`，保留原檔案。
4. 所有 upstream 每次探測都連線拒絕或逾時：覆蓋，回傳 `200`。
5. DNS、TLS、協定等錯誤：回傳 `503 probe_inconclusive`，保留原檔案。
6. 探測期間發現設定目錄有變更：回傳 `409 config_changed`，由呼叫端重試。

完全相同的 request 也遵循此規則，並非傳統「相同 PUT 永遠成功」。檔案相同時重寫不會修復 upstream。

若用不同名稱註冊相同入口，接管成功後保留原名稱。回應的 `name` 是後續查詢／刪除使用的名稱，`requestedName` 是本次請求的名稱。若請求同時衝突到兩條不同路由，回傳 `409 multiple_route_conflicts`，不自動合併。

探測只讀 response headers，不讀完整 body，也不跟隨 redirect。預設每個 upstream 最多 3 次、每次總時間 2 秒、間隔 0.2 秒；多 upstream 並行探測。短暫網路故障仍可能觸發接管，API 與 Traefik 必須具備相同 upstream 可達性。

## API

除 `/healthz`、`/docs`、`/redoc`、`/openapi.json` 外，所有端點要求 `Authorization: Bearer <token>`。

| Method | Path | 用途 |
| --- | --- | --- |
| PUT | `/v1/routes/{name}` | 建立／接管／更新 |
| GET | `/v1/routes` | 即時查詢 Traefik 的所有 HTTP 路由（所有 provider） |
| GET | `/v1/routes/{name}` | 查詢本專案管理的單一路由設定 |
| DELETE | `/v1/routes/{name}` | 明確刪除，不探測 upstream；成功 `204` |
| POST | `/v1/routes/preview` | 驗證輸入及 allowlist、預覽 YAML／TOML；不寫入、不探測、不判定目錄衝突 |

`DELETE` 是授權使用者的明確移除操作；不存在的名稱回傳 `404`。它不受「服務有回應不得接管」規則限制。

### 格式與名稱

在 `.env` 設定 `TRAEFIK_CONFIG_FORMAT=toml` 並重啟後，`PUT /v1/routes/my-app` 會產生 `my-app.toml`；設定 `yaml`（預設）則產生 `my-app.yml`。Request body 不需指定格式或名稱，名稱由 URL 決定。

已有路由更新時保留原本的副檔名與格式，避免留下兩份衝突設定。目錄內已有同名人工設定（包括另一種格式）時會拒絕建立，不會覆蓋。

`POST /v1/routes/preview` 回傳 `format` 與對應的 `yaml` 或 `toml` 內容欄位。GET 單筆與 PUT 回應使用 `name`，PUT 另含 `requestedName`；列表的管理識別欄位改為 `managedName`。這些欄位取代先前的 `id`、`requestedId`、`managedId`。

### 查詢 Traefik 執行狀態

`GET /v1/routes` 每次呼叫 `TRAEFIK_API_URL` 下的 `/http/routers`，依 `X-Next-Page` 讀完所有分頁；Traefik 回傳 `X-Next-Page: 1` 代表最後一頁，缺少或空白 header 也視為結束。來源是 Traefik 執行中的設定，不再以本地設定檔清單當作回覆。保留 Traefik 的所有欄位與狀態（包括 disabled/error），不將「已載入」誤當成後端健康；目前列出 HTTP routers，不包含 TCP／UDP。

```json
{
  "routes": [
    {
      "name": "gitea@file",
      "rule": "PathPrefix(`/gitea`)",
      "service": "gitea",
      "status": "enabled",
      "managed": false,
      "managedName": null
    },
    {
      "name": "my-app@file",
      "rule": "Host(`apps.example.internal`)",
      "service": "my-app",
      "status": "enabled",
      "managed": true,
      "managedName": "my-app"
    }
  ]
}
```

這是列表回應格式的變更：每筆改為 Traefik router 資料，加上 `managed` 與 `managedName`，不再回傳原本的 `name` / `entryPoint` / `route` 包裝。只有本地有有效管理標記且 router 名稱為對應的 `<name>@file` 才標記為 managed。對同一 provider/name 的標記代表本地管理權，不代表 Traefik 已同步最新檔案內容。

可用 `managedName` 呼叫 `/v1/routes/{name}` 查詢本地設定或進行管理；人工設定與其他 provider 的路由仍不可透過此 API 修改／刪除。PUT 成功後尚未被 Traefik 載入的路由，不會先出現在列表。

Traefik 無法連線、HTTP 非 200、JSON 或分頁異常回傳 `502`；超過整次列表查詢的時間上限回傳 `504`。不以空列表、本地檔案或不完整分頁代替成功結果；只有 Traefik 回覆空清單時才回傳空清單。一次查詢上限為 1,000 頁、解壓後 JSON 合計 10 MiB，超過會回報 `502`。

查詢仍要求本專案的 Bearer Token；此 token 不會轉送給 Traefik。Traefik API 目前需可直接存取；若其前方要求驗證，會回報 `502` 並附 `traefikStatus: 401/403`。

參考 [Traefik API 與分頁文件](https://doc.traefik.io/traefik/v3.2/operations/api/)。

```bash
curl -X PUT http://127.0.0.1:8000/v1/routes/my-app \
  -H "Authorization: Bearer $API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "host": "apps.example.internal",
    "pathPrefix": "/my-app",
    "stripPrefix": true,
    "upstreams": ["http://my-app:3000"],
    "probePath": "/healthz"
  }'
```

欄位規則：

- `name`：小寫英數字與 `-`，第一字元須為英數字，最長 63 字元。
- `host` / `pathPrefix` 至少一個，同時提供時以 AND 匹配；不接受任意 rule。
- `stripPrefix` 預設 false；啟用時需指定非根路徑。
- `upstreams`：1–16 個 HTTP/HTTPS origin，不接受帳密、路徑、query 或 fragment。
- `probePath`：預設 `/`，直接附加於 upstream origin，與公開路由前綴無關。
- Path 僅接受 `/`、英數字、`_~.-`，拒絕 `..`、重複 `/`、百分比編碼等歧義形式。

`/my-app` 使用 `Path('/my-app') OR PathPrefix('/my-app/')` 的語意，不會匹配 `/my-application`。應用本身仍須支援子路徑部署；StripPrefix 不會改寫網頁內容或 redirect。

## 設定

| 環境變數 | 預設 | 說明 |
| --- | --- | --- |
| `API_TOKEN` | 必填 | 至少 24 字元；更換後重啟 API |
| `TRAEFIK_CONFIG_DIR` | `./data/config` | API 寫入目錄 |
| `TRAEFIK_CONFIG_FORMAT` | `yaml` | 新建路由輸出格式：`yaml` → `{name}.yml`，`toml` → `{name}.toml` |
| `TRAEFIK_ENTRYPOINT` | `web` | 必須存在於 Traefik 靜態設定 |
| `TRAEFIK_API_URL` | `http://127.0.0.1:8083/api` | Traefik API base URL，含 `/api`，不包含 `/http/routers`；不是本專案的 `:8000` |
| `TRAEFIK_API_TIMEOUT_SECONDS` | `5` | 取得所有路由分頁的總時間上限，最多 60 秒 |
| `ALLOWED_UPSTREAM_HOSTS` | 空 | 逗號分隔的精確 DNS 名稱或 IP；`*` 允許所有主機名稱與 IP |
| `ALLOWED_UPSTREAM_CIDRS` | 空 | 逗號分隔 CIDR；只匹配 URL 中的 literal IP；`*` 允許所有 IPv4/IPv6 位址 |
| `PROBE_TIMEOUT_SECONDS` | `2` | 每次 GET 的總時間上限，最多 30 秒 |
| `PROBE_ATTEMPTS` | `3` | 最多 5 次 |
| `PROBE_INTERVAL_SECONDS` | `0.2` | 重試間隔，最多 5 秒 |
| `MAX_BODY_BYTES` | `16384` | 包含無 Content-Length 請求的 body 限制 |

兩份 allowlist 都空白時拒絕所有 upstream。若不限制目的地，可設定：

```dotenv
ALLOWED_UPSTREAM_HOSTS=*
ALLOWED_UPSTREAM_CIDRS=
```

只有單獨的 `*` 有特殊含義，不支援 `*.example.com` 等部分萬用字元。`ALLOWED_UPSTREAM_CIDRS=*` 僅放行直接填寫的 IP，不放行 DNS 名稱。即使放行所有目的地，API token 與 HTTP/HTTPS URL 格式驗證仍然有效。

使用明確的 Hostname allowlist 時，表示信任該名稱及 DNS 管理者，不把 DNS 解析結果當作 CIDR 授權依據；內網 DNS 名稱需明確加入。新舊 upstream 都必須符合目前 allowlist。移除舊 upstream 的授權後，接管探測會拒絕執行，需先修正部署設定。

HTTPX 停用環境 proxy (`trust_env=False`)，避免探測流量與 Traefik 實際路徑不同。私有 CA 需另行擴充明確的信任設定；不提供跳過 TLS 驗證選項。

目前本機 Traefik 的 dashboard router 將 `web` 入口（8083）的 `/api` 導向 `api@internal`，因此預設 URL 使用 `http://127.0.0.1:8083/api`。若走 Traefik insecure API 的預設 port，改成 `http://127.0.0.1:8080/api`；若 Traefik 位於另一台主機，請填入該主機可連線的 API 位址。

## 檔案、並行與既有設定

- 一條路由一個 `{name}.yml` 或 `{name}.toml`。router、service 使用相同 name，StripPrefix middleware 使用 `{name}-strip`；不加入 `api-` 前綴。第一行 comment 保存 v2 管理標記及 route metadata。舊版 v1 `api-*.yml` 不再作為本專案的受管理檔案。
- YAML／TOML 是持久化來源，每次操作重新讀取；沒有資料庫。
- metadata 與生成內容不一致時回報 `503`，不覆蓋人工修改。
- 原子寫入使用同目錄 `.tmp` 檔案、fsync、rename。暫存檔不會成為 Traefik 設定。
- 全部寫入使用應用內鎖；目錄 flock 拒絕第二個 instance。只支援單 worker／單 replica／本機 Linux filesystem，不以 NFS 作跨機協調。
- 人工管理的 YAML/TOML 不自動接管或刪除。可辨識的 literal Host/Path/PathPrefix 重疊及資源名稱衝突回傳 `409`。
- 複雜手寫規則（例如 HostRegexp、否定、混合 OR）無法完整判定語意；可能保守拒絕或未辨識重疊，需人工檢查。API 路由間只拒絕相同入口，較廣與較窄的入口依 Traefik 原生 priority 處理。
- 設定目錄應由可信管理員控制。探測後會重查內容，但不協調外部編輯器；不要同時手動改 API 管理的檔案。所有 symlink 均拒絕。
- 結構化 JSON 操作紀錄寫到應用 log，含原／新設定與探測結果，不含 token。請由主機的日誌管理機制保留 log；第一版未提供歷史版本還原。

## 本機部署

Traefik 與本專案皆直接在主機執行。確認 Traefik 的 `providers.file.directory` 指向 `TRAEFIK_CONFIG_DIR`，且 `watch: true`；執行本 API 的帳號需對該目錄有讀寫權限，Traefik 的執行帳號需有讀取權限。

例如在 `.env` 設定：

```dotenv
TRAEFIK_CONFIG_DIR=/home/stan/Infra/traefik/config
TRAEFIK_API_URL=http://127.0.0.1:8083/api
```

建置後直接啟動，bash 與 tcsh 均可使用：

```bash
./dist/traefik-route-api --env-file /absolute/path/.env --host 127.0.0.1 --port 8000
```

API 與 Traefik 都必須能連線到 upstream。若兩者在同一台主機，`localhost` 指向該主機；若位於不同主機，請使用雙方可達的服務 IP 或 hostname。

## 驗證

```bash
venv/bin/python -m pytest -q
venv/bin/python -m pip check
```

測試透過 ASGITransport 呼叫完整 API，使用暫存目錄與模擬 upstream，不需要網路 socket。

可在允許建立 socket 的 Linux 主機執行真正 Traefik v3 整合測試：

```bash
venv/bin/python scripts/smoke_traefik.py --traefik /home/stan/Infra/traefik/traefik
```

脚本啟動隔離的 Traefik 與兩個測試 HTTP server，使用隨機 loopback ports 和臨時目錄，驗證建立、StripPrefix、路徑邊界、拒絕在線接管、離線接管及刪除的自動載入，全程不重啟 Traefik。API 透過 in-process ASGI 呼叫；不使用正式 config 或既有 Traefik 程序。

此次開發環境禁止網路 socket，因此實際 Traefik reload 與主機間連線仍須在部署主機執行上述驗證。
