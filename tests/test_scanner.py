import io
import tarfile

from app.config import Rulebook
from app.scanner import FileRecord, collect_archive, decode_file, scan_snapshot


RULEBOOK = Rulebook()
REPOSITORY = {"owner": "example", "name": "demo", "full_name": "example/demo", "url": "https://github.com/example/demo"}
SHA = "a" * 40


def make_archive(files):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        root = tarfile.TarInfo("example-demo-" + SHA)
        root.type = tarfile.DIRTYPE
        archive.addfile(root)
        for path, content in files.items():
            data = content.encode("utf-8") if isinstance(content, str) else content
            member = tarfile.TarInfo("example-demo-" + SHA + "/" + path)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return buffer.getvalue()


def scan(files, scan_id="scan"):
    snapshot = collect_archive(io.BytesIO(make_archive(files)), RULEBOOK)
    return scan_snapshot(snapshot, RULEBOOK, REPOSITORY, SHA, scan_id)


def test_same_commit_is_deterministic():
    files = {
        "README.md": "Getting Started\nnpm start\n",
        "requirements.txt": "fastapi\n",
        "main.py": "print('ok')\n",
        "config.txt": "token=sk-abcdefghijklmnopqrstuvwxyz123456\npath=C:\\work\\app\n",
    }
    first = scan(files, "one")
    second = scan(files, "two")
    assert first.score == second.score
    assert first.dimensions == second.dimensions
    assert first.deductions == second.deductions
    assert [item.id for item in first.findings] == [item.id for item in second.findings]


def test_secret_rules_count_once_and_dimension_is_capped():
    files = {
        "README.md": "Getting Started\nnpm start\n",
        "requirements.txt": "fastapi\n",
        "main.py": "print('ok')\n",
        "config.txt": (
            "one=sk-abcdefghijklmnopqrstuvwxyz123456\n"
            "two=sk-zzzzzzzzzzzzzzzzzzzzzzzz999999\n"
            "aws=AKIAABCDEFGHIJKLMNOP\n"
            "auth=Bearer abcdefghijklmnopqrstuvwxyz123456\n"
            "-----BEGIN RSA PRIVATE KEY-----\n"
        ),
        ".env": "DEBUG=true\n",
    }
    report = scan(files)
    secret_findings = [item for item in report.findings if item.dimension == "secrets"]
    assert len([item for item in secret_findings if item.rule_id == "S001"]) == 2
    for rule_id in ("S001", "S002", "S003", "S004", "S005"):
        assert sum(item.applied_deduction for item in secret_findings if item.rule_id == rule_id) <= RULEBOOK.rules[
            next(index for index, rule in enumerate(RULEBOOK.rules) if rule["id"] == rule_id)
        ]["deduction"]
    assert report.dimensions["secrets"]["deduction"] <= 30
    assert report.dimensions["secrets"]["score"] >= 0
    assert report.score == 100 - sum(item["points"] for item in report.deductions)


def test_large_file_is_truncated_not_skipped():
    prefix = "token=sk-abcdefghijklmnopqrstuvwxyz123456\n" + "a" * (RULEBOOK.policy["max_file_bytes"] - 40)
    files = {
        "README.md": "Getting Started\nnpm start\n",
        "requirements.txt": "fastapi\n",
        "main.py": "print('ok')\n",
        "large.txt": prefix + "b" * 200,
    }
    report = scan(files)
    large = next(item for item in report.coverage["files"] if item["path"] == "large.txt")
    assert large["truncated"] is True
    assert large["scanned_bytes"] == RULEBOOK.policy["max_file_bytes"]
    assert any(item.rule_id == "S001" for item in report.findings)
    assert report.status == "complete"
    assert report.score_kind == "full"
    assert report.coverage["truncated_files"] == 1
    assert any("仅扫描前" in item for item in report.limitations)


def test_file_budget_keeps_first_sorted_paths():
    files = {"README.md": "Getting Started\nnpm start\n", "requirements.txt": "fastapi\n", "main.py": "ok\n"}
    for index in range(2005):
        files["f%04d.txt" % index] = "x"
    report = scan(files)
    assert report.coverage["total_files"] == 2008
    assert report.coverage["selected_files"] == 2000
    assert report.coverage["excluded_by_file_budget"] == 8
    assert report.status == "partial"
    assert "README.md" in {item["path"] for item in report.coverage["files"]}


def test_binary_extension_is_skipped():
    report = scan(
        {
            "README.md": "Getting Started\nnpm start\n",
            "requirements.txt": "fastapi\n",
            "main.py": "print('ok')\n",
            "logo.png": b"sk-abcdefghijklmnopqrstuvwxyz123456",
        }
    )
    assert report.coverage["binary_files"] == 1
    assert not any(item.dimension == "secrets" for item in report.findings)


