"""同源的轻量知识库界面，仅公开浏览器需要的配置。"""

from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse


STATIC_DIR = Path(__file__).resolve().parents[1] / "static"
ASSETS = {"app.js", "auth.js", "styles.css"}
router = APIRouter(include_in_schema=False)


def _issuer_origin(issuer: str) -> str:
    parts = urlsplit(issuer)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.netloc
        or parts.username
        or parts.password
        or any(char in parts.netloc for char in "'\"; ")
    ):
        raise HTTPException(503, "登录服务地址配置无效。")
    return f"{parts.scheme}://{parts.netloc}"


@router.get("/")
def frontend_page(request: Request) -> FileResponse:
    origin = _issuer_origin(request.app.state.settings.oidc_issuer)
    return FileResponse(
        STATIC_DIR / "index.html",
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                f"connect-src 'self' {origin}; img-src 'self' data:; "
                "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
            ),
        },
    )


@router.get("/ui/config")
def frontend_config(request: Request) -> JSONResponse:
    settings = request.app.state.settings
    _issuer_origin(settings.oidc_issuer)
    return JSONResponse(
        {
            "issuer": settings.oidc_issuer,
            "client_id": settings.oidc_frontend_client_id,
            "max_upload_bytes": settings.max_upload_bytes,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/ui/assets/{asset_name}")
def frontend_asset(asset_name: str) -> FileResponse:
    if asset_name not in ASSETS:
        raise HTTPException(404, "资源不存在。")
    return FileResponse(
        STATIC_DIR / asset_name,
        headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"},
    )
