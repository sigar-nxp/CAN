from sqlalchemy import Column, Integer, String, DateTime
from database.database import Base
from datetime import datetime

class EventLog(Base):
    __tablename__ = "event_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, default=datetime.utcnow)
    device_id = Column(Integer, index=True)
    device_name = Column(String)
    event_type = Column(String)
    card_uid = Column(String, nullable=True)
    details = Column(String, nullable=True)
