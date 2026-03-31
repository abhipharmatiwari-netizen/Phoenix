from __future__ import annotations

from typing import List

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

from app.api.auth_routes import _parse_token
from app.api.models import Role, User

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/bff/auth/login")

def get_current_user(token: str = Depends(oauth2_scheme)) -> User:
    payload = _parse_token(token)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # In a real app, you would fetch the user from the database
    # For the demo, we just construct a User model from the token payload
    return User(
        id=payload.get("sub"),
        email=payload.get("email"),
        name="", # Name is not in the token
        role=payload.get("role"),
    )

def require_role(required_roles: List[Role]):
    def _require_role(current_user: User = Depends(get_current_user)) -> None:
        if current_user.role not in required_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to access this resource.",
            )
    return _require_role
