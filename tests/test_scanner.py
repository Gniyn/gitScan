import io
import tarfile

from app.config import Rulebook
from app.scanner import collect_archive, scan_snapshot


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
    assert report.status == "partial"
    assert report.coverage["truncated_files"] == 1


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


def test_nested_dependency_manifest_counts_but_ignored_directory_does_not():
    nested = scan(
        {
            "README.md": "Getting Started\nnpm start\n",
            "app/requirements.txt": "fastapi\n",
            "main.py": "print('ok')\n",
        }
    )
    assert nested.checks["dependencies"]["state"] == "present"
    ignored = scan(
        {
            "README.md": "Getting Started\nnpm start\n",
            "node_modules/demo/package.json": '{"scripts":{"start":"node app.js"}}',
            "main.py": "print('ok')\n",
        }
    )
    assert ignored.checks["dependencies"]["state"] == "absent"
    assert any(item.rule_id == "R002" for item in ignored.findings)
