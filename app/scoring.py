from collections import defaultdict


def finding_order(finding, rules):
    location = finding.location
    return (
        rules[finding.rule_id]["priority"],
        location.path if location else "",
        location.line_start or 0 if location else 0,
        location.column_start or 0 if location else 0,
        finding.id,
    )


def score_findings(findings, rulebook):
    rules = {rule["id"]: rule for rule in rulebook.rules}
    findings.sort(key=lambda finding: finding_order(finding, rules))
    dimension_used = defaultdict(int)
    rule_used = defaultdict(int)
    representatives = {}
    deductions = []
    for finding in findings:
        rule = rules[finding.rule_id]
        proposed = rule["deduction"]
        finding.proposed_deduction = proposed
        if proposed == 0:
            finding.deduction_reason = "仅提示，不扣分"
            continue
        if rule.get("count_mode") == "once_per_rule" and finding.rule_id in representatives:
            finding.deduction_reason = "该规则已计分"
            finding.related_finding_id = representatives[finding.rule_id]
            continue
        representatives.setdefault(finding.rule_id, finding.id)
        rule_remaining = max(0, rule["max_deduction"] - rule_used[finding.rule_id])
        dimension_remaining = max(
            0, rulebook.dimensions[finding.dimension]["maximum"] - dimension_used[finding.dimension]
        )
        applied = max(0, min(proposed, rule_remaining, dimension_remaining))
        finding.applied_deduction = applied
        reasons = []
        if rule_remaining < proposed:
            reasons.append("规则扣分封顶")
        if dimension_remaining < min(proposed, rule_remaining):
            reasons.append("维度扣分封顶")
        finding.deduction_reason = "；".join(reasons) if reasons else "按规则扣分"
        rule_used[finding.rule_id] += applied
        dimension_used[finding.dimension] += applied
        if applied:
            deductions.append({"finding_id": finding.id, "rule_id": finding.rule_id, "points": applied})
    dimensions = {
        name: {
            "label": definition["label"],
            "maximum": definition["maximum"],
            "deduction": dimension_used[name],
            "score": max(0, definition["maximum"] - dimension_used[name]),
        }
        for name, definition in rulebook.dimensions.items()
    }
    return max(0, 100 - sum(dimension_used.values())), dimensions, deductions
