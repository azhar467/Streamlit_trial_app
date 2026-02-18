import requests
import urllib3
from urllib.parse import quote
import json
import base64
import re
import datetime
import difflib
import argparse
import sys
import time
import os
import tempfile
import signal
from functools import wraps

from loguru import logger
from rich.console import Console
from rich.table import Table
from prompt_toolkit.shortcuts import checkboxlist_dialog, radiolist_dialog, button_dialog
from prompt_toolkit.styles import Style

BASE_URL = ""
TOKEN = ""

PROJECT_NAMES = {}

JIRA_ID = "1293"
UPGRADE_TYPE = "java17-migration"
FEATURE_BRANCH = f"task-{JIRA_ID}-{UPGRADE_TYPE}"
SOURCE_BRANCH = "develop"
MR_TITLE = f"TASK-{JIRA_ID}: java migration"

TARGET_PARENT_VERSION = "1.8.3"
NEW_DEFAULT_PLATFORM = ""
REVIEWER_USERNAMES = []
ASSIGNEE_USERNAMES = []

AUTO_ROLLBACK_ON_FAILURE = True

STATE_FILE = None
ROLLBACK_FILE = None
INTERRUPTED = False

LOG_DIR_MIGRATION = "migration_logs"
LOG_DIR_ROLLBACK = "rollback_logs"
LOG_DIR_STATE = "state_logs"
AUDIT_LOG_DIR = "api_audit_logs"

HTTP_SESSION = None
SSL_VERIFY = True

CONSOLE = Console()

DIALOG_STYLE = Style.from_dict({
    "dialog":             "bg:#1e1e2e",
    "dialog frame.label": "bg:#313244 fg:#cdd6f4 bold",
    "dialog.body":        "bg:#1e1e2e fg:#cdd6f4",
    "button":             "bg:#45475a fg:#cdd6f4",
    "button.focused":     "bg:#89b4fa fg:#1e1e2e bold",
    "checkbox":           "fg:#cdd6f4",
    "checkbox-selected":  "fg:#a6e3a1 bold",
    "radio":              "fg:#cdd6f4",
    "radio-selected":     "fg:#89b4fa bold",
})

_LEVEL_MAP = {
    "INFO":     "info",
    "WARN":     "warning",
    "WARNING":  "warning",
    "ERROR":    "error",
    "DEBUG":    "debug",
    "CRITICAL": "critical",
    "SUCCESS":  "success",
}


def setup_logging():
    logger.remove()
    os.makedirs(LOG_DIR_MIGRATION, exist_ok=True)
    os.makedirs(AUDIT_LOG_DIR, exist_ok=True)

    logger.add(
        sys.stderr,
        colorize=True,
        filter=lambda r: not r["extra"].get("audit"),
        format=(
            "<green>{time:HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<level>{message}</level>"
        ),
        level="DEBUG",
    )

    logger.add(
        os.path.join(LOG_DIR_MIGRATION, "migration.log"),
        rotation="10 MB",
        retention="30 days",
        filter=lambda r: not r["extra"].get("audit"),
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}",
        level="DEBUG",
        encoding="utf-8",
    )

    logger.add(
        os.path.join(AUDIT_LOG_DIR, "audit.log"),
        rotation="10 MB",
        retention="30 days",
        filter=lambda r: r["extra"].get("audit"),
        format=(
            "{time:YYYY-MM-DD HH:mm:ss} | "
            "{extra[method]} | "
            "{extra[url]} | "
            "{extra[status]} | "
            "{message}"
        ),
        level="INFO",
        encoding="utf-8",
    )


def log(msg, level="INFO"):
    fn = getattr(logger, _LEVEL_MAP.get(level.upper(), "info"))
    fn(msg)


def setup_http_session(ssl_verify=True):
    global HTTP_SESSION
    session = requests.Session()
    session.verify = ssl_verify
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=10,
        pool_maxsize=20,
        max_retries=0,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({
        "PRIVATE-TOKEN": TOKEN,
        "Content-Type": "application/json",
    })
    HTTP_SESSION = session


def signal_handler(signum, frame):
    global INTERRUPTED
    INTERRUPTED = True
    log("[INTERRUPT] Received interrupt signal. Saving state...", "WARN")


def save_state(state_data, filename=None):
    global STATE_FILE
    if filename is None:
        os.makedirs(LOG_DIR_STATE, exist_ok=True)
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = os.path.join(LOG_DIR_STATE, f"migration_state_{timestamp}.json")
    STATE_FILE = filename
    state_data["last_updated"] = datetime.datetime.now().isoformat()
    target_dir = os.path.dirname(os.path.abspath(filename)) or "."
    os.makedirs(target_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=target_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state_data, f, indent=2)
        os.replace(tmp_path, filename)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    log(f"State saved to {filename}", "DEBUG")
    return filename


def load_state(filename):
    if not os.path.exists(filename):
        return None
    try:
        with open(filename, "r") as f:
            state = json.load(f)
        log(f"State loaded from {filename}", "INFO")
        return state
    except Exception as e:
        log(f"Error loading state from {filename}: {e}", "ERROR")
        return None


def create_rollback_snapshot(pid, p_name, branch_name, files_to_backup):
    snapshot = {
        "project_id": pid,
        "project_name": p_name,
        "branch": branch_name,
        "timestamp": datetime.datetime.now().isoformat(),
        "files": {},
    }
    try:
        br_info = api_call(f"projects/{pid}/repository/branches/{quote(branch_name, safe='')}")
        if isinstance(br_info, dict) and "commit" in br_info:
            snapshot["commit_sha"] = (br_info.get("commit") or {}).get("id")
        else:
            snapshot["commit_sha"] = None
    except Exception as e:
        log(f"Could not get branch head for rollback snapshot: {e}", "WARN")
        snapshot["commit_sha"] = None
    for file_path in files_to_backup:
        try:
            quoted_path = quote(file_path, safe="")
            res = api_call(
                f"projects/{pid}/repository/files/{quoted_path}"
                f"?ref={quote(branch_name, safe='')}"
            )
            if isinstance(res, dict) and "content" in res:
                content = base64.b64decode(res["content"]).decode("utf-8")
                snapshot["files"][file_path] = {"content": content, "exists": True}
            else:
                snapshot["files"][file_path] = {"content": None, "exists": False}
        except Exception as e:
            log(f"Could not backup {file_path} for rollback: {e}", "WARN")
            snapshot["files"][file_path] = {"content": None, "exists": False, "error": str(e)}
    return snapshot


def save_rollback_data(rollback_data, filename=None):
    global ROLLBACK_FILE
    if filename is None:
        os.makedirs(LOG_DIR_ROLLBACK, exist_ok=True)
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = os.path.join(LOG_DIR_ROLLBACK, f"rollback_data_{timestamp}.json")
    ROLLBACK_FILE = filename
    rollback_data["created_at"] = datetime.datetime.now().isoformat()
    with open(filename, "w") as f:
        json.dump(rollback_data, f, indent=2)
    log(f"Rollback data saved to {filename}", "INFO")
    return filename


def load_rollback_data(filename):
    if not os.path.exists(filename):
        log(f"Rollback file {filename} not found", "ERROR")
        return None
    try:
        with open(filename, "r") as f:
            data = json.load(f)
        log(f"Rollback data loaded from {filename}", "INFO")
        return data
    except Exception as e:
        log(f"Error loading rollback data: {e}", "ERROR")
        return None


def perform_rollback(rollback_file):
    log("=" * 70, "INFO")
    log("ROLLBACK MODE", "INFO")
    log("=" * 70, "INFO")
    data = load_rollback_data(rollback_file)
    if not data:
        return False
    snapshots = data.get("snapshots", [])
    if not snapshots:
        log("No snapshots found in rollback file", "WARN")
        return False
    log(f"Found {len(snapshots)} project(s) to rollback", "INFO")
    log(f"Rollback file created: {data.get('created_at', 'unknown')}", "INFO")
    success_count = 0
    fail_count = 0
    for snapshot in snapshots:
        pid = snapshot["project_id"]
        p_name = snapshot["project_name"]
        branch = snapshot["branch"]
        log(f"--- Rollback: {p_name} (ID: {pid}) ---", "INFO")
        project_success = True
        for file_path, file_data in snapshot.get("files", {}).items():
            if not file_data.get("exists"):
                log(f"File {file_path} did not exist before, skipping restore", "INFO")
                continue
            content = file_data.get("content")
            if content is None:
                log(f"No backup content for {file_path}, skipping", "WARN")
                continue
            try:
                commit_data = {
                    "branch": branch,
                    "commit_message": f"Rollback: Restore {file_path} to pre-migration state",
                    "actions": [{"action": "update", "file_path": file_path, "content": content}],
                }
                api_call(f"projects/{pid}/repository/commits", "POST", commit_data)
                log(f"Restored {file_path}", "INFO")
            except Exception as e:
                log(f"Error restoring {file_path}: {e}", "ERROR")
                project_success = False
        if project_success:
            success_count += 1
            log(f"Rollback completed successfully for {p_name}", "INFO")
        else:
            fail_count += 1
            log(f"Rollback completed with errors for {p_name}", "WARN")
    log("=" * 70, "INFO")
    log(f"ROLLBACK COMPLETE: {success_count} successful, {fail_count} with errors", "INFO")
    log("=" * 70, "INFO")
    return success_count > 0


def get_user_id_from_username(username):
    try:
        users = api_call(f"users?username={quote(username, safe='')}")
        if isinstance(users, list) and len(users) > 0:
            user_id = users[0].get("id")
            user_name = users[0].get("name", username)
            log(f"Resolved username '{username}' to user ID {user_id} ({user_name})", "DEBUG")
            return user_id
        else:
            log(f"Could not find user with username '{username}'", "WARN")
            return None
    except Exception as e:
        log(f"Error looking up username '{username}': {e}", "WARN")
        return None


def resolve_usernames_to_ids(usernames):
    user_ids = []
    for username in usernames:
        user_id = get_user_id_from_username(username.strip())
        if user_id:
            user_ids.append(user_id)
    return user_ids


