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
from functools import wraps

# --- CONFIGURATION (edit as needed) ---
BASE_URL = "" # Will be loaded from .env
TOKEN = ""  # Will be loaded from .env or token.txt file
PROJECT_IDS = [101, 102]  # Add all project IDs here or pass --projects

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

# Log directories
LOG_DIR_MIGRATION = "migration_logs"
LOG_DIR_ROLLBACK = "rollback_logs"
LOG_DIR_STATE = "state_logs"
LOG_DIR_DRY_RUN = "dry_run_logs"


def setup_file_logging(log_dir=LOG_DIR_MIGRATION):
    """
    Setup file logging with both file and console output.
    Creates timestamped log files for audit trail.
    """
    global FILE_LOGGER
    
    # Create log directory if it doesn't exist
    os.makedirs(log_dir, exist_ok=True)
    
    # Create timestamped log filename
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
    
    # Get current branch head
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


def perform_rollback(rollback_file, dry_run=False):
    """
    Rollback changes from a previous migration.
    
    Args:
        rollback_file: Path to rollback JSON file
        dry_run: If True, only show what would be rolled back
    """
    log("=" * 70, "INFO")
    log("ROLLBACK MODE", "INFO")
    log("=" * 70, "INFO")
    
    data = load_rollback_data(rollback_file)
    if not data:
        return
    
    snapshots = data.get('snapshots', [])
    
    if not snapshots:
        log("No snapshots found in rollback file", "WARN")
        return
    
    log(f"Found {len(snapshots)} project(s) to rollback", "INFO")
    log(f"Rollback file created: {data.get('created_at', 'unknown')}", "INFO")
    
    for snapshot in snapshots:
        pid = snapshot['project_id']
        p_name = snapshot['project_name']
        branch = snapshot['branch']
        
        log(f"\n--- Rollback: {p_name} (ID: {pid}) ---", "INFO")
        
        if dry_run:
            log(f"(dry-run) Would rollback {p_name} to commit {snapshot.get('commit_sha', 'unknown')[:8]}", "INFO")
            log(f"(dry-run) Would restore {len(snapshot.get('files', {}))} file(s)", "INFO")
            continue
        
        # Restore files
        for file_path, file_data in snapshot.get('files', {}).items():
            if not file_data.get('exists'):
                log(f"File {file_path} did not exist before, skipping restore", "INFO")
                continue
            
            content = file_data.get('content')
            if content is None:
                log(f"No backup content for {file_path}, skipping", "WARN")
                continue
            
            try:
                # Update file to previous content
                encoded_content = base64.b64encode(content.encode('utf-8')).decode('utf-8')
                quoted_path = urllib.parse.quote(file_path, safe='')
                
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
        
        log(f"Rollback completed for {p_name}", "INFO")
    
    log("\n" + "=" * 70, "INFO")
    log("ROLLBACK COMPLETE", "INFO")
    log("=" * 70, "INFO")


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
                        # Show line without revealing token value
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
        elapsed = time.time() - start_time
        if elapsed > timeout:
            log(f"Pipeline {pipeline_id} timed out after {int(elapsed)}s", "ERROR")
            return {"status": "timeout", "pipeline": None}
        
        try:
            pipeline = api_call(f"projects/{pid}/pipelines/{pipeline_id}")
            if isinstance(pipeline, dict) and not pipeline.get("error"):
                status = pipeline.get('status', 'unknown')
                
                # Log status changes
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


def trigger_manual_job(pid, job_id, dry_run=False):
    """
    Trigger a manual job (play button).
    Returns the job response.
    """
    if dry_run:
        log(f"(dry-run) Would trigger job {job_id}", "DRY")
        return {"status": "success"}
    
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
        elapsed = time.time() - start_time
        if elapsed > timeout:
            log(f"Job {job_id} timed out after {int(elapsed)}s", "ERROR")
            return {"status": "timeout", "job": None}
        
        try:
            job = api_call(f"projects/{pid}/jobs/{job_id}")
            if isinstance(job, dict) and not job.get("error"):
                status = job.get('status', 'unknown')
                
                # Log status changes
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


