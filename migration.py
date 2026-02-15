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
from functools import wraps

try:
    import readline
    READLINE_AVAILABLE = True
except ImportError:
    try:
        import pyreadline3 as readline
        READLINE_AVAILABLE = True
    except ImportError:
        READLINE_AVAILABLE = False
        print("Warning: readline not available. Tab completion disabled.")

BASE_URL = ""
TOKEN = ""

PROJECT_NAMES = {}

JIRA_ID = "1293"
UPGRADE_TYPE = "java17-migration"
FEATURE_BRANCH = f"task-{JIRA_ID}-{UPGRADE_TYPE}"
SOURCE_BRANCH = "develop"
MR_TITLE = f"TASK-{JIRA_ID}: java migration"

TARGET_PARENT_VERSION = "1.8.3"
NEW_DEFAULT_PLATFORM = "arn:aws:elasticbeanstalk:us-east-1::platform/Corretto 17 running on 64bit Amazon Linux 2/3.10.3"

AUTO_ROLLBACK_ON_FAILURE = True

ASSIGNEE_EMAIL = ""
REVIEWER_EMAILS = []

STATE_FILE = None
ROLLBACK_FILE = None
FILE_LOGGER = None
INTERRUPTED = False

LOG_DIR_MIGRATION = "migration_logs"
LOG_DIR_ROLLBACK = "rollback_logs"
LOG_DIR_STATE = "state_logs"

CURRENT_USER_ID = None
ASSIGNEE_USER_ID = None
REVIEWER_USER_IDS = []


