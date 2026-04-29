import os
from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse
from datetime import datetime
from database import db
from auth import create_token, get_google_token, get_google_user_info, GOOGLE_CLIENT_ID, GOOGLE_REDIRECT_URI

router = APIRouter(prefix="/auth", tags=["auth"])

@router.get("/login")
async def login():
    """重導向到 Google 登入頁"""
    url = (
        "https://accounts.google.com/o/oauth2/auth"
        f"?client_id={GOOGLE_CLIENT_ID}"
        f"&redirect_uri={GOOGLE_REDIRECT_URI}"
        "&response_type=code"
        "&scope=openid%20email%20profile"
    )
    return RedirectResponse(url)

@router.get("/callback")
async def callback(code: str = None, error: str = None):
    """Google OAuth callback"""
    if error or not code:
        return HTMLResponse("<script>window.location='/?error=login_failed'</script>")

    try:
        token_data = await get_google_token(code)
        access_token = token_data.get("access_token")
        if not access_token:
            raise Exception("無法取得 access token")

        user_info = await get_google_user_info(access_token)
        email = user_info.get("email")
        name  = user_info.get("name", email)
        picture = user_info.get("picture", "")

        # 存入或更新 DB
        existing = await db.users.find_one({"email": email})
        if not existing:
            await db.users.insert_one({
                "email":      email,
                "name":       name,
                "picture":    picture,
                "rules": {
                    "profit_trailing_pct": 20,
                    "stoploss_pct":        7,
                    "stoploss_ma":         ["monthly", "quarterly"],
                },
                "created_at": datetime.utcnow().isoformat()
            })
        else:
            await db.users.update_one(
                {"email": email},
                {"$set": {"name": name, "picture": picture, "last_login": datetime.utcnow().isoformat()}}
            )

        user_id = email  # 用 email 當 user_id
        jwt_token = create_token(user_id, email, name)

        # 把 token 傳回前端
        import urllib.parse
        base_url = os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:8000/auth/callback").replace("/auth/callback", "")
        user_data = urllib.parse.quote(f'{{"email":"{email}","name":"{name}","picture":"{picture}"}}')
        return RedirectResponse(f'{base_url}/?token={jwt_token}&user={user_data}')

    except Exception as e:
        return HTMLResponse(f"<script>window.location='/?error={str(e)}'</script>")

@router.get("/me")
async def get_me(token: dict = None):
    """取得目前登入用戶資訊"""
    from fastapi import Depends
    from auth import verify_token
    return {"message": "請使用 Bearer token 呼叫此 API"}