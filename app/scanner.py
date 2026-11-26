import codecs
import gzip
import hashlib
import heapq
import io
import tarfile
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import quote

from .models import Finding, Location, Report
from .scoring import score_findings


class ScanError(Exception):
    pass


class LimitedReader(io.RawIOBase):
    def __init__(self, source, limit, label, check_deadline=None):
        self.source = source
        self.limit = limit
        self.label = label
        self.total = 0
        self.check_deadline = check_deadline

    def readable(self):
        return True

    def read(self, size=-1):
        if self.check_deadline:
            self.check_deadline()
        amount = min(size if size >= 0 else 65536, self.limit - self.total + 1)
        chunk = self.source.read(amount)
        self.total += len(chunk)
        if self.total > self.limit:
            raise ScanError(self.label + "超出保护上限，无法生成确定性评分")
        return chunk

    def readinto(self, buffer):
        chunk = self.read(len(buffer))
        buffer[:len(chunk)] = chunk
        return len(chunk)


@dataclass
class FileRecord:
    path: str
    size: int
    prefix: bytes = b""
    skip_reason: Optional[str] = None
    text: Optional[str] = None
    truncated: bool = False

    def __lt__(self, other):
        return self.path > other.path


@dataclass
class ArchiveSnapshot:
    files: list
    paths: list
    compressed_bytes: int
    expanded_bytes: int
    other_members: int


def archive_path(name):
    if not name or name.startswith("/") or "\\" in name or any(ord(char) < 32 for char in name):
        raise ScanError("归档含不安全路径")
    parts = name.rstrip("/").split("/")
    if any(part in ("", ".", "..") or ":" in part for part in parts):
        raise ScanError("归档含不安全路径")
    return parts[0], "/".join(parts[1:])


def collect_archive(source, rulebook, check_deadline: Optional[Callable] = None):
    policy = rulebook.policy
    compressed = LimitedReader(source, policy["max_archive_bytes"], "归档下载量", check_deadline)
    selected = []
    paths = set()
    root = None
    other_members = 0
    member_count = 0
    try:
        with gzip.GzipFile(fileobj=compressed, mode="rb") as decompressor:
            expanded = LimitedReader(decompressor, policy["max_expanded_bytes"], "归档解压量", check_deadline)
            with tarfile.open(fileobj=expanded, mode="r|", bufsize=10240) as archive:
                for member in archive:
                    if check_deadline:
                        check_deadline()
                    member_count += 1
                    if member_count > policy["max_archive_members"]:
                        raise ScanError("归档条目数超出保护上限")
                    current_root, path = archive_path(member.name)
                    if root is None:
                        root = current_root
                    if current_root != root:
                        raise ScanError("归档顶层目录不一致")
                    if not member.isfile():
                        other_members += 1
                        continue
                    if not path or path in paths:
                        raise ScanError("归档含重复文件或不完整路径")
                    paths.add(path)
                    if len(selected) >= policy["max_files"] and path >= selected[0].path:
                        continue
                    record = FileRecord(path=path, size=member.size)
                    if rulebook.matches(path, policy["ignore_globs"]):
                        record.skip_reason = "ignored"
                    elif rulebook.matches(path, policy["binary_globs"]):
                        record.skip_reason = "binary"
                    else:
                        record.truncated = member.size > policy["max_file_bytes"]
                        stream = archive.extractfile(member)
                        if stream is None:
                            raise ScanError("无法读取普通文件")
                        with stream:
                            record.prefix = stream.read(min(member.size, policy["max_file_bytes"]))
                        if len(record.prefix) != min(member.size, policy["max_file_bytes"]):
                            raise ScanError("归档文件内容不完整")
                    if len(selected) < policy["max_files"]:
                        heapq.heappush(selected, record)
                    else:
                        heapq.heapreplace(selected, record)
            while expanded.read(65536):
                pass
    except (tarfile.TarError, EOFError, OSError) as error:
        raise ScanError("归档损坏或下载不完整") from error
    if root is None:
        raise ScanError("空归档无法扫描")
    files = sorted(selected, key=lambda record: record.path)
    for record in files:
        if not record.skip_reason:
            decode_file(record, rulebook)
    return ArchiveSnapshot(files, sorted(paths), compressed.total, expanded.total, other_members)


def decode_file(record, rulebook):
    policy = rulebook.policy
    encoding = "utf-16" if record.prefix.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)) else "utf-8"
    if encoding not in policy["encodings"]:
        record.skip_reason = "unsupported_encoding"
        record.prefix = b""
        return
    try:
        decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
        text = decoder.decode(record.prefix, final=not record.truncated)
        if text.startswith("\ufeff"):
            text = text[1:]
    except UnicodeDecodeError:
        record.skip_reason = "binary" if b"\x00" in record.prefix else "unsupported_encoding"
        record.truncated = False
        record.prefix = b""
        return
    controls = sum(ord(char) < 32 and char not in "\t\r\n" for char in text)
    if "\x00" in text or controls / max(1, len(text)) > policy["binary_control_ratio"]:
        record.skip_reason = "binary"
        record.truncated = False
        record.prefix = b""
        return
    record.text = text
    record.prefix = b""


