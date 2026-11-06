import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import (
    analysis,
    auth,
    companies,
    events,
    files,
    projects,
    results,
    risks,
    system,
    workbench,
)
from app.core.config import settings
from app.db.init_db import init_db
from app.services.analysis_service import AnalysisValidationError
from app.services.company_service import CompanyError
from app.services.file_service import FileValidationError
from app.services.rule_service import RuleError
from app.services.risk_service import RiskValidationError
from app.services.system_service import SystemError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="投标文件智能比对与风险分析系统",
    description=(
        "V1 业务框架：项目/企业/原始文件/逻辑标书/解析/两两比对/证据溯源/人工审核/Excel 报告。\n\n"
        "算法与 OCR 均为可替换的框架实现，不依赖外部模型部署。"
    ),
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(CompanyError)
async def company_error_handler(request: Request, exc: CompanyError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(FileValidationError)
async def file_error_handler(request: Request, exc: FileValidationError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(AnalysisValidationError)
async def analysis_error_handler(request: Request, exc: AnalysisValidationError):
    return JSONResponse(
        status_code=400,
        content={
            "detail": str(exc),
            "errors": exc.errors,
            "warnings": exc.warnings,
        },
    )


@app.exception_handler(RuleError)
async def rule_error_handler(request: Request, exc: RuleError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(RiskValidationError)
async def risk_error_handler(request: Request, exc: RiskValidationError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(SystemError)
async def system_error_handler(request: Request, exc: SystemError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.get("/health", tags=["system"])
def health() -> dict:
    return {"status": "ok", "app": settings.app_name, "environment": settings.environment}


@app.get("/api/v1/health", tags=["system"])
def api_health() -> dict:
    return {"status": "ok", "algorithm_version": settings.algorithm_version}


app.include_router(auth.router, prefix=settings.api_prefix)
app.include_router(projects.router, prefix=settings.api_prefix)
app.include_router(companies.router, prefix=settings.api_prefix)
app.include_router(files.router, prefix=settings.api_prefix)
app.include_router(analysis.router, prefix=settings.api_prefix)
app.include_router(results.router, prefix=settings.api_prefix)
app.include_router(events.router, prefix=settings.api_prefix)
app.include_router(workbench.router, prefix=settings.api_prefix)
app.include_router(risks.router, prefix=settings.api_prefix)
app.include_router(system.router, prefix=settings.api_prefix)