def test_windows_only_entry_and_docs():
    report = scan(
        {
            "README.md": "Getting Started\nrun start.bat\n",
            "requirements.txt": "fastapi\n",
            "start.bat": "@echo off\n",
        }
    )
    assert report.dimensions["windows"]["deduction"] == 15
    assert any(item.rule_id == "W001" for item in report.findings)
    assert any(item.rule_id == "W002" for item in report.findings)
    assert any(item.rule_id == "W003" and item.applied_deduction == 0 for item in report.findings)


def test_windows_scripts_are_summarized_once():
    files = {
        "README.md": "Getting Started\nrun start.bat\n",
        "requirements.txt": "fastapi\n",
        "start.bat": "@echo off\n",
    }
    for index in range(9):
        files["tool%d.bat" % index] = "@echo off\n"
    report = scan(files)
    assert sum(item.rule_id == "W003" for item in report.findings) == 1
    summary = next(item for item in report.findings if item.rule_id == "W003")
    assert summary.applied_deduction == 0
    assert "tool0.bat" in summary.evidence


def test_only_leading_utf8_bom_is_removed():
    content = "\ufeffhello\nbody\ufefftail"
    record = FileRecord(path="bom.txt", size=len(content.encode("utf-8")), prefix=content.encode("utf-8"))
    decode_file(record, RULEBOOK)
    assert record.text == "hello\nbody\ufefftail"

    interior = FileRecord(path="interior.txt", size=5, prefix="a\ufeffb".encode("utf-8"))
    decode_file(interior, RULEBOOK)
    assert interior.text == "a\ufeffb"


def test_hardcoded_path_variants_are_detected():
    # P001 按文件合并上报：同文件多条命中合并成一条，命中数与行号写进消息
    report = scan(
        {
            "README.md": "Getting Started\nnpm start\n",
            "requirements.txt": "fastapi\n",
            "main.py": "print('ok')\n",
            "config.txt": "E:\\data\n/home/alice/x\n\\\\nas\\share\\f.txt\n",
        }
    )
    path_findings = [item for item in report.findings if item.rule_id == "P001"]
    assert len(path_findings) == 1
    merged = path_findings[0]
    assert merged.hit_count == 3
    assert "命中 3 处" in merged.message
    assert merged.evidence


def test_path_findings_are_collapsed_per_file():
    report = scan(
        {
            "README.md": "Getting Started\nnpm start\n",
            "requirements.txt": "fastapi\n",
            "main.py": "print('ok')\n",
            "a.txt": "C:\\one\nC:\\two\nC:\\three\n",
            "b.txt": "D:\\four\n",
        }
    )
    path_findings = [item for item in report.findings if item.rule_id == "P001"]
    assert sorted(item.location.path for item in path_findings) == ["a.txt", "b.txt"]
    assert sum(item.applied_deduction > 0 for item in path_findings) == 2


def test_test_fixtures_are_excluded_from_content_scan():
    report = scan(
        {
            "README.md": "# demo\n\nSet TOKEN=sk-READMEKEY0123456789abcdefghijk before running.\n",
            "requirements.txt": "fastapi\n",
            "main.py": "print('ok')\n",
            "tests/test_scanner.py": (
                "one=sk-abcdefghijklmnopqrstuvwxyz123456\n"
                "aws=AKIAABCDEFGHIJKLMNOP\n"
                "auth=Bearer abcdefghijklmnopqrstuvwxyz123456\n"
                "-----BEGIN RSA PRIVATE KEY-----\n"
                'path="C:/Users/alice/dev/app.py"\n'
            ),
            "web/app.test.js": "const k='sk-zzzzzzzzzzzzzzzzzzzzzz999999'\n",
        }
    )
    paths = {item.location.path for item in report.findings if item.location}
    assert "tests/test_scanner.py" not in paths
    assert "web/app.test.js" not in paths
    readme_secret = [item for item in report.findings if item.rule_id == "S001" and item.location.path == "README.md"]
    assert len(readme_secret) == 1
    assert readme_secret[0].applied_deduction == 15
    assert any("已跳过 2 个测试样本文件" in item for item in report.limitations)
    assert report.coverage["content_skipped_files"] == 2


def test_documentation_secrets_are_still_reported():
    report = scan(
        {
            "README.md": "Getting Started\nnpm start\n",
            "requirements.txt": "fastapi\n",
            "main.py": "print('ok')\n",
            "docs/guide.md": "token=sk-DOCKEY0123456789abcdefghijklmn\n",
        }
    )
    docs = [item for item in report.findings if item.rule_id == "S001" and item.location.path == "docs/guide.md"]
    assert len(docs) == 1
    assert docs[0].applied_deduction == 15
    assert report.limitations[0] != "已跳过"


def test_test_fixtures_do_not_satisfy_readiness_checks():
    report = scan(
        {
            "README.md": "Getting Started\nnpm start\n",
            "tests/fixtures/requirements.txt": "fastapi\n",
            "main.py": "print('ok')\n",
        }
    )
    assert report.checks["readme"]["evidence_paths"] == ["README.md"]
    assert "tests/fixtures/requirements.txt" not in report.checks["dependencies"]["evidence_paths"]