def setup_file_logging(log_dir=LOG_DIR_MIGRATION):
    global FILE_LOGGER
    
    os.makedirs(log_dir, exist_ok=True)
    
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = os.path.join(log_dir, f"migration_{timestamp}.log")
    
    FILE_LOGGER = logging.getLogger('migration')
    FILE_LOGGER.setLevel(logging.DEBUG)
    
    FILE_LOGGER.handlers = []
    
    file_handler = logging.FileHandler(log_file, encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    file_formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
    file_handler.setFormatter(file_formatter)
    
    FILE_LOGGER.addHandler(file_handler)
    
    return log_file


def signal_handler(signum, frame):
    global INTERRUPTED
    INTERRUPTED = True
    log("\n[INTERRUPT] Received interrupt signal. Saving state...", "WARN")


def save_state(state_data, filename=None):
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
    
    if FILE_LOGGER:
        log_level = getattr(logging, level, logging.INFO)
        FILE_LOGGER.log(log_level, msg)


def load_config_from_env(debug=False):
    global ASSIGNEE_EMAIL, REVIEWER_EMAILS
    
    script_dir = os.path.dirname(os.path.abspath(__file__))
    env_file = os.path.join(script_dir, '.env')
    
    if not os.path.exists(env_file):
        if debug:
            log(".env file not found", "DEBUG")
        return
    
    try:
        with open(env_file, 'r') as f:
            lines = f.readlines()
            
            for i, line in enumerate(lines, 1):
                line = line.strip()
                
                if not line or line.startswith('#'):
                    continue
                
                if '=' in line:
                    try:
                        key, value = line.split('=', 1)
                        key = key.strip()
                        value = value.strip()
                        
                        if value.startswith('"') and value.endswith('"'):
                            value = value[1:-1]
                        elif value.startswith("'") and value.endswith("'"):
                            value = value[1:-1]
                        
                        if key == 'ASSIGNEE_EMAIL':
                            ASSIGNEE_EMAIL = value
                            if debug:
                                log(f"Loaded ASSIGNEE_EMAIL: {value}", "DEBUG")
                        
                        elif key == 'REVIEWER_EMAILS':
                            emails = [email.strip() for email in value.split(',') if email.strip()]
                            REVIEWER_EMAILS = emails
                            if debug:
                                log(f"Loaded REVIEWER_EMAILS: {emails}", "DEBUG")
                    
                    except (ValueError, IndexError) as e:
                        log(f"Error parsing config line {i}: {line} ({e})", "WARN")
                        continue
    
    except Exception as e:
        log(f"Error reading config from .env file: {e}", "WARN")


def load_projects_from_env(debug=False):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    env_file = os.path.join(script_dir, '.env')
    
    projects = {}
    
    if debug:
        log(f"Looking for projects in .env file at: {env_file}", "DEBUG")
    
    if not os.path.exists(env_file):
        if debug:
            log(".env file not found", "DEBUG")
        return projects
    
    try:
        with open(env_file, 'r') as f:
            lines = f.readlines()
            
            for i, line in enumerate(lines, 1):
                line = line.strip()
                
                if not line or line.startswith('#'):
                    continue
                
                if '=' in line and line.startswith('PROJECT_'):
                    try:
                        key, value = line.split('=', 1)
                        key = key.strip()
                        value = value.strip()
                        
                        if value.startswith('"') and value.endswith('"'):
                            value = value[1:-1]
                        elif value.startswith("'") and value.endswith("'"):
                            value = value[1:-1]
                        
                        project_id_str = key.replace('PROJECT_', '')
                        project_id = int(project_id_str)
                        
                        projects[project_id] = value
                        
                        if debug:
                            log(f"Loaded project: {project_id} -> {value}", "DEBUG")
                    except (ValueError, IndexError) as e:
                        log(f"Error parsing project line {i}: {line} ({e})", "WARN")
                        continue
        
        if projects:
            log(f"Loaded {len(projects)} project(s) from .env file", "INFO")
        else:
            log("No projects found in .env file (looking for PROJECT_<id>=<name> entries)", "WARN")
            
    except Exception as e:
        log(f"Error reading projects from .env file: {e}", "WARN")
    
    return projects


def load_token_from_file(debug=False):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    
    if debug:
        log(f"Script directory: {script_dir}", "DEBUG")
    
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
                    
                    if not line or line.startswith('#'):
                        continue
                    
                    if '=' in line:
                        key, value = line.split('=', 1)
                        key = key.strip()
                        value = value.strip()
                        
                        if debug:
                            log(f"Found key: '{key}', value length: {len(value)}", "DEBUG")
                        
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


def get_current_user():
    try:
        user_info = api_call("user")
        if isinstance(user_info, dict) and not user_info.get("error"):
            return user_info
        return None
    except Exception as e:
        log(f"Error fetching current user info: {e}", "ERROR")
        return None


def search_user_by_email(email):
    try:
        users = api_call(f"users?search={urllib.parse.quote(email)}")
        if isinstance(users, list):
            for user in users:
                if isinstance(user, dict):
                    user_email = user.get('email', '').lower()
                    public_email = user.get('public_email', '').lower()
                    
                    if user_email == email.lower() or public_email == email.lower():
                        return user
            
            if len(users) > 0:
                log(f"Email '{email}' not exact match, but found user: {users[0].get('name', 'Unknown')}", "WARN")
                return users[0]
        
        return None
    except Exception as e:
        log(f"Error searching for user with email '{email}': {e}", "ERROR")
        return None


def search_users_by_emails(email_list):
    found_users = []
    
    for email in email_list:
        log(f"Searching for user: {email}...", "INFO")
        user = search_user_by_email(email)
        
        if user:
            user_id = user.get('id')
            user_name = user.get('name', 'Unknown')
            user_username = user.get('username', 'Unknown')
            log(f"Found user: {user_name} (@{user_username}, ID: {user_id})", "INFO")
            found_users.append({
                'id': user_id,
                'name': user_name,
                'username': user_username,
                'email': email
            })
        else:
            log(f"Could not find user with email '{email}'", "WARN")
    
    return found_users


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
                
                if status in ['success', 'failed', 'canceled', 'skipped']:
                    return {"status": status, "pipeline": pipeline}
                
                time.sleep(check_interval)
            else:
                log(f"Error fetching pipeline {pipeline_id}: {pipeline.get('details', 'unknown')}", "WARN")
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
        if isinstance(job, dict) and job.get('name') == job_name:
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
                
                if status in ['success', 'failed', 'canceled', 'skipped']:
                    return {"status": status, "job": job}
                
                time.sleep(check_interval)
            else:
                log(f"Error fetching job {job_id}: {job.get('details', 'unknown')}", "WARN")
                time.sleep(check_interval)
        except Exception as e:
            log(f"Exception while monitoring job {job_id}: {e}", "WARN")
            time.sleep(check_interval)


def map_tag_to_deploy_job(tag_name):
    tag_lower = tag_name.lower().replace('azure-', '')
    
    deploy_jobs = {
        'dev': 'eb-deploy-dev-azure',
        'test': 'eb-deploy-test-azure',
        'performance': 'eb-deploy-performance-azure'
    }
    
    return deploy_jobs.get(tag_lower)


def validate_and_log_token_info(token, base_url):
    if not token:
        return {'valid': False, 'info': None}
    
    try:
        url = f"{base_url.rstrip('/')}/personal_access_tokens/self"
        req = urllib.request.Request(url, method="GET")
        req.add_header("PRIVATE-TOKEN", token)
        req.add_header("Content-Type", "application/json")
        
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode('utf-8'))
            
            token_info = {
                'id': data.get('id'),
                'name': data.get('name', 'N/A'),
                'scopes': data.get('scopes', []),
                'created_at': data.get('created_at'),
                'expires_at': data.get('expires_at'),
                'active': data.get('active', False),
                'revoked': data.get('revoked', False),
                'access_level': data.get('access_level', 'unknown')
            }
            
            log("=" * 70, "INFO")
            log("GitLab Token Information:", "INFO")
            log("=" * 70, "INFO")
            log(f"Token Name: {token_info['name']}", "INFO")
            log(f"Token ID: {token_info['id']}", "INFO")
            log(f"Active: {'Yes' if token_info['active'] else 'No'}", "INFO")
            log(f"Revoked: {'Yes' if token_info['revoked'] else 'No'}", "INFO")
            
            access_level = token_info.get('access_level')
            if access_level and access_level != 'unknown':
                access_level_names = {
                    10: 'Guest',
                    20: 'Reporter',
                    30: 'Developer',
                    40: 'Maintainer',
                    50: 'Owner'
                }
                access_name = access_level_names.get(access_level, f'Level {access_level}')
                log(f"Access Level: {access_name} ({access_level})", "INFO")
            
            if token_info['expires_at']:
                try:
                    from datetime import datetime
                    expiry_dt = datetime.fromisoformat(token_info['expires_at'].replace('Z', '+00:00'))
                    now_dt = datetime.now(expiry_dt.tzinfo)
                    
                    expiry_str = expiry_dt.strftime("%Y-%m-%d %H:%M:%S %Z")
                    log(f"Expires At: {expiry_str}", "INFO")
                    
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
            
            if token_info['scopes']:
                log(f"Permissions (Scopes): {', '.join(token_info['scopes'])}", "INFO")
            else:
                log("Permissions (Scopes): None detected (may have full access)", "INFO")
            
            log("=" * 70, "INFO")
            
            return {'valid': True, 'info': token_info}
            
    except urllib.error.HTTPError as e:
        if e.code == 404:
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
            return {'valid': True, 'info': None}
    except Exception as e:
        log(f"Error fetching token information: {e}", "WARN")
        return {'valid': True, 'info': None}


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
        raise


