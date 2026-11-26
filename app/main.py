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
app = FastAPI(title="Git 仓库接入体检", docs_url="/api/docs", redoc_url=None)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


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
    return {"id": job.id, "status": job.status}


@app.get("/api/scans/{job_id}")
def get_scan(job_id: str):
    job = manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="扫描任务不存在或已过期")
    return job


@app.post("/api/scans/{job_id}/advice")
async def scan_advice(job_id: str):
    job = manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="扫描任务不存在或已过期")
    if job["status"] != "complete" or not job["report"]:
        raise HTTPException(status_code=409, detail="扫描尚未完成")
    if job["advice"]:
        return job["advice"]
    if not ai_enabled():
        raise HTTPException(status_code=503, detail="服务端未配置 AI_API_KEY")
    report = Report.model_validate(job["report"])
    try:
        advice = await generate_advice(report)
    except RuntimeError as error:
        raise HTTPException(status_code=502, detail=str(error))
    manager.attach_advice(job_id, advice)
    return advice
