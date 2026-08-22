"""Resolve PR merge conflicts in an isolated checkout without executing PR code."""
import asyncio
import base64
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path, PurePosixPath

from app.services import github as gh
from app.services.conflict_ai import ConflictResolutionError, resolve_conflict_blocks

logger = logging.getLogger(__name__)

CONFLICT_PATTERN = re.compile(
    r"^<<<<<<< [^\n]*\n(.*?)^=======\n(.*?)^>>>>>>> [^\n]*(?:\n|$)",
    flags=re.MULTILINE | re.DOTALL,
)


class ConflictRepairError(Exception):
    """The conflict could not be repaired safely and automatically."""


async def repair_pull_request_conflicts(
    token: str,
    repo_full_name: str,
    pr_number: int,
    expected_head_sha: str,
    issue_title: str,
    issue_body: str,
    pr_title: str,
    pr_body: str,
) -> str:
    """Push one merge-resolution commit and return its SHA."""
    if os.getenv("AUTO_RESOLVE_CONFLICTS", "true").lower() not in {"1", "true", "yes"}:
        raise ConflictRepairError("Automatic conflict repair is disabled")
    if not shutil.which("git"):
        raise ConflictRepairError("Git is not installed in the service runtime")

    pr = await gh.get_pr(token, repo_full_name, pr_number)
    if pr.get("state") != "open" or pr.get("head", {}).get("sha") != expected_head_sha:
        raise ConflictRepairError("The PR changed before conflict repair started")
    if pr.get("mergeable_state") != "dirty" and pr.get("mergeable") is not False:
        raise ConflictRepairError("GitHub does not currently report a merge conflict")

    head = pr.get("head") or {}
    base = pr.get("base") or {}
    head_repo = (head.get("repo") or {}).get("full_name")
    head_url = (head.get("repo") or {}).get("clone_url")
    base_url = (base.get("repo") or {}).get("clone_url")
    base_sha = base.get("sha")
    base_ref = base.get("ref")
    head_ref = head.get("ref")
    if not all((head_repo, head_url, base_url, base_sha, base_ref, head_ref)):
        raise ConflictRepairError("GitHub omitted required branch information")
    if head_repo != repo_full_name and not pr.get("maintainer_can_modify", False):
        raise ConflictRepairError("The contributor did not allow maintainer branch edits")
    _validate_github_url(head_url)
    _validate_github_url(base_url)
    _validate_head_ref(head_ref)
    _validate_head_ref(base_ref)

    auth_env = _git_auth_env(token)
    with tempfile.TemporaryDirectory(prefix="repo-bot-conflict-") as temp_dir:
        checkout = Path(temp_dir)
        await _git(checkout, auth_env, "init")
        await _git(checkout, auth_env, "config", "user.name", "repo-review-bot")
        await _git(
            checkout, auth_env, "config", "user.email", "repo-review-bot@users.noreply.github.com"
        )
        await _git(checkout, auth_env, "remote", "add", "base", base_url)
        await _git(checkout, auth_env, "remote", "add", "head", head_url)
        await _git(
            checkout, auth_env, "fetch", "--no-tags", "base", f"refs/heads/{base_ref}"
        )
        await _git(
            checkout, auth_env, "fetch", "--no-tags", "head", f"refs/heads/{head_ref}"
        )
        await _git(checkout, auth_env, "cat-file", "-e", f"{base_sha}^{{commit}}")
        await _git(checkout, auth_env, "cat-file", "-e", f"{expected_head_sha}^{{commit}}")
        await _git(checkout, auth_env, "checkout", "-B", "repo-bot-repair", expected_head_sha)

        merge = await _git(
            checkout, auth_env, "merge", "--no-commit", "--no-ff", base_sha, check=False
        )
        if merge.returncode != 0:
            conflict_output = await _git(
                checkout, auth_env, "diff", "--name-only", "--diff-filter=U", "-z"
            )
            paths = [path for path in conflict_output.stdout.split("\0") if path]
            if not paths:
                raise ConflictRepairError(
                    f"Git merge failed without resolvable files: {_short(merge.stderr)}"
                )
            blocks, file_matches = _collect_conflicts(checkout, paths)
            try:
                replacements = await resolve_conflict_blocks(
                    issue_title, issue_body, pr_title, pr_body, blocks
                )
            except ConflictResolutionError as exc:
                raise ConflictRepairError(str(exc)) from exc
            _apply_resolutions(file_matches, replacements)
            await _git(checkout, auth_env, "add", "--", *paths)

        unresolved = await _git(
            checkout, auth_env, "diff", "--name-only", "--diff-filter=U"
        )
        if unresolved.stdout.strip():
            raise ConflictRepairError("Conflict markers remain after automatic resolution")
        check = await _git(checkout, auth_env, "diff", "--check", "HEAD", check=False)
        if check.returncode != 0:
            raise ConflictRepairError(f"Resolved diff failed git checks: {_short(check.stdout)}")

        await _git(
            checkout,
            auth_env,
            "commit",
            "-m",
            f"chore: resolve base conflicts for PR #{pr_number}",
        )
        new_sha = (await _git(checkout, auth_env, "rev-parse", "HEAD")).stdout.strip()

        refreshed = await gh.get_pr(token, repo_full_name, pr_number)
        if refreshed.get("head", {}).get("sha") != expected_head_sha:
            raise ConflictRepairError("The contributor pushed new commits during repair")
        push = await _git(
            checkout,
            auth_env,
            "push",
            "head",
            f"HEAD:refs/heads/{head_ref}",
            check=False,
        )
        if push.returncode != 0:
            raise ConflictRepairError(f"GitHub rejected the repair commit: {_short(push.stderr)}")
        logger.info(
            "Conflict repair pushed repo=%s pr=%s old_sha=%s new_sha=%s",
            repo_full_name, pr_number, expected_head_sha[:12], new_sha[:12],
        )
        return new_sha