def update_parent_block(match):
    block = match.group(0)
    block = re.sub(r"<version>.*?</version>", f"<version>{TARGET_PARENT_VERSION}</version>", block)
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
    
    for tag in all_tags:
        if isinstance(tag, dict) and tag.get('protected'):
            tag_name = tag.get('name', 'unknown')
            log(f"Tag '{tag_name}' is PROTECTED", "WARN")
    
    return all_tags


def filter_and_sort_deployment_tags(tags):
    if isinstance(tags, dict) and tags.get("error"):
        return {"error": True, "details": tags.get("details")}
    
    tag_list = tags if isinstance(tags, list) else []
    
    tag_categories = {
        'dev': {'priority': 1, 'variants': ['dev', 'azure-dev'], 'found': []},
        'test': {'priority': 2, 'variants': ['test', 'azure-test'], 'found': []},
        'performance': {'priority': 3, 'variants': ['performance', 'azure-performance'], 'found': []}
    }
    
    for tag in tag_list:
        if not isinstance(tag, dict):
            continue
        tag_name = tag.get('name', '').lower()
        
        for category, info in tag_categories.items():
            if tag_name in info['variants']:
                info['found'].append(tag)
                break
    
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
                        metadata['commit_message'] = commit_info.get('message', 'unknown').split('\n')[0]
                except Exception as e:
                    log(f"Could not fetch commit details for {file_path}: {e}", "DEBUG")
            
            return metadata
        else:
            return None
    except Exception as e:
        log(f"Error getting metadata for {file_path}: {e}", "DEBUG")
        return None


