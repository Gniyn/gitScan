import contextlib
import threading
import time
from dataclasses import dataclass
from urllib.parse import quote, urljoin, urlparse

import httpx

from .scanner import ScanError, collect_archive, scan_snapshot


class GitHubError(Exception):
    pass


@dataclass(frozen=True)
class Repository:
    owner: str
    name: str

    @property
    def full_name(self):
        return self.owner + "/" + self.name

    @property
    def url(self):
        return "https://github.com/" + self.full_name


def valid_segment(value):
    return bool(value) and value not in (".", "..") and all(
        char.isascii() and (char.isalnum() or char in "_.-") for char in value
    )


def parse_repository(value):
    if not isinstance(value, str) or len(value) > 512:
        raise GitHubError("请输入有效的公开 GitHub 仓库链接")
    parsed = urlparse(value.strip())
    if parsed.scheme != "https" or parsed.hostname not in ("github.com", "www.github.com"):
        raise GitHubError("只支持 https://github.com/{owner}/{repo} 链接")
    if parsed.username or parsed.password or parsed.port or parsed.query or parsed.fragment:
        raise GitHubError("仓库链接不能包含凭证、查询参数或片段")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2:
        raise GitHubError("请提供仓库根链接，而不是文件、分支或 Issue 链接")
    if parts[1].endswith(".git"):
        parts[1] = parts[1][:-4]
    if not all(valid_segment(part) for part in parts):
        raise GitHubError("仓库名称格式无效")
    return Repository(parts[0], parts[1])


class ResponseReader:
    def __init__(self, response):
        self.iterator = response.iter_raw()
        self.buffer = bytearray()
        self.eof = False

    def read(self, size=-1):
        if size < 0:
            while not self.eof:
                try:
                    self.buffer.extend(next(self.iterator))
                except StopIteration:
                    self.eof = True
            data = bytes(self.buffer)
            self.buffer.clear()
            return data
        while len(self.buffer) < size and not self.eof:
            try:
                self.buffer.extend(next(self.iterator))
            except StopIteration:
                self.eof = True
        data = bytes(self.buffer[:size])
        del self.buffer[:size]
        return data


