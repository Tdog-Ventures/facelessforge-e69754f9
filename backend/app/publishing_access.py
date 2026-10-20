"""Authorization for the operator-owned publishing credentials and scheduler."""
from fastapi import HTTPException


def ensure_operator(user: dict) -> None:
    if user.get('role') != 'admin':
        raise HTTPException(status_code=403, detail='Operator access required for shared publishing')


async def ensure_operator_project(project: dict) -> None:
    from .db import get_db
    owner = await get_db().users.find_one({'id': project.get('user_id')}, {'_id': 0, 'role': 1})
    ensure_operator(owner or {})