def detect_conflicts(pid, file_path, base_content, our_content, remote_ref):
    quoted_path = urllib.parse.quote(file_path, safe='')
    remote_res = api_call(f"projects/{pid}/repository/files/{quoted_path}?ref={urllib.parse.quote(remote_ref, safe='')}")
    
    if isinstance(remote_res, dict) and remote_res.get("error"):
        return None
    
    if "content" not in remote_res:
        return None
    
    remote_content = base64.b64decode(remote_res['content']).decode('utf-8')
    
    if remote_content == base_content:
        return None
    
    if remote_content == our_content:
        log(f"Remote {file_path} already contains our changes.", "INFO")
        return {"type": "already_applied", "file": file_path}
    
    base_lines = base_content.splitlines(keepends=True)
    our_lines = our_content.splitlines(keepends=True)
    remote_lines = remote_content.splitlines(keepends=True)
    
    our_diff = list(difflib.unified_diff(base_lines, our_lines, lineterm=''))
    remote_diff = list(difflib.unified_diff(base_lines, remote_lines, lineterm=''))
    
    our_changed_lines = extract_changed_line_numbers(our_diff)
    remote_changed_lines = extract_changed_line_numbers(remote_diff)
    
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
        return {
            "type": "non_overlapping",
            "file": file_path,
            "base_content": base_content,
            "our_content": our_content,
            "remote_content": remote_content
        }


def extract_changed_line_numbers(unified_diff):
    changed_lines = set()
    current_line = 0
    
    for line in unified_diff:
        if line.startswith('@@'):
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
    import difflib
    
    base_lines = base_content.splitlines(keepends=True)
    our_lines = our_content.splitlines(keepends=True)
    remote_lines = remote_content.splitlines(keepends=True)
    
    our_diff = list(difflib.unified_diff(base_lines, our_lines, lineterm=''))
    remote_diff = list(difflib.unified_diff(base_lines, remote_lines, lineterm=''))
    
    if remote_content == base_content:
        return our_content
    
    if our_content == base_content:
        return remote_content
    
    return None


def handle_conflict(conflict_info, pid, p_name):
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
    global ASSIGNEE_USER_ID, REVIEWER_USER_IDS
    
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
    mr_payload = {
        "source_branch": FEATURE_BRANCH,
        "target_branch": SOURCE_BRANCH,
        "title": MR_TITLE
    }
    
    if ASSIGNEE_USER_ID:
        mr_payload["assignee_ids"] = [ASSIGNEE_USER_ID]
        log(f"Setting MR assignee (ID: {ASSIGNEE_USER_ID})", "DEBUG")
    
    if REVIEWER_USER_IDS and len(REVIEWER_USER_IDS) > 0:
        mr_payload["reviewer_ids"] = REVIEWER_USER_IDS
        reviewer_ids_str = ", ".join([str(r) for r in REVIEWER_USER_IDS])
        log(f"Setting MR reviewers (IDs: {reviewer_ids_str})", "DEBUG")
    
    mr_result = api_call(f"projects/{pid}/merge_requests", "POST", mr_payload)
    
    if isinstance(mr_result, dict) and not mr_result.get("error"):
        mr_url = mr_result.get('web_url', 'N/A')
        log(f"[SUCCESS] MR created: {mr_url}", "INFO")
        if ASSIGNEE_USER_ID:
            log(f"  Assignee: ID {ASSIGNEE_USER_ID}", "INFO")
        if REVIEWER_USER_IDS and len(REVIEWER_USER_IDS) > 0:
            log(f"  Reviewers: {len(REVIEWER_USER_IDS)} reviewer(s) assigned", "INFO")
        return {'success': True, 'idempotent': False, 'url': mr_url}
    else:
        log(f"[ERROR] Failed to create MR: {mr_result.get('details', 'unknown error')}", "ERROR")
        return {'success': False, 'idempotent': False, 'url': None}


