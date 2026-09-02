from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from database.database import SessionLocal
from models.log import EventLog
from typing import Optional

router = APIRouter()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@router.get("/logs")
def get_logs(limit: int = Query(50, le=1000), device_id: Optional[int] = None, db: Session = Depends(get_db)):
    query = db.query(EventLog)
    if device_id is not None:
        query = query.filter(EventLog.device_id == device_id)
    logs = query.order_by(EventLog.timestamp.desc()).limit(limit).all()
    
    return [
        {
            "id": log.id,
            "timestamp": log.timestamp.isoformat() + "Z",
            "device_id": log.device_id,
            "device_name": log.device_name,
            "event_type": log.event_type,
            "card_uid": log.card_uid,
            "details": log.details
        }
        for log in logs
    ]
