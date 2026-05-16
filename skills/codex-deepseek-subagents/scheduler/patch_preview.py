import hashlib


def summarize_patch(patch_text):
    text = str(patch_text or "")
    files = []
    additions = 0
    deletions = 0
    for line in text.splitlines():
        if line.startswith("+++ b/"):
            files.append(line[6:].strip())
        elif line.startswith("+") and not line.startswith("+++"):
            additions += 1
        elif line.startswith("-") and not line.startswith("---"):
            deletions += 1
    return {
        "files": files,
        "additions": additions,
        "deletions": deletions,
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest() if text else "",
    }


def patch_summary_lines(summary):
    lines = ["Patch Preview:"]
    files = summary.get("files") or []
    if files:
        lines.append("  Files changed:")
        for path in files:
            lines.append(f"    M {path}")
    else:
        lines.append("  Files changed: (none)")
    lines.append(f"  Additions: {int(summary.get('additions') or 0)}")
    lines.append(f"  Deletions: {int(summary.get('deletions') or 0)}")
    return lines