def process_project(pid, choices=None, show_full=True, rollback_data=None, mode='full', state=None):
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
        
        actions = []
        br_check = api_call(f"projects/{pid}/repository/branches/{urllib.parse.quote(FEATURE_BRANCH, safe='')}")
        current_ref = FEATURE_BRANCH if isinstance(br_check, dict) and "name" in br_check else SOURCE_BRANCH
        
        if mode == 'full' and choices:
            if INTERRUPTED:
                log("Interrupted during file preparation", "WARN")
                result['interrupted'] = True
                return result
            
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

            if '2' in choices:
                res = api_call(f"projects/{pid}/repository/files/{urllib.parse.quote('.gitlab-ci.yml', safe='')}?ref={urllib.parse.quote(current_ref, safe='')}")
                if isinstance(res, dict) and res.get("error"):
                    log(f"Failed to fetch .gitlab-ci.yml for {p_name}: {res.get('details')}", "ERROR")
                elif "content" in res:
                    orig = base64.b64decode(res['content']).decode('utf-8')
                    upd = re.sub(r"^\s*image:.*(\n|$)", "", orig, flags=re.MULTILINE)
                    if orig != upd:
                        actions.append({"action": "update", "file_path": ".gitlab-ci.yml", "content": upd, "old_content": orig})

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
                
                print()
                
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
                
                if not prompt_yes_no(f"Commit these changes for {p_name}?", default=True):
                    log(f"User skipped commit for {p_name}", "INFO")
                    if state is not None:
                        state['skipped_projects'].append(pid)
                        save_state(state)
                    return result
                
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
                    log(f"Checking if changes already exist on {FEATURE_BRANCH}...", "INFO")
                    files_already_match, match_details = check_files_already_match(pid, actions, FEATURE_BRANCH)
                    
                    if files_already_match:
                        log(f"[IDEMPOTENT] All files already match desired state on {FEATURE_BRANCH}. Skipping commit.", "INFO")
                        for detail in match_details:
                            if detail['match']:
                                log(f"  File {detail['file']} already matches", "DEBUG")
                        result['committed'] = True
                        result['idempotent_commit'] = True
                    else:
                        log(f"Files need updating on {FEATURE_BRANCH}:", "INFO")
                        for detail in match_details:
                            if not detail['match']:
                                log(f"  File {detail['file']}: {detail.get('reason', 'needs update')}", "DEBUG")
                        
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
                log(f"Skipping deployment/MR for {p_name}", "INFO")
                result['success'] = result['committed']
                
            else:
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
        
        if minutes > 0:
            log(f"[COMPLETE] Completed {p_name} in {minutes}m {seconds}s", "INFO")
        else:
            log(f"[COMPLETE] Completed {p_name} in {seconds}s", "INFO")
        
        return result
        
    except Exception as e:
        log(f"[ERROR] Exception processing {p_name}: {e}", "ERROR")
        result['error'] = str(e)
        
        if AUTO_ROLLBACK_ON_FAILURE and rollback_data and rollback_data.get('snapshots'):
            log(f"[AUTO-ROLLBACK] Rolling back changes for {p_name}...", "WARN")
            rollback_file = save_rollback_data(rollback_data)
            perform_rollback(rollback_file)
        
        if state is not None:
            state['failed_projects'].append(pid)
            save_state(state)
        
        return result


def handle_deployment(pid, p_name, state=None):
    log(f"Starting deployment for {p_name}...", "INFO")
    
    if INTERRUPTED:
        log("Deployment interrupted", "WARN")
        return {'success': False, 'idempotent_tags': []}
    
    tags_resp = fetch_all_tags_for_project(pid)
    if isinstance(tags_resp, dict) and tags_resp.get("error"):
        log(f"Could not fetch tags for project {p_name}: {tags_resp.get('details')}", "ERROR")
        return {'success': False, 'idempotent_tags': []}

    available_tags_all = tags_resp if isinstance(tags_resp, list) else []
    
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


