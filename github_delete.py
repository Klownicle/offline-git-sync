"""Confirmed sync deletion, with optional bounded removal of all local data."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import time
import xml.etree.ElementTree as ET

import github_management as management
from github_sources import parse_repository


PREVIEW_SECONDS = 900
_APPLICATION_DIRECTORIES = {".sync-state", ".tools", ".git", ".github", "__pycache__",
                            "docs", "tests", "test-tmp", "assets", "static"}


def _issued(token):
    try:
        issued = token.split(":", 1)[0]
        if not 0 <= (time.time_ns() - int(issued)) / 1_000_000_000 <= PREVIEW_SECONDS:
            raise ValueError
        return issued
    except (AttributeError, TypeError, ValueError):
        raise ValueError("Delete preview is missing or expired. Preview Delete sync again.") from None


def _paths(base, repository):
    reference = parse_repository(repository, allow_url=False)
    base = Path(os.path.abspath(base))
    return [("source", reference.source_path(base)), ("releases", reference.releases_path(base))]


def _verify_task(sync, repository, xml):
    """Deleting a task requires its sole action to belong to this exact sync."""
    if xml is None:
        return
    root = ET.fromstring(xml.lstrip("\ufeff"))
    tag = lambda name: "{http://schemas.microsoft.com/windows/2004/02/mit/task}" + name
    actions = root.find(tag("Actions"))
    details, owned = [], 0
    for action in [] if actions is None else actions:
        if not isinstance(action.tag, str):
            continue
        executable = action.findtext(tag("Command"), "")
        arguments = action.findtext(tag("Arguments"), "")
        issue = (management._action_ownership_issue(sync, repository, executable, arguments)
                 if action.tag == tag("Exec") else "This is not a program execution action.")
        if issue is None and management._split_windows_arguments(arguments)[2] != repository:
            issue = "Repository capitalization differs from this sync; repair the task before deleting it."
        if issue is None:
            owned += 1
        details.append({"command": executable, "arguments": arguments,
                        "working_directory": action.findtext(tag("WorkingDirectory"), ""),
                        "reason": issue or "Matches this sync."})
    if len(details) != 1 or owned != 1:
        raise management.TaskActionConflict({
            "task_name": management.task_name(repository), "repository": repository,
            "expected": {"command": str(Path(sync.sys.executable).with_name("pythonw.exe")),
                         "arguments": subprocess.list2cmdline([str(Path(sync.__file__).resolve()),
                                                               "run-saved", repository])},
            "actions": details,
            "help": "Delete sync can remove a task only when its sole action belongs to this exact sync. "
                    "Review the Actions tab in Windows Task Scheduler. No task, settings, or local files were changed."},
            message="The scheduled task is not solely owned by this sync. Nothing was deleted. "
                    "Review the scheduled task details before trying Delete sync again.")


def _bounded_paths(sync, repository, jobs):
    base = management._no_links(sync.BASE).resolve(strict=True)
    paths = _paths(sync.BASE, repository)
    protected = [base / name for name in _APPLICATION_DIRECTORIES]
    other_paths = [path for name in jobs if name != repository for _, path in _paths(sync.BASE, name)]
    for _, path in paths:
        management._no_links(path)
        resolved = path.resolve(strict=path.exists())
        if resolved == base or not resolved.is_relative_to(base):
            raise ValueError("Delete target escaped the application directory")
        # Only the two exact repository paths are allowed. Never expand to their
        # owner/host parents, application files, or another configured sync.
        if resolved != Path(os.path.abspath(path)):
            raise ValueError("Delete target resolves to a different local path")
        if any(resolved == item or resolved.is_relative_to(item) or item.is_relative_to(resolved)
               for item in protected):
            raise ValueError("Delete target overlaps an application or state directory")
        if any(resolved == item or resolved.is_relative_to(item) or item.is_relative_to(resolved)
               for item in other_paths):
            raise ValueError("Local folders overlap another configured sync; local files were not deleted")
        if path.exists() and not path.is_dir():
            raise ValueError("A sync data path is not a directory; local files were not deleted")
    return paths


def _scan(paths):
    records, stamps = [], {}
    for kind, path in paths:
        exists = path.exists()
        stamp = management._tree_stamp(path) if exists else None
        stamps[str(path)] = stamp
        records.append({"kind": kind, "path": str(path), "exists": exists, "scanned": True,
                        "file_count": sum(stat.S_ISREG(item[1]) for item in stamp or []),
                        "directory_count": sum(stat.S_ISDIR(item[1]) for item in stamp or []),
                        "bytes": sum(item[2] for item in stamp or [] if stat.S_ISREG(item[1]))})
    return records, stamps


def _preview(sync, repository, delete_files, issued, *, private=False):
    if type(delete_files) is not bool:
        raise ValueError("Also delete local files must be true or false")
    jobs = sync.load_config()
    job = management._job(jobs, repository)
    task = management._task_xml(repository)
    _verify_task(sync, repository, task)
    stamps = {}
    if delete_files:
        records, stamps = _scan(_bounded_paths(sync, repository, jobs))
    else:
        # Keep-files deletion must not inspect source/release folders, even if
        # they are inaccessible, linked, large, or no longer on this computer.
        records = [{"kind": kind, "path": str(path), "exists": None, "scanned": False,
                    "file_count": None, "directory_count": None, "bytes": None}
                   for kind, path in _paths(sync.BASE, repository)]
    signature = [str(Path(os.path.abspath(sync.BASE))), repository, job, task,
                 delete_files, records, stamps, issued]
    digest = hashlib.sha256(json.dumps(signature, sort_keys=True).encode("utf-8")).hexdigest()
    result = {"repository": repository, "token": f"{issued}:{digest}", "delete_files": delete_files,
              "paths": records, "task_name": management.task_name(repository),
              "task_exists": task is not None, "expires_in": PREVIEW_SECONDS,
              "file_count": sum(item["file_count"] for item in records) if delete_files else None,
              "directory_count": sum(item["directory_count"] for item in records) if delete_files else None,
              "bytes": sum(item["bytes"] for item in records) if delete_files else None}
    if private:
        result.update(_task=task, _stamps=stamps, _jobs=jobs)
    return result


def preview(repository, delete_files=False):
    if type(delete_files) is not bool:
        raise ValueError("Also delete local files must be true or false")
    with management._locked(repository) as sync:
        return _preview(sync, repository, delete_files, str(time.time_ns()))


def _check_stamp(path, expected):
    management._no_links(path)
    actual = management._tree_stamp(path) if path.exists() else None
    if actual != expected:
        raise ValueError("Local files changed after the preview. Preview Delete sync again before deleting files.")


def _remove_tree(root):
    """Git objects can be read-only on Windows; retry only within this root."""
    management._no_links(root)
    resolved_root = root.resolve(strict=True)
    if resolved_root != Path(os.path.abspath(root)):
        raise ValueError("Delete target resolves to a different local path")
    def retry_read_only(operation, name, error):
        if not isinstance(error[1], PermissionError) or operation not in {os.unlink, os.remove, os.rmdir}:
            raise error[1]
        path = management._no_links(Path(name))
        resolved = path.resolve(strict=True)
        if resolved != resolved_root and not resolved.is_relative_to(resolved_root):
            raise ValueError("Read-only retry escaped the confirmed delete target")
        info = path.lstat()
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise ValueError("Read-only retry encountered an unsupported file")
        if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
            raise ValueError("Read-only retry cannot change a file with multiple hard links")
        os.chmod(path, info.st_mode | stat.S_IWRITE)
        operation(path)
    shutil.rmtree(root, onerror=retry_read_only)


def _stopped(result, checked, error):
    changed = bool(result["files_deleted"] or result["affected_path"])
    if checked["delete_files"]:
        remaining = []
        for item in checked["paths"]:
            path = Path(item["path"])
            try:
                management._no_links(path)
                if path.exists():
                    remaining.append(str(path))
            except (OSError, ValueError):
                remaining.append(str(path))
        result["remaining_paths"] = remaining
    files_gone = checked["delete_files"] and not result["remaining_paths"]
    detail = ("The selected local folders are gone. " if files_gone else
              "Some local files may have been deleted. " if changed else "No local files were deleted. ")
    result.update(removed=False, archived=True, partial=True, data_preserved=not changed,
                  local_files_deleted=files_gone, counts_are_minimum=bool(result["affected_path"]),
                  warning="Sync remains archived and its scheduled task is removed. " + detail +
                          f"Delete sync did not finish: {error}. Preview again to retry.")
    return result


def delete(repository, delete_files, token):
    if type(delete_files) is not bool:
        raise ValueError("Also delete local files must be true or false")
    issued = _issued(token)
    sync = management._sync()
    result = {"repository": repository, "delete_files": delete_files, "removed": False,
              "archived": False, "partial": False, "files_deleted": [], "affected_path": None,
              "remaining_paths": [], "deleted_file_count": 0, "deleted_bytes": 0,
              "counts_are_minimum": False, "data_preserved": True}
    try:
        with management._locked(repository) as sync:
            checked = _preview(sync, repository, delete_files, issued, private=True)
            if token != checked["token"]:
                raise ValueError("Settings, task, or local files changed. Preview Delete sync again.")
            jobs = checked["_jobs"]
            archived = dict(jobs[repository], archived=True)
            jobs[repository] = archived
            def remove_task(current):
                if current != checked["_task"]:
                    raise ValueError("The scheduled task changed. Preview Delete sync again.")
                _verify_task(sync, repository, current)
                if current is not None:
                    management._delete_task(repository)
            # Until this transaction succeeds, files remain untouched and the
            # original task can safely be restored if the config write fails.
            management._commit_scheduler(sync, repository, jobs, remove_task)
            result["archived"] = True
            try:
                if delete_files:
                    paths = _bounded_paths(sync, repository, jobs)
                    # Validate every original stamp before removing either root.
                    for _, path in paths:
                        _check_stamp(path, checked["_stamps"][str(path)])
                    records = {item["path"]: item for item in checked["paths"]}
                    for _, path in paths:
                        expected = checked["_stamps"][str(path)]
                        _check_stamp(path, expected)
                        if expected is None:
                            continue
                        result["affected_path"] = str(path)
                        _remove_tree(path)
                        result["files_deleted"].append(str(path))
                        result["deleted_file_count"] += records[str(path)]["file_count"]
                        result["deleted_bytes"] += records[str(path)]["bytes"]
                        result["affected_path"] = None
                    # A newly recreated root must not be forgotten by dropping
                    # its config; keep an archived entry for an explicit retry.
                    if any(path.exists() for _, path in paths):
                        raise ValueError("A local sync folder was recreated while deleting")
                sync.mark_deleted(repository)
                final_jobs = dict(jobs)
                del final_jobs[repository]
                management._write_config(sync, final_jobs)
            except Exception as error:
                return _stopped(result, checked, error)
            result.update(removed=True, archived=False, data_preserved=not delete_files,
                          local_files_deleted=delete_files)
            return result
    finally:
        from github_storage import invalidate_size
        if delete_files and (result["files_deleted"] or result["affected_path"]):
            invalidate_size(sync.BASE, repository)
        management._render(sync)


def remove_preserving_files(repository):
    """Compatibility remove action, using the same task and tombstone guards."""
    checked = preview(repository, False)
    result = delete(repository, False, checked["token"])
    if result["removed"]:
        result["removed"] = repository
    return result
