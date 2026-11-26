import asyncio
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .advice import ai_enabled, generate_advice
from .config import Rulebook
from .github import GitHubError, parse_repository, scan_repository
from .jobs import JobManager
from .models import Report, ScanRequest


ROOT = Path(__file__).resolve().parent.parent
rulebook = Rulebook()


def scan(url, scan_id, progress):
    return scan_repository(url, rulebook, scan_id, progress, os.environ.get("GITHUB_TOKEN"))


manager = JobManager(rulebook.policy, scan)


def run_advice(report):
    """线程池里跑协程：AI 调用是异步的，线程池需要同步入口。"""
    return asyncio.run(generate_advice(report))


app = FastAPI(title="Git 仓库接入体检", docs_url="/api/docs", redoc_url=None)
class NoCacheStatic(StaticFiles):
    """静态资源开发期不缓存：改了 index.html 刷新即可生效，避免评审看到旧版。"""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


app.mount("/static", NoCacheStatic(directory=ROOT / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html", headers={"Cache-Control": "no-cache, must-revalidate"})


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "github_token_configured": bool(os.environ.get("GITHUB_TOKEN")),
        "ai_enabled": ai_enabled(),
        "max_files": rulebook.policy["max_files"],
        "max_file_bytes": rulebook.policy["max_file_bytes"],
    }


@app.post("/api/scans", status_code=202)
def create_scan(request: ScanRequest):
    try:
        parse_repository(request.url)
    except GitHubError as error:
        raise HTTPException(status_code=422, detail=str(error))
    try:
        job = manager.create(request.url)
    except RuntimeError as error:
        raise HTTPException(status_code=429, detail=str(error))
    return {"id": job.id, "status": job.status, "reused": job.reused, "cached_commit": job.cached_commit}


@app.get("/api/scans/{job_id}")
def get_scan(job_id: str):
    job = manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="扫描任务不存在或已过期")
    return job


@app.post("/api/scans/{job_id}/advice", status_code=202)
def scan_advice(job_id: str):
    """提交 AI 建议任务后立即返回，前端轮询同一任务拿结果，避免长连接被中断。"""
    job = manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="扫描任务不存在或已过期")
    if job["status"] != "complete" or not job["report"]:
        raise HTTPException(status_code=409, detail="扫描尚未完成")
    if job["advice"]:
        return {"status": "ready"}
    if job["advice_error"]:
        manager.attach_advice(job_id, None, None)
    if not ai_enabled():
        raise HTTPException(status_code=503, detail="服务端未配置 AI_API_KEY")
    if manager.advice_in_flight(job_id):
        return {"status": "running"}
    report = Report.model_validate(job["report"])
    manager.attach_advice(job_id, None, None)
    manager.start_advice(job_id, report, run_advice)
    return {"status": "running"}