class ProjectCompleter:
    def __init__(self, projects):
        self.projects = projects
        self.project_list = []
        
        for pid, name in projects.items():
            self.project_list.append(str(pid))
            self.project_list.append(name)
        
        self.project_list.sort()
        self.matches = []
    
    def complete(self, text, state):
        if state == 0:
            if text:
                self.matches = [s for s in self.project_list 
                               if s.lower().startswith(text.lower())]
            else:
                self.matches = self.project_list[:]
        
        try:
            return self.matches[state]
        except IndexError:
            return None
    
    def display_matches(self, substitution, matches, longest_match_length):
        print()
        for match in matches:
            if match.isdigit():
                pid = int(match)
                if pid in self.projects:
                    print(f"  [{pid:>3}] {self.projects[pid]}")
            else:
                pid = self.name_to_id.get(match)
                if pid:
                    print(f"  [{pid:>3}] {match}")
        print(f"\n>> {readline.get_line_buffer()}", end='', flush=True)


def setup_readline_completion(projects):
    if not READLINE_AVAILABLE:
        return
    
    completer = ProjectCompleter(projects)
    completer.name_to_id = {name: pid for pid, name in projects.items()}
    completer.projects = projects
    
    readline.set_completer(completer.complete)
    
    if 'libedit' in readline.__doc__:
        readline.parse_and_bind("bind ^I rl_complete")
    else:
        readline.parse_and_bind("tab: complete")
    
    readline.set_completer_delims(' \t\n;')
    
    try:
        readline.set_completion_display_matches_hook(completer.display_matches)
    except AttributeError:
        pass


