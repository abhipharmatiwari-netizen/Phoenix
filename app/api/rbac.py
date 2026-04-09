from __future__ import annotations

from typing import List

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

from app.api.auth_routes import authenticate_bearer_token
from app.api.models import Role, User


oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


def get_current_user(token: str = Depends(oauth2_scheme)) -> User:
    payload = authenticate_bearer_token(f"Bearer {token}")
    return User(
        id=str(payload.get("id") or ""),
        email=str(payload.get("email") or ""),
        name=str(payload.get("name") or ""),
        role=Role(str(payload.get("role") or Role.VIEWER.value)),
    )



def require_role(required_roles: List[Role]):
    def _require_role(current_user: User = Depends(get_current_user)) -> None:
        if current_user.role not in required_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to access this resource.",
            )

    return _require_role
