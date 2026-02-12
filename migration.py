import urllib.request
import urllib.parse
import urllib.error
import json
import base64
import re
import datetime
import difflib
import argparse
import sys
import time
import os
import logging
import signal
import readline
from functools import wraps

# --- CONFIGURATION (edit as needed) ---
BASE_URL = "" # Will be loaded from .env
TOKEN = ""  # Will be loaded from .env or token.txt file

# Project names for tab-completion (ADD YOUR 15 PROJECTS HERE)
PROJECT_NAMES = {
    101: "user-authentication-service",
    102: "payment-gateway-api",
    103: "notification-service",
    104: "order-management-system",
    105: "inventory-tracker",
    106: "customer-portal",
    107: "admin-dashboard",
    108: "analytics-engine",
    109: "reporting-service",
    110: "data-pipeline",
    111: "email-service",
    112: "sms-gateway",
    113: "file-upload-service",
    114: "search-indexer",
    115: "cache-manager"
}

JIRA_ID = "1293"
UPGRADE_TYPE = "java17-migration"
FEATURE_BRANCH = f"task-{JIRA_ID}-{UPGRADE_TYPE}"
SOURCE_BRANCH = "develop"
MR_TITLE = f"TASK-{JIRA_ID}: java migration"

TARGET_PARENT_VERSION = "1.8.3"
NEW_DEFAULT_PLATFORM = "arn:aws:elasticbeanstalk:us-east-1::platform/Corretto 17 running on 64bit Amazon Linux 2/3.10.3"

AUTO_ROLLBACK_ON_FAILURE = True
# ---------------------------------------------------------

# Global variables for state management
STATE_FILE = None
ROLLBACK_FILE = None
FILE_LOGGER = None
INTERRUPTED = False

# Log directories
LOG_DIR_MIGRATION = "migration_logs"
LOG_DIR_ROLLBACK = "rollback_logs"
LOG_DIR_STATE = "state_logs"