def load_env_config(debug=False):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    env_file = os.path.join(script_dir, ".env")
    config = {
        "base_url": "",
        "token": "",
        "projects": {},
        "new_default_platform": "",
        "reviewer_usernames": [],
        "assignee_usernames": [],
        "ssl_verify": None,
    }
    if debug:
        log(f"Looking for .env file at: {env_file}", "DEBUG")
    if not os.path.exists(env_file):
        if debug:
            log(".env file not found", "DEBUG")
        return config
    try:
        with open(env_file, "r") as f:
            lines = f.readlines()
            for i, line in enumerate(lines, 1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, value = line.split("=", 1)
                    key = key.strip()
                    value = value.strip()
                    if value.startswith('"') and value.endswith('"'):
                        value = value[1:-1]
                    elif value.startswith("'") and value.endswith("'"):
                        value = value[1:-1]
                    if key in ("BASE_URL", "GITLAB_BASE_URL", "GITLAB_URL"):
                        config["base_url"] = value.rstrip("/")
                        if debug:
                            log(f"Loaded BASE_URL: {config['base_url']}", "DEBUG")
                    elif key == "GITLAB_VERIFY_SSL":
                        config["ssl_verify"] = value.strip().lower() != "false"
                        if debug:
                            log(f"Loaded GITLAB_VERIFY_SSL: {config['ssl_verify']}", "DEBUG")
                    elif key in ("GITLAB_TOKEN", "TOKEN"):
                        config["token"] = value
                        if debug:
                            log(f"Loaded TOKEN (length: {len(value)})", "DEBUG")
                    elif key == "NEW_DEFAULT_PLATFORM":
                        config["new_default_platform"] = value
                        if debug:
                            log(f"Loaded NEW_DEFAULT_PLATFORM: {value[:50]}...", "DEBUG")
                    elif key == "REVIEWER_USERNAMES":
                        if value:
                            try:
                                reviewer_usernames = [x.strip() for x in value.split(",") if x.strip()]
                                config["reviewer_usernames"] = reviewer_usernames
                                if debug:
                                    log(f"Loaded REVIEWER_USERNAMES: {reviewer_usernames}", "DEBUG")
                            except ValueError as e:
                                log(f"Error parsing REVIEWER_USERNAMES on line {i}: {value} ({e})", "WARN")
                    elif key == "ASSIGNEE_USERNAMES":
                        if value:
                            try:
                                assignee_usernames = [x.strip() for x in value.split(",") if x.strip()]
                                config["assignee_usernames"] = assignee_usernames
                                if debug:
                                    log(f"Loaded ASSIGNEE_USERNAMES: {assignee_usernames}", "DEBUG")
                            except ValueError as e:
                                log(f"Error parsing ASSIGNEE_USERNAMES on line {i}: {value} ({e})", "WARN")
                    elif key.startswith("PROJECT_"):
                        try:
                            project_id_str = key.replace("PROJECT_", "")
                            project_id = int(project_id_str)
                            config["projects"][project_id] = value
                            if debug:
                                log(f"Loaded project: {project_id} -> {value}", "DEBUG")
                        except (ValueError, IndexError) as e:
                            log(f"Error parsing project line {i}: {line} ({e})", "WARN")
        if config["projects"]:
            log(f"Loaded {len(config['projects'])} project(s) from .env file", "INFO")
        if config["new_default_platform"]:
            log("Loaded NEW_DEFAULT_PLATFORM from .env file", "INFO")
        if config["reviewer_usernames"]:
            log(
                f"Loaded {len(config['reviewer_usernames'])} reviewer username(s): "
                f"{', '.join(config['reviewer_usernames'])}",
                "INFO",
            )
        if config["assignee_usernames"]:
            log(
                f"Loaded {len(config['assignee_usernames'])} assignee username(s): "
                f"{', '.join(config['assignee_usernames'])}",
                "INFO",
            )
    except Exception as e:
        log(f"Error reading .env file: {e}", "WARN")
    return config


def load_projects_from_env(debug=False):
    config = load_env_config(debug=debug)
    return config["projects"]


def load_token_from_file(debug=False):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    if debug:
        log(f"Script directory: {script_dir}", "DEBUG")
    env_file = os.path.join(script_dir, ".env")
    if os.path.exists(env_file):
        try:
            with open(env_file, "r") as f:
                lines = f.readlines()
                for line in lines:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        key, value = line.split("=", 1)
                        key = key.strip()
                        value = value.strip()
                        if value.startswith('"') and value.endswith('"'):
                            value = value[1:-1]
                        elif value.startswith("'") and value.endswith("'"):
                            value = value[1:-1]
                        if key in ("GITLAB_TOKEN", "TOKEN"):
                            log(f"Token loaded from .env file (key: {key})", "INFO")
                            return value
        except Exception as e:
            log(f"Error reading .env file: {e}", "WARN")
    token_file = os.path.join(script_dir, "token.txt")
    if os.path.exists(token_file):
        try:
            with open(token_file, "r") as f:
                token = f.read().strip()
                if token:
                    log("Token loaded from token.txt file", "INFO")
                    return token
        except Exception as e:
            log(f"Error reading token.txt file: {e}", "WARN")
    return None


def retry(tries=3, delay=1, backoff=2, allowed_exceptions=(Exception,)):
    def deco(f):
        @wraps(f)
        def wrapper(*a, **kw):
            _tries, _delay = tries, delay
            while True:
                try:
                    return f(*a, **kw)
                except allowed_exceptions as e:
                    _tries -= 1
                    if _tries <= 0:
                        raise
                    log(
                        f"Transient error: {e!r}. Retrying in {_delay}s... "
                        f"(remaining attempts: {_tries})",
                        "WARN",
                    )
                    time.sleep(_delay)
                    _delay *= backoff
        return wrapper
    return deco


@retry(tries=3, delay=1, backoff=2, allowed_exceptions=(Exception,))
def api_call(endpoint, method="GET", data=None):
    url = f"{BASE_URL.rstrip('/')}/{endpoint.lstrip('/')}"
    if not TOKEN:
        raise RuntimeError(
            "API token not set. Provide --token on the command line "
            "or set the TOKEN constant in the script."
        )
    try:
        response = HTTP_SESSION.request(
            method=method,
            url=url,
            json=data if data is not None else None,
        )
        status_code = response.status_code
        response_body = response.text if status_code >= 400 else None
        body_snippet = (
            response_body[:500].replace("\n", " ").replace("\r", "")
            if response_body
            else ""
        )
        logger.bind(audit=True, method=method, url=url, status=status_code).info(body_snippet)
        if status_code == 429:
            retry_after = int(response.headers.get("Retry-After", 60))
            log(f"Rate limited (HTTP 429) on {url}. Waiting {retry_after}s before retry...", "WARN")
            time.sleep(retry_after)
            raise RuntimeError(f"HTTP 429 rate limit for {url}")
        if 500 <= status_code <= 599:
            raise RuntimeError(f"HTTP {status_code} returned for {url}")
        if not response.text:
            return {}
        if 400 <= status_code <= 499:
            return {
                "error": True,
                "details": f"HTTP {status_code} returned for {url}: {response.text}",
            }
        return response.json()
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Request failed for {url}: {e}")


def validate_and_log_token_info(token, base_url):
    if not token:
        return {"valid": False, "info": None}
    try:
        url = f"{base_url.rstrip('/')}/personal_access_tokens/self"
        with CONSOLE.status("[cyan]Validating GitLab token...", spinner="dots"):
            response = HTTP_SESSION.get(url)
        response_body = response.text if response.status_code >= 400 else None
        body_snippet = (
            response_body[:500].replace("\n", " ") if response_body else ""
        )
        logger.bind(
            audit=True, method="GET", url=url, status=response.status_code
        ).info(body_snippet)
        if response.status_code == 200:
            data = response.json()
            token_info = {
                "id": data.get("id"),
                "name": data.get("name", "N/A"),
                "scopes": data.get("scopes", []),
                "created_at": data.get("created_at"),
                "expires_at": data.get("expires_at"),
                "active": data.get("active", False),
                "revoked": data.get("revoked", False),
                "access_level": data.get("access_level", "unknown"),
            }
            log("=" * 70, "INFO")
            log("GitLab Token Information:", "INFO")
            log("=" * 70, "INFO")
            log(f"Token Name: {token_info['name']}", "INFO")
            log(f"Token ID: {token_info['id']}", "INFO")
            log(f"Active: {'Yes' if token_info['active'] else 'No'}", "INFO")
            log(f"Revoked: {'Yes' if token_info['revoked'] else 'No'}", "INFO")
            access_level = token_info.get("access_level")
            if access_level and access_level != "unknown":
                access_level_names = {
                    10: "Guest", 20: "Reporter", 30: "Developer",
                    40: "Maintainer", 50: "Owner",
                }
                access_name = access_level_names.get(access_level, f"Level {access_level}")
                log(f"Access Level: {access_name} ({access_level})", "INFO")
            if token_info["expires_at"]:
                try:
                    from datetime import datetime as _dt
                    expiry_dt = _dt.fromisoformat(
                        token_info["expires_at"].replace("Z", "+00:00")
                    )
                    now_dt = _dt.now(expiry_dt.tzinfo)
                    expiry_str = expiry_dt.strftime("%Y-%m-%d %H:%M:%S %Z")
                    log(f"Expires At: {expiry_str}", "INFO")
                    days_until_expiry = (expiry_dt - now_dt).days
                    if days_until_expiry < 0:
                        log(f"TOKEN EXPIRED {abs(days_until_expiry)} days ago!", "ERROR")
                    elif days_until_expiry == 0:
                        log("TOKEN EXPIRES TODAY!", "WARN")
                    elif days_until_expiry <= 7:
                        log(f"Token expires in {days_until_expiry} days", "WARN")
                    else:
                        log(f"Token expires in {days_until_expiry} days", "INFO")
                except Exception as e:
                    log(f"Expires At: {token_info['expires_at']}", "INFO")
                    log(f"Could not parse expiry date: {e}", "WARN")
            else:
                log("Expires At: Never (no expiration set)", "INFO")
            if token_info["scopes"]:
                log(f"Permissions (Scopes): {', '.join(token_info['scopes'])}", "INFO")
            else:
                log("Permissions (Scopes): None detected (may have full access)", "INFO")
            log("=" * 70, "INFO")
            return {"valid": True, "info": token_info}
        elif response.status_code == 404:
            log("Could not fetch detailed token information", "WARN")
            log("Token missing 'read_api' scope or GitLab version does not support this endpoint", "WARN")
            return {"valid": True, "info": None}
        elif response.status_code == 401:
            log("Token validation failed: Invalid or expired token", "ERROR")
            return {"valid": False, "info": None}
        else:
            log(f"Error validating token: HTTP {response.status_code}", "WARN")
            return {"valid": True, "info": None}
    except Exception as e:
        log(f"Error fetching token information: {e}", "WARN")
        return {"valid": True, "info": None}


def get_pipeline_for_commit(pid, commit_sha):
    try:
        pipelines = api_call(f"projects/{pid}/pipelines?sha={commit_sha}&per_page=1")
        if isinstance(pipelines, list) and len(pipelines) > 0:
            return pipelines[0]
        return None
    except Exception as e:
        log(f"Error fetching pipeline for commit {commit_sha[:8]}: {e}", "ERROR")
        return None


def wait_for_pipeline_completion(pid, pipeline_id, timeout=1800, check_interval=30):
    log(
        f"Waiting for pipeline {pipeline_id} to complete "
        f"(timeout: {timeout}s, checking every {check_interval}s)...",
        "INFO",
    )
    start_time = time.time()
    last_status = None
    with CONSOLE.status(
        f"[cyan]Pipeline {pipeline_id} running...", spinner="dots"
    ) as status:
        while True:
            if INTERRUPTED:
                log("Pipeline wait interrupted by user", "WARN")
                return {"status": "interrupted", "pipeline": None}
            elapsed = time.time() - start_time
            if elapsed > timeout:
                log(f"Pipeline {pipeline_id} timed out after {int(elapsed)}s", "ERROR")
                return {"status": "timeout", "pipeline": None}
            try:
                pipeline = api_call(f"projects/{pid}/pipelines/{pipeline_id}")
                if isinstance(pipeline, dict) and not pipeline.get("error"):
                    current_status = pipeline.get("status", "unknown")
                    if current_status != last_status:
                        log(f"Pipeline {pipeline_id} status: {current_status}", "INFO")
                        last_status = current_status
                    status.update(
                        f"[cyan]Checking GitLab Pipeline {pipeline_id}: "
                        f"[bold]{current_status}[/bold] ({int(elapsed)}s elapsed)"
                    )
                    if current_status in ["success", "failed", "canceled", "skipped"]:
                        return {"status": current_status, "pipeline": pipeline}
                    time.sleep(check_interval)
                else:
                    log(
                        f"Error fetching pipeline {pipeline_id}: "
                        f"{pipeline.get('details', 'unknown')}",
                        "WARN",
                    )
                    time.sleep(check_interval)
            except Exception as e:
                log(f"Exception while monitoring pipeline {pipeline_id}: {e}", "WARN")
                time.sleep(check_interval)


def get_pipeline_jobs(pid, pipeline_id):
    try:
        jobs = api_call(f"projects/{pid}/pipelines/{pipeline_id}/jobs?per_page=100")
        if isinstance(jobs, list):
            return jobs
        return []
    except Exception as e:
        log(f"Error fetching jobs for pipeline {pipeline_id}: {e}", "ERROR")
        return []


def find_job_by_name(jobs, job_name):
    for job in jobs:
        if isinstance(job, dict) and job.get("name") == job_name:
            return job
    return None


def trigger_manual_job(pid, job_id):
    try:
        response = api_call(f"projects/{pid}/jobs/{job_id}/play", method="POST")
        return response
    except Exception as e:
        log(f"Error triggering job {job_id}: {e}", "ERROR")
        return {"error": True, "details": str(e)}


def wait_for_job_completion(pid, job_id, timeout=900, check_interval=15):
    log(f"Waiting for job {job_id} to complete (timeout: {timeout}s)...", "INFO")
    start_time = time.time()
    last_status = None
    with CONSOLE.status(
        f"[cyan]Job {job_id} running...", spinner="dots"
    ) as status:
        while True:
            if INTERRUPTED:
                log("Job wait interrupted by user", "WARN")
                return {"status": "interrupted", "job": None}
            elapsed = time.time() - start_time
            if elapsed > timeout:
                log(f"Job {job_id} timed out after {int(elapsed)}s", "ERROR")
                return {"status": "timeout", "job": None}
            try:
                job = api_call(f"projects/{pid}/jobs/{job_id}")
                if isinstance(job, dict) and not job.get("error"):
                    current_status = job.get("status", "unknown")
                    job_name = job.get("name", "unknown")
                    if current_status != last_status:
                        log(f"Job '{job_name}' (ID: {job_id}) status: {current_status}", "INFO")
                        last_status = current_status
                    status.update(
                        f"[cyan]Checking GitLab Job '{job_name}': "
                        f"[bold]{current_status}[/bold] ({int(elapsed)}s elapsed)"
                    )
                    if current_status in ["success", "failed", "canceled", "skipped"]:
                        return {"status": current_status, "job": job}
                    time.sleep(check_interval)
                else:
                    log(f"Error fetching job {job_id}: {job.get('details', 'unknown')}", "WARN")
                    time.sleep(check_interval)
            except Exception as e:
                log(f"Exception while monitoring job {job_id}: {e}", "WARN")
                time.sleep(check_interval)


def map_tag_to_deploy_job(tag_name):
    tag_lower = tag_name.lower().replace("azure-", "")
    deploy_jobs = {
        "dev":         "eb-deploy-dev-azure",
        "test":        "eb-deploy-test-azure",
        "performance": "eb-deploy-performance-azure",
    }
    return deploy_jobs.get(tag_lower)


def update_parent_block(match):
    block = match.group(0)
    block = re.sub(
        r"<version>.*?</version>",
        f"<version>{TARGET_PARENT_VERSION}</version>",
        block,
    )
    block = re.sub(
        r"(parent-pom-).*?(\.xml)",
        lambda m: m.group(1) + TARGET_PARENT_VERSION + m.group(2),
        block,
    )
    return block


def show_unified_diff(path, old, new):
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    ud = "".join(
        difflib.unified_diff(
            old_lines, new_lines, fromfile=path, tofile=f"{path} (new)", lineterm=""
        )
    )
    return ud


def prompt_yes_no(prompt, default=False):
    result = button_dialog(
        title="Confirm",
        text=prompt,
        buttons=[("Yes", True), ("No", False)],
        style=DIALOG_STYLE,
    ).run()
    if result is None:
        return default
    return result


def create_feature_branch(pid, p_name):
    try:
        log(
            f"Creating feature branch '{FEATURE_BRANCH}' from '{SOURCE_BRANCH}' "
            f"for project {p_name}...",
            "INFO",
        )
        with CONSOLE.status(
            f"[cyan]Creating branch {FEATURE_BRANCH} for {p_name}...", spinner="dots"
        ):
            result = api_call(
                f"projects/{pid}/repository/branches",
                "POST",
                {"branch": FEATURE_BRANCH, "ref": SOURCE_BRANCH},
            )
        if isinstance(result, dict) and not result.get("error"):
            log(f"Feature branch '{FEATURE_BRANCH}' created successfully for {p_name}", "INFO")
            return True
        else:
            error_detail = (
                result.get("details", "unknown error") if isinstance(result, dict) else "unknown error"
            )
            log(
                f"Failed to create feature branch '{FEATURE_BRANCH}' for {p_name}: {error_detail}",
                "ERROR",
            )
            if "already exists" in str(error_detail).lower():
                log(
                    f"Branch '{FEATURE_BRANCH}' already exists for {p_name}, using existing branch",
                    "INFO",
                )
                return True
            return False
    except Exception as e:
        log(f"Exception while creating feature branch for {p_name}: {e}", "ERROR")
        return False


def fetch_tags_for_project(pid, per_page=100, page=1):
    endpoint = f"projects/{pid}/repository/tags?per_page={per_page}&page={page}"
    return api_call(endpoint)


def fetch_all_tags_for_project(pid, per_page=100):
    page = 1
    all_tags = []
    while True:
        try:
            resp = fetch_tags_for_project(pid, per_page=per_page, page=page)
        except Exception as e:
            return {"error": True, "details": str(e)}
        if isinstance(resp, dict) and resp.get("error"):
            return resp
        if not resp:
            break
        all_tags.extend(resp if isinstance(resp, list) else [])
        if len(resp) < per_page:
            break
        page += 1
    for tag in all_tags:
        if isinstance(tag, dict) and tag.get("protected"):
            tag_name = tag.get("name", "unknown")
            log(f"Tag '{tag_name}' is PROTECTED", "WARN")
    return all_tags


def filter_and_sort_deployment_tags(tags):
    if isinstance(tags, dict) and tags.get("error"):
        return {"error": True, "details": tags.get("details")}
    tag_list = tags if isinstance(tags, list) else []
    tag_categories = {
        "dev":         {"priority": 1, "variants": ["dev", "azure-dev"],                 "found": []},
        "test":        {"priority": 2, "variants": ["test", "azure-test"],                "found": []},
        "performance": {"priority": 3, "variants": ["performance", "azure-performance"],  "found": []},
    }
    for tag in tag_list:
        if not isinstance(tag, dict):
            continue
        tag_name = tag.get("name", "").lower()
        for category, info in tag_categories.items():
            if tag_name in info["variants"]:
                info["found"].append(tag)
                break
    sorted_tags = []
    found_categories = {}
    missing_categories = []
    for category, info in sorted(tag_categories.items(), key=lambda x: x[1]["priority"]):
        if info["found"]:
            sorted_tags.extend(info["found"])
            found_categories[category] = [t.get("name") for t in info["found"]]
        else:
            missing_categories.append(category)
    return {
        "sorted_tags":       sorted_tags,
        "found_categories":  found_categories,
        "missing_categories": missing_categories,
    }


def parse_version(vstr):
    m = re.search(r"(\d+(?:\.\d+)*)", vstr or "")
    if not m:
        return ()
    return tuple(int(x) for x in m.group(1).split("."))


def find_parent_version_in_pom(content):
    m = re.search(r"<parent>[\s\S]*?<version>(.*?)</version>[\s\S]*?</parent>", content, re.I)
    if m:
        return m.group(1).strip()
    m2 = re.search(r"<version>(.*?)</version>", content, re.I)
    if m2:
        return m2.group(1).strip()
    return None


def check_files_already_match(pid, actions, branch_ref):
    all_match = True
    details = []
    for act in actions:
        file_path = act["file_path"]
        desired_content = act["content"]
        quoted_path = quote(file_path, safe="")
        try:
            current_res = api_call(
                f"projects/{pid}/repository/files/{quoted_path}"
                f"?ref={quote(branch_ref, safe='')}"
            )
            if isinstance(current_res, dict) and "content" in current_res:
                current_content = base64.b64decode(current_res["content"]).decode("utf-8")
                if current_content == desired_content:
                    details.append({"file": file_path, "match": True})
                else:
                    details.append({"file": file_path, "match": False, "reason": "content differs"})
                    all_match = False
            else:
                details.append({"file": file_path, "match": False, "reason": "file not found or error"})
                all_match = False
        except Exception as e:
            details.append({"file": file_path, "match": False, "reason": f"error: {str(e)}"})
            all_match = False
    return all_match, details


def get_file_metadata(pid, file_path, branch_ref):
    quoted_path = quote(file_path, safe="")
    try:
        file_info = api_call(
            f"projects/{pid}/repository/files/{quoted_path}"
            f"?ref={quote(branch_ref, safe='')}"
        )
        if isinstance(file_info, dict) and not file_info.get("error"):
            metadata = {
                "file_path":       file_path,
                "last_commit_sha": file_info.get("last_commit_id", "unknown"),
                "last_commit_date": "unknown",
                "last_modified_by": "unknown",
                "commit_message":  "unknown",
            }
            commit_sha = file_info.get("last_commit_id")
            if commit_sha:
                try:
                    commit_info = api_call(f"projects/{pid}/repository/commits/{commit_sha}")
                    if isinstance(commit_info, dict) and not commit_info.get("error"):
                        metadata["last_commit_date"]  = commit_info.get("committed_date", "unknown")
                        metadata["last_modified_by"]  = commit_info.get("author_name", "unknown")
                        metadata["commit_message"]    = commit_info.get("message", "unknown").split("\n")[0]
                except Exception as e:
                    log(f"Could not fetch commit details for {file_path}: {e}", "DEBUG")
            return metadata
        else:
            return None
    except Exception as e:
        log(f"Error getting metadata for {file_path}: {e}", "DEBUG")
        return None


def extract_changed_line_numbers(unified_diff):
    changed_lines = set()
    current_line = 0
    for line in unified_diff:
        if line.startswith("@@"):
            match = re.search(r"@@ -(\d+),?(\d*) \+(\d+),?(\d*) @@", line)
            if match:
                current_line = int(match.group(1))
        elif line.startswith("-"):
            changed_lines.add(current_line)
            current_line += 1
        elif line.startswith("+"):
            changed_lines.add(current_line)
        elif line.startswith(" "):
            current_line += 1
    return changed_lines


def detect_conflicts(pid, file_path, base_content, our_content, remote_ref):
    quoted_path = quote(file_path, safe="")
    remote_res = api_call(
        f"projects/{pid}/repository/files/{quoted_path}"
        f"?ref={quote(remote_ref, safe='')}"
    )
    if isinstance(remote_res, dict) and remote_res.get("error"):
        return None
    if "content" not in remote_res:
        return None
    remote_content = base64.b64decode(remote_res["content"]).decode("utf-8")
    if remote_content == base_content:
        return None
    if remote_content == our_content:
        log(f"Remote {file_path} already contains our changes.", "INFO")
        return {"type": "already_applied", "file": file_path}
    base_lines   = base_content.splitlines(keepends=True)
    our_lines    = our_content.splitlines(keepends=True)
    remote_lines = remote_content.splitlines(keepends=True)
    our_diff    = list(difflib.unified_diff(base_lines, our_lines,    lineterm=""))
    remote_diff = list(difflib.unified_diff(base_lines, remote_lines, lineterm=""))
    our_changed_lines    = extract_changed_line_numbers(our_diff)
    remote_changed_lines = extract_changed_line_numbers(remote_diff)
    overlap = our_changed_lines.intersection(remote_changed_lines)
    if overlap:
        return {
            "type":              "conflict",
            "file":              file_path,
            "base_content":      base_content,
            "our_content":       our_content,
            "remote_content":    remote_content,
            "overlapping_lines": sorted(overlap),
        }
    else:
        return {
            "type":           "non_overlapping",
            "file":           file_path,
            "base_content":   base_content,
            "our_content":    our_content,
            "remote_content": remote_content,
        }


def attempt_three_way_merge(base_content, our_content, remote_content):
    if remote_content == base_content:
        return our_content
    if our_content == base_content:
        return remote_content
    return None


def handle_conflict(conflict_info, pid, p_name):
    file_path = conflict_info["file"]
    if conflict_info["type"] == "already_applied":
        log(f"File {file_path} already has our changes on remote. Skipping.", "INFO")
        return "skip"
    if conflict_info["type"] == "non_overlapping":
        log(f"File {file_path} has non-overlapping changes. Attempting automatic merge...", "INFO")
        merged = attempt_three_way_merge(
            conflict_info["base_content"],
            conflict_info["our_content"],
            conflict_info["remote_content"],
        )
        if merged:
            log(f"Successfully merged {file_path}.", "INFO")
            return merged
        else:
            log(f"Automatic merge failed for {file_path}.", "WARN")
    if conflict_info["type"] == "conflict":
        CONSOLE.log(f"[bold red]CONFLICT DETECTED in {file_path}[/bold red]")
        CONSOLE.log(f"Overlapping line numbers: {conflict_info.get('overlapping_lines', 'unknown')}")
        CONSOLE.print("\n[bold yellow]--- OUR CHANGES ---[/bold yellow]")
        CONSOLE.print(show_unified_diff(file_path, conflict_info["base_content"], conflict_info["our_content"]))
        CONSOLE.print("\n[bold yellow]--- REMOTE CHANGES ---[/bold yellow]")
        CONSOLE.print(show_unified_diff(file_path, conflict_info["base_content"], conflict_info["remote_content"]))
    resolution = radiolist_dialog(
        title=f"Conflict Resolution: {file_path}",
        text=(
            f"A conflict was detected in {file_path} for project {p_name}.\n"
            "Select how to resolve it:"
        ),
        values=[
            ("ours",   "Use our version  (overwrite remote changes)"),
            ("theirs", "Use remote version  (discard our changes)"),
            ("skip",   "Skip this file"),
            ("abort",  "Abort entire operation for this project"),
        ],
        style=DIALOG_STYLE,
    ).run()
    if resolution is None or resolution == "abort":
        log(f"Aborting operation for project {p_name}", "WARN")
        raise Exception("User aborted due to conflict")
    log(f"Conflict resolution for {file_path}: {resolution}", "INFO")
    return resolution


def has_changes_between_branches(pid, p_name):
    compare_endpoint = (
        f"projects/{pid}/repository/compare"
        f"?from={quote(SOURCE_BRANCH, safe='')}"
        f"&to={quote(FEATURE_BRANCH, safe='')}"
    )
    try:
        with CONSOLE.status(
            f"[cyan]Checking changes between {SOURCE_BRANCH} and {FEATURE_BRANCH} "
            f"for {p_name}...",
            spinner="dots",
        ):
            result = api_call(compare_endpoint)
        if isinstance(result, dict) and not result.get("error"):
            commits = result.get("commits", [])
            if not commits:
                log("No changes detected; skipping MR", "INFO")
                return False
            log(
                f"Detected {len(commits)} commit(s) between {SOURCE_BRANCH} "
                f"and {FEATURE_BRANCH} for {p_name}",
                "INFO",
            )
            return True
        else:
            log(f"Compare API error for {p_name}: {result.get('details', 'unknown')}", "WARN")
            return True
    except Exception as e:
        log(f"Could not compare branches for {p_name}: {e}", "WARN")
        return True


def create_mr_for_project(pid, p_name, rollback_data):
    log(f"--- Creating MR for: {p_name} (project id: {pid}) ---", "INFO")
    if INTERRUPTED:
        log("MR creation interrupted", "WARN")
        return {"success": False, "idempotent": False, "url": None}
    with CONSOLE.status(
        f"[cyan]Checking feature branch for {p_name}...", spinner="dots"
    ):
        br_check = api_call(
            f"projects/{pid}/repository/branches/{quote(FEATURE_BRANCH, safe='')}"
        )
    if not (isinstance(br_check, dict) and "name" in br_check):
        log(f"Feature branch '{FEATURE_BRANCH}' does not exist for {p_name}; skipping MR", "WARN")
        return {"success": False, "idempotent": False, "url": None, "skipped_no_branch": True}
    log(f"Checking if MR already exists for {FEATURE_BRANCH}...", "INFO")
    try:
        with CONSOLE.status(
            f"[cyan]Checking existing MRs for {p_name}...", spinner="dots"
        ):
            existing = api_call(
                f"projects/{pid}/merge_requests"
                f"?state=opened&source_branch={quote(FEATURE_BRANCH, safe='')}"
            )
    except Exception as e:
        existing = {"error": True, "details": str(e)}
    has_open_mr = isinstance(existing, list) and len(existing) > 0
    if isinstance(existing, dict) and existing.get("error"):
        log(f"Could not verify existing MRs for {p_name}: {existing.get('details')}", "ERROR")
        return {"success": False, "idempotent": False, "url": None}
    if has_open_mr:
        try:
            existing_mr  = existing[0]
            existing_url = existing_mr.get("web_url", "unknown")
            merge_status = existing_mr.get("merge_status", "")
            if merge_status == "cannot_be_merged":
                log(f"Existing MR has conflicts and cannot be merged: {existing_url}", "WARN")
                log(f"Manual conflict resolution is required for {p_name}", "WARN")
                return {"success": True, "idempotent": True, "url": existing_url, "mr_conflict": True}
            log(f"[IDEMPOTENT] MR already exists: {existing_url}", "INFO")
            return {"success": True, "idempotent": True, "url": existing_url}
        except Exception:
            log("[IDEMPOTENT] MR already exists (could not parse response).", "INFO")
            return {"success": True, "idempotent": True, "url": None}
    if not has_changes_between_branches(pid, p_name):
        return {"success": False, "idempotent": False, "url": None, "skipped_empty": True}
    log(f"Creating new MR for {p_name}...", "INFO")
    mr_payload = {
        "source_branch": FEATURE_BRANCH,
        "target_branch": SOURCE_BRANCH,
        "title":         MR_TITLE,
    }
    if REVIEWER_USERNAMES:
        log(f"Resolving reviewer usernames: {', '.join(REVIEWER_USERNAMES)}", "INFO")
        reviewer_ids = resolve_usernames_to_ids(REVIEWER_USERNAMES)
        if reviewer_ids:
            mr_payload["reviewer_ids"] = reviewer_ids
            log(f"Adding reviewers (IDs): {reviewer_ids}", "INFO")
        else:
            log("Could not resolve any reviewer usernames to IDs", "WARN")
    if ASSIGNEE_USERNAMES:
        log(f"Resolving assignee usernames: {', '.join(ASSIGNEE_USERNAMES)}", "INFO")
        assignee_ids = resolve_usernames_to_ids(ASSIGNEE_USERNAMES)
        if assignee_ids:
            mr_payload["assignee_ids"] = assignee_ids
            log(f"Adding assignees (IDs): {assignee_ids}", "INFO")
        else:
            log("Could not resolve any assignee usernames to IDs", "WARN")
    with CONSOLE.status(f"[cyan]Submitting MR for {p_name}...", spinner="dots"):
        mr_result = api_call(f"projects/{pid}/merge_requests", "POST", mr_payload)
    if isinstance(mr_result, dict) and not mr_result.get("error"):
        mr_url = mr_result.get("web_url", "N/A")
        log(f"MR created: {mr_url}", "INFO")
        if mr_result.get("reviewers"):
            reviewer_names = [r.get("name", "Unknown") for r in mr_result.get("reviewers", [])]
            log(f"Reviewers assigned: {', '.join(reviewer_names)}", "INFO")
        if mr_result.get("assignees"):
            assignee_names = [a.get("name", "Unknown") for a in mr_result.get("assignees", [])]
            log(f"Assignees assigned: {', '.join(assignee_names)}", "INFO")
        return {"success": True, "idempotent": False, "url": mr_url}
    else:
        error_detail = mr_result.get("details", "unknown error")
        log(f"Failed to create MR: {error_detail}", "ERROR")
        if "reviewer" in error_detail.lower() or "assignee" in error_detail.lower():
            log(
                "Check if REVIEWER_USERNAMES and ASSIGNEE_USERNAMES in .env are valid usernames",
                "WARN",
            )
        return {"success": False, "idempotent": False, "url": None}


def process_project(pid, choices=None, show_full=True, rollback_data=None, mode="full", state=None):
    if INTERRUPTED:
        log("Processing interrupted before starting project", "WARN")
        return {"project_id": pid, "success": False, "interrupted": True}
    project_start_time = time.time()
    with CONSOLE.status(f"[cyan]Fetching project info for ID {pid}...", spinner="dots"):
        project_info = api_call(f"projects/{pid}")
    p_name = project_info.get("name", f"ID:{pid}") if isinstance(project_info, dict) else f"ID:{pid}"
    log(f"--- Processing: {p_name} (project id: {pid}) ---", "INFO")
    result = {
        "project_id":      pid,
        "project_name":    p_name,
        "success":         False,
        "committed":       False,
        "deployed":        False,
        "mr_created":      False,
        "skipped_mr":      False,
        "mr_conflict":     False,
        "error":           None,
        "interrupted":     False,
        "idempotent_commit": False,
        "idempotent_mr":   False,
        "idempotent_tags": [],
    }
    try:
        if mode == "mr_only":
            mr_result = create_mr_for_project(pid, p_name, rollback_data)
            result["mr_created"]    = mr_result["success"]
            result["idempotent_mr"] = mr_result.get("idempotent", False)
            result["mr_conflict"]   = mr_result.get("mr_conflict", False)
            is_skipped = mr_result.get("skipped_empty") or mr_result.get("skipped_no_branch")
            result["skipped_mr"] = bool(is_skipped)
            if is_skipped:
                result["success"] = True
            else:
                result["success"] = mr_result["success"]
            if state is not None:
                if is_skipped:
                    state["skipped_projects"].append(pid)
                elif mr_result["success"]:
                    state["completed_projects"].append(pid)
                else:
                    state["failed_projects"].append(pid)
                save_state(state)
            return result

        actions = []
        with CONSOLE.status(
            f"[cyan]Checking branches for {p_name}...", spinner="dots"
        ):
            br_check = api_call(
                f"projects/{pid}/repository/branches/{quote(FEATURE_BRANCH, safe='')}"
            )
        current_ref = (
            FEATURE_BRANCH
            if isinstance(br_check, dict) and "name" in br_check
            else SOURCE_BRANCH
        )

        if mode == "full" and choices:
            if INTERRUPTED:
                log("Interrupted during file preparation", "WARN")
                result["interrupted"] = True
                return result

            with CONSOLE.status(
                f"[cyan]Fetching repository files for {p_name}...", spinner="dots"
            ):
                if "1" in choices:
                    res = api_call(
                        f"projects/{pid}/repository/files/"
                        f"{quote('pom.xml', safe='')}?ref={quote(current_ref, safe='')}"
                    )
                    if isinstance(res, dict) and res.get("error"):
                        log(f"Failed to fetch pom.xml for {p_name}: {res.get('details')}", "ERROR")
                    elif "content" in res:
                        orig = base64.b64decode(res["content"]).decode("utf-8")
                        upd = re.sub(
                            r"<(java\.version|maven\.compiler\.(source|target|release))>11</\1>",
                            r"<\1>17</\1>",
                            orig,
                        )
                        if "<parent>" in upd:
                            upd = re.sub(r"<parent>[\s\S]*?</parent>", update_parent_block, upd)
                        if orig != upd:
                            actions.append(
                                {"action": "update", "file_path": "pom.xml", "content": upd, "old_content": orig}
                            )
                if "2" in choices:
                    res = api_call(
                        f"projects/{pid}/repository/files/"
                        f"{quote('.gitlab-ci.yml', safe='')}?ref={quote(current_ref, safe='')}"
                    )
                    if isinstance(res, dict) and res.get("error"):
                        log(f"Failed to fetch .gitlab-ci.yml for {p_name}: {res.get('details')}", "ERROR")
                    elif "content" in res:
                        orig = base64.b64decode(res["content"]).decode("utf-8")
                        upd = re.sub(r"^\s*image:.*(\n|$)", "", orig, flags=re.MULTILINE)
                        if orig != upd:
                            actions.append(
                                {"action": "update", "file_path": ".gitlab-ci.yml", "content": upd, "old_content": orig}
                            )
                if "3" in choices:
                    path = quote(".elasticbeanstalk/config.yml", safe="")
                    res = api_call(
                        f"projects/{pid}/repository/files/{path}"
                        f"?ref={quote(current_ref, safe='')}"
                    )
                    if isinstance(res, dict) and res.get("error"):
                        log(
                            f"Failed to fetch .elasticbeanstalk/config.yml for {p_name}: "
                            f"{res.get('details')}",
                            "ERROR",
                        )
                    elif "content" in res:
                        orig = base64.b64decode(res["content"]).decode("utf-8")
                        if not NEW_DEFAULT_PLATFORM:
                            log("NEW_DEFAULT_PLATFORM not configured in .env file", "ERROR")
                            result["error"] = "NEW_DEFAULT_PLATFORM not configured"
                            if state is not None:
                                state["failed_projects"].append(pid)
                                save_state(state)
                            return result
                        upd = re.sub(
                            r"(default_platform:\s*).*$",
                            f"default_platform: {NEW_DEFAULT_PLATFORM}",
                            orig,
                            flags=re.MULTILINE,
                        )
                        if orig != upd:
                            actions.append(
                                {
                                    "action": "update",
                                    "file_path": ".elasticbeanstalk/config.yml",
                                    "content": upd,
                                    "old_content": orig,
                                }
                            )

            if actions:
                CONSOLE.log(f"\n[bold]PREVIEW: Proposed changes for {p_name}[/bold]")
                log("Fetching file metadata...", "INFO")
                for action in actions:
                    with CONSOLE.status(
                        f"[cyan]Fetching metadata for {action['file_path']}...", spinner="dots"
                    ):
                        metadata = get_file_metadata(pid, action["file_path"], current_ref)
                    if metadata:
                        CONSOLE.log(f"\n[cyan]File:[/cyan] {action['file_path']}")
                        CONSOLE.log(f"  Last modified by: {metadata['last_modified_by']}")
                        sha = metadata["last_commit_sha"]
                        CONSOLE.log(f"  Last commit: {sha[:8] if sha != 'unknown' else 'unknown'}")
                        CONSOLE.log(f"  Commit date: {metadata['last_commit_date']}")
                        CONSOLE.log(f"  Commit message: {metadata['commit_message'][:60]}...")
                    else:
                        CONSOLE.log(f"\n[cyan]File:[/cyan] {action['file_path']}")
                        CONSOLE.log("  (Metadata unavailable)")

                for action in actions:
                    if show_full:
                        ud = show_unified_diff(action["file_path"], action["old_content"], action["content"])
                        if ud.strip():
                            CONSOLE.print(f"\n[bold]--- Diff for {action['file_path']} ---[/bold]")
                            CONSOLE.print(ud)
                            CONSOLE.print(f"[bold]--- End diff for {action['file_path']} ---[/bold]\n")
                        else:
                            CONSOLE.log(f"  {action['file_path']} -> [+0 | -0 lines] (no textual diff)")
                    else:
                        diff = list(
                            difflib.ndiff(
                                action["old_content"].splitlines(),
                                action["content"].splitlines(),
                            )
                        )
                        added   = len([l for l in diff if l.startswith("+ ")])
                        removed = len([l for l in diff if l.startswith("- ")])
                        CONSOLE.log(f"  {action['file_path']} -> [+{added} | -{removed} lines]")

                if not prompt_yes_no(f"Commit these changes for {p_name}?", default=True):
                    log(f"User skipped commit for {p_name}", "INFO")
                    if state is not None:
                        state["skipped_projects"].append(pid)
                        save_state(state)
                    return result

                if INTERRUPTED:
                    log("Interrupted before commit", "WARN")
                    result["interrupted"] = True
                    return result

                if rollback_data is not None:
                    files_to_backup = [action["file_path"] for action in actions]
                    with CONSOLE.status(
                        f"[cyan]Creating rollback snapshot for {p_name}...", spinner="dots"
                    ):
                        snapshot = create_rollback_snapshot(pid, p_name, current_ref, files_to_backup)
                    rollback_data["snapshots"].append(snapshot)
                    log(f"Created rollback snapshot for {p_name}", "DEBUG")

                if not (isinstance(br_check, dict) and "name" in br_check):
                    branch_created = create_feature_branch(pid, p_name)
                    if not branch_created:
                        log(f"Cannot proceed without feature branch for {p_name}", "ERROR")
                        result["error"] = "Failed to create feature branch"
                        if state is not None:
                            state["failed_projects"].append(pid)
                            save_state(state)
                        return result

                log(f"Checking for conflicts on {FEATURE_BRANCH}...", "INFO")
                conflicts_detected = []
                for act in actions:
                    file_path    = act["file_path"]
                    base_content = act["old_content"]
                    our_content  = act["content"]
                    with CONSOLE.status(
                        f"[cyan]Checking conflicts for {file_path}...", spinner="dots"
                    ):
                        conflict = detect_conflicts(
                            pid, file_path, base_content, our_content, FEATURE_BRANCH
                        )
                    if conflict:
                        conflicts_detected.append((act, conflict))

                if conflicts_detected:
                    log(f"Found {len(conflicts_detected)} file(s) with potential conflicts.", "WARN")
                    for act, conflict in conflicts_detected:
                        try:
                            resolution = handle_conflict(conflict, pid, p_name)
                            if resolution == "skip":
                                actions = [a for a in actions if a["file_path"] != act["file_path"]]
                            elif resolution == "ours":
                                pass
                            elif resolution == "theirs":
                                actions = [a for a in actions if a["file_path"] != act["file_path"]]
                            elif isinstance(resolution, str) and resolution not in ["skip", "ours", "theirs"]:
                                for a in actions:
                                    if a["file_path"] == act["file_path"]:
                                        a["content"] = resolution
                                        break
                        except Exception as e:
                            log(f"Conflict handling aborted: {e}", "ERROR")
                            result["error"] = str(e)
                            if state is not None:
                                state["failed_projects"].append(pid)
                                save_state(state)
                            return result
                else:
                    log("No conflicts detected.", "INFO")

                if not actions:
                    log(f"No remaining changes to commit for {p_name} after conflict resolution.", "INFO")
                else:
                    log(f"Checking if changes already exist on {FEATURE_BRANCH}...", "INFO")
                    with CONSOLE.status(
                        f"[cyan]Verifying file state on {FEATURE_BRANCH}...", spinner="dots"
                    ):
                        files_already_match, match_details = check_files_already_match(
                            pid, actions, FEATURE_BRANCH
                        )
                    if files_already_match:
                        log(
                            f"[IDEMPOTENT] All files already match desired state on "
                            f"{FEATURE_BRANCH}. Skipping commit.",
                            "INFO",
                        )
                        for detail in match_details:
                            if detail["match"]:
                                log(f"  OK {detail['file']} already matches", "DEBUG")
                        result["committed"]       = True
                        result["idempotent_commit"] = True
                    else:
                        log(f"Files need updating on {FEATURE_BRANCH}:", "INFO")
                        for detail in match_details:
                            if not detail["match"]:
                                log(
                                    f"  X {detail['file']}: {detail.get('reason', 'needs update')}",
                                    "DEBUG",
                                )
                        commit_payload = {
                            "branch":         FEATURE_BRANCH,
                            "commit_message": f"fix: {UPGRADE_TYPE}",
                            "actions": [
                                {
                                    "action":    act["action"],
                                    "file_path": act["file_path"],
                                    "content":   act["content"],
                                }
                                for act in actions
                            ],
                        }
                        with CONSOLE.status(
                            f"[cyan]Committing changes for {p_name}...", spinner="dots"
                        ):
                            commit_resp = api_call(
                                f"projects/{pid}/repository/commits", "POST", commit_payload
                            )
                        if isinstance(commit_resp, dict) and not commit_resp.get("error"):
                            log(f"Changes committed for {p_name}", "INFO")
                            result["committed"] = True
                        else:
                            log(
                                f"Failed to commit changes: {commit_resp.get('details', 'unknown')}",
                                "ERROR",
                            )
                            result["error"] = "Commit failed"
                            if AUTO_ROLLBACK_ON_FAILURE and rollback_data and rollback_data.get("snapshots"):
                                log(f"[AUTO-ROLLBACK] Rolling back changes for {p_name}...", "WARN")
                                rollback_file = save_rollback_data(rollback_data)
                                perform_rollback(rollback_file)
                            if state is not None:
                                state["failed_projects"].append(pid)
                                save_state(state)
                            return result
            else:
                log(f"No file changes needed for {p_name}", "INFO")

        if INTERRUPTED:
            log("Interrupted before next step selection", "WARN")
            result["interrupted"] = True
            return result

        if mode == "full" or mode == "deploy_only":
            next_step = radiolist_dialog(
                title=f"Next Step: {p_name}",
                text=f"What would you like to do next for [b]{p_name}[/b]?",
                values=[
                    ("deploy", "Deploy  (create tags + trigger deployment pipeline)"),
                    ("mr",     "Create Merge Request"),
                    ("skip",   "Skip  (do nothing for this project)"),
                ],
                style=DIALOG_STYLE,
            ).run()
            if next_step is None:
                log(f"Next step cancelled; skipping {p_name}", "WARN")
                result["interrupted"] = True
                return result
            if next_step == "mr":
                mr_result = create_mr_for_project(pid, p_name, rollback_data)
                result["mr_created"]    = mr_result["success"]
                result["idempotent_mr"] = mr_result.get("idempotent", False)
                result["mr_conflict"]   = mr_result.get("mr_conflict", False)
                is_skipped = mr_result.get("skipped_empty") or mr_result.get("skipped_no_branch")
                result["skipped_mr"] = bool(is_skipped)
                if is_skipped:
                    result["success"] = result["committed"] or True
                else:
                    result["success"] = result["committed"] or mr_result["success"]
            elif next_step == "skip":
                log(f"Skipping deployment/MR for {p_name}", "INFO")
                result["success"] = result["committed"]
            else:
                deploy_result = handle_deployment(pid, p_name, state)
                result["deployed"]        = deploy_result["success"]
                result["idempotent_tags"] = deploy_result["idempotent_tags"]
                result["success"]         = result["committed"] or deploy_result["success"]

        if state is not None:
            if result["success"]:
                state["completed_projects"].append(pid)
            else:
                state["failed_projects"].append(pid)
            save_state(state)

        project_end_time = time.time()
        project_duration = project_end_time - project_start_time
        minutes = int(project_duration // 60)
        seconds = int(project_duration % 60)
        if minutes > 0:
            log(f"Completed {p_name} in {minutes}m {seconds}s", "INFO")
        else:
            log(f"Completed {p_name} in {seconds}s", "INFO")
        return result

    except Exception as e:
        log(f"Exception processing {p_name}: {e}", "ERROR")
        result["error"] = str(e)
        if AUTO_ROLLBACK_ON_FAILURE and rollback_data and rollback_data.get("snapshots"):
            log(f"[AUTO-ROLLBACK] Rolling back changes for {p_name}...", "WARN")
            rollback_file = save_rollback_data(rollback_data)
            perform_rollback(rollback_file)
        if state is not None:
            state["failed_projects"].append(pid)
            save_state(state)
        return result


def handle_deployment(pid, p_name, state=None):
    log(f"Starting deployment for {p_name}...", "INFO")
    if INTERRUPTED:
        log("Deployment interrupted", "WARN")
        return {"success": False, "idempotent_tags": []}

    with CONSOLE.status(f"[cyan]Fetching tags for {p_name}...", spinner="dots"):
        tags_resp = fetch_all_tags_for_project(pid)

    if isinstance(tags_resp, dict) and tags_resp.get("error"):
        log(f"Could not fetch tags for project {p_name}: {tags_resp.get('details')}", "ERROR")
        return {"success": False, "idempotent_tags": []}

    available_tags_all = tags_resp if isinstance(tags_resp, list) else []
    filter_result = filter_and_sort_deployment_tags(available_tags_all)
    if isinstance(filter_result, dict) and filter_result.get("error"):
        log(f"Error filtering tags: {filter_result.get('details')}", "ERROR")
        return {"success": False, "idempotent_tags": []}

    available_tags    = filter_result["sorted_tags"]
    found_categories  = filter_result["found_categories"]
    missing_categories = filter_result["missing_categories"]

    log(f"Deployment tags found for {p_name}:", "INFO")
    if found_categories:
        for category, tag_names in found_categories.items():
            log(f"  {category.upper()}: {', '.join(tag_names)}", "INFO")
    if missing_categories:
        for category in missing_categories:
            log(f"  {category.upper()}: NOT FOUND", "WARN")

    if not available_tags:
        log(f"No deployment tags found for {p_name}. Skipping deployment.", "WARN")
        return {"success": False, "idempotent_tags": []}

    tag_values = []
    for t in available_tags:
        if not isinstance(t, dict):
            continue
        commit_id        = (t.get("commit") or {}).get("id", "")
        protected_marker = "  [PROTECTED]" if t.get("protected") else ""
        label = (
            f"{t.get('name', '')}{protected_marker}  "
            f"{commit_id[:8] if commit_id else ''}"
        )
        tag_values.append((t.get("name"), label))

    selected_tag_names = checkboxlist_dialog(
        title=f"Tag Selection: {p_name}",
        text=f"Select deployment tags for [b]{p_name}[/b] (Space to toggle, Enter to confirm):",
        values=tag_values,
        style=DIALOG_STYLE,
    ).run()

    if not selected_tag_names:
        log("No tags selected; skipping deployment.", "INFO")
        return {"success": False, "idempotent_tags": []}

    if state and str(pid) in state.get("project_details", {}):
        completed_tags = state["project_details"][str(pid)].get("completed_tags", [])
        if completed_tags:
            log(
                f"Found {len(completed_tags)} already completed tag(s) for this project: "
                f"{', '.join(completed_tags)}",
                "INFO",
            )
            original_count     = len(selected_tag_names)
            selected_tag_names = [tag for tag in selected_tag_names if tag not in completed_tags]
            skipped_count      = original_count - len(selected_tag_names)
            if skipped_count > 0:
                log(f"Skipping {skipped_count} already completed tag(s) from selection", "INFO")
            if not selected_tag_names:
                log("All selected tags already completed. Nothing to do.", "INFO")
                return {"success": True, "idempotent_tags": completed_tags}

    with CONSOLE.status(
        f"[cyan]Fetching branch info for {FEATURE_BRANCH}...", spinner="dots"
    ):
        branch_info = api_call(
            f"projects/{pid}/repository/branches/{quote(FEATURE_BRANCH, safe='')}"
        )
    feature_head = None
    if isinstance(branch_info, dict) and not branch_info.get("error"):
        feature_head = (branch_info.get("commit") or {}).get("id")

    deployment_successful = False
    idempotent_tags = []

    for tag_name in selected_tag_names:
        if INTERRUPTED:
            log("Deployment interrupted during tag processing", "WARN")
            break

        log(f"Processing tag: {tag_name}", "INFO")
        quoted_tag = quote(tag_name, safe="")
        tag_obj = next(
            (t for t in available_tags_all if isinstance(t, dict) and t.get("name") == tag_name),
            None,
        )

        if tag_obj and tag_obj.get("protected"):
            log(f"Skipping PROTECTED tag '{tag_name}'", "ERROR")
            continue

        tag_commit_id = (tag_obj.get("commit") or {}).get("id") if tag_obj else None

        if tag_commit_id and feature_head and tag_commit_id == feature_head:
            log(
                f"[IDEMPOTENT] Tag '{tag_name}' already points to feature branch HEAD "
                f"({feature_head[:8]}). Skipping tag recreation.",
                "INFO",
            )
            idempotent_tags.append(tag_name)
            log(f"Tag '{tag_name}' is already correct. Proceeding with deployment check...", "INFO")
            created_tag_commit = tag_commit_id
        else:
            if tag_obj:
                log(
                    f"Deleting existing tag '{tag_name}' "
                    f"(currently at {tag_commit_id[:8] if tag_commit_id else 'unknown'})...",
                    "INFO",
                )
                with CONSOLE.status(
                    f"[cyan]Deleting tag {tag_name}...", spinner="dots"
                ):
                    api_call(f"projects/{pid}/repository/tags/{quoted_tag}", method="DELETE")

            log(
                f"Creating tag '{tag_name}' on {FEATURE_BRANCH} "
                f"({feature_head[:8] if feature_head else 'unknown'})...",
                "INFO",
            )
            with CONSOLE.status(
                f"[cyan]Creating tag {tag_name} on {FEATURE_BRANCH}...", spinner="dots"
            ):
                t_res = api_call(
                    f"projects/{pid}/repository/tags",
                    "POST",
                    {"tag_name": tag_name, "ref": FEATURE_BRANCH},
                )
            if isinstance(t_res, dict) and not t_res.get("error"):
                log(f"Created tag '{tag_name}' on {FEATURE_BRANCH}", "INFO")
                created_tag_commit = (
                    (t_res.get("commit") or {}).get("id") if isinstance(t_res, dict) else None
                )
            else:
                log(f"Failed to create tag '{tag_name}': {t_res.get('details')}", "ERROR")
                continue

        if created_tag_commit:
            log("Waiting 10 seconds for pipeline to be triggered...", "INFO")
            with CONSOLE.status("[cyan]Waiting for pipeline trigger...", spinner="dots"):
                time.sleep(10)
            if INTERRUPTED:
                log("Deployment interrupted during pipeline wait", "WARN")
                break

            with CONSOLE.status(
                f"[cyan]Looking for pipeline on commit {created_tag_commit[:8]}...",
                spinner="dots",
            ):
                pipeline = get_pipeline_for_commit(pid, created_tag_commit)

            if not pipeline:
                log(f"No pipeline found for commit {created_tag_commit[:8]}", "WARN")
                continue

            pipeline_id = pipeline.get("id")
            log(f"Found pipeline {pipeline_id}", "INFO")

            pipeline_result = wait_for_pipeline_completion(
                pid, pipeline_id, timeout=1800, check_interval=30
            )
            if pipeline_result["status"] == "interrupted":
                log("Pipeline monitoring interrupted", "WARN")
                break
            if pipeline_result["status"] != "success":
                log(f"Build {pipeline_result['status']} for tag '{tag_name}'", "ERROR")
                continue

            log(f"Build succeeded for tag '{tag_name}'!", "INFO")

            with CONSOLE.status(
                f"[cyan]Fetching pipeline jobs for {pipeline_id}...", spinner="dots"
            ):
                jobs = get_pipeline_jobs(pid, pipeline_id)

            terminate_job = find_job_by_name(jobs, "eb-terminate")
            if terminate_job and terminate_job.get("status") == "manual":
                log("Triggering 'eb-terminate' job...", "INFO")
                trigger_manual_job(pid, terminate_job.get("id"))
                terminate_result = wait_for_job_completion(
                    pid, terminate_job.get("id"), timeout=900
                )
                if terminate_result["status"] == "interrupted":
                    log("Terminate job interrupted", "WARN")
                    break
                if terminate_result["status"] != "success":
                    log(f"eb-terminate job {terminate_result['status']}", "ERROR")
                    continue

            deploy_job_name = map_tag_to_deploy_job(tag_name)
            if deploy_job_name:
                with CONSOLE.status(
                    f"[cyan]Fetching pipeline jobs for deploy step...", spinner="dots"
                ):
                    jobs = get_pipeline_jobs(pid, pipeline_id)
                deploy_job = find_job_by_name(jobs, deploy_job_name)

                if deploy_job is None:
                    log(
                        f"Job '{deploy_job_name}' not found in pipeline {pipeline_id} "
                        f"for tag '{tag_name}'; deployment aborted",
                        "ERROR",
                    )
                    if state is not None:
                        if str(pid) not in state["project_details"]:
                            state["project_details"][str(pid)] = {
                                "completed_tags": [], "failed_tags": [], "last_pipeline_id": None
                            }
                        state["project_details"][str(pid)]["failed_tags"].append(tag_name)
                        save_state(state)
                    continue

                if deploy_job.get("status") == "manual":
                    log(f"Triggering '{deploy_job_name}' job...", "INFO")
                    trigger_manual_job(pid, deploy_job.get("id"))
                    deploy_result = wait_for_job_completion(
                        pid, deploy_job.get("id"), timeout=1200
                    )
                    if deploy_result["status"] == "interrupted":
                        log("Deploy job interrupted", "WARN")
                        break
                    if deploy_result["status"] == "success":
                        log(f"Deployment completed for tag '{tag_name}'!", "INFO")
                        deployment_successful = True
                        if state is not None:
                            if str(pid) not in state["project_details"]:
                                state["project_details"][str(pid)] = {
                                    "completed_tags": [], "failed_tags": [], "last_pipeline_id": None
                                }
                            state["project_details"][str(pid)]["completed_tags"].append(tag_name)
                            state["project_details"][str(pid)]["last_pipeline_id"] = pipeline_id
                            save_state(state)
                            log(
                                f"State saved: tag '{tag_name}' marked complete for project {pid}",
                                "DEBUG",
                            )
                    else:
                        log(f"Deployment {deploy_result['status']} for tag '{tag_name}'", "ERROR")
                        if state is not None:
                            if str(pid) not in state["project_details"]:
                                state["project_details"][str(pid)] = {
                                    "completed_tags": [], "failed_tags": [], "last_pipeline_id": None
                                }
                            state["project_details"][str(pid)]["failed_tags"].append(tag_name)
                            save_state(state)
                else:
                    log(
                        f"Job '{deploy_job_name}' exists but is not in manual status "
                        f"(status: {deploy_job.get('status', 'unknown')}); skipping trigger",
                        "WARN",
                    )
        else:
            log(f"No commit available for tag '{tag_name}', skipping deployment", "WARN")

    return {"success": deployment_successful, "idempotent_tags": idempotent_tags}


def bulk_create_mrs(project_ids, rollback_data, state):
    log("=" * 70, "INFO")
    log("BULK MR CREATION MODE", "INFO")
    log("=" * 70, "INFO")
    results = {
        "total":       len(project_ids),
        "success":     0,
        "failed":      0,
        "skipped":     0,
        "interrupted": 0,
    }
    project_results = []
    for pid in project_ids:
        if INTERRUPTED:
            log("Bulk MR creation interrupted", "WARN")
            results["interrupted"] += 1
            break
        try:
            result = process_project(pid, mode="mr_only", rollback_data=rollback_data, state=state)
            project_results.append(result)
            if result.get("interrupted"):
                results["interrupted"] += 1
                break
            elif result["mr_created"]:
                results["success"] += 1
            elif result["error"]:
                results["failed"] += 1
            else:
                results["skipped"] += 1
        except Exception as e:
            log(f"Error processing project {pid}: {e}", "ERROR")
            results["failed"] += 1
    log("=" * 70, "INFO")
    log("BULK MR CREATION COMPLETE", "INFO")
    log("=" * 70, "INFO")
    log(f"Total Projects: {results['total']}", "INFO")
    log(f"Success: {results['success']}", "INFO")
    log(f"Failed: {results['failed']}", "INFO")
    log(f"Skipped: {results['skipped']}", "INFO")
    if results["interrupted"] > 0:
        log(f"Interrupted: {results['interrupted']}", "WARN")
    return project_results


def interactive_project_selection(projects):
    sorted_projects = sorted(projects.items(), key=lambda x: x[1])
    values = [
        (pid, f"[{pid:>4}]  {name}")
        for pid, name in sorted_projects
    ]
    selected = checkboxlist_dialog(
        title="Project Selection",
        text=(
            "Select the projects to migrate.\n"
            "Use arrow keys to move, Space to toggle, Enter to confirm, Ctrl-C to cancel."
        ),
        values=values,
        style=DIALOG_STYLE,
    ).run()
    if not selected:
        return []
    log(f"Selected {len(selected)} project(s)", "INFO")
    return selected


def parse_project_input(user_input, projects):
    if not user_input or user_input.strip().lower() == "all":
        return sorted(list(projects.keys()))
    name_to_id = {name: pid for pid, name in projects.items()}
    selected = []
    parts = [p.strip() for p in user_input.split(",")]
    for part in parts:
        if "-" in part and not any(c.isalpha() for c in part):
            try:
                start, end = part.split("-")
                start_id = int(start.strip())
                end_id   = int(end.strip())
                for pid in range(start_id, end_id + 1):
                    if pid in projects:
                        selected.append(pid)
                    else:
                        log(f"Project ID {pid} not found in range {start}-{end}", "WARN")
            except ValueError:
                log(f"Invalid range: {part}", "WARN")
                continue
        elif part.isdigit():
            pid = int(part)
            if pid in projects:
                selected.append(pid)
            else:
                log(f"Unknown project ID: {pid}", "WARN")
        elif part in name_to_id:
            selected.append(name_to_id[part])
        else:
            matches = [name for name in name_to_id if part.lower() in name.lower()]
            if len(matches) == 1:
                log(f"Auto-matched '{part}' to '{matches[0]}'", "INFO")
                selected.append(name_to_id[matches[0]])
            elif len(matches) > 1:
                log(f"Ambiguous project '{part}'. Matches: {', '.join(matches[:3])}", "WARN")
            else:
                log(f"Unknown project: {part}", "WARN")
    seen = set()
    result = []
    for pid in selected:
        if pid not in seen:
            seen.add(pid)
            result.append(pid)
    return result


def select_mode():
    result = radiolist_dialog(
        title="Migration Tool - Mode Selection",
        text=(
            "Choose the operation mode for this migration run.\n"
            "Use arrow keys to move, Space to select, Enter to confirm."
        ),
        values=[
            ("full",        "Full Migration      - File changes + Commit + Deploy or MR"),
            ("mr_bulk",     "Bulk MR Creation    - Create MRs for existing feature branches"),
            ("deploy_only", "Deploy Only         - Tag creation and pipeline deployment only"),
        ],
        style=DIALOG_STYLE,
    ).run()
    return result


def select_file_updates():
    result = checkboxlist_dialog(
        title="File Update Selection",
        text=(
            "Select which files to update for ALL selected projects.\n"
            "Use Space to toggle, Enter to confirm."
        ),
        values=[
            ("1", "pom.xml                       - Java version (11->17) + parent POM version"),
            ("2", ".gitlab-ci.yml                - Remove image directive"),
            ("3", ".elasticbeanstalk/config.yml  - Update default_platform"),
        ],
        style=DIALOG_STYLE,
    ).run()
    return result or []


def print_summary_table(project_results):
    if not project_results:
        return

    log("=" * 70, "INFO")
    log("FINAL SUMMARY", "INFO")
    log("=" * 70, "INFO")

    table = Table(
        title="Migration Summary",
        show_header=True,
        header_style="bold white on dark_blue",
        border_style="blue",
        show_lines=True,
    )
    table.add_column("Project Name",  style="white",  no_wrap=True, min_width=25)
    table.add_column("Commit Status", justify="center", min_width=18)
    table.add_column("MR Status",     justify="center", min_width=28)

    for r in project_results:
        name = r.get("project_name", f"ID:{r.get('project_id', '?')}")

        if r.get("interrupted"):
            commit_text  = "[bold yellow]Interrupted[/bold yellow]"
            commit_style = "yellow"
        elif r.get("idempotent_commit"):
            commit_text  = "[bold yellow]Skipped (match)[/bold yellow]"
            commit_style = "yellow"
        elif r.get("committed"):
            commit_text  = "[bold green]Committed[/bold green]"
            commit_style = "green"
        elif r.get("error") and "commit" in str(r.get("error", "")).lower():
            commit_text  = "[bold red]Failed[/bold red]"
            commit_style = "red"
        else:
            commit_text  = "[dim]Not attempted[/dim]"
            commit_style = "default"

        if r.get("interrupted"):
            mr_text  = "[bold yellow]Interrupted[/bold yellow]"
        elif r.get("mr_conflict"):
            mr_text  = "[bold red]Conflict - Manual Fix Needed[/bold red]"
        elif r.get("skipped_mr"):
            mr_text  = "[bold yellow]Skipped (No Changes/Branch)[/bold yellow]"
        elif r.get("idempotent_mr"):
            mr_text  = "[bold green]Exists[/bold green]"
        elif r.get("mr_created"):
            mr_text  = "[bold green]Created[/bold green]"
        else:
            mr_text  = "[dim]Not created[/dim]"

        table.add_row(name, commit_text, mr_text)

    CONSOLE.print(table)


def main():
    global TOKEN, PROJECT_NAMES, BASE_URL, INTERRUPTED, NEW_DEFAULT_PLATFORM
    global REVIEWER_USERNAMES, ASSIGNEE_USERNAMES, SSL_VERIFY

    signal.signal(signal.SIGINT,  signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    setup_logging()

    parser = argparse.ArgumentParser(description="Batch Java migration helper for GitLab projects.")
    parser.add_argument("--projects",     help="Comma-separated project IDs to process", default="")
    parser.add_argument("--full-diff",    help="Show full unified diffs", action="store_true", default=True)
    parser.add_argument("--summary-only", help="Show only line-count summary", action="store_true")
    parser.add_argument("--token",        help="GitLab private token string", default="")
    parser.add_argument("--base-url",     help="GitLab API base URL", default="")
    parser.add_argument("--rollback",     help="Rollback from a previous migration rollback file", default="")
    parser.add_argument("--mode",         help="Execution mode", choices=["full", "mr_bulk", "deploy_only"], default=None)
    parser.add_argument("--resume",       help="Resume from a previous state file", default="")

    args = parser.parse_args()

    if args.rollback:
        perform_rollback(args.rollback)
        sys.exit(0)

    log("Migration tool starting...", "INFO")

    state = {
        "completed_projects": [],
        "failed_projects":    [],
        "skipped_projects":   [],
        "start_time":         datetime.datetime.now().isoformat(),
        "project_details":    {},
    }

    if args.resume:
        loaded_state = load_state(args.resume)
        if loaded_state:
            state = loaded_state
            log(f"Resumed from previous state: {args.resume}", "INFO")
            log(f"Completed: {len(state.get('completed_projects', []))} projects", "INFO")
            log(f"Failed: {len(state.get('failed_projects', []))} projects", "INFO")
        else:
            log(f"Could not load state from {args.resume}, starting fresh", "WARN")

    rollback_data = {"snapshots": []}
    show_full = args.full_diff and not args.summary_only

    env_config = load_env_config(debug=False)

    if args.token:
        TOKEN = args.token.strip()
        log("Using token from command line argument", "INFO")
    elif env_config["token"]:
        TOKEN = env_config["token"]
    elif not TOKEN:
        file_token = load_token_from_file()
        if file_token:
            TOKEN = file_token
        else:
            log("No token found in .env or token.txt files", "WARN")

    if args.base_url:
        BASE_URL = args.base_url.strip().rstrip("/")
        log(f"Using BASE_URL from command line: {BASE_URL}", "INFO")
    elif env_config["base_url"]:
        BASE_URL = env_config["base_url"]

    if env_config["new_default_platform"]:
        NEW_DEFAULT_PLATFORM = env_config["new_default_platform"]
        log(f"NEW_DEFAULT_PLATFORM loaded from .env: {NEW_DEFAULT_PLATFORM[:80]}...", "INFO")
    else:
        log("WARNING: NEW_DEFAULT_PLATFORM not found in .env file", "WARN")
        log("Please add to .env: NEW_DEFAULT_PLATFORM=arn:aws:elasticbeanstalk:...", "WARN")

    if env_config["reviewer_usernames"]:
        REVIEWER_USERNAMES = env_config["reviewer_usernames"]

    if env_config["assignee_usernames"]:
        ASSIGNEE_USERNAMES = env_config["assignee_usernames"]

    PROJECT_NAMES = env_config["projects"]

    if not TOKEN:
        log("No token supplied. Please provide token via:", "ERROR")
        log("  1. --token command line argument", "ERROR")
        log("  2. .env file (GITLAB_TOKEN=your_token or TOKEN=your_token)", "ERROR")
        log("  3. token.txt file in script directory", "ERROR")
        sys.exit(1)

    if env_config["ssl_verify"] is not None:
        SSL_VERIFY = env_config["ssl_verify"]
    else:
        env_ssl_raw = os.environ.get("GITLAB_VERIFY_SSL", "").strip().lower()
        if env_ssl_raw == "false":
            SSL_VERIFY = False

    if not SSL_VERIFY:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        log("SSL verification disabled (GITLAB_VERIFY_SSL=False). InsecureRequestWarning suppressed.", "WARN")

    setup_http_session(ssl_verify=SSL_VERIFY)

    log("Validating GitLab token...", "INFO")
    token_validation = validate_and_log_token_info(TOKEN, BASE_URL)

    if not token_validation["valid"]:
        log("Token validation failed. Please check your token and try again.", "ERROR")
        sys.exit(1)

    if token_validation.get("info") and token_validation["info"].get("access_level"):
        access_level = token_validation["info"]["access_level"]
        access_level_names = {
            10: "Guest", 20: "Reporter", 30: "Developer", 40: "Maintainer", 50: "Owner"
        }
        access_name = access_level_names.get(access_level, f"Level {access_level}")
        log("=" * 70, "INFO")
        log(f"TOKEN ACCESS LEVEL: {access_name} ({access_level})", "INFO")
        log("=" * 70, "INFO")

    if not PROJECT_NAMES:
        log("No projects found in .env file. Please add projects in format:", "ERROR")
        log("  PROJECT_101=user-authentication-service", "ERROR")
        log("  PROJECT_102=payment-gateway-api", "ERROR")
        sys.exit(1)

    project_ids = []
    if args.projects:
        try:
            project_ids = parse_project_input(args.projects, PROJECT_NAMES)
            if not project_ids:
                log("No valid project IDs parsed from --projects argument.", "ERROR")
                sys.exit(1)
        except Exception:
            log("Invalid --projects value. Provide comma-separated integers.", "ERROR")
            sys.exit(1)
    else:
        project_ids = interactive_project_selection(PROJECT_NAMES)
        if not project_ids:
            log("No projects selected. Exiting.", "ERROR")
            sys.exit(1)

    if args.resume and state.get("completed_projects"):
        completed_set  = set(state["completed_projects"])
        original_count = len(project_ids)
        project_ids    = [pid for pid in project_ids if pid not in completed_set]
        skipped_count  = original_count - len(project_ids)
        if skipped_count > 0:
            log(f"Resuming: Skipping {skipped_count} already completed project(s)", "INFO")

    mode = args.mode

    if not mode:
        mode = select_mode()
        if not mode:
            log("No mode selected. Exiting.", "WARN")
            sys.exit(0)

    log(f"Selected mode: {mode.upper()}", "INFO")

    all_project_results = []

    try:
        if mode == "mr_bulk":
            all_project_results = bulk_create_mrs(project_ids, rollback_data, state)

        elif mode == "deploy_only":
            log("=" * 70, "INFO")
            log("DEPLOY ONLY MODE", "INFO")
            log("=" * 70, "INFO")
            for pid in project_ids:
                if INTERRUPTED:
                    log("Migration interrupted", "WARN")
                    break
                result = process_project(
                    pid, mode="deploy_only", rollback_data=rollback_data, state=state
                )
                all_project_results.append(result)

        else:
            global_choices = select_file_updates()
            if not global_choices:
                log("No file updates selected. Exiting.", "WARN")
                sys.exit(0)
            choice_labels = [
                "POM" if c == "1" else "CI" if c == "2" else "EB" if c == "3" else c
                for c in global_choices
            ]
            log(f"Will apply these updates: {', '.join(choice_labels)}", "INFO")
            log("=" * 70, "INFO")
            log("STARTING MIGRATION", "INFO")
            log("=" * 70, "INFO")
            for pid in project_ids:
                if INTERRUPTED:
                    log("Migration interrupted", "WARN")
                    break
                try:
                    result = process_project(
                        pid,
                        choices=global_choices,
                        show_full=show_full,
                        rollback_data=rollback_data,
                        mode="full",
                        state=state,
                    )
                    all_project_results.append(result)
                except KeyboardInterrupt:
                    log("[INTERRUPT] Keyboard interrupt received", "WARN")
                    INTERRUPTED = True
                    break
                except Exception as e:
                    log(f"Error processing project {pid}: {e}", "ERROR")

        if rollback_data["snapshots"]:
            rollback_file = save_rollback_data(rollback_data)
            log(f"Rollback data saved to: {rollback_file}", "INFO")
            log(f"To rollback, run: python3 {sys.argv[0]} --rollback {rollback_file}", "INFO")

        if INTERRUPTED:
            state_file = save_state(state)
            log(f"Migration interrupted. State saved to: {state_file}", "WARN")
            log(f"To resume, run: python3 {sys.argv[0]} --resume {state_file}", "INFO")

        log("=" * 70, "INFO")
        if INTERRUPTED:
            log("MIGRATION INTERRUPTED", "WARN")
        else:
            log("MIGRATION COMPLETE", "INFO")
        log("=" * 70, "INFO")

        if state:
            log(f"Completed: {len(state.get('completed_projects', []))} projects", "INFO")
            log(f"Failed:    {len(state.get('failed_projects', []))} projects", "INFO")
            log(f"Skipped:   {len(state.get('skipped_projects', []))} projects", "INFO")

        print_summary_table(all_project_results)

    except KeyboardInterrupt:
        log("[INTERRUPT] Keyboard interrupt received", "WARN")
        INTERRUPTED = True
        if state:
            state_file = save_state(state)
            log(f"State saved to: {state_file}", "INFO")
            log(f"To resume, run: python3 {sys.argv[0]} --resume {state_file}", "INFO")
        sys.exit(1)

    finally:
        if HTTP_SESSION is not None:
            HTTP_SESSION.close()
            log("HTTP session closed.", "DEBUG")


if __name__ == "__main__":
    main()
