from enum import Enum
from pydantic import BaseModel, EmailStr

class Role(str, Enum):
    VIEWER = "viewer"
    OPERATOR = "operator"
    ADMIN = "admin"
    BREAK_GLASS_ADMIN = "break_glass_admin"

class User(BaseModel):
    id: str
    email: EmailStr
    name: str
    role: Role = Role.VIEWER