def _collect_conflicts(checkout: Path, paths: list[str]) -> tuple[list[dict], list[dict]]:
    max_files = max(1, int(os.getenv("AUTO_RESOLVE_MAX_FILES", "6")))
    max_blocks = max(1, int(os.getenv("AUTO_RESOLVE_MAX_BLOCKS", "24")))
    max_chars = max(10_000, int(os.getenv("AUTO_RESOLVE_MAX_CONFLICT_CHARS", "80000")))
    if len(paths) > max_files:
        raise ConflictRepairError(f"Conflict touches {len(paths)} files; limit is {max_files}")

    blocks = []
    file_matches = []
    total_chars = 0
    for raw_path in paths:
        relative = PurePosixPath(raw_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ConflictRepairError("Git returned an unsafe conflict path")
        if relative.parts[:2] == (".github", "workflows") or raw_path == ".gitmodules":
            raise ConflictRepairError(f"Refusing to auto-resolve sensitive file {raw_path}")
        file_path = checkout.joinpath(*relative.parts).resolve()
        if checkout.resolve() not in file_path.parents:
            raise ConflictRepairError("Conflict path escaped the checkout")
        try:
            text = file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ConflictRepairError(f"Conflict file is not safe UTF-8 text: {raw_path}") from exc
        matches = list(CONFLICT_PATTERN.finditer(text))
        if not matches:
            raise ConflictRepairError(f"Could not parse conflict markers in {raw_path}")
        entries = []
        for match in matches:
            index = len(blocks) + 1
            before = text[max(0, match.start() - 1500):match.start()]
            after = text[match.end():match.end() + 1500]
            block = {
                "index": index,
                "path": raw_path,
                "before": before,
                "ours": match.group(1),
                "theirs": match.group(2),
                "after": after,
            }
            total_chars += sum(len(str(value)) for value in block.values())
            blocks.append(block)
            entries.append(match)
        file_matches.append({"path": file_path, "text": text, "matches": entries})

    if len(blocks) > max_blocks:
        raise ConflictRepairError(f"Conflict has {len(blocks)} blocks; limit is {max_blocks}")
    if total_chars > max_chars:
        raise ConflictRepairError(f"Conflict context is too large ({total_chars} characters)")
    return blocks, file_matches


def _apply_resolutions(file_matches: list[dict], replacements: list[str]) -> None:
    replacement_index = 0
    for file_entry in file_matches:
        text = file_entry["text"]
        parts = []
        cursor = 0
        for match in file_entry["matches"]:
            replacement = replacements[replacement_index]
            replacement_index += 1
            if match.group(0).endswith("\n") and replacement and not replacement.endswith("\n"):
                replacement += "\n"
            parts.extend((text[cursor:match.start()], replacement))
            cursor = match.end()
        parts.append(text[cursor:])
        resolved = "".join(parts)
        if CONFLICT_PATTERN.search(resolved):
            raise ConflictRepairError("A resolved file still contains Git conflict blocks")
        file_entry["path"].write_text(resolved, encoding="utf-8", newline="")
    if replacement_index != len(replacements):
        raise ConflictRepairError("AI returned extra conflict resolutions")


async def _git(
    cwd: Path,
    auth_env: dict,
    *args: str,
    check: bool = True,
):
    process = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        env=auth_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=180)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.communicate()
        raise ConflictRepairError(f"Git command timed out: {args[0]}") from exc
    result = _GitResult(
        process.returncode,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )
    if check and result.returncode != 0:
        raise ConflictRepairError(
            f"Git {args[0]} failed: {_short(result.stderr or result.stdout)}"
        )
    return result


class _GitResult:
    def __init__(self, returncode: int, stdout: str, stderr: str):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _git_auth_env(token: str) -> dict:
    encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    env = os.environ.copy()
    env.update({
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {encoded}",
    })
    return env


def _validate_github_url(url: str) -> None:
    if not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.git", url):
        raise ConflictRepairError("GitHub returned an unexpected clone URL")


def _validate_head_ref(ref: str) -> None:
    if not ref or ref.startswith("-") or any(char in ref for char in ("\n", "\r", " ")):
        raise ConflictRepairError("GitHub returned an unsafe head branch name")


def _short(value: str, limit: int = 500) -> str:
    return " ".join((value or "").split())[:limit]
