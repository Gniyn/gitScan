import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field


@dataclass
class Job:
    id: str
    url: str
    status: str = "queued"
    progress: str = "等待扫描"
    report: object = None
    advice: object = None
    error: str = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def public(self):
        return {
            "id": self.id,
            "status": self.status,
            "progress": self.progress,
            "report": self.report.model_dump(mode="json") if self.report else None,
            "advice": self.advice,
            "error": self.error,
        }


class JobManager:
    def __init__(self, policy, scanner):
        self.policy = policy
        self.scanner = scanner
        self.lock = threading.Lock()
        self.jobs = {}
        self.executor = ThreadPoolExecutor(max_workers=policy["max_active_scans"], thread_name_prefix="repo-scan")
        self.active = 0

    def _cleanup(self):
        cutoff = time.time() - self.policy["cache_ttl_seconds"]
        for job_id in [
            key for key, job in self.jobs.items()
            if job.status in ("complete", "failed") and job.updated_at < cutoff
        ]:
            del self.jobs[job_id]
        while len(self.jobs) > self.policy["cache_capacity"]:
            finished = [key for key, job in self.jobs.items() if job.status in ("complete", "failed")]
            if not finished:
                break
            del self.jobs[min(finished, key=lambda key: self.jobs[key].updated_at)]

    def create(self, url):
        with self.lock:
            self._cleanup()
            if self.active >= self.policy["max_active_scans"] + self.policy["max_queued_scans"]:
                raise RuntimeError("扫描队列已满，请稍后重试")
            job = Job(id=uuid.uuid4().hex, url=url)
            self.jobs[job.id] = job
            self.active += 1
        self.executor.submit(self._run, job)
        return job

    def _run(self, job):
        try:
            with self.lock:
                job.status = "running"
                job.progress = "开始扫描"
                job.updated_at = time.time()

            def progress(message):
                with self.lock:
                    job.progress = message
                    job.updated_at = time.time()

            report = self.scanner(job.url, job.id, progress)
            with self.lock:
                job.report = report
                job.status = "complete"
                job.progress = "完成"
                job.updated_at = time.time()
        except Exception as error:
            with self.lock:
                job.status = "failed"
                job.error = str(error)
                job.progress = "失败"
                job.updated_at = time.time()
        finally:
            with self.lock:
                self.active = max(0, self.active - 1)

    def get(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            return job.public() if job else None

    def attach_advice(self, job_id, advice):
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                return False
            job.advice = advice
            job.updated_at = time.time()
            return True