def generate_dry_run_summary(summary_data):
    """
    Generate and display a comprehensive dry-run summary.
    
    Args:
        summary_data: Dict with dry-run statistics
    """
    print("\n" + "=" * 70)
    print("DRY-RUN SUMMARY")
    print("=" * 70)
    
    # Projects overview
    total = summary_data.get('total_projects', 0)
    would_succeed = summary_data.get('would_succeed', 0)
    warnings = summary_data.get('warnings', 0)
    would_fail = summary_data.get('would_fail', 0)
    
    print(f"\nTotal Projects: {total}")
    if would_succeed > 0:
        print(f"  [SUCCESS] Would succeed: {would_succeed}")
    if warnings > 0:
        print(f"  [WARNING] Warnings: {warnings}")
    if would_fail > 0:
        print(f"  [FAILED] Would fail: {would_fail}")
    
    # Changes overview
    changes = summary_data.get('changes', {})
    if changes:
        print(f"\nChanges Overview:")
        if changes.get('pom_files', 0) > 0:
            print(f"  POM files: {changes['pom_files']}")
        if changes.get('ci_files', 0) > 0:
            print(f"  CI files: {changes['ci_files']}")
        if changes.get('eb_files', 0) > 0:
            print(f"  EB files: {changes['eb_files']}")
    
    # Actions overview
    actions = summary_data.get('actions', {})
    if actions:
        print(f"\nActions:")
        if actions.get('commits', 0) > 0:
            print(f"  Commits: {actions['commits']}")
        if actions.get('mrs', 0) > 0:
            print(f"  MRs: {actions['mrs']}")
        if actions.get('tags', 0) > 0:
            print(f"  Tags: {actions['tags']}")
            tag_breakdown = actions.get('tag_breakdown', {})
            if tag_breakdown:
                for tag_type, count in tag_breakdown.items():
                    print(f"    {tag_type}: {count}")
        if actions.get('deployments', 0) > 0:
            print(f"  Deployments: {actions['deployments']}")
    
    # Time estimate
    avg_time = summary_data.get('avg_time_per_project', 150)  # seconds
    total_time_seconds = total * avg_time
    
    hours = int(total_time_seconds // 3600)
    minutes = int((total_time_seconds % 3600) // 60)
    
    print(f"\nEstimated Time: ", end="")
    if hours > 0:
        print(f"{hours}h ", end="")
    print(f"{minutes}m")
    
    # Issues found
    issues = summary_data.get('issues', [])
    if issues:
        print(f"\n[WARNING] Issues Found:")
        for issue in issues[:10]:  # Show first 10
            print(f"  - {issue}")
        if len(issues) > 10:
            print(f"  ... and {len(issues) - 10} more")
    
    print("\n" + "=" * 70)
    
    # Save summary to file
    os.makedirs(LOG_DIR_DRY_RUN, exist_ok=True)
    summary_file = os.path.join(LOG_DIR_DRY_RUN, f"dry_run_summary_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    with open(summary_file, 'w') as f:
        json.dump(summary_data, f, indent=2)
    
    log(f"Dry-run summary saved to {summary_file}", "INFO")
    
    # Final reminder
    print("\n" + "[DRY-RUN] " + "="*60)
    print("[DRY-RUN] THIS WAS A SIMULATION ONLY")
    print("[DRY-RUN] NO ACTUAL CHANGES WERE MADE TO GITLAB")
    print("[DRY-RUN] " + "="*60)
    print("\nTo run for real and make actual changes:")
    print(f"  python3 {sys.argv[0]} --projects <your-projects>")
    print("\nOr run again without --dry-run flag\n")


def validate_and_log_token_info(token, base_url):
    """
    Validate the GitLab token and log its metadata including expiry date, scopes, and access level.
    
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
            
            # Log token information
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
                
                # Warn if insufficient permissions
                if access_level < 30:  # Less than Developer
                    log("[WARNING] Token has insufficient access level. Developer or Maintainer level required.", "WARN")
            
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
            
            # Display scopes/permissions
            if token_info['scopes']:
                log(f"Permissions (Scopes): {', '.join(token_info['scopes'])}", "INFO")
                
                # Check for required scopes
                required_scopes = ['api', 'write_repository', 'read_repository']
                recommended_scopes = ['read_api']
                
                missing_required = [s for s in required_scopes if s not in token_info['scopes']]
                missing_recommended = [s for s in recommended_scopes if s not in token_info['scopes']]
                
                if missing_required:
                    log(f"[WARNING] Missing REQUIRED scopes: {', '.join(missing_required)}", "WARN")
                    log("   Script may fail without these permissions!", "WARN")
                
                if missing_recommended:
                    log(f"[INFO] Missing recommended scopes: {', '.join(missing_recommended)}", "INFO")
                    log("   Some features may not work optimally", "INFO")
                
                if not missing_required and not missing_recommended:
                    log("[SUCCESS] All required and recommended scopes present", "INFO")
            else:
                log("Permissions (Scopes): None detected (may have full access)", "WARN")
            
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


def extract_company_domain_from_url(url):
    """
    Extract company domain from GitLab URL.
    
    Examples:
    - https://gitlab.company-domain.com/api/v4 -> company-domain.com
    - https://gitlab.acme.io/api/v4 -> acme.io
    - https://git.internal.corp/api/v4 -> internal.corp
    - https://gitlab.lfg/api/v4 -> lfg
    - https://gitlab.lfg.local/api/v4 -> lfg.local
    
    Returns the domain or None if unable to extract.
    """
    try:
        import re
        # Remove protocol and api path
        cleaned = re.sub(r'^https?://', '', url)
        cleaned = re.sub(r'/api/.*$', '', cleaned)
        cleaned = re.sub(r':\d+$', '', cleaned)  # Remove port if present
        
        # Extract domain (remove 'gitlab.' prefix if present)
        if cleaned.startswith('gitlab.'):
            domain = cleaned[7:]  # Remove 'gitlab.' prefix
        elif cleaned.startswith('git.'):
            domain = cleaned[4:]  # Remove 'git.' prefix
        else:
            domain = cleaned
        
        # Return domain if it looks valid
        # Accept even short domains like 'lfg' (minimum 2 chars)
        if len(domain) >= 2:
            return domain
        
        return None
    except Exception:
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
                    log(f"Transient error: {e!r}. Retrying in {_delay}s... (remaining attempts: {_tries})", "WARN")
                    time.sleep(_delay)
                    _delay *= backoff
        return wrapper
    return deco


@retry(tries=3, delay=1, backoff=2, allowed_exceptions=(Exception,))
def api_call(endpoint, method="GET", data=None, dry_run=False):
    """
    Wrapper for GitLab API calls.

    Behavior:
      - If dry_run=True and method != "GET", logs and returns sensible empty structures.
      - Raises exceptions on HTTP 429 or any 5xx to allow retry/backoff.
      - For other HTTP errors (4xx except 429) returns a dict with {"error": True, "details": "..."}
      - On success returns parsed JSON or {} when response body empty.
    """
    url = f"{BASE_URL.rstrip('/')}/{endpoint.lstrip('/')}"
    if dry_run and method != "GET":
        log(f"(dry-run) Would call: {method} {url} with data: {json.dumps(data) if data else None}", "DRY")
        if method == "POST":
            return {}
        if method == "DELETE":
            return {}
        return {}

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


def prompt_yes_no(prompt, default=False, non_interactive=False):
    if non_interactive:
        # In non-interactive mode, always return default
        log(f"{prompt} [auto: {'yes' if default else 'no'}]", "INFO")
        return default
    
    try:
        ans = input(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    except EOFError:
        return default
    if ans == '':
        return default
    return ans in ("y", "yes")


def create_feature_branch(pid, p_name, dry_run=False):
    """
    Create feature branch from source branch.
    Returns True if successful, False otherwise.
    """
    try:
        log(f"Creating feature branch '{FEATURE_BRANCH}' from '{SOURCE_BRANCH}' for project {p_name}...", "INFO")
        result = api_call(f"projects/{pid}/repository/branches", "POST", 
                         {"branch": FEATURE_BRANCH, "ref": SOURCE_BRANCH}, dry_run=dry_run)
        
        if dry_run:
            log(f"(dry-run) Would create branch '{FEATURE_BRANCH}'", "INFO")
            return True
        
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


def detect_conflicts(pid, file_path, base_content, our_content, remote_ref, dry_run=False):
    """
    Detect if there are conflicts between our changes and remote changes.
    
    Returns:
        - None if no conflict detected
        - dict with conflict info if conflict detected
    """
    if dry_run:
        # Skip conflict detection in dry-run mode
        log(f"(dry-run) Skipping conflict detection for {file_path}", "DRY")
        return None
    
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
        choice = input("Choose [1/2/3/4]: ").strip()
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


def process_project(pid, dry_run=False, skip_mr=False, show_full=True, choices=None, force_tags=False, 
                   auto_deploy=False, non_interactive=False, state=None, rollback_data=None, 
                   dry_run_summary=None, skip_file_changes=False):
    """
    Process a single project with file changes and/or deployment.
    
    Args:
        skip_file_changes: If True, skip file modification step (just deploy mode)
    """
    project_start_time = time.time()
    
    project_info = api_call(f"projects/{pid}")
    p_name = project_info.get('name', f"ID:{pid}") if isinstance(project_info, dict) else f"ID:{pid}"
    log(f"--- Processing: {p_name} (project id: {pid}) ---")
    actions = []

    committed_shas = set()
    created_tags = set()
    deployment_successful = False
    
    # Track for dry-run summary
    project_summary = {
        'success': True,
        'warnings': [],
        'changes': []
    }

    br_check = api_call(f"projects/{pid}/repository/branches/{urllib.parse.quote(FEATURE_BRANCH, safe='')}")
    current_ref = FEATURE_BRANCH if isinstance(br_check, dict) and "name" in br_check else SOURCE_BRANCH

    branch_head_before = None
    if isinstance(br_check, dict) and "commit" in br_check:
        branch_head_before = (br_check.get("commit") or {}).get("id")

    # Store original content for conflict detection
    original_contents = {}

    # WORKFLOW 1: Make file changes (skip if skip_file_changes=True)
    if not skip_file_changes:
        # 1. POM
        if choices and '1' in choices:
            res = api_call(f"projects/{pid}/repository/files/{urllib.parse.quote('pom.xml', safe='')}?ref={urllib.parse.quote(current_ref, safe='')}")
            if isinstance(res, dict) and res.get("error"):
                log(f"Failed to fetch pom.xml for {p_name}: {res.get('details')}", "ERROR")
            elif "content" in res:
                orig = base64.b64decode(res['content']).decode('utf-8')
                original_contents['pom.xml'] = orig
                upd = re.sub(r"<(java\.version|maven\.compiler\.(source|target|release))>11</\1>", r"<\1>17</\1>", orig)
                if "<parent>" in upd:
                    upd = re.sub(r"<parent>[\s\S]*?</parent>", update_parent_block, upd)
                if orig != upd:
                    actions.append({"action": "update", "file_path": "pom.xml", "content": upd, "old_content": orig})

        # 2. CI
        if choices and '2' in choices:
            res = api_call(f"projects/{pid}/repository/files/{urllib.parse.quote('.gitlab-ci.yml', safe='')}?ref={urllib.parse.quote(current_ref, safe='')}")
            if isinstance(res, dict) and res.get("error"):
                log(f"Failed to fetch .gitlab-ci.yml for {p_name}: {res.get('details')}", "ERROR")
            elif "content" in res:
                orig = base64.b64decode(res['content']).decode('utf-8')
                original_contents['.gitlab-ci.yml'] = orig
                upd = re.sub(r"^\s*image:.*(\n|$)", "", orig, flags=re.MULTILINE)
                if orig != upd:
                    actions.append({"action": "update", "file_path": ".gitlab-ci.yml", "content": upd, "old_content": orig})

        # 3. EB
        if choices and '3' in choices:
            path = urllib.parse.quote(".elasticbeanstalk/config.yml", safe='')
            res = api_call(f"projects/{pid}/repository/files/{path}?ref={urllib.parse.quote(current_ref, safe='')}")
            if isinstance(res, dict) and res.get("error"):
                log(f"Failed to fetch .elasticbeanstalk/config.yml for {p_name}: {res.get('details')}", "ERROR")
            elif "content" in res:
                orig = base64.b64decode(res['content']).decode('utf-8')
                original_contents['.elasticbeanstalk/config.yml'] = orig
                upd = re.sub(r"(default_platform:\s*).*$", f"default_platform: {NEW_DEFAULT_PLATFORM}", orig, flags=re.MULTILINE)
                if orig != upd:
                    actions.append({"action": "update", "file_path": ".elasticbeanstalk/config.yml", "content": upd, "old_content": orig})

        if actions:
            print(f"\nProposed changes for {p_name}:")
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
                
                # Track for dry-run summary
                if dry_run_summary is not None:
                    project_summary['changes'].append(action['file_path'])
                    if action['file_path'] == 'pom.xml':
                        dry_run_summary['changes']['pom_files'] += 1
                    elif action['file_path'] == '.gitlab-ci.yml':
                        dry_run_summary['changes']['ci_files'] += 1
                    elif action['file_path'] == '.elasticbeanstalk/config.yml':
                        dry_run_summary['changes']['eb_files'] += 1

            if prompt_yes_no(f"Commit changes for {p_name}?", default=False if not non_interactive else True, non_interactive=non_interactive):
                # Create rollback snapshot before making changes
                if not dry_run and rollback_data is not None:
                    files_to_backup = [action['file_path'] for action in actions]
                    snapshot = create_rollback_snapshot(pid, p_name, current_ref, files_to_backup)
                    rollback_data['snapshots'].append(snapshot)
                    log(f"Created rollback snapshot for {p_name}", "DEBUG")
                
                if dry_run_summary is not None:
                    dry_run_summary['actions']['commits'] += 1
                
                # Create feature branch if it doesn't exist
                if not (isinstance(br_check, dict) and "name" in br_check):
                    branch_created = create_feature_branch(pid, p_name, dry_run=dry_run)
                    if not branch_created and not dry_run:
                        log(f"[ERROR] Cannot proceed without feature branch for {p_name}", "ERROR")
                        return
                    
                    br_check = api_call(f"projects/{pid}/repository/branches/{urllib.parse.quote(FEATURE_BRANCH, safe='')}")
                    current_ref = FEATURE_BRANCH if isinstance(br_check, dict) and "name" in br_check else SOURCE_BRANCH
                    if isinstance(br_check, dict) and "commit" in br_check:
                        branch_head_before = (br_check.get("commit") or {}).get("id")

                # Conflict detection (skip in dry-run)
                if not dry_run:
                    log(f"Checking for conflicts on {FEATURE_BRANCH}...", "INFO")
                    conflicts_detected = []
                    
                    for act in actions:
                        file_path = act['file_path']
                        base_content = act['old_content']
                        our_content = act['content']
                        
                        conflict = detect_conflicts(pid, file_path, base_content, our_content, FEATURE_BRANCH, dry_run=dry_run)
                        if conflict:
                            conflicts_detected.append((act, conflict))
                    
                    if conflicts_detected:
                        log(f"Found {len(conflicts_detected)} file(s) with potential conflicts.", "WARN")
                        
                        # Handle each conflict
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
                                return
                    else:
                        log("No conflicts detected.", "INFO")
                else:
                    log(f"(dry-run) Skipping conflict detection", "DRY")
                    conflicts_detected = []

                if not actions:
                    log(f"No remaining changes to commit for {p_name} after conflict resolution.", "INFO")
                else:
                    commit_actions = []
                    if dry_run:
                        # In dry-run, just use all actions without checking remote state
                        commit_actions = [{"action": act["action"], "file_path": act["file_path"], "content": act["content"]} for act in actions]
                        log(f"(dry-run) Would commit {len(commit_actions)} file(s)", "DRY")
                    else:
                        for act in actions:
                            file_path = act['file_path']
                            quoted_path = urllib.parse.quote(file_path, safe='')
                            res = api_call(f"projects/{pid}/repository/files/{quoted_path}?ref={urllib.parse.quote(FEATURE_BRANCH, safe='')}")
                            desired = act['content']
                            if isinstance(res, dict) and res.get("error"):
                                log(f"Error fetching {file_path} on {FEATURE_BRANCH}: {res.get('details')}", "WARN")
                                commit_actions.append({"action": "create", "file_path": file_path, "content": desired})
                            elif "content" in res:
                                remote_content = base64.b64decode(res['content']).decode('utf-8')
                                if remote_content == desired:
                                    log(f"Skipping {file_path}: remote already matches desired content.", "INFO")
                                    continue
                                else:
                                    if file_path == "pom.xml":
                                        remote_ver_str = find_parent_version_in_pom(remote_content)
                                        remote_ver = parse_version(remote_ver_str)
                                        desired_ver = parse_version(TARGET_PARENT_VERSION)
                                        if remote_ver and desired_ver and remote_ver > desired_ver:
                                            log(f"Remote parent version {'.'.join(map(str,remote_ver))} is newer than desired {TARGET_PARENT_VERSION}.", "WARN")
                                            if not prompt_yes_no("Remote parent version appears newer. Overwrite (downgrade) anyway?", default=False, non_interactive=non_interactive):
                                                log(f"Skipping update of {file_path} due to newer remote version.", "INFO")
                                                continue
                                    commit_actions.append({"action": "update", "file_path": file_path, "content": desired})
                            else:
                                log(f"{file_path} not found on {FEATURE_BRANCH} (or fetch error). Will attempt to create it.", "INFO")
                                commit_actions.append({"action": "create", "file_path": file_path, "content": desired})

                    if not commit_actions:
                        log(f"No remaining changes to commit for {p_name}; skipping commit.", "INFO")
                    else:
                        if not dry_run and branch_head_before:
                            br_now = api_call(f"projects/{pid}/repository/branches/{urllib.parse.quote(FEATURE_BRANCH, safe='')}")
                            branch_head_now = (br_now.get("commit") or {}).get("id") if isinstance(br_now, dict) else None
                            if branch_head_now and branch_head_now != branch_head_before:
                                log(f"Branch {FEATURE_BRANCH} head changed from {branch_head_before[:8]} to {branch_head_now[:8]} while running. Aborting commit to avoid overwriting remote changes.", "WARN")
                                if prompt_yes_no("Branch changed — re-evaluate changes and continue anyway?", default=False, non_interactive=non_interactive):
                                    branch_head_before = branch_head_now
                                else:
                                    log("Skipping commit for this project due to branch head change.", "INFO")
                                    commit_actions = []
                        if commit_actions:
                            commit_payload = {"branch": FEATURE_BRANCH, "commit_message": f"fix: {UPGRADE_TYPE}", "actions": commit_actions}
                            commit_resp = api_call(f"projects/{pid}/repository/commits", "POST", commit_payload, dry_run=dry_run)
                            if dry_run:
                                log("(dry-run) Commit would be sent.", "INFO")
                            else:
                                if isinstance(commit_resp, dict) and not commit_resp.get("error"):
                                    sha = commit_resp.get("id") or (commit_resp.get("commit") or {}).get("id")
                                    if sha:
                                        committed_shas.add(sha)
                                        log(f"Recorded new commit {sha[:8]} for project {p_name}.", "INFO")
                                elif isinstance(commit_resp, list):
                                    for item in commit_resp:
                                        if isinstance(item, dict):
                                            sha = item.get("id") or (item.get("commit") or {}).get("id")
                                            if sha:
                                                committed_shas.add(sha)
                                else:
                                    log(f"Commit response: {commit_resp}", "DEBUG")
                                log("Commit request sent." if not dry_run else "(dry-run) Commit would be sent.", "INFO")
        else:
            log(f"No proposed changes for {p_name}.", "INFO")
    else:
        log(f"[INFO] Skipping file changes for {p_name} (deploy-only mode)", "INFO")

    # WORKFLOW 2: Tag and deployment orchestration
    do_orch = prompt_yes_no("Trigger tags/deployment? (y/n)", default=False if not skip_file_changes else True, non_interactive=non_interactive)
    if do_orch:
        tags_resp = fetch_all_tags_for_project(pid)
        if isinstance(tags_resp, dict) and tags_resp.get("error"):
            log(f"Could not fetch tags for project {p_name}: {tags_resp.get('details')}", "ERROR")
            return

        available_tags_all = tags_resp if isinstance(tags_resp, list) else []
        
        # Filter and sort deployment tags
        filter_result = filter_and_sort_deployment_tags(available_tags_all)
        
        if isinstance(filter_result, dict) and filter_result.get("error"):
            log(f"Error filtering tags: {filter_result.get('details')}", "ERROR")
            return
        
        available_tags = filter_result['sorted_tags']
        found_categories = filter_result['found_categories']
        missing_categories = filter_result['missing_categories']
        
        # Log what was found
        log(f"Deployment tags found for {p_name}:", "INFO")
        if found_categories:
            for category, tag_names in found_categories.items():
                log(f"  {category.upper()}: {', '.join(tag_names)}", "INFO")
        
        # Log missing tags
        if missing_categories:
            for category in missing_categories:
                log(f"  {category.upper()}: NOT FOUND", "WARN")
        
        # Check if both dev and test are missing
        dev_missing = 'dev' in missing_categories
        test_missing = 'test' in missing_categories
        
        if dev_missing and test_missing:
            log(f"No dev or test tags present for project {p_name}.", "ERROR")
            if not prompt_yes_no("Do you want to create new deployment tags on the feature branch?", default=False, non_interactive=non_interactive):
                return
            
            # Ask which tags to create
            print("\nWhich tags would you like to create?")
            print("  1. dev (or azure-dev)")
            print("  2. test (or azure-test)")
            print("  3. Both dev and test")
            print("  4. Custom tag name")
            
            choice = input("Enter choice [1/2/3/4]: ").strip()
            tags_to_create = []
            
            # Detect naming convention from existing tags
            has_azure_prefix = any('azure-' in t.get('name', '').lower() for t in available_tags_all if isinstance(t, dict))
            
            if choice == '1':
                tags_to_create = ['azure-dev' if has_azure_prefix else 'dev']
            elif choice == '2':
                tags_to_create = ['azure-test' if has_azure_prefix else 'test']
            elif choice == '3':
                if has_azure_prefix:
                    tags_to_create = ['azure-dev', 'azure-test']
                else:
                    tags_to_create = ['dev', 'test']
            elif choice == '4':
                custom_tag = input("Enter custom tag name: ").strip()
                if custom_tag:
                    tags_to_create = [custom_tag]
            
            if tags_to_create:
                for tag_name in tags_to_create:
                    t_res = api_call(f"projects/{pid}/repository/tags", "POST", 
                                   {"tag_name": tag_name, "ref": FEATURE_BRANCH}, dry_run=dry_run)
                    if dry_run:
                        log(f"(dry-run) Would create tag '{tag_name}' on {FEATURE_BRANCH}", "DRY")
                    else:
                        if not isinstance(t_res, dict) or not t_res.get("error"):
                            created_tags.add(tag_name)
                            created_tag_commit = (t_res.get('commit') or {}).get('id') if isinstance(t_res, dict) else None
                            if created_tag_commit:
                                committed_shas.add(created_tag_commit)
                            log(f"Created tag '{tag_name}' on {FEATURE_BRANCH}", "INFO")
                        else:
                            log(f"Failed to create tag '{tag_name}': {t_res.get('details')}", "ERROR")
            return
        
        # If only dev is missing, suggest creating it
        if dev_missing and not test_missing:
            log(f"Dev tag is missing for project {p_name}.", "WARN")
            test_tag_names = found_categories.get('test', [])
            if test_tag_names:
                has_azure_prefix = any('azure-' in name for name in test_tag_names)
                suggested_tag = 'azure-dev' if has_azure_prefix else 'dev'
                
                if prompt_yes_no(f"Test tag exists but dev tag is missing. Create '{suggested_tag}' tag?", default=False, non_interactive=non_interactive):
                    t_res = api_call(f"projects/{pid}/repository/tags", "POST", 
                                   {"tag_name": suggested_tag, "ref": FEATURE_BRANCH}, dry_run=dry_run)
                    if dry_run:
                        log(f"(dry-run) Would create tag '{suggested_tag}' on {FEATURE_BRANCH}", "DRY")
                    else:
                        if not isinstance(t_res, dict) or not t_res.get("error"):
                            created_tags.add(suggested_tag)
                            created_tag_commit = (t_res.get('commit') or {}).get('id') if isinstance(t_res, dict) else None
                            if created_tag_commit:
                                committed_shas.add(created_tag_commit)
                            log(f"Created tag '{suggested_tag}' on {FEATURE_BRANCH}", "INFO")
                            available_tags.insert(0, {'name': suggested_tag, 'commit': {'id': created_tag_commit}})
                        else:
                            log(f"Failed to create tag '{suggested_tag}': {t_res.get('details')}", "ERROR")
                            return
        
        # If only test is missing, suggest creating it
        if test_missing and not dev_missing:
            log(f"Test tag is missing for project {p_name}.", "WARN")
            dev_tag_names = found_categories.get('dev', [])
            if dev_tag_names:
                has_azure_prefix = any('azure-' in name for name in dev_tag_names)
                suggested_tag = 'azure-test' if has_azure_prefix else 'test'
                
                if prompt_yes_no(f"Dev tag exists but test tag is missing. Create '{suggested_tag}' tag?", default=False, non_interactive=non_interactive):
                    t_res = api_call(f"projects/{pid}/repository/tags", "POST", 
                                   {"tag_name": suggested_tag, "ref": FEATURE_BRANCH}, dry_run=dry_run)
                    if dry_run:
                        log(f"(dry-run) Would create tag '{suggested_tag}' on {FEATURE_BRANCH}", "DRY")
                    else:
                        if not isinstance(t_res, dict) or not t_res.get("error"):
                            created_tags.add(suggested_tag)
                            created_tag_commit = (t_res.get('commit') or {}).get('id') if isinstance(t_res, dict) else None
                            if created_tag_commit:
                                committed_shas.add(created_tag_commit)
                            log(f"Created tag '{suggested_tag}' on {FEATURE_BRANCH}", "INFO")
                            available_tags.append({'name': suggested_tag, 'commit': {'id': created_tag_commit}})
                        else:
                            log(f"Failed to create tag '{suggested_tag}': {t_res.get('details')}", "ERROR")
                            return
        
        if not available_tags:
            log(f"No deployment tags (dev/test/performance) found for project {p_name}.", "WARN")
            if not prompt_yes_no("Do you want to create a new tag on the feature branch anyway?", default=False, non_interactive=non_interactive):
                return
            new_tag_name = input("Enter tag name to create on feature branch: ").strip()
            if new_tag_name:
                t_res = api_call(f"projects/{pid}/repository/tags", "POST", {"tag_name": new_tag_name, "ref": FEATURE_BRANCH}, dry_run=dry_run)
                if dry_run:
                    log(f"(dry-run) Would create tag '{new_tag_name}' on {FEATURE_BRANCH}", "DRY")
                else:
                    if not isinstance(t_res, dict) or not t_res.get("error"):
                        created_tags.add(new_tag_name)
                        created_tag_commit = (t_res.get('commit') or {}).get('id') if isinstance(t_res, dict) else None
                        if created_tag_commit:
                            committed_shas.add(created_tag_commit)
                        log(f"Created tag '{new_tag_name}' on {FEATURE_BRANCH}", "INFO")
                    else:
                        log(f"Failed to create tag '{new_tag_name}': {t_res.get('details')}", "ERROR")
            return

        print(f"\nAvailable deployment tags for {p_name} (sorted by deployment order):")
        for i, t in enumerate(available_tags):
            commit_id = (t.get('commit') or {}).get('id', '') if isinstance(t, dict) else ''
            protected_marker = " [PROTECTED]" if t.get('protected') else ""
            print(f"  {i+1:>3}. {t.get('name', '')}{protected_marker}  {commit_id[:8] if commit_id else ''}")

        sel = input("Select tags to delete & recreate by index or name (e.g. '1,3' or 'dev,test') or 'all' or leave empty to skip: ").strip()
        selected_tag_names = parse_tag_selection_input(sel, available_tags)
        if not selected_tag_names:
            log("No tags selected; skipping tag orchestration for this project.", "INFO")
        else:
            print(f"Selected tags: {', '.join(selected_tag_names)}")
            
            # Check if any selected tags are protected
            protected_tags = []
            for tag_name in selected_tag_names:
                tag_obj = next((t for t in available_tags_all if isinstance(t, dict) and t.get('name') == tag_name), None)
                if tag_obj and tag_obj.get('protected'):
                    protected_tags.append(tag_name)
            
            if protected_tags:
                log(f"WARNING: The following tags are PROTECTED and cannot be deleted: {', '.join(protected_tags)}", "ERROR")
                log("Protected tags will be skipped during tag orchestration.", "WARN")
                if not prompt_yes_no("Continue with non-protected tags only?", default=False, non_interactive=non_interactive):
                    log("Tag orchestration cancelled by user.", "INFO")
                    return
                selected_tag_names = [t for t in selected_tag_names if t not in protected_tags]
                if not selected_tag_names:
                    log("No non-protected tags remaining; skipping tag orchestration.", "INFO")
                    return
            
            if not prompt_yes_no("Proceed with deleting (if present) and recreating these tags on the feature branch?", default=False, non_interactive=non_interactive):
                log("Tag orchestration cancelled by user.", "INFO")
            else:
                branch_info_for_tag = api_call(f"projects/{pid}/repository/branches/{urllib.parse.quote(FEATURE_BRANCH, safe='')}")
                feature_head = None
                if isinstance(branch_info_for_tag, dict) and not branch_info_for_tag.get("error"):
                    feature_head = (branch_info_for_tag.get("commit") or {}).get("id")

                for tag in selected_tag_names:
                    quoted_tag = urllib.parse.quote(tag, safe='')
                    tag_obj = next((t for t in available_tags_all if isinstance(t, dict) and t.get('name') == tag), None)
                    tag_commit_id = (tag_obj.get('commit') or {}).get('id') if tag_obj else None

                    if tag_commit_id and tag_commit_id in committed_shas:
                        log(f"Skipping tag '{tag}': it already points to a commit created in this run ({tag_commit_id[:8]}).", "INFO")
                        continue
                    if tag in created_tags:
                        log(f"Skipping tag '{tag}': it was created earlier in this run.", "INFO")
                        continue
                    
                    # Only skip if tag points to feature head AND force_tags is False
                    if not force_tags and tag_commit_id and feature_head and tag_commit_id == feature_head:
                        log(f"Skipping tag '{tag}': already points to feature branch head {feature_head[:8]}. Use --force-tags to recreate anyway.", "INFO")
                        continue
                    
                    if force_tags and tag_commit_id and feature_head and tag_commit_id == feature_head:
                        log(f"Force recreating tag '{tag}' (already at feature branch head {feature_head[:8]})", "INFO")

                    exists_locally = tag_obj is not None
                    if exists_locally:
                        if tag_obj.get('protected'):
                            log(f"Skipping PROTECTED tag '{tag}' - cannot delete.", "ERROR")
                            continue
                        log(f"Tag '{tag}' exists and will be deleted.", "INFO")
                        api_call(f"projects/{pid}/repository/tags/{quoted_tag}", method="DELETE", dry_run=dry_run)

                    t_res = api_call(f"projects/{pid}/repository/tags", "POST", {"tag_name": tag, "ref": FEATURE_BRANCH}, dry_run=dry_run)
                    if dry_run:
                        log(f"(dry-run) Would create tag '{tag}' on {FEATURE_BRANCH}", "DRY")
                    else:
                        if not isinstance(t_res, dict) or not t_res.get("error"):
                            log(f"Created tag '{tag}' on {FEATURE_BRANCH}", "INFO")
                            created_tags.add(tag)
                            created_tag_commit = (t_res.get('commit') or {}).get('id') if isinstance(t_res, dict) else None
                            if created_tag_commit:
                                committed_shas.add(created_tag_commit)
                                
                                # Ask if user wants full deployment orchestration (or use --auto-deploy flag)
                                should_deploy = auto_deploy or prompt_yes_no(f"Tag '{tag}' created. Do you want to wait for build and auto-deploy?", default=True, non_interactive=non_interactive)
                                
                                if should_deploy:
                                    log(f"Starting deployment orchestration for tag '{tag}'...", "INFO")
                                    
                                    # Wait a few seconds for pipeline to be created
                                    log("Waiting 10 seconds for pipeline to be triggered...", "INFO")
                                    time.sleep(10)
                                    
                                    # Get the pipeline for this commit
                                    pipeline = get_pipeline_for_commit(pid, created_tag_commit)
                                    if not pipeline:
                                        log(f"No pipeline found for commit {created_tag_commit[:8]}. Skipping orchestration.", "WARN")
                                        continue
                                    
                                    pipeline_id = pipeline.get('id')
                                    pipeline_url = pipeline.get('web_url', 'N/A')
                                    log(f"Found pipeline {pipeline_id}: {pipeline_url}", "INFO")
                                    
                                    # Wait for build to complete
                                    result = wait_for_pipeline_completion(pid, pipeline_id, timeout=1800, check_interval=30)
                                    
                                    if result['status'] != 'success':
                                        log(f"Build {result['status']} for tag '{tag}'. Stopping orchestration.", "ERROR")
                                        continue
                                    
                                    log(f"Build succeeded for tag '{tag}'! Proceeding to deployment...", "INFO")
                                    
                                    # Get all jobs from the pipeline
                                    jobs = get_pipeline_jobs(pid, pipeline_id)
                                    if not jobs:
                                        log(f"No jobs found in pipeline {pipeline_id}. Skipping deployment.", "WARN")
                                        continue
                                    
                                    # Step 1: Trigger eb-terminate job
                                    terminate_job = find_job_by_name(jobs, 'eb-terminate')
                                    if terminate_job:
                                        terminate_job_id = terminate_job.get('id')
                                        terminate_status = terminate_job.get('status')
                                        
                                        log(f"Found 'eb-terminate' job (ID: {terminate_job_id}, status: {terminate_status})", "INFO")
                                        
                                        if terminate_status == 'manual':
                                            log("Triggering 'eb-terminate' job...", "INFO")
                                            trigger_result = trigger_manual_job(pid, terminate_job_id, dry_run=dry_run)
                                            
                                            if not dry_run and (not isinstance(trigger_result, dict) or not trigger_result.get('error')):
                                                # Wait for terminate job to complete
                                                terminate_result = wait_for_job_completion(pid, terminate_job_id, timeout=900)
                                                
                                                if terminate_result['status'] != 'success':
                                                    log(f"eb-terminate job {terminate_result['status']}. Stopping deployment.", "ERROR")
                                                    continue
                                                
                                                log("eb-terminate job completed successfully!", "INFO")
                                            elif dry_run:
                                                log("(dry-run) Would wait for eb-terminate to complete", "DRY")
                                        else:
                                            log(f"eb-terminate job is not manual (status: {terminate_status}). Skipping trigger.", "WARN")
                                    else:
                                        log("'eb-terminate' job not found in pipeline. Skipping termination step.", "WARN")
                                    
                                    # Step 2: Trigger deployment job based on tag name
                                    deploy_job_name = map_tag_to_deploy_job(tag)
                                    if not deploy_job_name:
                                        log(f"No deployment job mapping found for tag '{tag}'. Skipping deploy.", "WARN")
                                        continue
                                    
                                    # Refresh jobs list to get updated statuses
                                    jobs = get_pipeline_jobs(pid, pipeline_id)
                                    deploy_job = find_job_by_name(jobs, deploy_job_name)
                                    
                                    if deploy_job:
                                        deploy_job_id = deploy_job.get('id')
                                        deploy_status = deploy_job.get('status')
                                        
                                        log(f"Found '{deploy_job_name}' job (ID: {deploy_job_id}, status: {deploy_status})", "INFO")
                                        
                                        if deploy_status == 'manual':
                                            log(f"Triggering '{deploy_job_name}' job...", "INFO")
                                            trigger_result = trigger_manual_job(pid, deploy_job_id, dry_run=dry_run)
                                            
                                            if not dry_run and (not isinstance(trigger_result, dict) or not trigger_result.get('error')):
                                                # Wait for deploy job to complete
                                                deploy_result = wait_for_job_completion(pid, deploy_job_id, timeout=1200)
                                                
                                                if deploy_result['status'] == 'success':
                                                    log(f"[SUCCESS] Deployment completed successfully for tag '{tag}'!", "INFO")
                                                    deployment_successful = True
                                                else:
                                                    log(f"Deployment {deploy_result['status']} for tag '{tag}'.", "ERROR")
                                            elif dry_run:
                                                log(f"(dry-run) Would wait for {deploy_job_name} to complete", "DRY")
                                        else:
                                            log(f"{deploy_job_name} job is not manual (status: {deploy_status}). Skipping trigger.", "WARN")
                                    else:
                                        log(f"'{deploy_job_name}' job not found in pipeline. Cannot deploy.", "ERROR")
                                        log(f"Available jobs: {', '.join([j.get('name', 'unknown') for j in jobs])}", "INFO")
                        else:
                            log(f"Failed to create tag '{tag}': {t_res.get('details')}", "ERROR")
    
    # WORKFLOW 3: Create MR after successful deployment (NEW FLOW)
    if deployment_successful and not skip_mr:
        log(f"[SUCCESS] Deployment successful for {p_name}!", "INFO")
        
        # Check if MR already exists
        try:
            existing = api_call(f"projects/{pid}/merge_requests?state=opened&source_branch={urllib.parse.quote(FEATURE_BRANCH, safe='')}")
        except Exception as e:
            existing = {"error": True, "details": str(e)}

        has_open_mr = isinstance(existing, list) and len(existing) > 0
        if isinstance(existing, dict) and existing.get("error"):
            log(f"Could not verify existing MRs for {p_name}: {existing.get('details')}", "ERROR")

        if has_open_mr:
            try:
                existing_url = existing[0].get('web_url', 'unknown')
                log(f"MR already exists: {existing_url}", "INFO")
            except Exception:
                log("MR already exists (could not parse response).", "INFO")
        else:
            # Ensure feature branch exists
            if not (isinstance(br_check, dict) and "name" in br_check):
                if prompt_yes_no(f"Feature branch {FEATURE_BRANCH} not found. Create it from {SOURCE_BRANCH} so an MR can be opened?", default=False, non_interactive=non_interactive):
                    branch_created = create_feature_branch(pid, p_name, dry_run=dry_run)
                    if not branch_created and not dry_run:
                        log(f"[ERROR] Cannot create MR without feature branch for {p_name}", "ERROR")
                    else:
                        br_check = api_call(f"projects/{pid}/repository/branches/{urllib.parse.quote(FEATURE_BRANCH, safe='')}")

            if isinstance(br_check, dict) and "name" in br_check:
                if prompt_yes_no(f"Deployment successful! Create merge request for {p_name} from {FEATURE_BRANCH} into {SOURCE_BRANCH}?", default=True, non_interactive=non_interactive):
                    mr_payload = {"source_branch": FEATURE_BRANCH, "target_branch": SOURCE_BRANCH, "title": MR_TITLE}
                    mr_result = api_call(f"projects/{pid}/merge_requests", "POST", mr_payload, dry_run=dry_run)
                    
                    if not dry_run:
                        if isinstance(mr_result, dict) and not mr_result.get("error"):
                            mr_url = mr_result.get('web_url', 'N/A')
                            log(f"[SUCCESS] MR created: {mr_url}", "INFO")
                        else:
                            log(f"[ERROR] Failed to create MR: {mr_result.get('details', 'unknown error')}", "ERROR")
                    else:
                        log("(dry-run) MR would be created.", "INFO")
            else:
                log(f"Skipping MR creation because feature branch {FEATURE_BRANCH} does not exist.", "INFO")
    elif not deployment_successful and not skip_mr and do_orch:
        log(f"[INFO] Skipping MR creation for {p_name} - deployment was not successful", "INFO")
    
    # Log completion time for this project
    project_end_time = time.time()
    project_duration = project_end_time - project_start_time
    minutes = int(project_duration // 60)
    seconds = int(project_duration % 60)
    
    if minutes > 0:
        log(f"[COMPLETE] Completed {p_name} in {minutes}m {seconds}s", "INFO")
    else:
        log(f"[COMPLETE] Completed {p_name} in {seconds}s", "INFO")
    
    # Update state tracking
    if state is not None:
        if project_summary['success']:
            state['completed_projects'].append(pid)
            if dry_run_summary is not None:
                dry_run_summary['would_succeed'] += 1
        else:
            state['failed_projects'].append(pid)
            if dry_run_summary is not None:
                dry_run_summary['would_fail'] += 1
        
        # Save state after each project (for resume capability)
        save_state(state)
    
    # Update dry-run summary
    if dry_run_summary is not None:
        if project_summary['warnings']:
            dry_run_summary['warnings'] += 1
            dry_run_summary['issues'].extend([f"{p_name}: {w}" for w in project_summary['warnings']])


def process_in_batches(project_ids, batch_size=3, batch_delay=30, per_project_prompt=False, **kwargs):
    total = len(project_ids)
    if total == 0:
        log("No projects to process.", "INFO")
        return
    for start in range(0, total, batch_size):
        batch = project_ids[start:start + batch_size]
        log(f"Starting batch {start // batch_size + 1}: projects {batch}", "INFO")
        for pid in batch:
            try:
                # Per-project file selection if enabled
                if per_project_prompt:
                    project_info = api_call(f"projects/{pid}")
                    p_name = project_info.get('name', f"ID:{pid}") if isinstance(project_info, dict) else f"ID:{pid}"
                    print(f"\n{'='*60}")
                    print(f"PROJECT: {p_name} (ID: {pid})")
                    print(f"{'='*60}")
                    raw_choices = input(f"Select updates for {p_name} (1:POM, 2:CI, 3:EB, 4:ALL, 0:SKIP): ").replace(" ", "")
                    
                    if raw_choices == '0' or raw_choices.lower() == 'skip':
                        log(f"Skipping project {p_name}", "INFO")
                        continue
                    
                    proj_choices = raw_choices.split(',') if raw_choices else []
                    if '4' in proj_choices:
                        proj_choices = [c for c in proj_choices if c != '4']
                        for c in ('3', '2', '1'):
                            if c not in proj_choices:
                                proj_choices.insert(0, c)
                    
                    # Override the choices in kwargs
                    kwargs_copy = kwargs.copy()
                    kwargs_copy['choices'] = proj_choices
                    process_project(pid, **kwargs_copy)
                else:
                    process_project(pid, **kwargs)
            except Exception as e:
                log(f"Unexpected error processing project {pid}: {e}", "ERROR")
        if start + batch_size < total:
            log(f"Sleeping {batch_delay}s before next batch...", "INFO")
            time.sleep(batch_delay)


def main():
    global TOKEN, PROJECT_IDS, BASE_URL

    parser = argparse.ArgumentParser(description="Batch Java migration helper for GitLab projects.")
    parser.add_argument("--projects", help="Comma-separated project IDs to process (overrides PROJECT_IDS)", default="")
    parser.add_argument("--full-diff", help="Show full unified diffs (default: True)", action="store_true", default=True)
    parser.add_argument("--summary-only", help="Show only line-count summary (overrides --full-diff)", action="store_true")
    parser.add_argument("--dry-run", help="Do not perform write operations (branches, commits, MRs, tags)", action="store_true")
    parser.add_argument("--skip-mr", help="Do not create merge requests (MR creation will be skipped)", action="store_true")
    parser.add_argument("--force-tags", help="Force tag recreation even if tag already points to feature branch head", action="store_true")
    parser.add_argument("--auto-deploy", help="Automatically wait for build and trigger deployment jobs after tag creation", action="store_true")
    parser.add_argument("--non-interactive", help="Non-interactive mode: no prompts, auto-proceed with all actions (implies --auto-deploy)", action="store_true")
    parser.add_argument("--token", help="GitLab private token string (overrides file-based tokens)", default="")
    parser.add_argument("--base-url", help="GitLab API base URL (overrides .env and script constant)", default="")
    parser.add_argument("--batch-size", help="Number of projects to process in each batch", type=int, default=3)
    parser.add_argument("--batch-delay", help="Delay in seconds between batches", type=int, default=30)
    parser.add_argument("--per-project-prompt", help="Ask which files to update for each project individually", action="store_true")
    
    # New feature flags
    parser.add_argument("--resume", help="Resume from a previous state file", default="")
    parser.add_argument("--rollback", help="Rollback changes from a previous migration using rollback file", default="")
    parser.add_argument("--workflow", help="Choose workflow: 'changes' (make changes + deploy + MR) or 'deploy' (just deploy)", 
                       choices=['changes', 'deploy'], default=None)
    
    args = parser.parse_args()

    # Handle rollback mode
    if args.rollback:
        perform_rollback(args.rollback, dry_run=args.dry_run)
        sys.exit(0)

    # Setup file logging
    log_file = setup_file_logging(LOG_DIR_MIGRATION)
    log(f"Logging to file: {log_file}", "INFO")
    
    # Initialize state and rollback tracking
    state = {
        'completed_projects': [],
        'failed_projects': [],
        'skipped_projects': [],
        'start_time': datetime.datetime.now().isoformat()
    }
    
    rollback_data = {
        'snapshots': []
    }
    
    # Resume from previous state if requested
    resume_from_project = None
    if args.resume:
        loaded_state = load_state(args.resume)
        if loaded_state:
            state = loaded_state
            completed = state.get('completed_projects', [])
            if completed:
                resume_from_project = completed[-1]
                log(f"Resuming from last completed project: {resume_from_project}", "INFO")
        else:
            log(f"Could not load state from {args.resume}, starting fresh", "WARN")
    
    # Dry-run summary tracking
    dry_run_summary = {
        'total_projects': 0,
        'would_succeed': 0,
        'warnings': 0,
        'would_fail': 0,
        'changes': {'pom_files': 0, 'ci_files': 0, 'eb_files': 0},
        'actions': {'commits': 0, 'mrs': 0, 'tags': 0, 'deployments': 0, 'tag_breakdown': {}},
        'issues': [],
        'avg_time_per_project': 150  # Default estimate in seconds
    }

    dry_run = args.dry_run
    skip_mr = args.skip_mr
    force_tags = args.force_tags
    auto_deploy = args.auto_deploy or args.non_interactive
    non_interactive = args.non_interactive
    per_project_prompt = args.per_project_prompt and not args.non_interactive
    show_full = args.full_diff and not args.summary_only
    
    # Load TOKEN from file if not already set
    if args.token:
        TOKEN = args.token.strip()
        log("Using token from command line argument", "INFO")
    elif not TOKEN:
        file_token = load_token_from_file()
        if file_token:
            TOKEN = file_token
        else:
            log("No token found in .env or token.txt files", "WARN")
    
    # Load BASE_URL
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
        log("  4. Edit TOKEN constant in the script", "ERROR")
        sys.exit(1)
    
    # Auto-detect company domain
    company_domain = extract_company_domain_from_url(BASE_URL)
    if company_domain:
        log(f"Auto-detected company domain: {company_domain}", "INFO")
    
    # Validate token
    log("Validating GitLab token...", "INFO")
    token_validation = validate_and_log_token_info(TOKEN, BASE_URL)
    
    if not token_validation['valid']:
        log("Token validation failed. Please check your token and try again.", "ERROR")
        sys.exit(1)

    # Load project IDs
    project_ids = PROJECT_IDS
    if args.projects:
        try:
            project_ids = [int(x.strip()) for x in args.projects.split(",") if x.strip()]
        except Exception:
            log("Invalid --projects value. Provide comma-separated integers.", "ERROR")
            sys.exit(1)
    if not project_ids:
        log("Please add PROJECT_IDS or pass --projects.", "ERROR")
        sys.exit(1)
    
    dry_run_summary['total_projects'] = len(project_ids)
    
    # Filter out already completed projects if resuming
    if resume_from_project:
        completed_set = set(state.get('completed_projects', []))
        project_ids = [pid for pid in project_ids if pid not in completed_set]
        skipped_count = len(completed_set)
        if skipped_count > 0:
            log(f"Skipping {skipped_count} already completed project(s)", "INFO")
            dry_run_summary['total_projects'] = len(project_ids)
    
    # WORKFLOW SELECTION (NEW!)
    skip_file_changes = False
    
    if args.workflow:
        workflow = args.workflow
    else:
        # Interactive workflow selection
        print("\n" + "=" * 70)
        print("WORKFLOW SELECTION")
        print("=" * 70)
        print("\nChoose your workflow:\n")
        print("1. MAKE CHANGES + DEPLOY + MR")
        print("   - Modify files (POM, CI, EB)")
        print("   - Create/update tags")
        print("   - Trigger deployment")
        print("   - Create MR after successful deployment")
        print()
        print("2. JUST DEPLOY")
        print("   - Skip file modifications")
        print("   - Create/update tags only")
        print("   - Trigger deployment")
        print()
        
        workflow_choice = input("Choose workflow [1/2] (default: 1): ").strip()
        
        if workflow_choice == '2':
            workflow = 'deploy'
            skip_file_changes = True
            log("[INFO] Selected workflow: JUST DEPLOY (skip file changes)", "INFO")
        else:
            workflow = 'changes'
            log("[INFO] Selected workflow: MAKE CHANGES + DEPLOY + MR", "INFO")
    
    if workflow == 'deploy':
        skip_file_changes = True
        log("[INFO] Workflow: JUST DEPLOY (skip file changes)", "INFO")
    else:
        log("[INFO] Workflow: MAKE CHANGES + DEPLOY + MR", "INFO")
    
    # Dry-run mode selection
    if not args.dry_run:
        print("\n" + "=" * 70)
        print("DRY-RUN MODE SELECTION")
        print("=" * 70)
        print("\nWould you like to run in DRY-RUN mode first?\n")
        print("[DRY-RUN] DRY-RUN MODE (SIMULATION ONLY):")
        print("   - Shows all proposed changes and diffs")
        print("   - Simulates commits, MRs, tags, deployments")
        print("   - Generates summary report")
        print("   - NO actual changes made to GitLab")
        print()
        print("[ACTUAL] ACTUAL RUN MODE (MAKES REAL CHANGES):")
        print("   - Creates real commits")
        print("   - Creates real MRs")
        print("   - Creates/recreates tags")
        print("   - Triggers actual deployments")
        print("   - [WARNING] This will modify GitLab!")
        print()
        
        dry_run_choice = input("Run in DRY-RUN mode? [Y/n] (recommended: Y): ").strip().lower()
        
        if dry_run_choice in ('', 'y', 'yes'):
            dry_run = True
            print("\n" + "[DRY-RUN] " + "=" * 60)
            print("[DRY-RUN] RUNNING IN DRY-RUN MODE (SIMULATION ONLY)")
            print("[DRY-RUN] NO ACTUAL CHANGES WILL BE COMMITTED TO GITLAB")
            print("[DRY-RUN] " + "=" * 60 + "\n")
        else:
            print("\n" + "[ACTUAL] " + "=" * 60)
            print("[ACTUAL] RUNNING IN ACTUAL MODE - REAL CHANGES WILL BE MADE")
            print("[ACTUAL] [WARNING] This will commit changes to GitLab!")
            print("[ACTUAL] " + "=" * 60 + "\n")
            
            confirm = input("Are you sure you want to proceed with ACTUAL changes? [y/N]: ").strip().lower()
            if confirm not in ('y', 'yes'):
                log("User chose not to proceed with actual run. Exiting.", "INFO")
                sys.exit(0)

    # File selection (only if not skipping file changes)
    global_choices = None
    if not skip_file_changes and not per_project_prompt:
        if dry_run:
            print("\n[DRY-RUN] Showing what WOULD be changed (no actual commits)")
        
        raw_choices = input("Select updates to run for ALL projects (1:POM, 2:CI, 3:EB, 4:ALL, 0:EXIT), e.g. 1,2 or 4 or 0: ").replace(" ", "")
        
        if raw_choices == '0' or raw_choices.lower() == 'exit':
            log("User chose to exit. No changes will be made.", "INFO")
            sys.exit(0)
        
        global_choices = raw_choices.split(',') if raw_choices else []
        if '4' in global_choices:
            global_choices = [c for c in global_choices if c != '4']
            for c in ('3', '2', '1'):
                if c not in global_choices:
                    global_choices.insert(0, c)
        
        log(f"Will apply these updates to all projects: {', '.join(['POM' if c=='1' else 'CI' if c=='2' else 'EB' if c=='3' else c for c in global_choices])}", "INFO")
    elif skip_file_changes:
        log("[INFO] Skipping file selection (deploy-only mode)", "INFO")
        global_choices = []  # Empty choices for deploy-only mode

    # Process projects
    log("=" * 70, "INFO")
    log("STARTING MIGRATION", "INFO")
    log("=" * 70, "INFO")
    
    process_in_batches(
        project_ids,
        batch_size=args.batch_size,
        batch_delay=args.batch_delay,
        per_project_prompt=per_project_prompt,
        dry_run=dry_run,
        skip_mr=skip_mr,
        show_full=show_full,
        choices=global_choices,
        force_tags=force_tags,
        auto_deploy=auto_deploy,
        non_interactive=non_interactive,
        state=state,
        rollback_data=rollback_data,
        dry_run_summary=dry_run_summary if dry_run else None,
        skip_file_changes=skip_file_changes  # NEW parameter
    )
    
    # Save rollback data
    if rollback_data['snapshots'] and not dry_run:
        save_rollback_data(rollback_data)
        log(f"[SUCCESS] Rollback data saved. Use --rollback {ROLLBACK_FILE} to revert changes if needed.", "INFO")
    
    # Generate dry-run summary
    if dry_run:
        generate_dry_run_summary(dry_run_summary)
    
    log("=" * 70, "INFO")
    log("MIGRATION COMPLETE", "INFO")
    log("=" * 70, "INFO")


if __name__ == "__main__":
    main()
