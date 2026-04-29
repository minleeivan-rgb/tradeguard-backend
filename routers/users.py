from fastapi import APIRouter, HTTPException
from bson import ObjectId
from datetime import datetime
from database import db
from models import UserRules, CustomRule

router = APIRouter(prefix="/users", tags=["users"])

def fix_id(doc):
    if doc and "_id" in doc:
        doc["_id"] = str(doc["_id"])
    return doc

@router.get("/{user_id}/rules")
async def get_rules(user_id: str):
    user = await db.users.find_one({"name": user_id})
    if not user:
        raise HTTPException(status_code=404, detail="找不到用戶")
    return user.get("rules", {})

@router.put("/{user_id}/rules")
async def update_rules(user_id: str, rules: UserRules):
    await db.users.update_one({"name": user_id}, {"$set": {"rules": rules.dict()}}, upsert=True)
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
    await db.custom_rules.update_one(
        {"_id": ObjectId(rule_id), "user_id": user_id},
        {"$set": {**rule.dict(), "updated_at": datetime.utcnow().isoformat()}}
    )
    return {"message": "規則更新成功"}

@router.delete("/{user_id}/custom-rules/{rule_id}")
async def delete_custom_rule(user_id: str, rule_id: str):
    await db.custom_rules.delete_one({"_id": ObjectId(rule_id), "user_id": user_id})
    return {"message": "規則刪除成功"}
