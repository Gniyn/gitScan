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
    advice_error: str = None
    error: str = None
    reused: bool = False
    cached_commit: str = None
    merged_into: str = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def public(self):
        return {
            "id": self.id,
            "status": self.status,
            "progress": self.progress,
            "report": self.report.model_dump(mode="json") if self.report else None,
            "advice": self.advice,
            "advice_error": self.advice_error,
            "error": self.error,
            "reused": self.reused,
            "cached_commit": self.cached_commit,
            "merged_into": self.merged_into,
        }


class JobManager:
    def __init__(self, policy, scanner):
        self.policy = policy
        self.scanner = scanner
        self.lock = threading.Lock()
        self.jobs = {}
        self.executor = ThreadPoolExecutor(max_workers=policy["max_active_scans"], thread_name_prefix="repo-scan")
        self.active = 0
        self.advice_tasks = {}
        self.advice_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ai-advice")

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
            cached = self._reuse(url)
            if cached is not None:
                return cached
            inflight = self._inflight(url)
            if inflight is not None:
                return inflight
            if self.active >= self.policy["max_active_scans"] + self.policy["max_queued_scans"]:
                raise RuntimeError("扫描队列已满，请稍后重试")
            job = Job(id=uuid.uuid4().hex, url=url)
            self.jobs[job.id] = job
            self.active += 1
        self.executor.submit(self._run, job)
        return job

    def _inflight(self, url):
        """同一仓库正在扫或排队时并入那个任务，避免重复消耗 GitHub 配额。"""
        for job in self.jobs.values():
            if job.url == url and job.status in ("queued", "running"):
                merged = Job(id=uuid.uuid4().hex, url=url)
                merged.status = job.status
                merged.progress = job.progress
                merged.report = job.report
                merged.reused = True
                merged.merged_into = job.id
                merged.cached_commit = "in-flight:" + job.id
                self.jobs[merged.id] = merged
                return merged
        return None

    def _reuse(self, url):
        """短时间内重复提交同一仓库时直接给上一次结果；跨过 refresh_ttl 就重扫（report 里固化 commit_sha）。"""
        ttl = self.policy["refresh_ttl_seconds"]
        now = time.time()
        for job in self.jobs.values():
            if job.url == url and job.status == "complete" and job.report is not None and now - job.updated_at <= ttl:
                reused = Job(id=uuid.uuid4().hex, url=url)
                reused.status = "complete"
                reused.progress = "直接复用上一次扫描结果"
                reused.report = job.report
                reused.reused = True
                reused.cached_commit = job.report.commit_sha
                self.jobs[reused.id] = reused
                return reused
        return None

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

    def advice_in_flight(self, job_id):
        with self.lock:
            return job_id in self.advice_tasks

    def start_advice(self, job_id, report, generator):
        """把 AI 建议放到线程池里跑，前端轮询结果；同步等待会被网关/浏览器中断。"""
        with self.lock:
            self.advice_tasks[job_id] = True

        def work():
            try:
                advice = generator(report)
                self.attach_advice(job_id, advice, None)
            except Exception as error:  # AI 侧失败不能让任务永久卡在"生成中"
                self.attach_advice(job_id, None, str(error)[:200])
            finally:
                with self.lock:
                    self.advice_tasks.pop(job_id, None)

        self.advice_executor.submit(work)

    def get(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                return None
            # 并入别的任务时直接读源任务的进度，避免两份状态各走各的
            source = self.jobs.get(job.merged_into) if job.merged_into else None
            if source is not None:
                snapshot = source.public()
                snapshot["id"] = job.id
                snapshot["reused"] = True
                snapshot["cached_commit"] = job.cached_commit
                snapshot["merged_into"] = job.merged_into
                return snapshot
            return job.public()

    def attach_advice(self, job_id, advice=None, error=None):
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                return False
            job.advice = advice
            job.advice_error = error
            job.updated_at = time.time()
            return True
