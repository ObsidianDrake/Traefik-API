"""Export public API documentation without loading .env or starting the service."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from route_api.main import create_app
from route_api.settings import Settings


def export(destination):
    # Explicit settings bypass .env. No lifespan, file scan or HTTP request runs.
    app = create_app(Settings(config_dir=Path("unused-docs-config"),
                              token="documentation-placeholder-not-a-real-token"))
    schema = app.openapi()
    schema["info"]["description"] = (
        "以名稱管理 Traefik HTTP 路由，支援 YAML 與 TOML。\n\n"
        "此頁為靜態參考文件；請至本機服務的 `/docs` 測試 API。\n\n"
        "除 `/healthz` 外，所有端點皆需 `Authorization: Bearer <API_TOKEN>`。"
    )
    schema["servers"] = [{"url": "http://127.0.0.1:8000", "description": "本機 API（預設）"}]
    descriptions = {
        ("/healthz", "get"): ("檢查 API 存活", "只檢查本服務，不探測 Traefik 或後端服務。"),
        ("/v1/routes", "get"): ("列出 Traefik HTTP 路由", "即時讀取所有分頁，保留原始狀態並附 managed 與 managedName。包含人工路由；不是僅列出本專案建立的檔案。"),
        ("/v1/routes/{name}", "get"): ("取得路由設定", "讀取本專案管理的路由設定；找不到時回傳 404。"),
        ("/v1/routes/{name}", "put"): ("建立或接管路由", "新建回傳 201；既有路由的所有 upstream 重試後仍無回應才允許更新（200）。任何 HTTP 回應均拒絕接管（409）；DNS、TLS 等無法判定的錯誤回傳 503。格式由 TRAEFIK_CONFIG_FORMAT 決定，新檔名為 {name}.yml 或 {name}.toml。"),
        ("/v1/routes/{name}", "delete"): ("刪除路由", "刪除本專案管理的路由檔案，成功回傳 204。此明確移除操作不探測 upstream。"),
        ("/v1/routes/preview", "post"): ("預覽路由設定", "驗證輸入與 allowlist，回傳 format 及 yaml 或 toml 內容；不写入檔案、不探測 upstream，也不判定目錄衝突。"),
    }
    for (path, method), (summary, description) in descriptions.items():
        operation = schema["paths"][path][method]
        operation.update(summary=summary, description=description,
                         tags=["Health" if path == "/healthz" else "Routes"])
        if path != "/healthz":
            operation["responses"]["401"] = {"description": "API token 缺少或不正確"}
        if "{name}" in path and method in ("get", "delete"):
            operation["responses"]["404"] = {"description": "找不到受管理的路由"}
        if method == "put":
            operation["responses"].update({
                "201": {"description": "新路由設定已寫入"},
                "409": {"description": "路由或檔案衝突，或原服務仍有回應"},
                "503": {"description": "無法判定原服務狀態，或設定檔不可用"},
            })
    schema["components"]["schemas"]["Route"]["examples"] = [{
        "pathPrefix": "/my-app", "stripPrefix": True,
        "upstreams": ["http://127.0.0.1:3000"], "probePath": "/",
    }]
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("docs/openapi.json"))
    export(parser.parse_args().output)