def content_scan_targets(snapshot, rulebook, rule=None):
    """Files eligible for a content scan, split into scanned and excluded."""
    rule_globs = (rule or {}).get("exclude_paths", [])
    global_globs = rulebook.policy.get("content_exclude_globs", [])
    targets = []
    excluded = []
    for record in snapshot.files:
        if record.text is None:
            continue
        if rulebook.matches(record.path, rule_globs) or rulebook.matches(record.path, global_globs):
            excluded.append(record.path)
        else:
            targets.append(record)
    return targets, excluded


def evaluate_checks(snapshot, rulebook):
    records = {record.path: record for record in snapshot.files}
    budget_excluded = len(snapshot.paths) > len(snapshot.files)
    scannable = {record.path for record in content_scan_targets(snapshot, rulebook)[0]}
    checks = {}
    for name, definition in rulebook.checks.items():
        inventory = [
            path for path, record in records.items()
            if record.skip_reason != "ignored"
            and path in scannable
            and rulebook.matches(path, definition.get("paths", []))
        ]
        evidence = list(inventory)
        unknown = False
        for index, selector in enumerate(definition.get("content", [])):
            candidates = [
                path for path, record in records.items()
                if record.skip_reason != "ignored"
                and path in scannable
                and rulebook.matches(path, selector["paths"])
            ]
            for path in candidates:
                record = records.get(path)
                if record is None or record.text is None or path not in scannable:
                    unknown = True
                    continue
                if rulebook.patterns[(name, index)].search(record.text):
                    evidence.append(path)
                elif record.truncated:
                    unknown = True
        state = "present" if evidence else "unknown" if unknown or (not inventory and budget_excluded) else "absent"
        checks[name] = {
            "label": definition["label"],
            "state": state,
            "evidence_paths": sorted(set(evidence)),
            "checked_patterns": definition.get("paths", []) + [
                pattern for selector in definition.get("content", []) for pattern in selector["paths"]
            ],
        }
    return checks


def make_finding(rule, rulebook, repository, commit_sha, path=None, text=None, start=None, end=None, evidence=""):
    location = None
    source_url = None
    if path is not None:
        location = Location(path=path)
        source_url = repository["url"] + "/blob/" + commit_sha + "/" + quote(path, safe="/")
        if text is not None and start is not None:
            location.line_start = text.count("\n", 0, start) + 1
            location.column_start = start - text.rfind("\n", 0, start)
            final_offset = max(start, end - 1)
            location.line_end = text.count("\n", 0, final_offset) + 1
            location.column_end = final_offset - text.rfind("\n", 0, final_offset)
            source_url += "#L" + str(location.line_start)
    identity = "\0".join([commit_sha, rulebook.digest, rule["id"], path or "", str(start), str(end)])
    return Finding(
        id=hashlib.sha256(identity.encode()).hexdigest()[:24],
        rule_id=rule["id"], rule_version=rulebook.data["version"], dimension=rule["dimension"],
        severity=rule["severity"], confidence=rule["confidence"], title=rule["title"],
        message=rule["message"], location=location, evidence=rulebook.redact(evidence)[:rulebook.policy["max_evidence_chars"]],
        proposed_deduction=rule["deduction"], recommendation=rule["recommendation"], source_url=source_url,
    )