def interactive_project_selection(projects):
    selected_ids = []
    
    setup_readline_completion(projects)
    
    name_to_id = {name: pid for pid, name in projects.items()}
    
    print("\n" + "=" * 70)
    print("INTERACTIVE PROJECT SELECTION")
    print("=" * 70)
    if READLINE_AVAILABLE:
        print("\nType project name or ID and press TAB for autocomplete")
        print("(Press TAB twice to see all matches)")
    else:
        print("\nType project name or ID")
    print("Commands:")
    print("  'all'   - select all projects")
    print("  'list'  - show currently selected projects")
    print("  'done'  - finish selection")
    print("  'exit'  - exit the script")
    print("-" * 70)
    
    while True:
        try:
            if READLINE_AVAILABLE:
                try:
                    import sys
                    if sys.platform == 'win32':
                        readline.set_startup_hook(lambda: readline.insert_text(''))
                except:
                    pass
                search = input(">> ").strip()
            else:
                search = input("Enter project: ").strip()
        except (EOFError, KeyboardInterrupt):
            log("\n[INTERRUPT] Project selection cancelled", "WARN")
            return []
        
        if search.lower() in ['exit', 'quit']:
            if selected_ids:
                try:
                    confirm = input(f"\nYou have {len(selected_ids)} project(s) selected. Exit anyway? [y/N]: ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    sys.exit(0)
                if confirm in ['y', 'yes']:
                    log("\n[EXIT] User exited project selection", "INFO")
                    sys.exit(0)
                else:
                    print("Continuing selection...")
                    continue
            else:
                log("\n[EXIT] User exited project selection", "INFO")
                sys.exit(0)
        
        if search.lower() == 'list':
            if selected_ids:
                print(f"\nCurrently selected projects ({len(selected_ids)}):")
                for pid in selected_ids:
                    print(f"  [{pid:>3}] {projects.get(pid, 'Unknown')}")
            else:
                print("\nNo projects selected yet.")
            continue
        
        if search.lower() == 'done':
            if selected_ids:
                break
            else:
                print("No projects selected yet. Please select at least one project or type 'exit' to cancel.")
                continue
        
        if search == '':
            if selected_ids:
                break
            else:
                print("No projects selected yet. Please select at least one project or type 'exit' to cancel.")
                continue
        
        if search.lower() == 'all':
            selected_ids = sorted(list(projects.keys()))
            log(f"Selected all {len(selected_ids)} projects", "INFO")
            break
        
        if search.isdigit():
            pid = int(search)
            if pid in projects:
                if pid in selected_ids:
                    print(f"Already selected: {projects[pid]} (ID: {pid})")
                else:
                    selected_ids.append(pid)
                    print(f"Added: {projects[pid]} (ID: {pid})")
                    print(f"Total selected: {len(selected_ids)} project(s)")
                continue
            else:
                print(f"No project found with ID: {pid}")
                continue
        
        if search in name_to_id:
            pid = name_to_id[search]
            if pid in selected_ids:
                print(f"Already selected: {search} (ID: {pid})")
            else:
                selected_ids.append(pid)
                print(f"Added: {search} (ID: {pid})")
                print(f"Total selected: {len(selected_ids)} project(s)")
            continue
        
        search_lower = search.lower()
        partial_matches = [(pid, name) for pid, name in projects.items() 
                          if search_lower in name.lower()]
        
        if len(partial_matches) == 1:
            pid, name = partial_matches[0]
            if pid in selected_ids:
                print(f"Already selected: {name} (ID: {pid})")
            else:
                selected_ids.append(pid)
                print(f"Added: {name} (ID: {pid})")
                print(f"Total selected: {len(selected_ids)} project(s)")
        elif len(partial_matches) > 1:
            print(f"\nMultiple matches found for '{search}':")
            for pid, name in sorted(partial_matches, key=lambda x: x[1])[:10]:
                status = " [SELECTED]" if pid in selected_ids else ""
                print(f"  [{pid:>3}] {name}{status}")
            if len(partial_matches) > 10:
                print(f"  ... and {len(partial_matches) - 10} more")
            print(f"\nType more characters or the full name to select.")
        else:
            print(f"No project found matching '{search}'.")
            if READLINE_AVAILABLE:
                print("Try using TAB for autocomplete.")
    
    if selected_ids:
        print("\n" + "=" * 70)
        print(f"FINAL SELECTION: {len(selected_ids)} project(s)")
        print("=" * 70)
        for pid in selected_ids:
            print(f"  [{pid:>3}] {projects.get(pid, 'Unknown')}")
        print("=" * 70)
    
    return selected_ids


def parse_project_input(user_input, projects):
    if not user_input or user_input.strip().lower() == 'all':
        return sorted(list(projects.keys()))
    
    name_to_id = {name: pid for pid, name in projects.items()}
    
    selected = []
    parts = [p.strip() for p in user_input.split(',')]
    
    for part in parts:
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


def main():
    global TOKEN, PROJECT_NAMES, BASE_URL, INTERRUPTED, CURRENT_USER_ID, ASSIGNEE_USER_ID, REVIEWER_USER_IDS

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    parser = argparse.ArgumentParser(description="Batch Java migration helper for GitLab projects.")
    parser.add_argument("--projects", help="Comma-separated project IDs to process", default="")
    parser.add_argument("--full-diff", help="Show full unified diffs (default: True)", action="store_true", default=True)
    parser.add_argument("--summary-only", help="Show only line-count summary", action="store_true")
    parser.add_argument("--token", help="GitLab private token string", default="")
    parser.add_argument("--base-url", help="GitLab API base URL", default="")
    parser.add_argument("--rollback", help="Rollback changes from a previous migration", default="")
    parser.add_argument("--mode", help="Execution mode", choices=['full', 'mr_bulk', 'deploy_only'], default=None)
    parser.add_argument("--resume", help="Resume from a previous state file", default="")
    
    args = parser.parse_args()

    if args.rollback:
        perform_rollback(args.rollback)
        sys.exit(0)

    log_file = setup_file_logging(LOG_DIR_MIGRATION)
    log(f"Logging to file: {log_file}", "INFO")
    
    state = {
        'completed_projects': [],
        'failed_projects': [],
        'skipped_projects': [],
        'start_time': datetime.datetime.now().isoformat(),
        'project_details': {}
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

    load_config_from_env()

    log("Fetching current user information...", "INFO")
    current_user = get_current_user()
    if current_user:
        CURRENT_USER_ID = current_user.get('id')
        current_user_name = current_user.get('name', 'Unknown')
        current_user_email = current_user.get('email', 'Unknown')
        log(f"Current user: {current_user_name} ({current_user_email}, ID: {CURRENT_USER_ID})", "INFO")
    else:
        log("Could not fetch current user information", "WARN")
    
    if ASSIGNEE_EMAIL:
        log("=" * 70, "INFO")
        log("SEARCHING FOR ASSIGNEE", "INFO")
        log("=" * 70, "INFO")
        
        assignee_user = search_user_by_email(ASSIGNEE_EMAIL)
        if assignee_user:
            ASSIGNEE_USER_ID = assignee_user.get('id')
            assignee_name = assignee_user.get('name', 'Unknown')
            log(f"Found assignee: {assignee_name} ({ASSIGNEE_EMAIL}, ID: {ASSIGNEE_USER_ID})", "INFO")
        else:
            log(f"Could not find user with email '{ASSIGNEE_EMAIL}'", "WARN")
            log("MRs will use current user as assignee", "INFO")
            ASSIGNEE_USER_ID = CURRENT_USER_ID
        
        log("=" * 70, "INFO")
    else:
        log("No ASSIGNEE_EMAIL configured, using current user as assignee", "INFO")
        ASSIGNEE_USER_ID = CURRENT_USER_ID
    
    if REVIEWER_EMAILS:
        log("=" * 70, "INFO")
        log("SEARCHING FOR REVIEWERS", "INFO")
        log("=" * 70, "INFO")
        
        reviewer_users = search_users_by_emails(REVIEWER_EMAILS)
        
        if reviewer_users:
            REVIEWER_USER_IDS = [user['id'] for user in reviewer_users]
            log(f"Found {len(REVIEWER_USER_IDS)} reviewer(s) total", "INFO")
            for user in reviewer_users:
                log(f"  - {user['name']} ({user['email']})", "INFO")
        else:
            log("No reviewers found. MRs will be created without reviewers.", "WARN")
        
        log("=" * 70, "INFO")
    else:
        log("No REVIEWER_EMAILS configured in .env file", "INFO")

    PROJECT_NAMES = load_projects_from_env()
    
    if not PROJECT_NAMES:
        log("No projects found in .env file. Please add projects in format:", "ERROR")
        log("  PROJECT_101=user-authentication-service", "ERROR")
        log("  PROJECT_102=payment-gateway-api", "ERROR")
        sys.exit(1)

    print("\n" + "=" * 70)
    print("AVAILABLE PROJECTS")
    print("=" * 70)
    print(f"\nTotal projects found: {len(PROJECT_NAMES)}\n")
    
    sorted_projects = sorted(PROJECT_NAMES.items(), key=lambda x: x[0])
    
    for pid, pname in sorted_projects:
        print(f"  [{pid:>3}] {pname}")
    
    print("=" * 70)

    project_ids = []
    if args.projects:
        try:
            project_ids = [int(x.strip()) for x in args.projects.split(",") if x.strip()]
        except Exception:
            log("Invalid --projects value. Provide comma-separated integers.", "ERROR")
            sys.exit(1)
    else:
        project_ids = interactive_project_selection(PROJECT_NAMES)
        
        if not project_ids:
            log("No projects selected. Exiting.", "ERROR")
            sys.exit(1)
    
    if args.resume and state.get('completed_projects'):
        completed_set = set(state['completed_projects'])
        original_count = len(project_ids)
        project_ids = [pid for pid in project_ids if pid not in completed_set]
        skipped_count = original_count - len(project_ids)
        if skipped_count > 0:
            log(f"Resuming: Skipping {skipped_count} already completed project(s)", "INFO")
    
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
    
    try:
        if mode == 'mr_bulk':
            bulk_create_mrs(project_ids, rollback_data, state)
            
        elif mode == 'deploy_only':
            log("=" * 70, "INFO")
            log("DEPLOY ONLY MODE", "INFO")
            log("=" * 70, "INFO")
            
            for pid in project_ids:
                if INTERRUPTED:
                    log("Migration interrupted", "WARN")
                    break
                process_project(pid, mode='deploy_only', rollback_data=rollback_data, state=state)
            
        else:
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
