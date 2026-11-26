import fnmatch
import hashlib
import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent.parent


class Rulebook:
    def __init__(self, path=ROOT / "rules.yaml"):
        raw = Path(path).read_bytes()
        self.digest = hashlib.sha256(raw).hexdigest()
        self.data = yaml.safe_load(raw)
        self.policy = self.data["policy"]
        self.dimensions = self.data["dimensions"]
        self.checks = self.data["checks"]
        self.rules = self.data["rules"]
        self.patterns = {}
        self._validate()

    def _validate(self):
        if sum(item["maximum"] for item in self.dimensions.values()) != 100:
            raise ValueError("Dimension maximums must sum to 100")
        if not 1 <= self.policy["max_files"] <= 2000:
            raise ValueError("max_files must be between 1 and 2000")
        if not 1 <= self.policy["max_file_bytes"] <= 262144:
            raise ValueError("max_file_bytes must be between 1 and 262144")
        for name, value in self.policy.items():
            if isinstance(value, (int, float)) and value <= 0:
                raise ValueError("Policy limits must be positive: " + name)
        identifiers = set()
        for rule in self.rules:
            if rule["id"] in identifiers or rule["dimension"] not in self.dimensions:
                raise ValueError("Duplicate rule or unknown dimension")
            identifiers.add(rule["id"])
            if rule["kind"] not in ("condition", "path", "content"):
                raise ValueError("Unknown rule kind")
            if rule["deduction"] < 0 or rule["max_deduction"] < 0:
                raise ValueError("Deductions cannot be negative")
            if rule.get("count_mode", "per_finding") not in ("per_finding", "once_per_rule", "per_file"):
                raise ValueError("Unknown count mode")
            if rule.get("report_mode", "per_finding") not in ("per_finding", "summary", "file_summary"):
                raise ValueError("Unknown report mode")
            if rule.get("report_mode") == "summary" and rule["kind"] != "path":
                raise ValueError("Summary report mode is only valid for path rules")
            if rule.get("report_mode") == "file_summary" and rule["kind"] != "content":
                raise ValueError("File summary report mode is only valid for content rules")
            if rule["dimension"] == "secrets" and rule.get("count_mode") != "once_per_rule":
                raise ValueError("Secret rules must count once per rule")
            for check, state in rule.get("when", {}).items():
                if check not in self.checks or state not in ("present", "absent"):
                    raise ValueError("Invalid condition")
            if "pattern" in rule:
                self.patterns[rule["id"]] = re.compile(rule["pattern"])
        for check, definition in self.checks.items():
            for index, selector in enumerate(definition.get("content", [])):
                self.patterns[(check, index)] = re.compile(selector["pattern"])
        self.placeholder = re.compile(self.policy["placeholder_pattern"])

    @staticmethod
    def matches(path, patterns):
        return any(fnmatch.fnmatchcase(path.casefold(), pattern.casefold()) for pattern in patterns)

    @staticmethod
    def matches_any(path, patterns):
        """Case-sensitive glob match, used where a glob is tuned to a file name."""
        return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)

    def redact(self, text):
        result = text
        for rule in self.rules:
            if rule.get("redact"):
                result = self.patterns[rule["id"]].sub("[REDACTED]", result)
        return result