def scan_snapshot(snapshot, rulebook, repository, commit_sha, scan_id):
    checks = evaluate_checks(snapshot, rulebook)
    findings = []
    overlaps = {}

    def append(finding):
        if len(findings) >= rulebook.policy["max_findings"]:
            raise ScanError("发现数量超出保护上限，未输出不完整评分")
        findings.append(finding)

    ordered_rules = sorted(rulebook.rules, key=lambda rule: (rule["priority"], rule["id"]))
    for rule in ordered_rules:
        if rule["kind"] == "condition":
            if all(checks[name]["state"] == state for name, state in rule["when"].items()):
                evidence_paths = sorted({path for name in rule["when"] for path in checks[name]["evidence_paths"]})
                evidence = "证据文件：" + ", ".join(evidence_paths) if evidence_paths else "检查候选：" + ", ".join(
                    pattern for name in rule["when"] for pattern in checks[name]["checked_patterns"]
                )
                append(make_finding(rule, rulebook, repository, commit_sha, path=evidence_paths[0] if evidence_paths else None, evidence=evidence))
        elif rule["kind"] == "path":
            matched_paths = [
                record.path for record in snapshot.files
                if record.skip_reason is None
                and rulebook.matches(record.path, rule["paths"])
                and not rulebook.matches(record.path, rule.get("exclude_paths", []))
            ]
            if rule.get("report_mode") == "summary":
                if matched_paths:
                    append(make_finding(
                        rule, rulebook, repository, commit_sha, path=matched_paths[0],
                        evidence="仓库文件清单：" + ", ".join(matched_paths),
                    ))
            else:
                for path in matched_paths:
                    append(make_finding(rule, rulebook, repository, commit_sha, path=path, evidence="仓库文件清单：" + path))
        else:
            targets, _ = content_scan_targets(snapshot, rulebook, rule)
            per_file = rule.get("report_mode") == "file_summary"
            merged = {}
            collapsed_lines = {}
            for record in targets:
                for match in rulebook.patterns[rule["id"]].finditer(record.text):
                    if rule.get("redact") and rulebook.placeholder.search(match.group()):
                        continue
                    group = rule.get("overlap_group")
                    if group:
                        spans = overlaps.setdefault((record.path, group), [])
                        if any(match.start() < previous_end and match.end() > previous_start for previous_start, previous_end in spans):
                            continue
                        spans.append(match.span())
                    if rule.get("redact"):
                        evidence = "[REDACTED]（敏感命中内容不展示）"
                    else:
                        line_start = record.text.rfind("\n", 0, match.start()) + 1
                        line_end = record.text.find("\n", match.end())
                        evidence = record.text[line_start:line_end if line_end >= 0 else len(record.text)]
                    finding = make_finding(rule, rulebook, repository, commit_sha, record.path, record.text, match.start(), match.end(), evidence)
                    if per_file:
                        # 同一文件同一规则只上报一条，命中条数与行号合并进消息，避免同一条问题刷屏
                        entry = merged.get(record.path)
                        if entry is None:
                            entry = merged[record.path] = finding
                            entry.hit_count = 0
                        entry.hit_count += 1
                        line_starts = collapsed_lines.setdefault(record.path, [])
                        if finding.location and finding.location.line_start:
                            line_starts.append(finding.location.line_start)
                    else:
                        append(finding)
            for entry in merged.values():
                lines = "、".join(str(line) for line in collapsed_lines.get(entry.location.path, []))
                entry.message = "%s（本文件命中 %s 处%s）" % (
                    entry.message, entry.hit_count, "，行 %s" % lines if lines else "")
                append(entry)
    score, dimensions, deductions = score_findings(findings, rulebook)
    excluded = len(snapshot.paths) - len(snapshot.files)
    truncated = [record for record in snapshot.files if record.truncated]
    unsupported = [record for record in snapshot.files if record.skip_reason == "unsupported_encoding"]
    limitations = []
    skipped = sorted({path for rule in ordered_rules if rule["kind"] == "content"
                      for path in content_scan_targets(snapshot, rulebook, rule)[1]})
    if skipped:
        limitations.append("已跳过 %s 个测试样本文件，其内容未参与密钥与路径检测（如 %s）。"
                           % (len(skipped), ", ".join(skipped[:3]) + (" 等" if len(skipped) > 3 else "")))
    if excluded:
        limitations.append("超过文件预算：按相对路径排序仅保留前 %s 个文件，%s 个文件未进入内容扫描。" % (rulebook.policy["max_files"], excluded))
    if truncated:
        limitations.append("%s 个文件仅扫描前 %s 字节，未扫描后缀不能判定为安全。" % (len(truncated), rulebook.policy["max_file_bytes"]))
    if unsupported:
        limitations.append("%s 个文件编码不支持，未扫描其内容。" % len(unsupported))
    limitations.append("仅检查当前 commit，不检查 Git 历史、不验证凭证有效性、不执行仓库内容。")
    partial = bool(excluded)
    coverage = {
        "total_files": len(snapshot.paths),
        "selected_files": len(snapshot.files),
        "scanned_files": sum(record.text is not None for record in snapshot.files),
        "content_skipped_files": len(skipped),
        "excluded_by_file_budget": excluded,
        "binary_files": sum(record.skip_reason == "binary" for record in snapshot.files),
        "ignored_files": sum(record.skip_reason == "ignored" for record in snapshot.files),
        "unsupported_encoding_files": len(unsupported),
        "truncated_files": len(truncated),
        "other_archive_members": snapshot.other_members,
        "compressed_bytes": snapshot.compressed_bytes,
        "expanded_bytes": snapshot.expanded_bytes,
        "scanned_bytes": sum(min(record.size, rulebook.policy["max_file_bytes"]) for record in snapshot.files if record.text is not None),
        "limits": {"max_files": rulebook.policy["max_files"], "max_file_bytes": rulebook.policy["max_file_bytes"]},
        "files": [
            {"path": record.path, "original_bytes": record.size,
             "scanned_bytes": min(record.size, rulebook.policy["max_file_bytes"]) if record.text is not None else 0,
             "truncated": record.truncated, "skip_reason": record.skip_reason}
            for record in snapshot.files
        ],
    }
    return Report(
        scan_id=scan_id, repository=repository, commit_sha=commit_sha,
        engine_version=rulebook.data["engine_version"], rules_digest=rulebook.digest,
        status="partial" if partial else "complete", score=score,
        score_kind="scanned_scope" if partial else "full", dimensions=dimensions,
        checks=checks, findings=findings, deductions=deductions, coverage=coverage, limitations=limitations,
    )
