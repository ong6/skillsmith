#!/usr/bin/env python3
"""Lint a skill folder against the Claude Code and Codex authoring rules."""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import re
import sys


NAME_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
LINK_PATTERN = re.compile(r"\]\(([^)\s]+)\)")
MAX_NAME = 64
MAX_DESCRIPTION = 1024
MAX_BODY_LINES = 500
# Claude may preview a long reference with `head -100`, so its scope must show in the first lines.
TOC_THRESHOLD = 100
TOC_WINDOW = 30
TOC_HEADING = re.compile(r"^#{1,6}\s*(?:table of )?contents\b", re.IGNORECASE | re.MULTILINE)
TOC_ENTRY = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+\[[^\]]+\]\(#", re.MULTILINE)
# Bundled files that document the folder rather than instruct the agent.
NON_REFERENCE_DOCS = {"README.md", "CHANGELOG.md", "CONTRIBUTING.md", "LICENSE.md", "SECURITY.md",
                      "UPSTREAM.md", "NOTICE.md"}
SKIPPED_DIRS = {"tests", "evals", "node_modules", "__pycache__", ".venv", "venv"}
INSTALL_HINT = re.compile(
    r"pip3? install|uv (?:run|pip|tool)|pipx|brew install|npm (?:i|install)|npx |"
    r"requirements\.txt|auto-?install|bootstraps?|venv", re.IGNORECASE)
INLINE_DEPENDENCIES = re.compile(r"^# /// script$", re.MULTILINE)
PROHIBITION = re.compile(r"\b(?:not|never|don't|avoid)\b", re.IGNORECASE)
# Modules the host application provides when it runs the script; pip cannot install them.
HOST_MODULES = {"bpy", "bmesh", "mathutils", "bpy_extras", "gpu", "gpu_extras", "blf", "aud",
                "idprop", "freestyle"}


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Parse the flat YAML subset skills use: scalars, quoted scalars and block scalars."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("SKILL.md must open with a --- frontmatter block")
    try:
        end = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration as error:
        raise ValueError("frontmatter block is not closed") from error
    fields: dict[str, str] = {}
    key = None
    block: list[str] | None = None
    folded = False
    for line in lines[1:end]:
        if block is not None and (line.startswith(" ") or not line.strip()):
            block.append(line.strip())
            continue
        if block is not None:
            fields[key] = (" " if folded else "\n").join(part for part in block if part)
            block = None
        match = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if not match:
            raise ValueError(f"unparseable frontmatter line: {line!r}")
        key, value = match.group(1), match.group(2).strip()
        if value[:1] in (">", "|"):
            block, folded = [], value.startswith(">")
        elif len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            fields[key] = value[1:-1]
        else:
            fields[key] = value
    if block is not None:
        fields[key] = (" " if folded else "\n").join(part for part in block if part)
    return fields, "\n".join(lines[end + 1:])


def bundled(skill_dir: Path, suffix: str) -> list[Path]:
    """Files with the suffix inside the skill, excluding tests, evals and environments."""
    root = skill_dir.resolve()
    found = []
    for path in sorted(root.rglob(f"*{suffix}")):
        relative = path.relative_to(root)
        if path.is_file() and not SKIPPED_DIRS.intersection(relative.parts[:-1]):
            found.append(path)
    return found


def mentions(text: str, source: Path, target: Path) -> bool:
    """Whether text in source points at target by markdown link or by a path in prose."""
    for link in LINK_PATTERN.findall(text):
        relative = link.split("#", 1)[0]
        if relative and not re.match(r"^[a-z]+:", relative):
            if (source.parent / relative).resolve() == target:
                return True
    try:
        relative = target.relative_to(source.parent).as_posix()
    except ValueError:
        return False
    return re.search(r"(?<![\w/.-])" + re.escape(relative) + r"(?![\w/-])", text) is not None


def has_contents(text: str) -> bool:
    head = "\n".join(text.splitlines()[:TOC_WINDOW])
    return bool(TOC_HEADING.search(head)) or len(TOC_ENTRY.findall(head)) >= 3


def reference_layout(skill_dir: Path, skill_text: str) -> list[str]:
    """Warn on long references without contents, and on references SKILL.md never reaches directly."""
    warnings = []
    root = skill_dir.resolve()
    skill_file = root / "SKILL.md"
    references = [path for path in bundled(skill_dir, ".md")
                  if path != skill_file and path.name not in NON_REFERENCE_DOCS]
    texts = {path: path.read_text(encoding="utf-8", errors="replace") for path in references}
    for path, text in texts.items():
        relative = path.relative_to(root).as_posix()
        lines = len(text.splitlines())
        if lines > TOC_THRESHOLD and not has_contents(text):
            warnings.append(
                f"{relative} is {lines} lines with no contents list in its first {TOC_WINDOW}; "
                "a partial read may miss later sections"
            )
        if mentions(skill_text, skill_file, path):
            continue
        parents = [other.relative_to(root).as_posix() for other, other_text in texts.items()
                   if other != path and mentions(other_text, other, path)]
        if parents:
            warnings.append(
                f"{relative} is reachable only through {', '.join(parents)}; link it from SKILL.md"
            )
        else:
            warnings.append(f"{relative} is never referenced from SKILL.md")
    return warnings


def third_party_imports(script: Path, skill_root: Path) -> list[str]:
    """Modules a Python script imports that are not stdlib, host-provided or bundled scripts.

    Scripts in sibling skills count as bundled: one skill may compose another's helpers.
    """
    try:
        tree = ast.parse(script.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return []
    local = {path.stem for path in script.parent.glob("*.py")}
    local.update(path.stem for path in skill_root.parent.glob("*/scripts/*.py"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module.split(".")[0])
    return sorted(name for name in names if name not in sys.stdlib_module_names
                  and name not in local | HOST_MODULES and name != "__future__")


def dependencies(skill_dir: Path, skill_text: str) -> list[str]:
    """Warn when a bundled script needs packages the instructions never say how to get."""
    warnings = []
    root = skill_dir.resolve()
    instructions = skill_text + "".join(
        path.read_text(encoding="utf-8", errors="replace") for path in bundled(skill_dir, ".md"))
    for script in bundled(skill_dir, ".py"):
        relative = script.relative_to(root).as_posix()
        text = script.read_text(encoding="utf-8", errors="replace")
        if INLINE_DEPENDENCIES.search(text):
            python_call = re.compile(r"python3?\s+(?:\S*/)?" + re.escape(script.name))
            invoked = any(python_call.search(line) and not PROHIBITION.search(line)
                          for line in instructions.splitlines())
            if invoked and not re.search(r"uv run[^\n]*" + re.escape(script.name), instructions):
                warnings.append(
                    f"{relative} declares inline dependencies but is invoked with python; "
                    "invoke it with `uv run`"
                )
            continue
        packages = third_party_imports(script, root)
        if packages and not INSTALL_HINT.search(instructions) and not INSTALL_HINT.search(text):
            warnings.append(
                f"{relative} imports {', '.join(packages)}; give the install line next to the script"
            )
    return warnings


def lint(skill_dir: Path) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    skill_file = skill_dir / "SKILL.md"
    if not skill_file.is_file():
        return [f"missing {skill_file}"], warnings
    try:
        fields, body = parse_frontmatter(skill_file.read_text(encoding="utf-8"))
    except ValueError as error:
        return [str(error)], warnings

    name = fields.get("name", "")
    description = fields.get("description", "")
    extra = sorted(set(fields) - {"name", "description"})
    if extra:
        warnings.append(f"frontmatter keys beyond name and description: {', '.join(extra)}")
    if not name:
        errors.append("frontmatter needs name")
    elif not NAME_PATTERN.match(name) or len(name) > MAX_NAME:
        errors.append(f"name must be lowercase letters, digits and hyphens, at most {MAX_NAME} chars")
    elif name != skill_dir.resolve().name:
        errors.append(f"name {name!r} does not match folder {skill_dir.resolve().name!r}")
    if not description:
        errors.append("frontmatter needs description")
    else:
        if len(description) > MAX_DESCRIPTION:
            errors.append(f"description is {len(description)} chars; limit is {MAX_DESCRIPTION}")
        if "<" in description or ">" in description:
            errors.append("description must not contain angle brackets")
        if not re.search(r"\b(use|when|for)\b", description, re.IGNORECASE):
            warnings.append("description reads as a summary; name the requests that should trigger it")
        if not re.search(r"\bnot\b", description, re.IGNORECASE):
            warnings.append("description names no near-miss; add a 'not for ...' clause")

    body_lines = len(body.splitlines())
    if body_lines > MAX_BODY_LINES:
        errors.append(f"body is {body_lines} lines; split into references past {MAX_BODY_LINES}")

    for target in LINK_PATTERN.findall(body):
        if re.match(r"^[a-z]+:", target) or target.startswith("#"):
            continue
        relative = target.split("#", 1)[0]
        path = (skill_dir / relative).resolve()
        if not path.exists():
            errors.append(f"broken link: {target}")
        elif skill_dir.resolve() not in path.parents and path != skill_dir.resolve():
            warnings.append(f"link leaves the skill folder: {target}")
        elif len(Path(relative).parts) > 2:
            warnings.append(f"reference nested more than one level deep: {target}")

    skill_text = skill_file.read_text(encoding="utf-8")
    warnings.extend(reference_layout(skill_dir, skill_text))
    warnings.extend(dependencies(skill_dir, skill_text))

    policy = skill_dir / "agents" / "openai.yaml"
    if policy.is_file():
        explicit = re.search(r"allow_implicit_invocation:\s*false", policy.read_text(encoding="utf-8"))
        if explicit and "explicit" not in description.lower():
            warnings.append("Codex policy is explicit-only; say explicit-only in the description too")
    return errors, warnings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("skill_dir", type=Path)
    parser.add_argument("--json", action="store_true", help="print a machine-readable result")
    args = parser.parse_args()
    errors, warnings = lint(args.skill_dir)
    if args.json:
        print(json.dumps({"ok": not errors, "errors": errors, "warnings": warnings}, indent=2))
    else:
        for message in errors:
            print(f"error: {message}", file=sys.stderr)
        for message in warnings:
            print(f"warning: {message}", file=sys.stderr)
        if not errors:
            print(f"lint passed: {args.skill_dir}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
