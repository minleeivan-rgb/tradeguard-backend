from fastapi import APIRouter, HTTPException
from bson import ObjectId
from bson.errors import InvalidId
from datetime import datetime
from database import db
from models import UserRules, CustomRule

router = APIRouter(prefix="/users", tags=["users"])

def fix_id(doc):
    if doc and "_id" in doc:
        doc["_id"] = str(doc["_id"])
    return doc

# FIX: 原本用 {"name": user_id}，但 auth_router 儲存用戶時 key 是 "email"
#      全部改為 {"email": user_id}

@router.get("/{user_id}/rules")
async def get_rules(user_id: str):
    user = await db.users.find_one({"email": user_id})
    if not user:
        raise HTTPException(status_code=404, detail="找不到用戶")
    return user.get("rules", {})

@router.put("/{user_id}/rules")
async def update_rules(user_id: str, rules: UserRules):
    await db.users.update_one({"email": user_id}, {"$set": {"rules": rules.dict()}}, upsert=True)
    return {"message": "規則更新成功"}

@router.get("/{user_id}/custom-rules")
async def get_custom_rules(user_id: str):
    rules = []
    async for r in db.custom_rules.find({"user_id": user_id}).sort("created_at", 1):
        rules.append(fix_id(r))
    return rules

@router.post("/{user_id}/custom-rules")
async def add_custom_rule(user_id: str, rule: CustomRule):
    data = rule.dict()
    data["user_id"] = user_id
    data["created_at"] = datetime.utcnow().isoformat()
    result = await db.custom_rules.insert_one(data)
    return {"id": str(result.inserted_id), "message": "規則新增成功"}

@router.put("/{user_id}/custom-rules/{rule_id}")
async def update_custom_rule(user_id: str, rule_id: str, rule: CustomRule):
    # FIX: 加入 ObjectId 格式驗證，避免 bson.errors.InvalidId 回傳 500
    try:
        oid = ObjectId(rule_id)
    except InvalidId:
        raise HTTPException(status_code=400, detail="rule_id 格式不正確")
    await db.custom_rules.update_one(
        {"_id": oid, "user_id": user_id},
        {"$set": {**rule.dict(), "updated_at": datetime.utcnow().isoformat()}}
    )
    return {"message": "規則更新成功"}

@router.delete("/{user_id}/custom-rules/{rule_id}")
async def delete_custom_rule(user_id: str, rule_id: str):
    # FIX: 加入 ObjectId 格式驗證
    try:
        oid = ObjectId(rule_id)
    except InvalidId:
        raise HTTPException(status_code=400, detail="rule_id 格式不正確")
    await db.custom_rules.delete_one({"_id": oid, "user_id": user_id})
    return {"message": "規則刪除成功"}