def setup_file_logging(log_dir=LOG_DIR_MIGRATION):
    """
    Setup file logging with both file and console output.
    Creates timestamped log files for audit trail.
    """
    global FILE_LOGGER
    
    os.makedirs(log_dir, exist_ok=True)
    
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = os.path.join(log_dir, f"migration_{timestamp}.log")
    
    # Setup logging
    FILE_LOGGER = logging.getLogger('migration')
    FILE_LOGGER.setLevel(logging.DEBUG)
    
    # Remove existing handlers to avoid duplicates
    FILE_LOGGER.handlers = []
    
    # File handler - detailed logs
    file_handler = logging.FileHandler(log_file, encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    file_formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
    file_handler.setFormatter(file_formatter)
    
    # Add handler
    FILE_LOGGER.addHandler(file_handler)
    
    return log_file


def signal_handler(signum, frame):
    """Handle interrupt signals gracefully."""
    global INTERRUPTED
    INTERRUPTED = True
    log("\n[INTERRUPT] Received interrupt signal. Saving state...", "WARN")


def save_state(state_data, filename=None):
    """Save migration state to file for resume capability."""
    global STATE_FILE
    
    if filename is None:
        os.makedirs(LOG_DIR_STATE, exist_ok=True)
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = os.path.join(LOG_DIR_STATE, f"migration_state_{timestamp}.json")
    
    STATE_FILE = filename
    state_data['last_updated'] = datetime.datetime.now().isoformat()
    
    with open(filename, 'w') as f:
        json.dump(state_data, f, indent=2)
    
    log(f"State saved to {filename}", "DEBUG")
    return filename


def load_state(filename):
    """Load migration state from file."""
    if not os.path.exists(filename):
        return None
    
    try:
        with open(filename, 'r') as f:
            state = json.load(f)
        log(f"State loaded from {filename}", "INFO")
        return state
    except Exception as e:
        log(f"Error loading state from {filename}: {e}", "ERROR")
        return None


def create_rollback_snapshot(pid, p_name, branch_name, files_to_backup):
    """
    Create a rollback snapshot before making changes.
    
    Args:
        pid: Project ID
        p_name: Project name
        branch_name: Branch to snapshot
        files_to_backup: List of file paths to backup
    
    Returns:
        Snapshot dict with rollback information
    """
    snapshot = {
        'project_id': pid,
        'project_name': p_name,
        'branch': branch_name,
        'timestamp': datetime.datetime.now().isoformat(),
        'files': {}
    }
    
    try:
        br_info = api_call(f"projects/{pid}/repository/branches/{urllib.parse.quote(branch_name, safe='')}")
        if isinstance(br_info, dict) and "commit" in br_info:
            snapshot['commit_sha'] = (br_info.get("commit") or {}).get("id")
        else:
            snapshot['commit_sha'] = None
    except Exception as e:
        log(f"Could not get branch head for rollback snapshot: {e}", "WARN")
        snapshot['commit_sha'] = None
    
    # Backup file contents
    for file_path in files_to_backup:
        try:
            quoted_path = urllib.parse.quote(file_path, safe='')
            res = api_call(f"projects/{pid}/repository/files/{quoted_path}?ref={urllib.parse.quote(branch_name, safe='')}")
            
            if isinstance(res, dict) and "content" in res:
                content = base64.b64decode(res['content']).decode('utf-8')
                snapshot['files'][file_path] = {
                    'content': content,
                    'exists': True
                }
            else:
                snapshot['files'][file_path] = {
                    'content': None,
                    'exists': False
                }
        except Exception as e:
            log(f"Could not backup {file_path} for rollback: {e}", "WARN")
            snapshot['files'][file_path] = {
                'content': None,
                'exists': False,
                'error': str(e)
            }
    
    return snapshot


def save_rollback_data(rollback_data, filename=None):
    """Save rollback snapshots to file."""
    global ROLLBACK_FILE
    
    if filename is None:
        os.makedirs(LOG_DIR_ROLLBACK, exist_ok=True)
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = os.path.join(LOG_DIR_ROLLBACK, f"rollback_data_{timestamp}.json")
    
    ROLLBACK_FILE = filename
    rollback_data['created_at'] = datetime.datetime.now().isoformat()
    
    with open(filename, 'w') as f:
        json.dump(rollback_data, f, indent=2)
    
    log(f"Rollback data saved to {filename}", "INFO")
    return filename


def load_rollback_data(filename):
    """Load rollback data from file."""
    if not os.path.exists(filename):
        log(f"Rollback file {filename} not found", "ERROR")
        return None
    
    try:
        with open(filename, 'r') as f:
            data = json.load(f)
        log(f"Rollback data loaded from {filename}", "INFO")
        return data
    except Exception as e:
        log(f"Error loading rollback data: {e}", "ERROR")
        return None


def perform_rollback(rollback_file):
    """
    Rollback changes from a previous migration.
    
    Args:
        rollback_file: Path to rollback JSON file
    """
    log("=" * 70, "INFO")
    log("ROLLBACK MODE", "INFO")
    log("=" * 70, "INFO")
    
    data = load_rollback_data(rollback_file)
    if not data:
        return False
    
    snapshots = data.get('snapshots', [])
    
    if not snapshots:
        log("No snapshots found in rollback file", "WARN")
        return False
    
    log(f"Found {len(snapshots)} project(s) to rollback", "INFO")
    log(f"Rollback file created: {data.get('created_at', 'unknown')}", "INFO")
    
    success_count = 0
    fail_count = 0
    
    for snapshot in snapshots:
        pid = snapshot['project_id']
        p_name = snapshot['project_name']
        branch = snapshot['branch']
        
        log(f"\n--- Rollback: {p_name} (ID: {pid}) ---", "INFO")
        
        # Restore files
        project_success = True
        for file_path, file_data in snapshot.get('files', {}).items():
            if not file_data.get('exists'):
                log(f"File {file_path} did not exist before, skipping restore", "INFO")
                continue
            
            content = file_data.get('content')
            if content is None:
                log(f"No backup content for {file_path}, skipping", "WARN")
                continue
            
            try:
                            commit_data = {
                    "branch": branch,
                    "commit_message": f"Rollback: Restore {file_path} to pre-migration state",
                    "actions": [{
                        "action": "update",
                        "file_path": file_path,
                        "content": content
                    }]
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
    
    log("\n" + "=" * 70, "INFO")
    log(f"ROLLBACK COMPLETE: {success_count} successful, {fail_count} with errors", "INFO")
    log("=" * 70, "INFO")
    
    return success_count > 0


def log(msg, level="INFO"):
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    console_msg = f"[{timestamp}] [{level}] {msg}"
    print(console_msg)
    
    # Also log to file if file logger is setup
    if FILE_LOGGER:
        log_level = getattr(logging, level, logging.INFO)
        FILE_LOGGER.log(log_level, msg)


def load_token_from_file(debug=False):
    """
    Load GitLab token from .env or token.txt file in the script's directory.
    
    Priority:
    1. .env file (looks for GITLAB_TOKEN=xxx or TOKEN=xxx)
    2. token.txt file (reads entire content as token)
    
    Returns the token string or None if not found.
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    
    if debug:
        log(f"Script directory: {script_dir}", "DEBUG")
    
    # Try .env file first
    env_file = os.path.join(script_dir, '.env')
    if debug:
        log(f"Looking for .env file at: {env_file}", "DEBUG")
        log(f".env file exists: {os.path.exists(env_file)}", "DEBUG")
    
    if os.path.exists(env_file):
        try:
            with open(env_file, 'r') as f:
                lines = f.readlines()
                if debug:
                    log(f"Read {len(lines)} lines from .env file", "DEBUG")
                
                for i, line in enumerate(lines, 1):
                    original_line = line
                    line = line.strip()
                    
                    if debug and line:
                                            if '=' in line and not line.startswith('#'):
                            key = line.split('=', 1)[0].strip()
                            log(f"Line {i}: key='{key}'", "DEBUG")
                        else:
                            log(f"Line {i}: {line[:30]}...", "DEBUG")
                    
                    # Skip comments and empty lines
                    if not line or line.startswith('#'):
                        continue
                    # Look for GITLAB_TOKEN or TOKEN
                    if '=' in line:
                        key, value = line.split('=', 1)
                        key = key.strip()
                        value = value.strip()
                        
                        if debug:
                            log(f"Found key: '{key}', value length: {len(value)}", "DEBUG")
                        
                        # Remove quotes if present
                        if value.startswith('"') and value.endswith('"'):
                            value = value[1:-1]
                        elif value.startswith("'") and value.endswith("'"):
                            value = value[1:-1]
                        
                        if key in ('GITLAB_TOKEN', 'TOKEN'):
                            log(f"Token loaded from .env file (key: {key})", "INFO")
                            if debug:
                                log(f"Token length: {len(value)} characters", "DEBUG")
                            return value
        except Exception as e:
            log(f"Error reading .env file: {e}", "WARN")
    
    # Try token.txt file
    token_file = os.path.join(script_dir, 'token.txt')
    if debug:
        log(f"Looking for token.txt file at: {token_file}", "DEBUG")
        log(f"token.txt file exists: {os.path.exists(token_file)}", "DEBUG")
    
    if os.path.exists(token_file):
        try:
            with open(token_file, 'r') as f:
                token = f.read().strip()
                if token:
                    log("Token loaded from token.txt file", "INFO")
                    if debug:
                        log(f"Token length: {len(token)} characters", "DEBUG")
                    return token
        except Exception as e:
            log(f"Error reading token.txt file: {e}", "WARN")
    
    return None


def get_pipeline_for_commit(pid, commit_sha):
    """
    Get the pipeline associated with a specific commit.
    Returns the pipeline object or None.
    """
    try:
        pipelines = api_call(f"projects/{pid}/pipelines?sha={commit_sha}&per_page=1")
        if isinstance(pipelines, list) and len(pipelines) > 0:
            return pipelines[0]
        return None
    except Exception as e:
        log(f"Error fetching pipeline for commit {commit_sha[:8]}: {e}", "ERROR")
        return None


def wait_for_pipeline_completion(pid, pipeline_id, timeout=1800, check_interval=30):
    """
    Wait for a pipeline to complete (success, failed, or canceled).
    
    Args:
        pid: Project ID
        pipeline_id: Pipeline ID to monitor
        timeout: Maximum time to wait in seconds (default: 30 minutes)
        check_interval: How often to check status in seconds (default: 30s)
    
    Returns:
        Dict with 'status' (success/failed/canceled/timeout) and 'pipeline' object
    """
    log(f"Waiting for pipeline {pipeline_id} to complete (timeout: {timeout}s, checking every {check_interval}s)...", "INFO")
    
    start_time = time.time()
    last_status = None
    
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
                status = pipeline.get('status', 'unknown')
                
                            if status != last_status:
                    log(f"Pipeline {pipeline_id} status: {status}", "INFO")
                    last_status = status
                
                # Check if pipeline is complete
                if status in ['success', 'failed', 'canceled', 'skipped']:
                    return {"status": status, "pipeline": pipeline}
                
                # Pipeline still running
                time.sleep(check_interval)
            else:
                log(f"Error fetching pipeline {pipeline_id}: {pipeline.get('details', 'unknown')}", "WARN")
                time.sleep(check_interval)
        except Exception as e:
            log(f"Exception while monitoring pipeline {pipeline_id}: {e}", "WARN")
            time.sleep(check_interval)


def get_pipeline_jobs(pid, pipeline_id):
    """
    Get all jobs for a specific pipeline.
    Returns list of job objects.
    """
    try:
        jobs = api_call(f"projects/{pid}/pipelines/{pipeline_id}/jobs?per_page=100")
        if isinstance(jobs, list):
            return jobs
        return []
    except Exception as e:
        log(f"Error fetching jobs for pipeline {pipeline_id}: {e}", "ERROR")
        return []


def find_job_by_name(jobs, job_name):
    """
    Find a job by name in the jobs list.
    Returns the job object or None.
    """
    for job in jobs:
        if isinstance(job, dict) and job.get('name') == job_name:
            return job
    return None


def trigger_manual_job(pid, job_id):
    """
    Trigger a manual job (play button).
    Returns the job response.
    """
    try:
        response = api_call(f"projects/{pid}/jobs/{job_id}/play", method="POST")
        return response
    except Exception as e:
        log(f"Error triggering job {job_id}: {e}", "ERROR")
        return {"error": True, "details": str(e)}


def wait_for_job_completion(pid, job_id, timeout=900, check_interval=15):
    """
    Wait for a specific job to complete.
    
    Args:
        pid: Project ID
        job_id: Job ID to monitor
        timeout: Maximum time to wait in seconds (default: 15 minutes)
        check_interval: How often to check status in seconds (default: 15s)
    
    Returns:
        Dict with 'status' (success/failed/canceled/timeout) and 'job' object
    """
    log(f"Waiting for job {job_id} to complete (timeout: {timeout}s)...", "INFO")
    
    start_time = time.time()
    last_status = None
    
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
                status = job.get('status', 'unknown')
                
                            if status != last_status:
                    job_name = job.get('name', 'unknown')
                    log(f"Job '{job_name}' (ID: {job_id}) status: {status}", "INFO")
                    last_status = status
                
                # Check if job is complete
                if status in ['success', 'failed', 'canceled', 'skipped']:
                    return {"status": status, "job": job}
                
                # Job still running
                time.sleep(check_interval)
            else:
                log(f"Error fetching job {job_id}: {job.get('details', 'unknown')}", "WARN")
                time.sleep(check_interval)
        except Exception as e:
            log(f"Exception while monitoring job {job_id}: {e}", "WARN")
            time.sleep(check_interval)


def map_tag_to_deploy_job(tag_name):
    """
    Map tag name to corresponding deploy job name.
    
    Examples:
    - dev -> eb-deploy-dev-azure
    - azure-dev -> eb-deploy-dev-azure
    - test -> eb-deploy-test-azure
    - azure-test -> eb-deploy-test-azure
    - performance -> eb-deploy-performance-azure
    - azure-performance -> eb-deploy-performance-azure
    """
    tag_lower = tag_name.lower().replace('azure-', '')
    
    deploy_jobs = {
        'dev': 'eb-deploy-dev-azure',
        'test': 'eb-deploy-test-azure',
        'performance': 'eb-deploy-performance-azure'
    }
    
    return deploy_jobs.get(tag_lower)


def validate_and_log_token_info(token, base_url):
    """
    Validate the GitLab token and log its metadata including expiry date and access level.
    
    Returns:
        dict with 'valid' (bool) and 'info' (dict with token details)
    """
    if not token:
        return {'valid': False, 'info': None}
    
    try:
        # Call the personal access tokens endpoint to get token info
        # Note: This requires the token to have 'read_api' scope
        url = f"{base_url.rstrip('/')}/personal_access_tokens/self"
        req = urllib.request.Request(url, method="GET")
        req.add_header("PRIVATE-TOKEN", token)
        req.add_header("Content-Type", "application/json")
        
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode('utf-8'))
            
            # Extract token information
            token_info = {
                'id': data.get('id'),
                'name': data.get('name', 'N/A'),
                'scopes': data.get('scopes', []),
                'created_at': data.get('created_at'),
                'expires_at': data.get('expires_at'),
                'active': data.get('active', False),
                'revoked': data.get('revoked', False),
                'access_level': data.get('access_level', 'unknown')  # GitLab 15.0+
            }
            
                    log("=" * 70, "INFO")
            log("GitLab Token Information:", "INFO")
            log("=" * 70, "INFO")
            log(f"Token Name: {token_info['name']}", "INFO")
            log(f"Token ID: {token_info['id']}", "INFO")
            log(f"Active: {'Yes' if token_info['active'] else 'No'}", "INFO")
            log(f"Revoked: {'Yes' if token_info['revoked'] else 'No'}", "INFO")
            
            # Display access level if available
            access_level = token_info.get('access_level')
            if access_level and access_level != 'unknown':
                # GitLab access levels: 10=Guest, 20=Reporter, 30=Developer, 40=Maintainer, 50=Owner
                access_level_names = {
                    10: 'Guest',
                    20: 'Reporter',
                    30: 'Developer',
                    40: 'Maintainer',
                    50: 'Owner'
                }
                access_name = access_level_names.get(access_level, f'Level {access_level}')
                log(f"Access Level: {access_name} ({access_level})", "INFO")
            
            # Parse and display expiry date
            if token_info['expires_at']:
                try:
                    from datetime import datetime
                    expiry_dt = datetime.fromisoformat(token_info['expires_at'].replace('Z', '+00:00'))
                    now_dt = datetime.now(expiry_dt.tzinfo)
                    
                    # Format expiry date
                    expiry_str = expiry_dt.strftime("%Y-%m-%d %H:%M:%S %Z")
                    log(f"Expires At: {expiry_str}", "INFO")
                    
                    # Calculate days until expiry
                    days_until_expiry = (expiry_dt - now_dt).days
                    
                    if days_until_expiry < 0:
                        log(f"[ERROR] TOKEN EXPIRED {abs(days_until_expiry)} days ago!", "ERROR")
                    elif days_until_expiry == 0:
                        log(f"[WARNING] TOKEN EXPIRES TODAY!", "WARN")
                    elif days_until_expiry <= 7:
                        log(f"[WARNING] Token expires in {days_until_expiry} days", "WARN")
                    elif days_until_expiry <= 30:
                        log(f"Token expires in {days_until_expiry} days", "INFO")
                    else:
                        log(f"Token expires in {days_until_expiry} days", "INFO")
                        
                except Exception as e:
                    log(f"Expires At: {token_info['expires_at']}", "INFO")
                    log(f"Could not parse expiry date: {e}", "WARN")
            else:
                log("Expires At: Never (no expiration set)", "INFO")
            
            # Display scopes/permissions (without warnings)
            if token_info['scopes']:
                log(f"Permissions (Scopes): {', '.join(token_info['scopes'])}", "INFO")
            else:
                log("Permissions (Scopes): None detected (may have full access)", "INFO")
            
            log("=" * 70, "INFO")
            
            return {'valid': True, 'info': token_info}
            
    except urllib.error.HTTPError as e:
        if e.code == 404:
            # Endpoint not available - older GitLab version or token doesn't have read_api scope
            log("=" * 70, "WARN")
            log("Could not fetch detailed token information", "WARN")
            log("This may be due to:", "WARN")
            log("  - Token missing 'read_api' scope", "WARN")
            log("  - GitLab version doesn't support this endpoint", "WARN")
            log("=" * 70, "WARN")
            return {'valid': True, 'info': None}
        elif e.code == 401:
            log("Token validation failed: Invalid or expired token", "ERROR")
            return {'valid': False, 'info': None}
        else:
            log(f"Error validating token: HTTP {e.code}", "WARN")
            return {'valid': True, 'info': None}  # Assume valid, continue
    except Exception as e:
        log(f"Error fetching token information: {e}", "WARN")
        return {'valid': True, 'info': None}  # Assume valid, continue


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
                    log(f"Transient error: {e!r}. Retrying in {_delay}s... (remaining attempts: {_tries})", "WARN")
                    time.sleep(_delay)
                    _delay *= backoff
        return wrapper
    return deco


@retry(tries=3, delay=1, backoff=2, allowed_exceptions=(Exception,))
def api_call(endpoint, method="GET", data=None):
    """
    Wrapper for GitLab API calls.

    Behavior:
      - Raises exceptions on HTTP 429 or any 5xx to allow retry/backoff.
      - For other HTTP errors (4xx except 429) returns a dict with {"error": True, "details": "..."}
      - On success returns parsed JSON or {} when response body empty.
    """
    url = f"{BASE_URL.rstrip('/')}/{endpoint.lstrip('/')}"

    if not TOKEN:
        raise RuntimeError("API token not set. Provide --token on the command line or set the TOKEN constant in the script.")

    body = json.dumps(data).encode("utf-8") if data else None
    req = urllib.request.Request(url, method=method, data=body)
    req.add_header("PRIVATE-TOKEN", TOKEN)
    req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, data=body) as response:
            code = response.getcode()
            text = response.read().decode("utf-8")
            if not text:
                return {}
            if code == 429 or (500 <= code <= 599):
                raise RuntimeError(f"HTTP {code} returned for {url}")
            if 400 <= code <= 499:
                return {"error": True, "details": f"HTTP {code} returned for {url}: {text}"}
            return json.loads(text)
    except urllib.error.HTTPError as he:
        code = getattr(he, 'code', None)
        if code == 429 or (code and 500 <= code <= 599):
            raise
        return {"error": True, "details": f"HTTPError {code}: {he.reason}"}
    except Exception:
        # Let the retry decorator handle transient network/timeout errors
        raise


def update_parent_block(match):
    block = match.group(0)
    # replace the inner <version>...</version> with the target version
    block = re.sub(r"<version>.*?</version>", f"<version>{TARGET_PARENT_VERSION}</version>", block)
    # Use a callable replacement to avoid ambiguous backreference parsing (e.g. "\11")
    block = re.sub(r"(parent-pom-).*?(\.xml)", lambda m: m.group(1) + TARGET_PARENT_VERSION + m.group(2), block)
    return block


def show_unified_diff(path, old, new):
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    ud = ''.join(difflib.unified_diff(old_lines, new_lines, fromfile=path, tofile=f"{path} (new)", lineterm=''))
    return ud


def prompt_yes_no(prompt, default=False):
    try:
        ans = input(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        log("\n[INTERRUPT] User interrupted prompt", "WARN")
        return default
    if ans == '':
        return default
    return ans in ("y", "yes")


def create_feature_branch(pid, p_name):
    """
    Create feature branch from source branch.
    Returns True if successful, False otherwise.
    """
    try:
        log(f"Creating feature branch '{FEATURE_BRANCH}' from '{SOURCE_BRANCH}' for project {p_name}...", "INFO")
        result = api_call(f"projects/{pid}/repository/branches", "POST", 
                         {"branch": FEATURE_BRANCH, "ref": SOURCE_BRANCH})
        
        if isinstance(result, dict) and not result.get("error"):
            log(f"[SUCCESS] Feature branch '{FEATURE_BRANCH}' created successfully for {p_name}", "INFO")
            return True
        else:
            error_detail = result.get('details', 'unknown error') if isinstance(result, dict) else 'unknown error'
            log(f"[ERROR] Failed to create feature branch '{FEATURE_BRANCH}' for {p_name}: {error_detail}", "ERROR")
            
            # Check if branch already exists
            if "already exists" in str(error_detail).lower():
                log(f"[INFO] Branch '{FEATURE_BRANCH}' already exists for {p_name}, will use existing branch", "INFO")
                return True
            
            return False
    except Exception as e:
        log(f"[ERROR] Exception while creating feature branch for {p_name}: {e}", "ERROR")
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
    
    # Check for protected tags
    for tag in all_tags:
        if isinstance(tag, dict) and tag.get('protected'):
            tag_name = tag.get('name', 'unknown')
            log(f"Tag '{tag_name}' is PROTECTED", "WARN")
    
    return all_tags


def filter_and_sort_deployment_tags(tags):
    """
    Filter tags to only include deployment-related tags (dev, test, performance variants)
    and sort them in deployment order.
    
    Priority order:
    1. dev, azure-dev
    2. test, azure-test
    3. performance, azure-performance
    
    Returns a dict with:
    - 'sorted_tags': list of tags in priority order
    - 'found_categories': dict showing which tag types were found
    - 'missing_categories': list of missing standard tags
    """
    if isinstance(tags, dict) and tags.get("error"):
        return {"error": True, "details": tags.get("details")}
    
    tag_list = tags if isinstance(tags, list) else []
    
    # Define tag categories and their priority
    tag_categories = {
        'dev': {'priority': 1, 'variants': ['dev', 'azure-dev'], 'found': []},
        'test': {'priority': 2, 'variants': ['test', 'azure-test'], 'found': []},
        'performance': {'priority': 3, 'variants': ['performance', 'azure-performance'], 'found': []}
    }
    
    # Categorize tags
    for tag in tag_list:
        if not isinstance(tag, dict):
            continue
        tag_name = tag.get('name', '').lower()
        
        for category, info in tag_categories.items():
            if tag_name in info['variants']:
                info['found'].append(tag)
                break
    
    # Build sorted list
    sorted_tags = []
    found_categories = {}
    missing_categories = []
    
    for category, info in sorted(tag_categories.items(), key=lambda x: x[1]['priority']):
        if info['found']:
            sorted_tags.extend(info['found'])
            found_categories[category] = [t.get('name') for t in info['found']]
        else:
            missing_categories.append(category)
    
    return {
        'sorted_tags': sorted_tags,
        'found_categories': found_categories,
        'missing_categories': missing_categories
    }


def parse_tag_selection_input(selection_str, available_tags):
    if not selection_str:
        return []
    s = selection_str.strip()
    if s.lower() == "all":
        return [t['name'] for t in available_tags]
    parts = [p.strip() for p in s.split(",") if p.strip()]
    chosen = []
    idx_map = {str(i+1): t['name'] for i, t in enumerate(available_tags)}
    name_set = {t['name'] for t in available_tags}
    for p in parts:
        if p in idx_map:
            chosen.append(idx_map[p])
        elif p in name_set:
            chosen.append(p)
        else:
            continue
    seen = set()
    result = []
    for name in chosen:
        if name not in seen:
            seen.add(name)
            result.append(name)
    return result


def parse_version(vstr):
    m = re.search(r'(\d+(?:\.\d+)*)', vstr or "")
    if not m:
        return ()
    return tuple(int(x) for x in m.group(1).split('.'))


def find_parent_version_in_pom(content):
    m = re.search(r"<parent>[\s\S]*?<version>(.*?)</version>[\s\S]*?</parent>", content, re.I)
    if m:
        return m.group(1).strip()
    m2 = re.search(r"<version>(.*?)</version>", content, re.I)
    if m2:
        return m2.group(1).strip()
    return None


def check_files_already_match(pid, actions, branch_ref):
    """
    Check if all files in actions already match their desired state on the branch.
    
    Args:
        pid: Project ID
        actions: List of action dicts with file_path and content
        branch_ref: Branch to check against
    
    Returns:
        tuple: (all_match: bool, details: list of file status)
    """
    all_match = True
    details = []
    
    for act in actions:
        file_path = act['file_path']
        desired_content = act['content']
        quoted_path = urllib.parse.quote(file_path, safe='')
        
        try:
            current_res = api_call(f"projects/{pid}/repository/files/{quoted_path}?ref={urllib.parse.quote(branch_ref, safe='')}")
            if isinstance(current_res, dict) and "content" in current_res:
                current_content = base64.b64decode(current_res['content']).decode('utf-8')
                if current_content == desired_content:
                    details.append({'file': file_path, 'match': True})
                else:
                    details.append({'file': file_path, 'match': False, 'reason': 'content differs'})
                    all_match = False
            else:
                details.append({'file': file_path, 'match': False, 'reason': 'file not found or error'})
                all_match = False
        except Exception as e:
            details.append({'file': file_path, 'match': False, 'reason': f'error: {str(e)}'})
            all_match = False
    
    return all_match, details


def get_file_metadata(pid, file_path, branch_ref):
    """
    Get file metadata including last commit info.
    
    Args:
        pid: Project ID
        file_path: Path to file
        branch_ref: Branch reference
    
    Returns:
        dict with: last_commit_sha, last_commit_date, last_modified_by, commit_message
    """
    quoted_path = urllib.parse.quote(file_path, safe='')
    
    try:
            file_info = api_call(f"projects/{pid}/repository/files/{quoted_path}?ref={urllib.parse.quote(branch_ref, safe='')}")
        
        if isinstance(file_info, dict) and not file_info.get("error"):
            metadata = {
                'file_path': file_path,
                'last_commit_sha': file_info.get('last_commit_id', 'unknown'),
                'last_commit_date': 'unknown',
                'last_modified_by': 'unknown',
                'commit_message': 'unknown'
            }
            
                    commit_sha = file_info.get('last_commit_id')
            if commit_sha:
                try:
                    commit_info = api_call(f"projects/{pid}/repository/commits/{commit_sha}")
                    if isinstance(commit_info, dict) and not commit_info.get("error"):
                        metadata['last_commit_date'] = commit_info.get('committed_date', 'unknown')
                        metadata['last_modified_by'] = commit_info.get('author_name', 'unknown')
                        metadata['commit_message'] = commit_info.get('message', 'unknown').split('\n')[0]  # First line only
                except Exception as e:
                    log(f"Could not fetch commit details for {file_path}: {e}", "DEBUG")
            
            return metadata
        else:
            return None
    except Exception as e:
        log(f"Error getting metadata for {file_path}: {e}", "DEBUG")
        return None


def detect_conflicts(pid, file_path, base_content, our_content, remote_ref):
    """
    Detect if there are conflicts between our changes and remote changes.
    
    Returns:
        - None if no conflict detected
        - dict with conflict info if conflict detected
    """
    quoted_path = urllib.parse.quote(file_path, safe='')
    remote_res = api_call(f"projects/{pid}/repository/files/{quoted_path}?ref={urllib.parse.quote(remote_ref, safe='')}")
    
    if isinstance(remote_res, dict) and remote_res.get("error"):
        # File might not exist on remote, which is not a conflict
        return None
    
    if "content" not in remote_res:
        return None
    
    remote_content = base64.b64decode(remote_res['content']).decode('utf-8')
    
    # If remote content equals base content, no remote changes occurred
    if remote_content == base_content:
        return None
    
    # If remote content equals our desired content, no conflict (already applied)
    if remote_content == our_content:
        log(f"Remote {file_path} already contains our changes.", "INFO")
        return {"type": "already_applied", "file": file_path}
    
    # Check if we can do a three-way merge
    base_lines = base_content.splitlines(keepends=True)
    our_lines = our_content.splitlines(keepends=True)
    remote_lines = remote_content.splitlines(keepends=True)
    
    # Try to detect if changes overlap
    our_diff = list(difflib.unified_diff(base_lines, our_lines, lineterm=''))
    remote_diff = list(difflib.unified_diff(base_lines, remote_lines, lineterm=''))
    
    # Extract changed line numbers
    our_changed_lines = extract_changed_line_numbers(our_diff)
    remote_changed_lines = extract_changed_line_numbers(remote_diff)
    
    # Check for overlapping changes
    overlap = our_changed_lines.intersection(remote_changed_lines)
    
    if overlap:
        return {
            "type": "conflict",
            "file": file_path,
            "base_content": base_content,
            "our_content": our_content,
            "remote_content": remote_content,
            "overlapping_lines": sorted(overlap)
        }
    else:
        # Changes don't overlap, might be safe to merge
        return {
            "type": "non_overlapping",
            "file": file_path,
            "base_content": base_content,
            "our_content": our_content,
            "remote_content": remote_content
        }


def extract_changed_line_numbers(unified_diff):
    """Extract line numbers that were changed from a unified diff."""
    changed_lines = set()
    current_line = 0
    
    for line in unified_diff:
        if line.startswith('@@'):
            # Parse line number from hunk header
            match = re.search(r'@@ -(\d+),?(\d*) \+(\d+),?(\d*) @@', line)
            if match:
                current_line = int(match.group(1))
        elif line.startswith('-'):
            changed_lines.add(current_line)
            current_line += 1
        elif line.startswith('+'):
            changed_lines.add(current_line)
        elif line.startswith(' '):
            current_line += 1
    
    return changed_lines


def attempt_three_way_merge(base_content, our_content, remote_content):
    """
    Attempt a simple three-way merge.
    Returns merged content if successful, None if conflicts exist.
    """
    import difflib
    
    base_lines = base_content.splitlines(keepends=True)
    our_lines = our_content.splitlines(keepends=True)
    remote_lines = remote_content.splitlines(keepends=True)
    
    # Use difflib to generate a merge
    # This is a simplified approach; a real implementation would use proper 3-way merge
    our_diff = list(difflib.unified_diff(base_lines, our_lines, lineterm=''))
    remote_diff = list(difflib.unified_diff(base_lines, remote_lines, lineterm=''))
    
    # For simplicity, if both diffs exist and are different, we have a potential conflict
    # A real implementation would apply patches sequentially
    
    # Try applying our changes to remote content
    if remote_content == base_content:
        return our_content
    
    if our_content == base_content:
        return remote_content
    
    # Both changed - return None to indicate manual resolution needed
    return None


def handle_conflict(conflict_info, pid, p_name):
    """
    Handle detected conflicts by presenting options to the user.
    
    Returns:
        - "skip": skip this file
        - "ours": use our version
        - "theirs": use remote version  
        - "manual": user will manually merge
        - content string: merged content to use
    """
    file_path = conflict_info['file']
    
    if conflict_info['type'] == 'already_applied':
        log(f"File {file_path} already has our changes on remote. Skipping.", "INFO")
        return "skip"
    
    if conflict_info['type'] == 'non_overlapping':
        log(f"File {file_path} has non-overlapping changes. Attempting automatic merge...", "INFO")
        merged = attempt_three_way_merge(
            conflict_info['base_content'],
            conflict_info['our_content'],
            conflict_info['remote_content']
        )
        if merged:
            log(f"Successfully merged {file_path}.", "INFO")
            return merged
        else:
            log(f"Automatic merge failed for {file_path}.", "WARN")
    
    if conflict_info['type'] == 'conflict':
        print(f"\n{'='*80}")
        print(f"CONFLICT DETECTED in {file_path}")
        print(f"{'='*80}")
        print(f"Remote branch has changes that conflict with your local changes.")
        print(f"Overlapping line numbers: {conflict_info.get('overlapping_lines', 'unknown')}")
        print(f"\n--- OUR CHANGES ---")
        print(show_unified_diff(file_path, conflict_info['base_content'], conflict_info['our_content']))
        print(f"\n--- REMOTE CHANGES ---")
        print(show_unified_diff(file_path, conflict_info['base_content'], conflict_info['remote_content']))
        print(f"{'='*80}\n")
    
    print(f"\nConflict resolution options for {file_path}:")
    print("  1. Use our version (overwrite remote changes)")
    print("  2. Use remote version (discard our changes)")
    print("  3. Skip this file")
    print("  4. Abort entire operation")
    
    while True:
        try:
            choice = input("Choose [1/2/3/4]: ").strip()
        except (EOFError, KeyboardInterrupt):
            log("\n[INTERRUPT] User interrupted conflict resolution", "WARN")
            raise Exception("User interrupted during conflict resolution")
        
        if choice == '1':
            log(f"Using our version for {file_path}", "INFO")
            return "ours"
        elif choice == '2':
            log(f"Using remote version for {file_path}", "INFO")
            return "theirs"
        elif choice == '3':
            log(f"Skipping {file_path}", "INFO")
            return "skip"
        elif choice == '4':
            log(f"Aborting operation for project {p_name}", "WARN")
            raise Exception("User aborted due to conflict")
        else:
            print("Invalid choice. Please enter 1, 2, 3, or 4.")


def create_mr_for_project(pid, p_name, rollback_data):
    """
    Create a merge request for a project.
    
    Returns:
        dict: {'success': bool, 'idempotent': bool, 'url': str or None}
    """
    log(f"--- Creating MR for: {p_name} (project id: {pid}) ---", "INFO")
    
    if INTERRUPTED:
        log("MR creation interrupted", "WARN")
        return {'success': False, 'idempotent': False, 'url': None}
    
    br_check = api_call(f"projects/{pid}/repository/branches/{urllib.parse.quote(FEATURE_BRANCH, safe='')}")
    
    if not (isinstance(br_check, dict) and "name" in br_check):
        log(f"Feature branch '{FEATURE_BRANCH}' does not exist for {p_name}. Creating it...", "WARN")
        branch_created = create_feature_branch(pid, p_name)
        if not branch_created:
            log(f"[ERROR] Cannot create MR without feature branch for {p_name}", "ERROR")
            return {'success': False, 'idempotent': False, 'url': None}
        br_check = api_call(f"projects/{pid}/repository/branches/{urllib.parse.quote(FEATURE_BRANCH, safe='')}")
    
    log(f"Comparing {FEATURE_BRANCH} with {SOURCE_BRANCH}...", "INFO")
    try:
        compare_result = api_call(
            f"projects/{pid}/repository/compare?"
            f"from={urllib.parse.quote(SOURCE_BRANCH, safe='')}&"
            f"to={urllib.parse.quote(FEATURE_BRANCH, safe='')}"
        )
        
        if isinstance(compare_result, dict) and not compare_result.get("error"):
            diffs = compare_result.get('diffs', [])
            commits = compare_result.get('commits', [])
            
            if not diffs and not commits:
                log(f"[SKIP] Feature branch has no changes compared to {SOURCE_BRANCH}. Skipping MR creation.", "INFO")
                return {'success': False, 'idempotent': False, 'url': None}
            
            log(f"Branch has {len(commits)} commit(s) and {len(diffs)} file diff(s)", "INFO")
        else:
            log(f"Could not compare branches: {compare_result.get('details', 'unknown')}", "WARN")
    except Exception as e:
        log(f"Error comparing branches: {e}", "WARN")
    
    log(f"Checking if MR already exists for {FEATURE_BRANCH}...", "INFO")
    try:
        existing = api_call(f"projects/{pid}/merge_requests?state=opened&source_branch={urllib.parse.quote(FEATURE_BRANCH, safe='')}")
    except Exception as e:
        existing = {"error": True, "details": str(e)}

    has_open_mr = isinstance(existing, list) and len(existing) > 0
    if isinstance(existing, dict) and existing.get("error"):
        log(f"Could not verify existing MRs for {p_name}: {existing.get('details')}", "ERROR")
        return {'success': False, 'idempotent': False, 'url': None}

    if has_open_mr:
        try:
            existing_url = existing[0].get('web_url', 'unknown')
            log(f"[IDEMPOTENT] MR already exists: {existing_url}", "INFO")
            return {'success': True, 'idempotent': True, 'url': existing_url}
        except Exception:
            log("[IDEMPOTENT] MR already exists (could not parse response).", "INFO")
            return {'success': True, 'idempotent': True, 'url': None}
    
    log(f"Creating new MR for {p_name}...", "INFO")
    mr_payload = {"source_branch": FEATURE_BRANCH, "target_branch": SOURCE_BRANCH, "title": MR_TITLE}
    mr_result = api_call(f"projects/{pid}/merge_requests", "POST", mr_payload)
    
    if isinstance(mr_result, dict) and not mr_result.get("error"):
        mr_url = mr_result.get('web_url', 'N/A')
        log(f"[SUCCESS] MR created: {mr_url}", "INFO")
        return {'success': True, 'idempotent': False, 'url': mr_url}
    else:
        log(f"[ERROR] Failed to create MR: {mr_result.get('details', 'unknown error')}", "ERROR")
        return {'success': False, 'idempotent': False, 'url': None}


def process_project(pid, choices=None, show_full=True, rollback_data=None, mode='full', state=None):
    """
    Process a single project with file changes and/or deployment.
    
    Args:
        pid: Project ID
        choices: List of file changes to make (1=POM, 2=CI, 3=EB)
        show_full: Show full diffs vs summary
        rollback_data: Dict to store rollback snapshots
        mode: 'full' (changes+deploy/MR), 'mr_only' (just create MR), 'deploy_only' (just deploy)
        state: State dict for tracking progress
    
    Returns:
        Dict with status information
    """
    # Check for interruption at start
    if INTERRUPTED:
        log("Processing interrupted before starting project", "WARN")
        return {'project_id': pid, 'success': False, 'interrupted': True}
    
    project_start_time = time.time()
    
    project_info = api_call(f"projects/{pid}")
    p_name = project_info.get('name', f"ID:{pid}") if isinstance(project_info, dict) else f"ID:{pid}"
    log(f"--- Processing: {p_name} (project id: {pid}) ---", "INFO")
    
    result = {
        'project_id': pid,
        'project_name': p_name,
        'success': False,
        'committed': False,
        'deployed': False,
        'mr_created': False,
        'error': None,
        'interrupted': False,
        'idempotent_commit': False,
        'idempotent_mr': False,
        'idempotent_tags': []
    }
    
    try:
        # MODE: MR ONLY
        if mode == 'mr_only':
            mr_result = create_mr_for_project(pid, p_name, rollback_data)
            result['mr_created'] = mr_result['success']
            result['idempotent_mr'] = mr_result['idempotent']
            result['success'] = mr_result['success']
            
            if state is not None:
                if mr_result['success']:
                    state['completed_projects'].append(pid)
                else:
                    state['failed_projects'].append(pid)
                save_state(state)
            
            return result
        
        # MODE: FULL or DEPLOY_ONLY
        actions = []
        br_check = api_call(f"projects/{pid}/repository/branches/{urllib.parse.quote(FEATURE_BRANCH, safe='')}")
        current_ref = FEATURE_BRANCH if isinstance(br_check, dict) and "name" in br_check else SOURCE_BRANCH
        
        # STEP 1: Prepare file changes (skip for deploy_only mode)
        if mode == 'full' and choices:
                    if INTERRUPTED:
                log("Interrupted during file preparation", "WARN")
                result['interrupted'] = True
                return result
            
            # 1. POM
            if '1' in choices:
                res = api_call(f"projects/{pid}/repository/files/{urllib.parse.quote('pom.xml', safe='')}?ref={urllib.parse.quote(current_ref, safe='')}")
                if isinstance(res, dict) and res.get("error"):
                    log(f"Failed to fetch pom.xml for {p_name}: {res.get('details')}", "ERROR")
                elif "content" in res:
                    orig = base64.b64decode(res['content']).decode('utf-8')
                    upd = re.sub(r"<(java\.version|maven\.compiler\.(source|target|release))>11</\1>", r"<\1>17</\1>", orig)
                    if "<parent>" in upd:
                        upd = re.sub(r"<parent>[\s\S]*?</parent>", update_parent_block, upd)
                    if orig != upd:
                        actions.append({"action": "update", "file_path": "pom.xml", "content": upd, "old_content": orig})

            # 2. CI
            if '2' in choices:
                res = api_call(f"projects/{pid}/repository/files/{urllib.parse.quote('.gitlab-ci.yml', safe='')}?ref={urllib.parse.quote(current_ref, safe='')}")
                if isinstance(res, dict) and res.get("error"):
                    log(f"Failed to fetch .gitlab-ci.yml for {p_name}: {res.get('details')}", "ERROR")
                elif "content" in res:
                    orig = base64.b64decode(res['content']).decode('utf-8')
                    upd = re.sub(r"^\s*image:.*(\n|$)", "", orig, flags=re.MULTILINE)
                    if orig != upd:
                        actions.append({"action": "update", "file_path": ".gitlab-ci.yml", "content": upd, "old_content": orig})

            # 3. EB
            if '3' in choices:
                path = urllib.parse.quote(".elasticbeanstalk/config.yml", safe='')
                res = api_call(f"projects/{pid}/repository/files/{path}?ref={urllib.parse.quote(current_ref, safe='')}")
                if isinstance(res, dict) and res.get("error"):
                    log(f"Failed to fetch .elasticbeanstalk/config.yml for {p_name}: {res.get('details')}", "ERROR")
                elif "content" in res:
                    orig = base64.b64decode(res['content']).decode('utf-8')
                    upd = re.sub(r"(default_platform:\s*).*$", f"default_platform: {NEW_DEFAULT_PLATFORM}", orig, flags=re.MULTILINE)
                    if orig != upd:
                        actions.append({"action": "update", "file_path": ".elasticbeanstalk/config.yml", "content": upd, "old_content": orig})

                    if actions:
                print(f"\n{'='*70}")
                print(f"PREVIEW: Proposed changes for {p_name}")
                print(f"{'='*70}")
                
                            log("Fetching file metadata...", "INFO")
                for action in actions:
                    metadata = get_file_metadata(pid, action['file_path'], current_ref)
                    if metadata:
                        print(f"\nFile: {action['file_path']}")
                        print(f"   Last modified by: {metadata['last_modified_by']}")
                        print(f"   Last commit: {metadata['last_commit_sha'][:8] if metadata['last_commit_sha'] != 'unknown' else 'unknown'}")
                        print(f"   Commit date: {metadata['last_commit_date']}")
                        print(f"   Commit message: {metadata['commit_message'][:60]}...")
                    else:
                        print(f"\nFile: {action['file_path']}")
                        print(f"   (Metadata unavailable)")
                
                print()  # Blank line before diffs
                
                            for action in actions:
                    if show_full:
                        ud = show_unified_diff(action['file_path'], action['old_content'], action['content'])
                        if ud.strip():
                            print(f"\n--- Diff for {action['file_path']} ---")
                            print(ud)
                            print(f"--- End diff for {action['file_path']} ---\n")
                        else:
                            print(f"   {action['file_path']} -> [+0 | -0 lines] (no textual diff)")
                    else:
                        diff = list(difflib.ndiff(action['old_content'].splitlines(), action['content'].splitlines()))
                        added = len([l for l in diff if l.startswith('+ ')])
                        removed = len([l for l in diff if l.startswith('- ')])
                        print(f"   {action['file_path']} -> [+{added} | -{removed} lines]")
                print(f"{'='*70}\n")
                
                # Ask to commit
                if not prompt_yes_no(f"Commit these changes for {p_name}?", default=True):
                    log(f"User skipped commit for {p_name}", "INFO")
                    if state is not None:
                        state['skipped_projects'].append(pid)
                        save_state(state)
                    return result
                
                # Check for interruption before committing
                if INTERRUPTED:
                    log("Interrupted before commit", "WARN")
                    result['interrupted'] = True
                    return result
                
                            if rollback_data is not None:
                    files_to_backup = [action['file_path'] for action in actions]
                    snapshot = create_rollback_snapshot(pid, p_name, current_ref, files_to_backup)
                    rollback_data['snapshots'].append(snapshot)
                    log(f"Created rollback snapshot for {p_name}", "DEBUG")
                
                            if not (isinstance(br_check, dict) and "name" in br_check):
                    branch_created = create_feature_branch(pid, p_name)
                    if not branch_created:
                        log(f"[ERROR] Cannot proceed without feature branch for {p_name}", "ERROR")
                        result['error'] = "Failed to create feature branch"
                        
                        if state is not None:
                            state['failed_projects'].append(pid)
                            save_state(state)
                        
                        return result
                
                # Conflict detection
                log(f"Checking for conflicts on {FEATURE_BRANCH}...", "INFO")
                conflicts_detected = []
                
                for act in actions:
                    file_path = act['file_path']
                    base_content = act['old_content']
                    our_content = act['content']
                    
                    conflict = detect_conflicts(pid, file_path, base_content, our_content, FEATURE_BRANCH)
                    if conflict:
                        conflicts_detected.append((act, conflict))
                
                if conflicts_detected:
                    log(f"Found {len(conflicts_detected)} file(s) with potential conflicts.", "WARN")
                    
                                    for act, conflict in conflicts_detected:
                        try:
                            resolution = handle_conflict(conflict, pid, p_name)
                            
                            if resolution == "skip":
                                actions = [a for a in actions if a['file_path'] != act['file_path']]
                            elif resolution == "ours":
                                pass
                            elif resolution == "theirs":
                                actions = [a for a in actions if a['file_path'] != act['file_path']]
                            elif isinstance(resolution, str) and resolution not in ["skip", "ours", "theirs"]:
                                for a in actions:
                                    if a['file_path'] == act['file_path']:
                                        a['content'] = resolution
                                        break
                        except Exception as e:
                            log(f"Conflict handling aborted: {e}", "ERROR")
                            result['error'] = str(e)
                            
                            if state is not None:
                                state['failed_projects'].append(pid)
                                save_state(state)
                            
                            return result
                else:
                    log("No conflicts detected.", "INFO")
                
                if not actions:
                    log(f"No remaining changes to commit for {p_name} after conflict resolution.", "INFO")
                else:
                    # IDEMPOTENCY CHECK: Verify if files already match desired state
                    log(f"Checking if changes already exist on {FEATURE_BRANCH}...", "INFO")
                    files_already_match, match_details = check_files_already_match(pid, actions, FEATURE_BRANCH)
                    
                    if files_already_match:
                        log(f"[IDEMPOTENT] All files already match desired state on {FEATURE_BRANCH}. Skipping commit.", "INFO")
                        for detail in match_details:
                            if detail['match']:
                                log(f"  {detail['file']} already matches", "DEBUG")
                        result['committed'] = True  # Mark as "committed" since state is correct
                        result['idempotent_commit'] = True
                    else:
                        log(f"Files need updating on {FEATURE_BRANCH}:", "INFO")
                        for detail in match_details:
                            if not detail['match']:
                                log(f"  ✗ {detail['file']}: {detail.get('reason', 'needs update')}", "DEBUG")
                        
                        # Commit changes
                        commit_payload = {
                            "branch": FEATURE_BRANCH,
                            "commit_message": f"fix: {UPGRADE_TYPE}",
                            "actions": [{"action": act["action"], "file_path": act["file_path"], "content": act["content"]} for act in actions]
                        }
                        commit_resp = api_call(f"projects/{pid}/repository/commits", "POST", commit_payload)
                        
                        if isinstance(commit_resp, dict) and not commit_resp.get("error"):
                            log(f"[SUCCESS] Changes committed for {p_name}", "INFO")
                            result['committed'] = True
                        else:
                            log(f"[ERROR] Failed to commit changes: {commit_resp.get('details', 'unknown')}", "ERROR")
                            result['error'] = "Commit failed"
                            
                            # Auto-rollback on failure
                            if AUTO_ROLLBACK_ON_FAILURE and rollback_data and rollback_data.get('snapshots'):
                                log(f"[AUTO-ROLLBACK] Rolling back changes for {p_name}...", "WARN")
                                rollback_file = save_rollback_data(rollback_data)
                                perform_rollback(rollback_file)
                            
                            if state is not None:
                                state['failed_projects'].append(pid)
                                save_state(state)
                            
                            return result
            else:
                log(f"No file changes needed for {p_name}", "INFO")
                
                if not (isinstance(br_check, dict) and "name" in br_check):
                    log(f"No changes needed and feature branch does not exist. Skipping deployment/MR options.", "INFO")
                    result['success'] = True
                    if state is not None:
                        state['completed_projects'].append(pid)
                        save_state(state)
                    return result
                
                try:
                    compare_result = api_call(
                        f"projects/{pid}/repository/compare?"
                        f"from={urllib.parse.quote(SOURCE_BRANCH, safe='')}&"
                        f"to={urllib.parse.quote(FEATURE_BRANCH, safe='')}"
                    )
                    
                    if isinstance(compare_result, dict) and not compare_result.get("error"):
                        diffs = compare_result.get('diffs', [])
                        commits = compare_result.get('commits', [])
                        
                        if not diffs and not commits:
                            log(f"Feature branch has no changes compared to {SOURCE_BRANCH}. Skipping deployment/MR options.", "INFO")
                            result['success'] = True
                            if state is not None:
                                state['completed_projects'].append(pid)
                                save_state(state)
                            return result
                except Exception as e:
                    log(f"Could not compare branches: {e}", "WARN")
        
        if INTERRUPTED:
            log("Interrupted before next step selection", "WARN")
            result['interrupted'] = True
            return result
        
        if mode == 'full' or mode == 'deploy_only':
            print(f"\n{'='*60}")
            print(f"What would you like to do next for {p_name}?")
            print(f"{'='*60}")
            print("1. Deploy changes (create tags + trigger deployment)")
            print("2. Create Merge Request")
            print("3. Skip (do nothing)")
            
            try:
                choice = input("Choose [1/2/3] (default: 1): ").strip()
            except (EOFError, KeyboardInterrupt):
                log("\n[INTERRUPT] User interrupted next step selection", "WARN")
                result['interrupted'] = True
                return result
            
            if choice == '2':
                            mr_result = create_mr_for_project(pid, p_name, rollback_data)
                result['mr_created'] = mr_result['success']
                result['idempotent_mr'] = mr_result['idempotent']
                result['success'] = result['committed'] or mr_result['success']
                
            elif choice == '3':
                # Skip
                log(f"Skipping deployment/MR for {p_name}", "INFO")
                result['success'] = result['committed']
                
            else:
                # Default: Deploy (option 1)
                deploy_result = handle_deployment(pid, p_name, state)
                result['deployed'] = deploy_result['success']
                result['idempotent_tags'] = deploy_result['idempotent_tags']
                result['success'] = result['committed'] or deploy_result['success']
        
            if state is not None:
            if result['success']:
                state['completed_projects'].append(pid)
            else:
                state['failed_projects'].append(pid)
            save_state(state)
        
            project_end_time = time.time()
        project_duration = project_end_time - project_start_time
        minutes = int(project_duration // 60)
        seconds = int(project_duration % 60)
        
        if state is not None:
            state['project_times'][str(pid)] = {
                'name': p_name,
                'duration_seconds': project_duration
            }
            save_state(state)
        
        if minutes > 0:
            log(f"[COMPLETE] Completed {p_name} in {minutes}m {seconds}s", "INFO")
        else:
            log(f"[COMPLETE] Completed {p_name} in {seconds}s", "INFO")
        
        return result
        
    except Exception as e:
        log(f"[ERROR] Exception processing {p_name}: {e}", "ERROR")
        result['error'] = str(e)
        
        # Auto-rollback on failure
        if AUTO_ROLLBACK_ON_FAILURE and rollback_data and rollback_data.get('snapshots'):
            log(f"[AUTO-ROLLBACK] Rolling back changes for {p_name}...", "WARN")
            rollback_file = save_rollback_data(rollback_data)
            perform_rollback(rollback_file)
        
        if state is not None:
            state['failed_projects'].append(pid)
            save_state(state)
        
        return result


def handle_deployment(pid, p_name, state=None):
    """
    Handle tag creation and deployment orchestration for a project.
    
    Args:
        pid: Project ID
        p_name: Project name
        state: State dict for saving progress at tag level
    
    Returns:
        dict: {'success': bool, 'idempotent_tags': list of tag names that were skipped}
    """
    log(f"Starting deployment for {p_name}...", "INFO")
    
    if INTERRUPTED:
        log("Deployment interrupted", "WARN")
        return {'success': False, 'idempotent_tags': []}
    
    # Fetch tags
    tags_resp = fetch_all_tags_for_project(pid)
    if isinstance(tags_resp, dict) and tags_resp.get("error"):
        log(f"Could not fetch tags for project {p_name}: {tags_resp.get('details')}", "ERROR")
        return {'success': False, 'idempotent_tags': []}

    available_tags_all = tags_resp if isinstance(tags_resp, list) else []
    
    # Filter and sort deployment tags
    filter_result = filter_and_sort_deployment_tags(available_tags_all)
    
    if isinstance(filter_result, dict) and filter_result.get("error"):
        log(f"Error filtering tags: {filter_result.get('details')}", "ERROR")
        return {'success': False, 'idempotent_tags': []}
    
    available_tags = filter_result['sorted_tags']
    found_categories = filter_result['found_categories']
    missing_categories = filter_result['missing_categories']
    
    log(f"Deployment tags found for {p_name}:", "INFO")
    if found_categories:
        for category, tag_names in found_categories.items():
            log(f"  {category.upper()}: {', '.join(tag_names)}", "INFO")
    
    if missing_categories:
        for category in missing_categories:
            log(f"  {category.upper()}: NOT FOUND", "WARN")
    
    if not available_tags:
        log(f"No deployment tags found for {p_name}. Skipping deployment.", "WARN")
        return {'success': False, 'idempotent_tags': []}
    
    print(f"\nAvailable deployment tags for {p_name}:")
    for i, t in enumerate(available_tags):
        commit_id = (t.get('commit') or {}).get('id', '') if isinstance(t, dict) else ''
        protected_marker = " [PROTECTED]" if t.get('protected') else ""
        print(f"  {i+1:>3}. {t.get('name', '')}{protected_marker}  {commit_id[:8] if commit_id else ''}")
    
    try:
        sel = input("Select tags to deploy (e.g. '1' or 'dev' or 'all'): ").strip()
    except (EOFError, KeyboardInterrupt):
        log("\n[INTERRUPT] User interrupted tag selection", "WARN")
        return {'success': False, 'idempotent_tags': []}
    
    selected_tag_names = parse_tag_selection_input(sel, available_tags)
    
    if not selected_tag_names:
        log("No tags selected; skipping deployment.", "INFO")
        return {'success': False, 'idempotent_tags': []}
    
    # Check for already completed tags when resuming
    if state and str(pid) in state.get('project_details', {}):
        completed_tags = state['project_details'][str(pid)].get('completed_tags', [])
        if completed_tags:
            log(f"Found {len(completed_tags)} already completed tag(s) for this project: {', '.join(completed_tags)}", "INFO")
            original_count = len(selected_tag_names)
            selected_tag_names = [tag for tag in selected_tag_names if tag not in completed_tags]
            skipped_count = original_count - len(selected_tag_names)
            if skipped_count > 0:
                log(f"Skipping {skipped_count} already completed tag(s) from selection", "INFO")
            
            if not selected_tag_names:
                log("All selected tags already completed. Nothing to do.", "INFO")
                return {'success': True, 'idempotent_tags': completed_tags}
    
    branch_info = api_call(f"projects/{pid}/repository/branches/{urllib.parse.quote(FEATURE_BRANCH, safe='')}")
    feature_head = None
    if isinstance(branch_info, dict) and not branch_info.get("error"):
        feature_head = (branch_info.get("commit") or {}).get("id")
    
    deployment_successful = False
    idempotent_tags = []
    
    for tag_name in selected_tag_names:
            if INTERRUPTED:
            log("Deployment interrupted during tag processing", "WARN")
            break
        
        log(f"\nProcessing tag: {tag_name}", "INFO")
        
            quoted_tag = urllib.parse.quote(tag_name, safe='')
        tag_obj = next((t for t in available_tags_all if isinstance(t, dict) and t.get('name') == tag_name), None)
        
        if tag_obj and tag_obj.get('protected'):
            log(f"Skipping PROTECTED tag '{tag_name}'", "ERROR")
            continue
        
        # IDEMPOTENCY CHECK: If tag already points to feature HEAD, skip
        tag_commit_id = (tag_obj.get('commit') or {}).get('id') if tag_obj else None
        
        if tag_commit_id and feature_head and tag_commit_id == feature_head:
            log(f"[IDEMPOTENT] Tag '{tag_name}' already points to feature branch HEAD ({feature_head[:8]}). Skipping tag recreation.", "INFO")
            
                    idempotent_tags.append(tag_name)
            
                    log(f"Tag '{tag_name}' is already correct. Proceeding with deployment check...", "INFO")
            created_tag_commit = tag_commit_id
            
        else:
                    if tag_obj:
                log(f"Deleting existing tag '{tag_name}' (currently at {tag_commit_id[:8] if tag_commit_id else 'unknown'})...", "INFO")
                api_call(f"projects/{pid}/repository/tags/{quoted_tag}", method="DELETE")
            
                    log(f"Creating tag '{tag_name}' on {FEATURE_BRANCH} ({feature_head[:8] if feature_head else 'unknown'})...", "INFO")
            t_res = api_call(f"projects/{pid}/repository/tags", "POST", {"tag_name": tag_name, "ref": FEATURE_BRANCH})
            
            if isinstance(t_res, dict) and not t_res.get("error"):
                log(f"Created tag '{tag_name}' on {FEATURE_BRANCH}", "INFO")
                created_tag_commit = (t_res.get('commit') or {}).get('id') if isinstance(t_res, dict) else None
            else:
                log(f"Failed to create tag '{tag_name}': {t_res.get('details')}", "ERROR")
                continue
        
        # Proceed with deployment if we have a commit
        if created_tag_commit:
                            log("Waiting 10 seconds for pipeline to be triggered...", "INFO")
                time.sleep(10)
                
                            if INTERRUPTED:
                    log("Deployment interrupted during pipeline wait", "WARN")
                    break
                
                pipeline = get_pipeline_for_commit(pid, created_tag_commit)
                if not pipeline:
                    log(f"No pipeline found for commit {created_tag_commit[:8]}", "WARN")
                    continue
                
                pipeline_id = pipeline.get('id')
                log(f"Found pipeline {pipeline_id}", "INFO")
                
                            result = wait_for_pipeline_completion(pid, pipeline_id, timeout=1800, check_interval=30)
                
                if result['status'] == 'interrupted':
                    log("Pipeline monitoring interrupted", "WARN")
                    break
                
                if result['status'] != 'success':
                    log(f"Build {result['status']} for tag '{tag_name}'", "ERROR")
                    continue
                
                log(f"Build succeeded for tag '{tag_name}'!", "INFO")
                
                            jobs = get_pipeline_jobs(pid, pipeline_id)
                
                            terminate_job = find_job_by_name(jobs, 'eb-terminate')
                if terminate_job and terminate_job.get('status') == 'manual':
                    log("Triggering 'eb-terminate' job...", "INFO")
                    trigger_manual_job(pid, terminate_job.get('id'))
                    terminate_result = wait_for_job_completion(pid, terminate_job.get('id'), timeout=900)
                    
                    if terminate_result['status'] == 'interrupted':
                        log("Terminate job interrupted", "WARN")
                        break
                    
                    if terminate_result['status'] != 'success':
                        log(f"eb-terminate job {terminate_result['status']}", "ERROR")
                        continue
                
                            deploy_job_name = map_tag_to_deploy_job(tag_name)
                if deploy_job_name:
                    jobs = get_pipeline_jobs(pid, pipeline_id)
                    deploy_job = find_job_by_name(jobs, deploy_job_name)
                    
                    if deploy_job and deploy_job.get('status') == 'manual':
                        log(f"Triggering '{deploy_job_name}' job...", "INFO")
                        trigger_manual_job(pid, deploy_job.get('id'))
                        deploy_result = wait_for_job_completion(pid, deploy_job.get('id'), timeout=1200)
                        
                        if deploy_result['status'] == 'interrupted':
                            log("Deploy job interrupted", "WARN")
                            break
                        
                        if deploy_result['status'] == 'success':
                            log(f"[SUCCESS] Deployment completed for tag '{tag_name}'!", "INFO")
                            deployment_successful = True
                            
                                                    if state is not None:
                                if str(pid) not in state['project_details']:
                                    state['project_details'][str(pid)] = {
                                        'completed_tags': [],
                                        'failed_tags': [],
                                        'last_pipeline_id': None
                                    }
                                state['project_details'][str(pid)]['completed_tags'].append(tag_name)
                                state['project_details'][str(pid)]['last_pipeline_id'] = pipeline_id
                                save_state(state)
                                log(f"State saved: tag '{tag_name}' marked complete for project {pid}", "DEBUG")
                        else:
                            log(f"Deployment {deploy_result['status']} for tag '{tag_name}'", "ERROR")
                                                    if state is not None:
                                if str(pid) not in state['project_details']:
                                    state['project_details'][str(pid)] = {
                                        'completed_tags': [],
                                        'failed_tags': [],
                                        'last_pipeline_id': None
                                    }
                                state['project_details'][str(pid)]['failed_tags'].append(tag_name)
                                save_state(state)
        else:
            log(f"No commit available for tag '{tag_name}', skipping deployment", "WARN")
    
    return {'success': deployment_successful, 'idempotent_tags': idempotent_tags}


def bulk_create_mrs(project_ids, rollback_data, state):
    """
    Create merge requests for all projects in bulk.
    
    Args:
        project_ids: List of project IDs
        rollback_data: Dict to store rollback snapshots
        state: State dict for tracking progress
    
    Returns:
        Dict with success/failure counts
    """
    log("=" * 70, "INFO")
    log("BULK MR CREATION MODE", "INFO")
    log("=" * 70, "INFO")
    
    results = {
        'total': len(project_ids),
        'success': 0,
        'failed': 0,
        'skipped': 0,
        'interrupted': 0
    }
    
    for pid in project_ids:
            if INTERRUPTED:
            log("Bulk MR creation interrupted", "WARN")
            results['interrupted'] += 1
            break
        
        try:
            result = process_project(pid, mode='mr_only', rollback_data=rollback_data, state=state)
            
            if result.get('interrupted'):
                results['interrupted'] += 1
                break
            elif result['mr_created']:
                results['success'] += 1
            elif result['error']:
                results['failed'] += 1
            else:
                results['skipped'] += 1
                
        except Exception as e:
            log(f"Error processing project {pid}: {e}", "ERROR")
            results['failed'] += 1
    
    # Summary
    log("\n" + "=" * 70, "INFO")
    log("BULK MR CREATION COMPLETE", "INFO")
    log("=" * 70, "INFO")
    log(f"Total Projects: {results['total']}", "INFO")
    log(f"Success: {results['success']}", "INFO")
    log(f"Failed: {results['failed']}", "INFO")
    log(f"Skipped: {results['skipped']}", "INFO")
    if results['interrupted'] > 0:
        log(f"Interrupted: {results['interrupted']}", "WARN")
    
    return results


# ============================================================================
# TAB-COMPLETION FUNCTIONS
# ============================================================================

class ProjectCompleter:
    """Tab-completion handler for project names and IDs"""
    
    def __init__(self, projects):
        self.projects = projects
        self.project_names = [name for name in projects.values()]
        self.project_ids = [str(pid) for pid in projects.keys()]
        self.options = self.project_names + self.project_ids + ['all']
    
    def complete(self, text, state):
        """
        Readline completion function.
        
        Args:
            text: Current text being completed
            state: Current iteration (0, 1, 2, ... for each match)
        
        Returns:
            Next matching option or None when done
        """
        if state == 0:
            if text:
                # Find all options that start with the text (case-insensitive)
                self.matches = [
                    option for option in self.options 
                    if option.lower().startswith(text.lower())
                ]
            else:
                # No text - return all options
                self.matches = self.options[:]
        
        try:
            return self.matches[state]
        except IndexError:
            return None


def setup_tab_completion(projects):
    """Setup readline tab-completion for projects"""
    completer = ProjectCompleter(projects)
    readline.set_completer(completer.complete)
    
    # Set delimiters to allow hyphens in project names
    readline.set_completer_delims(' \t\n,')
    
    # Platform-specific binding
    if 'libedit' in readline.__doc__:
        # macOS (uses libedit)
        readline.parse_and_bind("bind ^I rl_complete")
    else:
        # Linux (uses GNU readline)
        readline.parse_and_bind("tab: complete")


def parse_project_input(user_input, projects):
    """
    Parse user input and return list of project IDs.
    
    Supports:
    - Project IDs: "101,102,103"
    - Project names: "user-authentication-service,payment-gateway-api"
    - Mixed: "101,payment-gateway-api,103"
    - Ranges: "101-105"
    - 'all' keyword
    
    Args:
        user_input: String from user
        projects: Dict of {id: name}
    
    Returns:
        List of project IDs
    """
    if not user_input or user_input.strip().lower() == 'all':
        return sorted(list(projects.keys()))
    
    name_to_id = {name: pid for pid, name in projects.items()}
    
    selected = []
    parts = [p.strip() for p in user_input.split(',')]
    
    for part in parts:
        # Check for range (e.g., "101-105")
        if '-' in part and not any(c.isalpha() for c in part):
            try:
                start, end = part.split('-')
                start_id = int(start.strip())
                end_id = int(end.strip())
                for pid in range(start_id, end_id + 1):
                    if pid in projects:
                        selected.append(pid)
                    else:
                        log(f"Project ID {pid} not found in range {start}-{end}", "WARN")
            except ValueError:
                log(f"Invalid range: {part}", "WARN")
                continue
        
        # Check if it's a project ID
        elif part.isdigit():
            pid = int(part)
            if pid in projects:
                selected.append(pid)
            else:
                log(f"Unknown project ID: {pid}", "WARN")
        
        # Check if it's a project name
        elif part in name_to_id:
            selected.append(name_to_id[part])
        
        # Try fuzzy matching
        else:
            matches = [name for name in name_to_id if part.lower() in name.lower()]
            if len(matches) == 1:
                log(f"Auto-matched '{part}' to '{matches[0]}'", "INFO")
                selected.append(name_to_id[matches[0]])
            elif len(matches) > 1:
                log(f"Ambiguous project '{part}'. Matches: {', '.join(matches[:3])}", "WARN")
            else:
                log(f"Unknown project: {part}", "WARN")
    
    # Remove duplicates while preserving order
    seen = set()
    result = []
    for pid in selected:
        if pid not in seen:
            seen.add(pid)
            result.append(pid)
    
    return result


def main():
    global TOKEN, PROJECT_IDS, BASE_URL, INTERRUPTED

    # Setup signal handlers for graceful interruption
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    parser = argparse.ArgumentParser(description="Batch Java migration helper for GitLab projects.")
    parser.add_argument("--projects", help="Comma-separated project IDs to process (overrides PROJECT_IDS)", default="")
    parser.add_argument("--full-diff", help="Show full unified diffs (default: True)", action="store_true", default=True)
    parser.add_argument("--summary-only", help="Show only line-count summary (overrides --full-diff)", action="store_true")
    parser.add_argument("--token", help="GitLab private token string (overrides file-based tokens)", default="")
    parser.add_argument("--base-url", help="GitLab API base URL (overrides .env and script constant)", default="")
    parser.add_argument("--rollback", help="Rollback changes from a previous migration using rollback file", default="")
    parser.add_argument("--mode", help="Execution mode", choices=['full', 'mr_bulk', 'deploy_only'], default=None)
    parser.add_argument("--resume", help="Resume from a previous state file", default="")
    
    args = parser.parse_args()

    if args.rollback:
        perform_rollback(args.rollback)
        sys.exit(0)

    # Setup file logging
    log_file = setup_file_logging(LOG_DIR_MIGRATION)
    log(f"Logging to file: {log_file}", "INFO")
    
    # Initialize state tracking
    state = {
        'completed_projects': [],
        'failed_projects': [],
        'skipped_projects': [],
        'start_time': datetime.datetime.now().isoformat(),
        'project_details': {},
        'project_times': {}
    }
    
    # Resume from previous state if requested
    if args.resume:
        loaded_state = load_state(args.resume)
        if loaded_state:
            state = loaded_state
            log(f"Resumed from previous state: {args.resume}", "INFO")
            log(f"Completed: {len(state.get('completed_projects', []))} projects", "INFO")
            log(f"Failed: {len(state.get('failed_projects', []))} projects", "INFO")
        else:
            log(f"Could not load state from {args.resume}, starting fresh", "WARN")
    
    # Initialize rollback tracking
    rollback_data = {
        'snapshots': []
    }
    
    show_full = args.full_diff and not args.summary_only
    
    if args.token:
        TOKEN = args.token.strip()
        log("Using token from command line argument", "INFO")
    elif not TOKEN:
        file_token = load_token_from_file()
        if file_token:
            TOKEN = file_token
        else:
            log("No token found in .env or token.txt files", "WARN")
    
    if args.base_url:
        BASE_URL = args.base_url.strip().rstrip('/')
        log(f"Using BASE_URL from command line: {BASE_URL}", "INFO")
    else:
        try:
            script_dir = os.path.dirname(os.path.abspath(__file__))
            env_file = os.path.join(script_dir, '.env')
            if os.path.exists(env_file):
                with open(env_file, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith('#'):
                            continue
                        if '=' in line:
                            key, value = line.split('=', 1)
                            key = key.strip()
                            value = value.strip()
                            if value.startswith('"') and value.endswith('"'):
                                value = value[1:-1]
                            elif value.startswith("'") and value.endswith("'"):
                                value = value[1:-1]
                            
                            if key in ('BASE_URL', 'GITLAB_BASE_URL', 'GITLAB_URL'):
                                BASE_URL = value.rstrip('/')
                                log(f"BASE_URL loaded from .env file: {BASE_URL}", "INFO")
                                break
        except Exception as e:
            log(f"Error loading BASE_URL from .env: {e}", "WARN")

    if not TOKEN:
        log("No token supplied. Please provide token via:", "ERROR")
        log("  1. --token command line argument", "ERROR")
        log("  2. .env file (GITLAB_TOKEN=your_token or TOKEN=your_token)", "ERROR")
        log("  3. token.txt file in script directory", "ERROR")
        sys.exit(1)
    
    # Validate token
    log("Validating GitLab token...", "INFO")
    token_validation = validate_and_log_token_info(TOKEN, BASE_URL)
    
    if not token_validation['valid']:
        log("Token validation failed. Please check your token and try again.", "ERROR")
        sys.exit(1)
    
    if token_validation.get('info') and token_validation['info'].get('access_level'):
        access_level = token_validation['info']['access_level']
        access_level_names = {
            10: 'Guest',
            20: 'Reporter',
            30: 'Developer',
            40: 'Maintainer',
            50: 'Owner'
        }
        access_name = access_level_names.get(access_level, f'Level {access_level}')
        log("=" * 70, "INFO")
        log(f"TOKEN ACCESS LEVEL: {access_name} ({access_level})", "INFO")
        log("=" * 70, "INFO")

    project_ids = list(PROJECT_NAMES.keys()) if PROJECT_NAMES else []
    if args.projects:
        try:
            project_ids = [int(x.strip()) for x in args.projects.split(",") if x.strip()]
        except Exception:
            log("Invalid --projects value. Provide comma-separated integers.", "ERROR")
            sys.exit(1)
    else:
        # Interactive mode with tab-completion
        if PROJECT_NAMES:
            print("\n" + "=" * 70)
            print("PROJECT SELECTION (Tab-Completion Enabled)")
            print("=" * 70)
            print("\nAvailable Projects:")
            
                    for pid, name in sorted(PROJECT_NAMES.items()):
                print(f"  {pid:>3}: {name}")
            
            print("\n" + "-" * 70)
            print("How to select projects:")
            print("  • Type partial name + TAB  (e.g., 'use' + TAB → 'user-authentication-service')")
            print("  • Type ID + TAB            (e.g., '10' + TAB → shows '101, 102, ...')")
            print("  • Use ranges               (e.g., '101-105')")
            print("  • Type 'all'               (selects all projects)")
            print("  • Separate with commas     (e.g., '101,user-auth,105')")
            print("-" * 70 + "\n")
            
            # Enable tab-completion
            setup_tab_completion(PROJECT_NAMES)
            
            try:
                user_input = input("Enter projects: ").strip()
                project_ids = parse_project_input(user_input, PROJECT_NAMES)
            except (KeyboardInterrupt, EOFError):
                log("\n[INTERRUPT] Project selection cancelled", "WARN")
                sys.exit(0)
            
            if not project_ids:
                log("No valid projects selected.", "ERROR")
                sys.exit(1)
            
                    print("\n" + "=" * 70)
            log(f"Selected {len(project_ids)} project(s):", "INFO")
            for pid in project_ids:
                log(f"  • {pid:>3}: {PROJECT_NAMES.get(pid, 'Unknown')}", "INFO")
            print("=" * 70 + "\n")
    
    if not project_ids:
        log("Please add PROJECT_IDS or pass --projects.", "ERROR")
        sys.exit(1)
    
    # Filter out already completed projects if resuming
    if args.resume and state.get('completed_projects'):
        completed_set = set(state['completed_projects'])
        original_count = len(project_ids)
        project_ids = [pid for pid in project_ids if pid not in completed_set]
        skipped_count = original_count - len(project_ids)
        if skipped_count > 0:
            log(f"Resuming: Skipping {skipped_count} already completed project(s)", "INFO")
    
    # TOP-LEVEL MODE SELECTION
    mode = args.mode
    
    if not mode:
        print("\n" + "=" * 70)
        print("MIGRATION TOOL - MODE SELECTION")
        print("=" * 70)
        print("\nChoose your operation mode:\n")
        print("1. FULL MIGRATION (Changes + Commit + Deploy/MR)")
        print("   - Show file change previews")
        print("   - Ask to commit each project")
        print("   - After commit, choose: Deploy OR Create MR")
        print()
        print("2. BULK MR CREATION")
        print("   - Create MRs for all projects at once")
        print("   - Uses existing feature branches")
        print("   - No file changes or deployments")
        print()
        print("3. DEPLOY ONLY")
        print("   - Skip file changes")
        print("   - Only handle tag creation and deployment")
        print()
        
        try:
            mode_choice = input("Choose mode [1/2/3] (default: 1): ").strip()
        except (EOFError, KeyboardInterrupt):
            log("\n[INTERRUPT] Interrupted during mode selection", "WARN")
            sys.exit(0)
        
        if mode_choice == '2':
            mode = 'mr_bulk'
        elif mode_choice == '3':
            mode = 'deploy_only'
        else:
            mode = 'full'
    
    log(f"Selected mode: {mode.upper()}", "INFO")
    
    # Execute based on mode
    try:
        if mode == 'mr_bulk':
            # BULK MR CREATION
            bulk_create_mrs(project_ids, rollback_data, state)
            
        elif mode == 'deploy_only':
            # DEPLOY ONLY MODE
            log("=" * 70, "INFO")
            log("DEPLOY ONLY MODE", "INFO")
            log("=" * 70, "INFO")
            
            for pid in project_ids:
                if INTERRUPTED:
                    log("Migration interrupted", "WARN")
                    break
                process_project(pid, mode='deploy_only', rollback_data=rollback_data, state=state)
            
        else:
            # FULL MIGRATION MODE
            # File selection
            try:
                raw_choices = input("\nSelect updates for ALL projects (1:POM, 2:CI, 3:EB, 4:ALL, 0:EXIT): ").replace(" ", "")
            except (EOFError, KeyboardInterrupt):
                log("\n[INTERRUPT] Interrupted during file selection", "WARN")
                sys.exit(0)
            
            if raw_choices == '0' or raw_choices.lower() == 'exit':
                log("User chose to exit.", "INFO")
                sys.exit(0)
            
            global_choices = raw_choices.split(',') if raw_choices else []
            if '4' in global_choices:
                global_choices = [c for c in global_choices if c != '4']
                for c in ('3', '2', '1'):
                    if c not in global_choices:
                        global_choices.insert(0, c)
            
            log(f"Will apply these updates: {', '.join(['POM' if c=='1' else 'CI' if c=='2' else 'EB' if c=='3' else c for c in global_choices])}", "INFO")
            
                    log("=" * 70, "INFO")
            log("STARTING MIGRATION", "INFO")
            log("=" * 70, "INFO")
            
            for pid in project_ids:
                if INTERRUPTED:
                    log("Migration interrupted", "WARN")
                    break
                    
                try:
                    process_project(pid, choices=global_choices, show_full=show_full, rollback_data=rollback_data, mode='full', state=state)
                except KeyboardInterrupt:
                    log("\n[INTERRUPT] Keyboard interrupt received", "WARN")
                    INTERRUPTED = True
                    break
                except Exception as e:
                    log(f"Error processing project {pid}: {e}", "ERROR")
        
            if rollback_data['snapshots']:
            rollback_file = save_rollback_data(rollback_data)
            log(f"\n[SUCCESS] Rollback data saved to: {rollback_file}", "INFO")
            log(f"To rollback, run: python3 {sys.argv[0]} --rollback {rollback_file}", "INFO")
        
            if INTERRUPTED:
            state_file = save_state(state)
            log(f"\n[INTERRUPT] Migration interrupted. State saved to: {state_file}", "WARN")
            log(f"To resume, run: python3 {sys.argv[0]} --resume {state_file}", "INFO")
        
        log("\n" + "=" * 70, "INFO")
        if INTERRUPTED:
            log("MIGRATION INTERRUPTED", "WARN")
        else:
            log("MIGRATION COMPLETE", "INFO")
        log("=" * 70, "INFO")
        
        if state:
            log(f"\nSummary:", "INFO")
            log(f"  Completed: {len(state.get('completed_projects', []))} projects", "INFO")
            log(f"  Failed: {len(state.get('failed_projects', []))} projects", "INFO")
            log(f"  Skipped: {len(state.get('skipped_projects', []))} projects", "INFO")
            
            if state.get('project_times'):
                project_times = state['project_times']
                durations = [pt['duration_seconds'] for pt in project_times.values() if 'duration_seconds' in pt]
                
                if durations:
                    total_runtime = sum(durations)
                    average_runtime = total_runtime / len(durations)
                    slowest_time = max(durations)
                    slowest_project = next((pt['name'] for pt in project_times.values() if pt.get('duration_seconds') == slowest_time), 'unknown')
                    
                    log(f"\nExecution Metrics:", "INFO")
                    log(f"  Total runtime: {int(total_runtime // 60)}m {int(total_runtime % 60)}s", "INFO")
                    log(f"  Average per project: {int(average_runtime // 60)}m {int(average_runtime % 60)}s", "INFO")
                    log(f"  Slowest project: {slowest_project} ({int(slowest_time // 60)}m {int(slowest_time % 60)}s)", "INFO")
            
            if hasattr(state, 'idempotency_stats'):
                stats = state['idempotency_stats']
                if stats.get('idempotent_commits', 0) > 0 or stats.get('idempotent_mrs', 0) > 0 or stats.get('idempotent_tags', 0) > 0:
                    log(f"\nIdempotency Guards Triggered:", "INFO")
                    if stats.get('idempotent_commits', 0) > 0:
                        log(f"  Skipped commits (files already match): {stats['idempotent_commits']}", "INFO")
                    if stats.get('idempotent_mrs', 0) > 0:
                        log(f"  Skipped MRs (already exist): {stats['idempotent_mrs']}", "INFO")
                    if stats.get('idempotent_tags', 0) > 0:
                        log(f"  Skipped tag recreations (already at HEAD): {stats['idempotent_tags']}", "INFO")
    
    except KeyboardInterrupt:
        log("\n[INTERRUPT] Keyboard interrupt received", "WARN")
        INTERRUPTED = True
        
            if state:
            state_file = save_state(state)
            log(f"State saved to: {state_file}", "INFO")
            log(f"To resume, run: python3 {sys.argv[0]} --resume {state_file}", "INFO")
        
        sys.exit(1)


if __name__ == "__main__":
    main()