class GitHubReader:
    def __init__(self, policy, token=None):
        self.policy = policy
        self.token = token
        self.base = policy["github_api_base"].rstrip("/")
        self.last_request = 0.0
        self.throttle_lock = threading.Lock()
        self.client = httpx.Client(
            timeout=httpx.Timeout(policy["http_timeout_seconds"]),
            follow_redirects=False,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "git-repo-health-check"},
        )

    def close(self):
        self.client.close()

    def _headers(self, authenticated=True):
        headers = {}
        if authenticated and self.token:
            headers["Authorization"] = "Bearer " + self.token
        return headers

    def _throttle(self):
        with self.throttle_lock:
            delay = 1 / self.policy["github_requests_per_second"] - (time.monotonic() - self.last_request)
            if delay > 0:
                time.sleep(delay)
            self.last_request = time.monotonic()

    def _retry_delay(self, response, attempt):
        retry_after = response.headers.get("retry-after")
        if retry_after and retry_after.isdigit():
            return int(retry_after)
        if response.headers.get("x-ratelimit-remaining") == "0":
            reset = response.headers.get("x-ratelimit-reset")
            if reset and reset.isdigit():
                return max(1, int(reset) - int(time.time()) + 1)
        return 2 ** attempt

    def _get_json(self, path):
        url = self.base + path
        for attempt in range(self.policy["github_retries"] + 1):
            self._throttle()
            try:
                response = self.client.get(url, headers=self._headers())
            except httpx.HTTPError as error:
                if attempt >= self.policy["github_retries"]:
                    raise GitHubError("GitHub 请求失败，请稍后重试") from error
                time.sleep(2 ** attempt)
                continue
            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError as error:
                    raise GitHubError("GitHub 返回了无法解析的数据") from error
            if response.status_code == 404:
                raise GitHubError("仓库不存在，或不是公开仓库")
            if response.status_code == 401:
                raise GitHubError("GitHub token 无效或已过期")
            if response.status_code in (403, 429):
                wait = self._retry_delay(response, attempt)
                if attempt < self.policy["github_retries"] and wait <= self.policy["max_rate_wait_seconds"]:
                    time.sleep(wait)
                    continue
                reset = response.headers.get("x-ratelimit-reset")
                remaining_minutes = max(1, (int(reset) - int(time.time())) // 60) if reset and reset.isdigit() else None
                hint = "（匿名配额每小时 60 次，约 %s 分钟后重置）" % remaining_minutes if remaining_minutes else ""
                raise GitHubError("GitHub 限流，请稍后重试或由管理员配置只读 token" + hint)
            if response.status_code >= 500 and attempt < self.policy["github_retries"]:
                time.sleep(2 ** attempt)
                continue
            raise GitHubError("GitHub 返回 HTTP " + str(response.status_code))
        raise GitHubError("GitHub 请求失败")

    @contextlib.contextmanager
    def _tarball(self, repository, sha):
        url = self.base + "/repos/" + quote(repository.owner, safe="") + "/" + quote(repository.name, safe="") + "/tarball/" + sha
        authenticated = True
        attempt = 0
        redirects = 0
        while redirects < 4:
            self._throttle()
            manager = self.client.stream("GET", url, headers=self._headers(authenticated))
            response = manager.__enter__()
            try:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise GitHubError("GitHub 归档重定向缺少目标")
                    target = urljoin(url, location)
                    parsed = urlparse(target)
                    if (
                        parsed.scheme != "https"
                        or parsed.hostname not in self.policy["download_hosts"]
                        or parsed.username
                        or parsed.password
                        or parsed.port
                    ):
                        raise GitHubError("拒绝跟随非 GitHub 归档地址")
                    url = target
                    authenticated = False
                    redirects += 1
                    continue
                if response.status_code in (403, 429):
                    wait = self._retry_delay(response, attempt)
                    if attempt < self.policy["github_retries"] and wait <= self.policy["max_rate_wait_seconds"]:
                        attempt += 1
                        time.sleep(wait)
                        continue
                    reset = response.headers.get("x-ratelimit-reset")
                    remaining_minutes = max(1, (int(reset) - int(time.time())) // 60) if reset and reset.isdigit() else None
                    hint = "（匿名配额每小时 60 次，约 %s 分钟后重置）" % remaining_minutes if remaining_minutes else ""
                    raise GitHubError("GitHub 限流，请稍后重试或由管理员配置只读 token" + hint)
                if response.status_code >= 500:
                    if attempt < self.policy["github_retries"]:
                        attempt += 1
                        time.sleep(2 ** attempt)
                        continue
                    raise GitHubError("GitHub 归档服务暂时不可用")
                if response.status_code != 200:
                    raise GitHubError("下载归档失败，HTTP " + str(response.status_code))
                yield response
                return
            finally:
                manager.__exit__(None, None, None)
        raise GitHubError("归档重定向次数过多")


def scan_repository(url, rulebook, scan_id, progress, token=None):
    repository = parse_repository(url)
    reader = GitHubReader(rulebook.policy, token)
    try:
        progress("查询公开仓库信息")
        metadata = reader._get_json(
            "/repos/" + quote(repository.owner, safe="") + "/" + quote(repository.name, safe="")
        )
        if metadata.get("private"):
            raise GitHubError("仅支持公开仓库")
        default_branch = metadata.get("default_branch")
        if not default_branch:
            raise GitHubError("仓库没有可扫描的默认分支")
        progress("固定默认分支的 commit")
        commit = reader._get_json(
            "/repos/" + quote(repository.owner, safe="") + "/" + quote(repository.name, safe="")
            + "/commits/" + quote(default_branch, safe="")
        )
        sha = commit.get("sha")
        if (
            not isinstance(sha, str)
            or len(sha) != 40
            or any(char not in "0123456789abcdef" for char in sha)
        ):
            raise GitHubError("GitHub 未返回有效 commit")
        progress("流式下载并解包 tarball")
        started = time.monotonic()

        def check_deadline():
            if time.monotonic() - started > rulebook.policy["scan_timeout_seconds"]:
                raise ScanError("扫描超时，未生成不完整评分")

        for attempt in range(rulebook.policy["github_retries"] + 1):
            try:
                with reader._tarball(repository, sha) as response:
                    snapshot = collect_archive(ResponseReader(response), rulebook, check_deadline)
                break
            except httpx.HTTPError as error:
                if attempt >= rulebook.policy["github_retries"]:
                    raise GitHubError("下载归档时网络中断，请稍后重试") from error
                time.sleep(2 ** attempt)
        progress("匹配规则并计算评分")
        report = scan_snapshot(
            snapshot, rulebook,
            {"owner": repository.owner, "name": repository.name, "full_name": repository.full_name, "url": repository.url},
            sha, scan_id,
        )
        progress("完成")
        return report
    finally:
        reader.close()
