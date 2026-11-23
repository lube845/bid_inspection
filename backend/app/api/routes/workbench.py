from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models import User
from app.services import access_service, workbench_service

router = APIRouter(tags=["workbench"])


@router.get("/workbench")
def workbench_summary(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """全局工作台：统计卡片、待办清单、最近项目/任务、风险统计。

    只汇总当前用户可见的项目；系统管理员看到全部。
    """
    return workbench_service.summary(
        db, user=None if access_service.is_system_admin(user) else user
    )
