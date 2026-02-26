from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import threading
from datetime import datetime
import base64
import re
import time
import json
import os
import urllib.parse

app = Flask(__name__)
CORS(app)

import migration_script as ms

env_config = ms.load_env_config()
PROJECT_NAMES = env_config['projects']

ms.BASE_URL = env_config['base_url']
ms.TOKEN = env_config['token']
ms.NEW_DEFAULT_PLATFORM = env_config['new_default_platform']
ms.REVIEWER_USERNAMES = env_config['reviewer_usernames']
ms.ASSIGNEE_USERNAMES = env_config['assignee_usernames']


def _read_env_file_robust():
    """
    Read the .env file with full robustness:
      - Handles UTF-8 BOM (added silently by Windows Notepad)
      - Handles CRLF and LF line endings
      - Accepts both  KEY=VALUE  and  KEY = VALUE  forms
      - Strips surrounding quotes from values
    Returns dict of all parsed key→value pairs (raw, uppercased keys).
    """
    env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    result = {}
    if not os.path.exists(env_file):
        print(f"[ENV] .env not found at {env_file}")
        return result
    try:
        # utf-8-sig strips the BOM if present; utf-8 is fine if not
        with open(env_file, encoding='utf-8-sig', errors='replace') as f:
            raw = f.read()
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            k = k.strip().upper()
            v = v.strip()
            # Strip surrounding quotes
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                v = v[1:-1]
            result[k] = v
        print(f"[ENV] Parsed {len(result)} keys from .env: {', '.join(sorted(result.keys()))}")
    except Exception as ex:
        print(f"[ENV] Error reading .env: {ex}")
    return result


def _apply_env_dict(env_dict):
    """Apply parsed .env dict to ms.* globals and PROJECT_NAMES."""
    global PROJECT_NAMES
    token_keys   = ('GITLAB_TOKEN', 'TOKEN')
    base_keys    = ('BASE_URL', 'GITLAB_BASE_URL', 'GITLAB_URL')
    for k in token_keys:
        if env_dict.get(k):
            ms.TOKEN = env_dict[k]
            print(f"[ENV] TOKEN set from key '{k}' (length={len(ms.TOKEN)})")
            break
    for k in base_keys:
        if env_dict.get(k):
            ms.BASE_URL = env_dict[k].rstrip('/')
            print(f"[ENV] BASE_URL set from key '{k}': {ms.BASE_URL}")
            break
    if env_dict.get('NEW_DEFAULT_PLATFORM'):
        ms.NEW_DEFAULT_PLATFORM = env_dict['NEW_DEFAULT_PLATFORM']
    if env_dict.get('REVIEWER_USERNAMES'):
        ms.REVIEWER_USERNAMES = [x.strip() for x in env_dict['REVIEWER_USERNAMES'].split(',') if x.strip()]
    if env_dict.get('ASSIGNEE_USERNAMES'):
        ms.ASSIGNEE_USERNAMES = [x.strip() for x in env_dict['ASSIGNEE_USERNAMES'].split(',') if x.strip()]
    if env_dict.get('GITLAB_VERIFY_SSL'):
        ms.SSL_VERIFY = env_dict['GITLAB_VERIFY_SSL'].lower() != 'false'
    # PRESTO_PROJECT_IDS — comma-separated list of GitLab project IDs
    if env_dict.get('PRESTO_PROJECT_IDS'):
        ids = [x.strip() for x in env_dict['PRESTO_PROJECT_IDS'].split(',') if x.strip().isdigit()]
        env_config['presto_project_ids'] = [int(i) for i in ids]
        print(f"[ENV] PRESTO_PROJECT_IDS set: {env_config['presto_project_ids']}")
    # Actuator URLs (DEV / TEST / plain)
    for k, v in env_dict.items():
        if k.startswith('ACTUATOR_'):
            rest = k[len('ACTUATOR_'):]
            if rest.endswith('_DEV'):
                pid = rest[:-4]
                if pid.isdigit():
                    env_config.setdefault('actuator_urls_dev', {})[pid] = v.rstrip('/')
            elif rest.endswith('_TEST'):
                pid = rest[:-5]
                if pid.isdigit():
                    env_config.setdefault('actuator_urls_test', {})[pid] = v.rstrip('/')
            elif rest.endswith('_PERF'):
                pid = rest[:-5]
                if pid.isdigit():
                    env_config.setdefault('actuator_urls_perf', {})[pid] = v.rstrip('/')
            elif rest.isdigit():
                env_config.setdefault('actuator_urls', {})[rest] = v.rstrip('/')
        elif k.startswith('PERF_'):
            rest = k[len('PERF_'):]
            if rest.endswith('_DEV'):
                pid = rest[:-4]
                if pid.isdigit():
                    env_config.setdefault('perf_urls_dev', {})[pid] = v.rstrip('/')
            elif rest.endswith('_TEST'):
                pid = rest[:-5]
                if pid.isdigit():
                    env_config.setdefault('perf_urls_test', {})[pid] = v.rstrip('/')
            elif rest.isdigit():
                env_config.setdefault('perf_urls', {})[rest] = v.rstrip('/')
    # Projects
    new_projects = {int(k.replace('PROJECT_', '')): v
                    for k, v in env_dict.items()
                    if k.startswith('PROJECT_') and k[8:].isdigit()}
    if new_projects:
        PROJECT_NAMES = new_projects
        print(f"[ENV] Loaded {len(PROJECT_NAMES)} project(s)")


# ── Robust fallback: if migration_script couldn't read the token (e.g. BOM),
#    read the .env ourselves with BOM-safe encoding and patch the globals.
if not ms.TOKEN:
    print("[ENV] migration_script returned empty token — attempting BOM-safe fallback read")
    _fallback_env = _read_env_file_robust()
    if _fallback_env:
        _apply_env_dict(_fallback_env)
        if ms.TOKEN:
            print(f"[ENV] Fallback read succeeded — token loaded (length={len(ms.TOKEN)})")
        else:
            print("[ENV] WARNING: fallback read found no token either — check GITLAB_TOKEN in .env")
    # Store for diagnostics
    env_config['_raw_env'] = _fallback_env
else:
    # Even when migration_script succeeded, do a BOM-safe read to cover projects/extra keys
    _fallback_env = _read_env_file_robust()
    env_config['_raw_env'] = _fallback_env
    print(f"[ENV] Token loaded via migration_script (length={len(ms.TOKEN)})")


def _load_extended_env_config():
    """Parse additional .env keys not handled by migration_script.load_env_config:
      ACTUATOR_<project_id>   = https://my-app.internal/actuator
      PARENT_POM_PROJECT_ID   = 999  (GitLab project ID of the parent pom repo)
      PRESTO_PROJECT_IDS      = 123,456,789  (comma-separated project IDs for Presto cert mgmt)
    """
    extra = {
        'actuator_urls': {},
        'actuator_urls_dev': {},
        'actuator_urls_test': {},
        'actuator_urls_perf': {},
        'perf_urls': {},
        'perf_urls_dev': {},
        'perf_urls_test': {},
        'parent_pom_project_id': None,
        'presto_project_ids': [],          # ← NEW
    }
    env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    if not os.path.exists(env_file):
        return extra
    try:
        with open(env_file, encoding='utf-8-sig', errors='replace') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, v = line.split('=', 1)
                k, v = k.strip().upper(), v.strip().strip('"\'')
                if k.startswith('ACTUATOR_'):
                    rest = k[len('ACTUATOR_'):]
                    if rest.endswith('_DEV'):
                        pid = rest[:-4]
                        if pid.isdigit():
                            extra['actuator_urls_dev'][pid] = v.rstrip('/')
                    elif rest.endswith('_TEST'):
                        pid = rest[:-5]
                        if pid.isdigit():
                            extra['actuator_urls_test'][pid] = v.rstrip('/')
                    elif rest.endswith('_PERF'):
                        pid = rest[:-5]
                        if pid.isdigit():
                            extra['actuator_urls_perf'][pid] = v.rstrip('/')
                    elif rest.isdigit():
                        extra['actuator_urls'][rest] = v.rstrip('/')
                elif k.startswith('PERF_'):
                    rest = k[len('PERF_'):]
                    if rest.endswith('_DEV'):
                        pid = rest[:-4]
                        if pid.isdigit():
                            extra['perf_urls_dev'][pid] = v.rstrip('/')
                    elif rest.endswith('_TEST'):
                        pid = rest[:-5]
                        if pid.isdigit():
                            extra['perf_urls_test'][pid] = v.rstrip('/')
                    elif rest.isdigit():
                        extra['perf_urls'][rest] = v.rstrip('/')
                elif k == 'PARENT_POM_PROJECT_ID' and v.isdigit():
                    extra['parent_pom_project_id'] = int(v)
                elif k == 'PRESTO_PROJECT_IDS':          # ← NEW
                    ids = [x.strip() for x in v.split(',') if x.strip().isdigit()]
                    extra['presto_project_ids'] = [int(i) for i in ids]
                    print(f"[ENV] PRESTO_PROJECT_IDS loaded: {extra['presto_project_ids']}")
    except Exception as ex:
        print(f"[WARN] _load_extended_env_config: {ex}")
    return extra

_ext_config = _load_extended_env_config()
# Attach to env_config dict so /api/reload-config can refresh them together
env_config['actuator_urls']         = _ext_config['actuator_urls']
env_config['actuator_urls_dev']     = _ext_config['actuator_urls_dev']
env_config['actuator_urls_test']    = _ext_config['actuator_urls_test']
env_config['actuator_urls_perf']    = _ext_config['actuator_urls_perf']
env_config['perf_urls']             = _ext_config['perf_urls']
env_config['perf_urls_dev']         = _ext_config['perf_urls_dev']
env_config['perf_urls_test']        = _ext_config['perf_urls_test']
env_config['parent_pom_project_id'] = _ext_config['parent_pom_project_id']
env_config['presto_project_ids']    = _ext_config['presto_project_ids']   # ← NEW

# CRITICAL: setup_http_session() is only called in the CLI __main__ block.
# Flask never hits __main__, so HTTP_SESSION stays None and every api_call
# fails with AttributeError then retries 3x (1s+2s+4s = ~7s per call).
# We must initialise the session here so Flask requests work immediately.
_ssl_verify = env_config.get('ssl_verify')
if _ssl_verify is None:
    _ssl_verify = True
ms.SSL_VERIFY = _ssl_verify
ms.setup_http_session(ssl_verify=_ssl_verify)
print(f"[INFO] HTTP session initialised (ssl_verify={_ssl_verify}, token_set={bool(ms.TOKEN)})")

active_tasks = {}
project_history = []
pipeline_status = {}  # Track pipeline status per project
project_interrupt_flags = {}  # Per-project stop signals: {project_id: threading.Event}

_TERMINAL_TASK_STATUSES = {'success', 'failed', 'cancelled', 'stopped', 'idempotent', 'partial_success', 'no_changes'}
_ACTIVE_TASKS_MAX_AGE_S = 1800   # prune terminal tasks older than 30 minutes
_ACTIVE_TASKS_MAX_ENTRIES = 200  # hard cap before forced prune

def _cleanup_active_tasks():
    """Remove terminal tasks older than 30 min (or when the dict exceeds the hard cap).
    Called at the end of every background thread so the dict never grows unbounded.
    Not thread-safe on its own, but Python's GIL makes dict mutation safe enough here
    given we only call this from thread finally blocks and iterate a snapshot copy.
    """
    now_ts = time.time()
    to_delete = []
    for task_id, task in list(active_tasks.items()):
        if task.get('status') not in _TERMINAL_TASK_STATUSES:
            continue
        # Task IDs are formatted as "<project_id>_<epoch_ms>" or "bulk_mr_<epoch_ms>" etc.
        try:
            epoch_ms = int(task_id.rsplit('_', 1)[-1])
            age_s = now_ts - (epoch_ms / 1000.0)
            if age_s > _ACTIVE_TASKS_MAX_AGE_S:
                to_delete.append(task_id)
        except (ValueError, IndexError):
            pass
    # If still over cap after age-based prune, remove oldest terminal entries
    if len(active_tasks) - len(to_delete) > _ACTIVE_TASKS_MAX_ENTRIES:
        terminal = [(tid, t) for tid, t in active_tasks.items()
                    if t.get('status') in _TERMINAL_TASK_STATUSES and tid not in to_delete]
        terminal.sort(key=lambda x: x[0])  # sort by task_id (which embeds timestamp)
        overflow = len(active_tasks) - len(to_delete) - _ACTIVE_TASKS_MAX_ENTRIES
        to_delete.extend(tid for tid, _ in terminal[:overflow])
    for tid in to_delete:
        active_tasks.pop(tid, None)
    if to_delete:
        print(f"[CLEANUP] Pruned {len(to_delete)} stale task(s) from active_tasks (remaining: {len(active_tasks)})")

# ── Concurrency / Queue Control ───────────────────────────────────────────────
MAX_CONCURRENT = int(os.environ.get('MAX_CONCURRENT_MIGRATIONS', '3'))
_migration_semaphore = threading.Semaphore(MAX_CONCURRENT)

# Lock that serialises writes to migration_script module-level globals
# (FEATURE_BRANCH, JIRA_ID, MR_TITLE, REVIEWER_USERNAMES, ASSIGNEE_USERNAMES).
# These globals are read inside ms.create_feature_branch / ms.create_mr_for_project,
# so we hold this lock for the duration of those calls.  Direct ms.api_call()
# invocations with explicit endpoints do NOT need the lock.
_ms_globals_lock = threading.RLock()

# _queue_state: keyed by str(project_id)
# Each entry: {'position': int, 'status': 'queued'|'running'|'done'|'cancelled', 'cancel_event': Event}
_queue_state: dict = {}
_queue_lock = threading.Lock()
_queue_counter = 0  # monotonic ticket number

# Circuit breaker
_cb_state = {
    'paused': False,
    'consecutive_failures': 0,
    'THRESHOLD': 2,
    'resume_event': threading.Event(),
}
_cb_state['resume_event'].set()  # starts unpaused


def _queue_register(pid_str: str) -> threading.Event:
    """Assign a queue ticket. Returns a cancel_event the caller can set to abort."""
    global _queue_counter
    with _queue_lock:
        _queue_counter += 1
        cancel_ev = threading.Event()
        _queue_state[pid_str] = {
            'position': _queue_counter,
            'status': 'queued',
            'cancel_event': cancel_ev,
        }
    return cancel_ev


def _queue_acquire(pid_str: str) -> bool:
    """Block until a semaphore slot is available (or cancelled). Returns False if cancelled."""
    state = _queue_state.get(pid_str)
    if not state:
        return True  # untracked — proceed immediately

    cancel_ev = state['cancel_event']
    pos = state.get('position', '?')
    print(f"[QUEUE] Project {pid_str} registered at queue position #{pos} — waiting for a concurrency slot (max concurrent: {MAX_CONCURRENT})")

    # Wait if circuit breaker is paused (up to 5 min before auto-continuing)
    if not _cb_state['resume_event'].is_set():
        print(f"[CIRCUIT-BREAKER] Project {pid_str} is blocked — circuit breaker is paused after consecutive failures. "
              f"Use /api/circuit-breaker/resume or wait up to 5 min for auto-resume.")
    _cb_state['resume_event'].wait(timeout=300)

    # Poll for semaphore while checking cancel; log a heartbeat every 60 s
    _wait_start = time.time()
    _last_heartbeat = _wait_start
    while True:
        if cancel_ev.is_set():
            with _queue_lock:
                if pid_str in _queue_state:
                    _queue_state[pid_str]['status'] = 'cancelled'
            print(f"[QUEUE] Project {pid_str} cancelled while waiting in queue")
            return False
        acquired = _migration_semaphore.acquire(blocking=False)
        if acquired:
            waited_s = int(time.time() - _wait_start)
            with _queue_lock:
                if pid_str in _queue_state:
                    _queue_state[pid_str]['status'] = 'running'
            print(f"[QUEUE] Project {pid_str} acquired slot after {waited_s}s wait — starting migration")
            return True
        now = time.time()
        if now - _last_heartbeat >= 60:
            waited_s = int(now - _wait_start)
            running_count = sum(1 for v in _queue_state.values() if v.get('status') == 'running')
            print(f"[QUEUE] Project {pid_str} still waiting... ({waited_s}s elapsed, {running_count}/{MAX_CONCURRENT} slots in use)")
            _last_heartbeat = now
        time.sleep(0.5)


def _queue_release(pid_str: str, succeeded: bool):
    """Release semaphore and update circuit breaker."""
    _migration_semaphore.release()
    with _queue_lock:
        if pid_str in _queue_state:
            _queue_state[pid_str]['status'] = 'done'

    if succeeded:
        if _cb_state['consecutive_failures'] > 0:
            print(f"[CIRCUIT-BREAKER] Project {pid_str} succeeded — resetting consecutive failure count "
                  f"(was {_cb_state['consecutive_failures']})")
        _cb_state['consecutive_failures'] = 0
    else:
        _cb_state['consecutive_failures'] += 1
        print(f"[CIRCUIT-BREAKER] Project {pid_str} failed — consecutive failures: "
              f"{_cb_state['consecutive_failures']}/{_cb_state['THRESHOLD']}")
        if _cb_state['consecutive_failures'] >= _cb_state['THRESHOLD']:
            _cb_state['paused'] = True
            _cb_state['resume_event'].clear()
            print(f"[CIRCUIT-BREAKER] *** PAUSED *** after {_cb_state['consecutive_failures']} consecutive failures. "
                  f"All queued projects will hold until you call POST /api/circuit-breaker/resume "
                  f"or 5 minutes elapse.")
# ─────────────────────────────────────────────────────────────────────────────
# ── Directory enforcement ─────────────────────────────────────────────────────
# Both STATE_FILE and PIPELINE_FILE must always live under state_logs/.
# We create state_logs/ here at module load time so it exists before any
# function tries to read or write these files, even on a fresh checkout.
_STATE_LOGS_DIR = 'state_logs'
os.makedirs(_STATE_LOGS_DIR, exist_ok=True)

STATE_FILE    = os.path.join(_STATE_LOGS_DIR, 'project_state.json')
PIPELINE_FILE = os.path.join(_STATE_LOGS_DIR, 'pipeline_state.json')

# ── One-time migration: move legacy root-level project_state.json if present ──
_LEGACY_STATE_FILE = 'project_state.json'
if os.path.exists(_LEGACY_STATE_FILE) and not os.path.exists(STATE_FILE):
    try:
        import shutil
        shutil.move(_LEGACY_STATE_FILE, STATE_FILE)
        print(f"[MIGRATE] Moved {_LEGACY_STATE_FILE} -> {STATE_FILE}")
    except Exception as _mv_err:
        print(f"[WARN] Could not migrate {_LEGACY_STATE_FILE} -> {STATE_FILE}: {_mv_err}")
# ──────────────────────────────────────────────────────────────────────────────

def load_project_state():
    global project_history
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                project_history = json.load(f)
        except (json.JSONDecodeError, OSError, ValueError):
            project_history = []
    else:
        project_history = []

def save_project_state():
    with open(STATE_FILE, 'w') as f:
        json.dump(project_history, f, indent=2)

def load_pipeline_state():
    global pipeline_status
    # Double-check directory exists before every load (handles edge-case where
    # the directory was removed while the server was running)
    os.makedirs(_STATE_LOGS_DIR, exist_ok=True)
    assert PIPELINE_FILE == os.path.join(_STATE_LOGS_DIR, 'pipeline_state.json'), \
        f"PIPELINE_FILE path drift detected: {PIPELINE_FILE}"
    if os.path.exists(PIPELINE_FILE):
        try:
            with open(PIPELINE_FILE, 'r') as f:
                pipeline_status = json.load(f)
                print(f"[INFO] Loaded {len(pipeline_status)} pipeline status entries from cache")
        except Exception as e:
            print(f"[WARN] Could not load pipeline state: {e}")
            pipeline_status = {}
    else:
        pipeline_status = {}

def save_pipeline_state():
    try:
        with open(PIPELINE_FILE, 'w') as f:
            json.dump(pipeline_status, f, indent=2)
    except Exception as e:
        print(f"[ERROR] Could not save pipeline state: {e}")

def add_project_to_history(project_id, project_name, action, status, details=None):
    # ── FIX: declare global so the slice assignment doesn't create a local shadow ──
    global project_history
    entry = {
        'project_id': project_id,
        'project_name': project_name,
        'action': action,
        'status': status,
        'timestamp': datetime.now().isoformat(),
        'details': details or {}
    }
    project_history.insert(0, entry)
    if len(project_history) > 100:
        project_history = project_history[:100]
    save_project_state()

def update_pipeline_status(project_id, status, pipeline_id=None, commit_sha=None, committer_name=None, commit_message=None, workflow_stage=None, path_with_namespace=None, pipeline_started_at=None):
    """Update pipeline status for a project, preserving existing details if not provided"""
    existing = pipeline_status.get(str(project_id), {})
    # Set pipeline_started_at only when a new pipeline_id is first registered (running state)
    new_pipeline_id = pipeline_id if pipeline_id is not None else existing.get('pipeline_id')
    old_pipeline_id = existing.get('pipeline_id')
    if pipeline_started_at is not None:
        effective_pipeline_started_at = pipeline_started_at
    elif new_pipeline_id and new_pipeline_id != old_pipeline_id and status == 'running':
        effective_pipeline_started_at = datetime.now().isoformat()
    else:
        effective_pipeline_started_at = existing.get('pipeline_started_at')

    # Compute pipeline_completed_at: set exactly once, when build pipeline first reaches success
    # (workflow_stage == pipeline_success) so metrics duration is always start→build-done.
    effective_workflow_stage = workflow_stage if workflow_stage is not None else existing.get('workflow_stage', 'idle')
    if status == 'success' and effective_workflow_stage == 'pipeline_success' and not existing.get('pipeline_completed_at'):
        effective_pipeline_completed_at = datetime.now().isoformat()
    else:
        effective_pipeline_completed_at = existing.get('pipeline_completed_at')

    pipeline_status[str(project_id)] = {
        'status': status,
        'pipeline_id': new_pipeline_id,
        'commit_sha': commit_sha if commit_sha is not None else existing.get('commit_sha'),
        'committer_name': committer_name if committer_name is not None else existing.get('committer_name', 'Unknown'),
        'commit_message': commit_message if commit_message is not None else existing.get('commit_message', ''),
        'timestamp': existing.get('timestamp') if status in ('success', 'failed') and existing.get('timestamp') else datetime.now().isoformat(),
        # Workflow stage: idle → committed → pipeline_success → deployed → mr_raised → merged
        'workflow_stage': effective_workflow_stage,
        # Full GitLab path (e.g. "my-group/my-subgroup/project") for building correct web URLs
        'path_with_namespace': path_with_namespace if path_with_namespace is not None else existing.get('path_with_namespace', ''),
        # ISO timestamp for when the *specific pipeline* started (used for live timer in UI)
        'pipeline_started_at': effective_pipeline_started_at,
        # ISO timestamp for when the build pipeline completed — used for accurate duration metrics
        'pipeline_completed_at': effective_pipeline_completed_at,
    }
    save_pipeline_state()
    print(f"[PIPELINE] Updated status for project {project_id}: {status} (stage: {pipeline_status[str(project_id)]['workflow_stage']})")

load_project_state()
load_pipeline_state()

# ── Ensure ALL log directories exist at startup ───────────────────────────────
# state_logs is already created above, but we re-assert here to keep the list
# complete and explicit.  exist_ok=True makes this a safe no-op on restarts.
_LOG_DIRS = ['migration_logs', 'rollback_logs', 'state_logs', 'api_audit_logs']
for _log_dir in _LOG_DIRS:
    os.makedirs(_log_dir, exist_ok=True)
    print(f"[INIT] Log directory ready: {_log_dir}/")
# ──────────────────────────────────────────────────────────────────────────────

# Initialize file logging
try:
    log_file = ms.setup_file_logging()
    print(f"[INFO] File logging initialized: {log_file}")
except Exception as e:
    print(f"[WARN] Could not initialize file logging: {e}")

# Initialize API audit logging
try:
    ms.setup_api_audit_logging()
    print(f"[INFO] API audit logging initialized: {ms.AUDIT_LOG_FILE}")
except Exception as e:
    print(f"[WARN] Could not initialize API audit logging: {e}")

def recheck_running_pipelines():
    """On startup, re-check any pipelines saved as 'running' to get their actual status"""
    for project_id_str, status_data in list(pipeline_status.items()):
        if status_data.get('status') == 'running':
            pipeline_id = status_data.get('pipeline_id')
            commit_sha = status_data.get('commit_sha')
            committer_name = status_data.get('committer_name', 'Unknown')
            commit_message = status_data.get('commit_message', '')
            if pipeline_id:
                try:
                    project_id = int(project_id_str)
                    pipeline_resp = ms.api_call(f"projects/{project_id}/pipelines/{pipeline_id}")
                    if isinstance(pipeline_resp, dict) and not pipeline_resp.get('error'):
                        actual_status = pipeline_resp.get('status', 'unknown')
                        if actual_status in ['success', 'failed', 'canceled', 'skipped']:
                            mapped = 'success' if actual_status == 'success' else 'failed'
                            # On success, advance to pipeline_success stage; on fail keep committed
                            existing_stage = status_data.get('workflow_stage', 'idle')
                            new_stage = 'pipeline_success' if mapped == 'success' and existing_stage in ('committed', 'idle') else existing_stage
                            update_pipeline_status(project_id, mapped, pipeline_id=pipeline_id,
                                                   commit_sha=commit_sha, committer_name=committer_name,
                                                   commit_message=commit_message, workflow_stage=new_stage)
                            print(f"[STARTUP] Re-checked pipeline {pipeline_id} → {mapped} (stage: {new_stage})")
                except Exception as e:
                    print(f"[WARN] Could not re-check pipeline {pipeline_id}: {e}")

# Run startup pipeline re-check in background so server starts immediately
threading.Thread(target=recheck_running_pipelines, daemon=True).start()



def _build_feature_branch(branch_num, branch_prefix=None, branch_suffix=None):
    """Build feature branch name from components, with sensible defaults."""
    prefix = branch_prefix or 'task-'
    suffix = branch_suffix or 'java17-migration'
    return f"{prefix}{branch_num}-{suffix}"


def _resolve_source_branch(project_id):
    """Return the first existing branch in: develop → master → main.
    Falls back to 'develop' if none can be verified (avoids blocking callers)."""
    for candidate in ('develop', 'master', 'main'):
        resp = ms.api_call(f"projects/{project_id}/repository/branches/{urllib.parse.quote(candidate, safe='')}")
        if isinstance(resp, dict) and resp.get('name'):
            return candidate
    return 'develop'  # last-resort fallback


@app.route('/')
def index():
    try:
        return send_file('index.html')
    except FileNotFoundError:
        return "<h1>Error: index.html not found</h1><p>Make sure index.html is in the same directory as app.py</p>", 404


@app.route('/favicon.ico')
def favicon():
    try:
        return send_file('favicon.ico', mimetype='image/x-icon')
    except FileNotFoundError:
        return '', 204  # No content if favicon not found


@app.route('/api/config', methods=['GET'])
def get_config():
    """Get configuration from .env file"""
    # Get token info
    token_info = {'valid': False, 'reason': 'unknown'}
    try:
        if not ms.TOKEN or ms.TOKEN.strip() == '':
            token_info['reason'] = 'no_token'
        elif not ms.BASE_URL or ms.BASE_URL.strip() == '':
            token_info['reason'] = 'no_base_url'
        elif ms.HTTP_SESSION is None:
            token_info['reason'] = 'session_not_initialised'
        else:
            user_resp = ms.api_call('user')
            if isinstance(user_resp, dict) and not user_resp.get('error'):
                token_info['valid'] = True
                token_info['reason'] = 'ok'
                # Check token expiry if available — non-fatal if this call fails
                try:
                    token_resp = ms.api_call('personal_access_tokens/self')
                    if isinstance(token_resp, dict) and 'expires_at' in token_resp:
                        token_info['expires_at'] = token_resp['expires_at']
                except Exception:
                    pass
            else:
                err_detail = user_resp.get('details', '') if isinstance(user_resp, dict) else ''
                if '401' in err_detail or 'Unauthorized' in err_detail:
                    token_info['reason'] = 'invalid'
                elif '404' in err_detail or 'Not Found' in err_detail:
                    token_info['reason'] = 'wrong_base_url'
                else:
                    token_info['reason'] = 'invalid'
    except Exception as _e:
        _emsg = str(_e)
        if 'NoneType' in _emsg or 'session' in _emsg.lower():
            token_info['reason'] = 'session_not_initialised'
        elif 'Connection' in _emsg or 'timeout' in _emsg.lower() or 'refused' in _emsg.lower():
            token_info['reason'] = 'connection_error'
        else:
            token_info['reason'] = 'connection_error'
    
    # Fetch path_with_namespace for each project in parallel so we don't
    # pay an N×RTT serial penalty on every Load Config click.
    import concurrent.futures

    def _fetch_project_path(pid_pname):
        pid, pname = pid_pname
        try:
            proj_resp = ms.api_call(f"projects/{pid}")
            if isinstance(proj_resp, dict) and not proj_resp.get('error'):
                path = proj_resp.get('path_with_namespace') or pname
            else:
                path = pname
        except Exception:
            path = pname
        return {'id': pid, 'name': pname, 'path_with_namespace': path}

    projects = []
    if PROJECT_NAMES:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(10, len(PROJECT_NAMES))) as ex:
            results = list(ex.map(_fetch_project_path, PROJECT_NAMES.items()))
        projects = results

    return jsonify({
        'success': True,
        'base_url': ms.BASE_URL,
        'projects': projects,
        'assignees': ms.ASSIGNEE_USERNAMES,
        'reviewers': ms.REVIEWER_USERNAMES,
        'new_default_platform': ms.NEW_DEFAULT_PLATFORM,
        'token_info': token_info,
        'actuator_urls_dev':   list(env_config.get('actuator_urls_dev',  {}).keys()),
        'actuator_urls_test':  list(env_config.get('actuator_urls_test', {}).keys()),
        'actuator_urls_perf':  list(env_config.get('actuator_urls_perf', {}).keys()),
        'actuator_urls_plain': list(env_config.get('actuator_urls',      {}).keys()),
        'perf_urls_dev':       list(env_config.get('perf_urls_dev',      {}).keys()),
        'perf_urls_test':      list(env_config.get('perf_urls_test',     {}).keys()),
        'perf_urls_plain':     list(env_config.get('perf_urls',          {}).keys()),
        # ── NEW: list of project IDs that use Presto / JKS cert management ──
        'presto_project_ids':  env_config.get('presto_project_ids', []),
    })


@app.route('/api/token/refresh', methods=['POST'])
def refresh_token_info():
    """Refresh GitLab token information"""
    try:
        token_info = {'valid': False, 'reason': 'unknown'}
        if not ms.TOKEN or ms.TOKEN.strip() == '':
            token_info['reason'] = 'no_token'
        else:
            user_resp = ms.api_call('user')
            if isinstance(user_resp, dict) and not user_resp.get('error'):
                token_info['valid'] = True
                token_info['reason'] = 'ok'
                token_info['username'] = user_resp.get('username', 'Unknown')
                
                # Get token expiry
                token_resp = ms.api_call('personal_access_tokens/self')
                if isinstance(token_resp, dict) and 'expires_at' in token_resp:
                    token_info['expires_at'] = token_resp['expires_at']
                    token_info['scopes'] = token_resp.get('scopes', [])
            else:
                token_info['reason'] = 'invalid'
        return jsonify({
            'success': True,
            'token_info': token_info
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<int:project_id>/preview', methods=['POST'])
def preview_changes(project_id):
    """Generate preview of changes before committing"""
    try:
        data = request.json
        choices = data.get('choices', [])
        branch_num = data.get('branch_num', '12938')

        # Compute branch name locally — do NOT write to ms.FEATURE_BRANCH here.
        # Flask serves requests concurrently; writing to shared ms globals before
        # a thread-spawning endpoint would create a race condition.
        _feature_branch = _build_feature_branch(branch_num, data.get("branch_prefix","task-"), data.get("branch_suffix","java17-migration"))

        # Get project info
        project_info = ms.api_call(f"projects/{project_id}")
        p_name = project_info.get('name', f"ID:{project_id}") if isinstance(project_info, dict) else f"ID:{project_id}"

        # Check which branch to use for preview
        br_check = ms.api_call(f"projects/{project_id}/repository/branches/{urllib.parse.quote(_feature_branch, safe='')}")
        current_ref = _feature_branch if isinstance(br_check, dict) and "name" in br_check else ms.SOURCE_BRANCH
        
        changes = []
        
        # Helper: get last commit info for a file on a branch
        def get_file_commit_info(pid, file_path, ref):
            try:
                commits_resp = ms.api_call(f"projects/{pid}/repository/commits?path={urllib.parse.quote(file_path, safe='')}&ref_name={urllib.parse.quote(ref, safe='')}&per_page=1")
                if isinstance(commits_resp, list) and len(commits_resp) > 0:
                    c = commits_resp[0]
                    return {
                        'last_commit_id': c.get('id', ''),
                        'commit_author': c.get('author_name', c.get('committer_name', 'Unknown')),
                        'commit_date': c.get('committed_date', c.get('created_at', ''))
                    }
            except Exception:
                pass
            return {'last_commit_id': '', 'commit_author': 'Unknown', 'commit_date': ''}
        
        # Process each selected choice
        if '1' in choices:  # POM
            try:
                res = ms.api_call(f"projects/{project_id}/repository/files/{urllib.parse.quote('pom.xml', safe='')}?ref={urllib.parse.quote(current_ref, safe='')}")
                if isinstance(res, dict) and "content" in res:
                    orig = base64.b64decode(res['content']).decode('utf-8')
                    upd = re.sub(r"<(java\.version|jdk\.version|maven\.compiler\.(source|target|release))>(?:1\.\d+|\d+)</\1>", r"<\1>17</\1>", orig)
                    if "<parent>" in upd:
                        upd = re.sub(r"<parent>[\s\S]*?</parent>", ms.update_parent_block, upd)
                    if orig != upd:
                        diff = generate_diff(orig, upd)
                        commit_info = get_file_commit_info(project_id, 'pom.xml', current_ref)
                        changes.append({
                            'file': 'pom.xml',
                            'file_path': 'pom.xml',
                            'old': orig,
                            'new': upd,
                            'diff': diff,
                            **commit_info
                        })
            except Exception as e:
                print(f"[ERROR] Preview POM for {project_id}: {e}")
        
        if '2' in choices:  # CI
            try:
                res = ms.api_call(f"projects/{project_id}/repository/files/{urllib.parse.quote('.gitlab-ci.yml', safe='')}?ref={urllib.parse.quote(current_ref, safe='')}")
                if isinstance(res, dict) and "content" in res:
                    orig = base64.b64decode(res['content']).decode('utf-8')
                    upd = re.sub(r"^\s*image:.*(\n|$)", "", orig, flags=re.MULTILINE)
                    if orig != upd:
                        diff = generate_diff(orig, upd)
                        commit_info = get_file_commit_info(project_id, '.gitlab-ci.yml', current_ref)
                        changes.append({
                            'file': '.gitlab-ci.yml',
                            'file_path': '.gitlab-ci.yml',
                            'old': orig,
                            'new': upd,
                            'diff': diff,
                            **commit_info
                        })
            except Exception as e:
                print(f"[ERROR] Preview CI for {project_id}: {e}")
        
        if '3' in choices:  # EB Config
            try:
                path = urllib.parse.quote(".elasticbeanstalk/config.yml", safe='')
                res = ms.api_call(f"projects/{project_id}/repository/files/{path}?ref={urllib.parse.quote(current_ref, safe='')}")
                if isinstance(res, dict) and "content" in res:
                    orig = base64.b64decode(res['content']).decode('utf-8')
                    upd = re.sub(r"(default_platform:\s*).*$", f"default_platform: {ms.NEW_DEFAULT_PLATFORM}", orig, flags=re.MULTILINE)
                    if orig != upd:
                        diff = generate_diff(orig, upd)
                        commit_info = get_file_commit_info(project_id, '.elasticbeanstalk/config.yml', current_ref)
                        changes.append({
                            'file': '.elasticbeanstalk/config.yml',
                            'file_path': '.elasticbeanstalk/config.yml',
                            'old': orig,
                            'new': upd,
                            'diff': diff,
                            **commit_info
                        })
            except Exception as e:
                print(f"[ERROR] Preview EB for {project_id}: {e}")
        
        return jsonify({
            'success': True,
            'project': p_name,
            'actions': changes,  # renamed from 'changes' to match frontend expectations
            'changes': changes,  # keep for backwards compat
            'branch': _feature_branch
        })
        
    except Exception as e:
        print(f"[ERROR] Preview failed for project {project_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<int:project_id>/commit', methods=['POST'])
def commit_project(project_id):
    """Commit changes to a project"""
    try:
        data = request.json
        choices = data.get('choices', [])
        branch_num = data.get('branch_num', '12938')
        custom_commit_message = (data.get('commit_message') or '').strip() or f"fix: java17-migration"

        # Compute all branch-specific values as LOCAL variables.
        # These are captured in the closure below so the background thread always
        # sees the values for THIS request, even if another request arrives and
        # computes different values before the thread actually starts running.
        _feature_branch = _build_feature_branch(branch_num, data.get("branch_prefix","task-"), data.get("branch_suffix","java17-migration"))
        _mr_title = f"TASK-{branch_num}: java migration"
        _jira_id = branch_num
        
        # Generate a unique task ID
        task_id = f"{project_id}_{int(time.time() * 1000)}"
        
        # Mark task as running
        active_tasks[task_id] = {
            'status': 'running',
            'project_id': project_id,
            'operation': 'commit',
            'logs': []
        }
        
        # Update pipeline status to "committing" / queued
        pid_str = str(project_id)
        cancel_event = _queue_register(pid_str)
        update_pipeline_status(project_id, 'queued')
        
        # Start background thread
        def commit_thread():
            _acquired = False
            _commit_succeeded = False
            _message = custom_commit_message  # capture in closure
            try:
                # ── Hard guard: never commit to develop / master / main ───────
                _COMMIT_PROTECTED = {'develop', 'master', 'main'}
                if _feature_branch.lower() in _COMMIT_PROTECTED:
                    active_tasks[task_id]['logs'].append({
                        'level': 'ERROR',
                        'message': (
                            f'🚫 Refusing to commit to protected branch "{_feature_branch}". '
                            f'Commits must always target a feature branch, never develop/master/main. '
                            f'Check your branch_num / branch_prefix / branch_suffix settings.'
                        ),
                        'timestamp': datetime.now().isoformat()
                    })
                    active_tasks[task_id]['status'] = 'failed'
                    update_pipeline_status(project_id, 'commit_failed')
                    print(f"[SECURITY] Blocked commit attempt to protected branch '{_feature_branch}' "
                          f"for project {project_id}")
                    return
                # ─────────────────────────────────────────────────────────────

                # ── Wait for a concurrency slot (or cancellation) ────────────
                update_pipeline_status(project_id, 'queued')
                ok = _queue_acquire(pid_str)
                if not ok:
                    active_tasks[task_id]['logs'].append({
                        'level': 'WARN',
                        'message': 'Migration cancelled while queued',
                        'timestamp': datetime.now().isoformat()
                    })
                    active_tasks[task_id]['status'] = 'cancelled'
                    update_pipeline_status(project_id, 'cancelled')
                    return
                _acquired = True
                update_pipeline_status(project_id, 'committing')
                # ─────────────────────────────────────────────────────────────

                try:
                    project_info = ms.api_call(f"projects/{project_id}")
                    p_name = project_info.get('name', f"ID:{project_id}") if isinstance(project_info, dict) else f"ID:{project_id}"
                    # Full namespace path (e.g. "group/subgroup/repo") needed for correct GitLab web URLs
                    p_path = (project_info.get('path_with_namespace') or p_name) if isinstance(project_info, dict) and not project_info.get('error') else p_name
                    active_tasks[task_id]['logs'].append({
                        'level': 'INFO',
                        'message': f'Starting commit for {p_name}...',
                        'timestamp': datetime.now().isoformat()
                    })
                
                    # ── Branch check / create ────────────────────────────────────────
                    br_check = ms.api_call(f"projects/{project_id}/repository/branches/{urllib.parse.quote(_feature_branch, safe='')}")
                    branch_existed = isinstance(br_check, dict) and "name" in br_check

                    if branch_existed:
                        # Branch already exists — read files directly from it and
                        # only commit what still needs changing.
                        active_tasks[task_id]['logs'].append({
                            'level': 'INFO',
                            'message': f'🔍 Branch "{_feature_branch}" already exists — scanning each file to see what still needs changing…',
                            'timestamp': datetime.now().isoformat()
                        })
                        current_ref = _feature_branch
                    else:
                        # Branch does not exist — create it from source, then read
                        # files from source to build the full action set.
                        active_tasks[task_id]['logs'].append({
                            'level': 'INFO',
                            'message': f'🌿 Branch "{_feature_branch}" not found — creating from {ms.SOURCE_BRANCH}…',
                            'timestamp': datetime.now().isoformat()
                        })
                        with _ms_globals_lock:
                            ms.FEATURE_BRANCH = _feature_branch
                            ms.JIRA_ID = _jira_id
                            ms.create_feature_branch(project_id, p_name)
                        current_ref = ms.SOURCE_BRANCH

                    # ── File inspection ──────────────────────────────────────────────
                    # Read each selected file from current_ref, apply the migration
                    # transform, and add to actions only if the content differs.
                    actions = []
                    files_to_backup = []

                    active_tasks[task_id]['logs'].append({
                        'level': 'INFO',
                        'message': f'📂 Reading files from: {current_ref}',
                        'timestamp': datetime.now().isoformat()
                    })

                    if '1' in choices:  # POM
                        res = ms.api_call(f"projects/{project_id}/repository/files/{urllib.parse.quote('pom.xml', safe='')}?ref={urllib.parse.quote(current_ref, safe='')}")
                        if isinstance(res, dict) and "content" in res:
                            orig = base64.b64decode(res['content']).decode('utf-8')
                            upd = re.sub(r"<(java\.version|jdk\.version|maven\.compiler\.(source|target|release))>(?:1\.\d+|\d+)</\1>", r"<\1>17</\1>", orig)
                            if "<parent>" in upd:
                                upd = re.sub(r"<parent>[\s\S]*?</parent>", ms.update_parent_block, upd)
                            if orig != upd:
                                actions.append({"action": "update", "file_path": "pom.xml", "content": upd})
                                files_to_backup.append('pom.xml')
                                active_tasks[task_id]['logs'].append({'level': 'INFO', 'message': '  pom.xml → needs update ✏️', 'timestamp': datetime.now().isoformat()})
                            else:
                                active_tasks[task_id]['logs'].append({'level': 'INFO', 'message': '  pom.xml → already migrated ✓', 'timestamp': datetime.now().isoformat()})
                        elif isinstance(res, dict) and res.get('error'):
                            active_tasks[task_id]['logs'].append({'level': 'WARN', 'message': f'  pom.xml → could not fetch: {res.get("details","unknown")}', 'timestamp': datetime.now().isoformat()})
                        else:
                            active_tasks[task_id]['logs'].append({'level': 'WARN', 'message': '  pom.xml → not found on this branch (skipping)', 'timestamp': datetime.now().isoformat()})

                    if '2' in choices:  # CI
                        res = ms.api_call(f"projects/{project_id}/repository/files/{urllib.parse.quote('.gitlab-ci.yml', safe='')}?ref={urllib.parse.quote(current_ref, safe='')}")
                        if isinstance(res, dict) and "content" in res:
                            orig = base64.b64decode(res['content']).decode('utf-8')
                            upd = re.sub(r"^\s*image:.*(\n|$)", "", orig, flags=re.MULTILINE)
                            if orig != upd:
                                actions.append({"action": "update", "file_path": ".gitlab-ci.yml", "content": upd})
                                files_to_backup.append('.gitlab-ci.yml')
                                active_tasks[task_id]['logs'].append({'level': 'INFO', 'message': '  .gitlab-ci.yml → needs update ✏️', 'timestamp': datetime.now().isoformat()})
                            else:
                                active_tasks[task_id]['logs'].append({'level': 'INFO', 'message': '  .gitlab-ci.yml → already migrated ✓', 'timestamp': datetime.now().isoformat()})
                        elif isinstance(res, dict) and res.get('error'):
                            active_tasks[task_id]['logs'].append({'level': 'WARN', 'message': f'  .gitlab-ci.yml → could not fetch: {res.get("details","unknown")}', 'timestamp': datetime.now().isoformat()})
                        else:
                            active_tasks[task_id]['logs'].append({'level': 'WARN', 'message': '  .gitlab-ci.yml → not found on this branch (skipping)', 'timestamp': datetime.now().isoformat()})

                    if '3' in choices:  # EB Config
                        path = urllib.parse.quote(".elasticbeanstalk/config.yml", safe='')
                        res = ms.api_call(f"projects/{project_id}/repository/files/{path}?ref={urllib.parse.quote(current_ref, safe='')}")
                        if isinstance(res, dict) and "content" in res:
                            orig = base64.b64decode(res['content']).decode('utf-8')
                            upd = re.sub(r"(default_platform:\s*).*$", f"default_platform: {ms.NEW_DEFAULT_PLATFORM}", orig, flags=re.MULTILINE)
                            if orig != upd:
                                actions.append({"action": "update", "file_path": ".elasticbeanstalk/config.yml", "content": upd})
                                files_to_backup.append('.elasticbeanstalk/config.yml')
                                active_tasks[task_id]['logs'].append({'level': 'INFO', 'message': '  .elasticbeanstalk/config.yml → needs update ✏️', 'timestamp': datetime.now().isoformat()})
                            else:
                                active_tasks[task_id]['logs'].append({'level': 'INFO', 'message': '  .elasticbeanstalk/config.yml → already migrated ✓', 'timestamp': datetime.now().isoformat()})
                        elif isinstance(res, dict) and res.get('error'):
                            active_tasks[task_id]['logs'].append({'level': 'WARN', 'message': f'  .elasticbeanstalk/config.yml → could not fetch: {res.get("details","unknown")}', 'timestamp': datetime.now().isoformat()})
                        else:
                            active_tasks[task_id]['logs'].append({'level': 'WARN', 'message': '  .elasticbeanstalk/config.yml → not found on this branch (skipping)', 'timestamp': datetime.now().isoformat()})

                    # ── Decision after scan ──────────────────────────────────────────
                    if not actions:
                        source_label = (f'"{_feature_branch}"' if branch_existed
                                        else f'"{_feature_branch}" (freshly created from {ms.SOURCE_BRANCH})')
                        active_tasks[task_id]['logs'].append({
                            'level': 'INFO',
                            'message': f'✅ No changes required — {source_label} already has all migration changes. Marking as committed.',
                            'timestamp': datetime.now().isoformat()
                        })
                        active_tasks[task_id]['status'] = 'idempotent'
                        update_pipeline_status(project_id, 'success', workflow_stage='committed',
                                               path_with_namespace=p_path)
                        add_project_to_history(project_id, p_name, 'commit', 'already_done')
                        return

                    # When the branch was freshly created we read from SOURCE_BRANCH,
                    # so run a safety-net check against the new feature branch.
                    # When the branch already existed we read directly from it above,
                    # so check_files_already_match is redundant — skip it.
                    if not branch_existed:
                        all_match, _ = ms.check_files_already_match(project_id, actions, _feature_branch)
                        if all_match:
                            active_tasks[task_id]['logs'].append({
                                'level': 'INFO',
                                'message': f'Changes already present on freshly created {_feature_branch} — marking as committed.',
                                'timestamp': datetime.now().isoformat()
                            })
                            active_tasks[task_id]['status'] = 'idempotent'
                            update_pipeline_status(project_id, 'success', workflow_stage='committed',
                                                   path_with_namespace=p_path)
                            add_project_to_history(project_id, p_name, 'commit', 'already_committed')
                            return

                    active_tasks[task_id]['logs'].append({
                        'level': 'INFO',
                        'message': f'📝 {len(actions)} file(s) need committing — proceeding…',
                        'timestamp': datetime.now().isoformat()
                    })
                    for a in actions:
                        active_tasks[task_id]['logs'].append({
                            'level': 'INFO',
                            'message': f'  → {a["file_path"]}',
                            'timestamp': datetime.now().isoformat()
                        })

                    # Save rollback snapshot before committing
                    try:
                        snapshot = ms.create_rollback_snapshot(project_id, p_name, current_ref, files_to_backup)
                        rollback_data = {
                            'project_id': project_id,
                            'project_name': p_name,
                            'branch': current_ref,
                            'snapshots': [snapshot]
                        }
                        ms.save_rollback_data(rollback_data)
                        active_tasks[task_id]['logs'].append({
                            'level': 'INFO',
                            'message': '📸 Rollback snapshot saved',
                            'timestamp': datetime.now().isoformat()
                        })
                    except Exception as rb_err:
                        print(f"[WARN] Could not save rollback snapshot: {rb_err}")
                        active_tasks[task_id]['logs'].append({
                            'level': 'WARN',
                            'message': f'⚠️ Rollback snapshot could not be saved: {rb_err} — commit will still proceed, but manual rollback will not be available.',
                            'timestamp': datetime.now().isoformat()
                        })
                
                    # Commit changes
                    active_tasks[task_id]['logs'].append({
                        'level': 'INFO',
                        'message': f'Committing {len(actions)} file(s) to {_feature_branch}...',
                        'timestamp': datetime.now().isoformat()
                    })
                
                    commit_payload = {
                        "branch": _feature_branch,
                        "commit_message": _message,
                        "actions": actions
                    }
                
                    commit_resp = ms.api_call(f"projects/{project_id}/repository/commits", "POST", commit_payload)
                
                    if isinstance(commit_resp, dict) and not commit_resp.get("error"):
                        commit_sha = commit_resp.get('id', 'unknown')
                        committer_name = commit_resp.get('committer_name') or commit_resp.get('author_name', 'Unknown')
                        commit_message = commit_resp.get('title') or commit_resp.get('message', 'fix: java17-migration')
                    
                        active_tasks[task_id]['commit_sha'] = commit_sha
                        active_tasks[task_id]['file_count'] = len(actions)
                        active_tasks[task_id]['logs'].append({
                            'level': 'SUCCESS',
                            'message': f'✅ Commit successful! SHA: {commit_sha[:8]}',
                            'timestamp': datetime.now().isoformat()
                        })
                        active_tasks[task_id]['status'] = 'success'
                    
                        # ── SHA-STRICT: Lock status as 'syncing' immediately post-commit ──────
                        update_pipeline_status(project_id, 'syncing', commit_sha=commit_sha,
                                               committer_name=committer_name, commit_message=commit_message,
                                               workflow_stage='committed', path_with_namespace=p_path)
                    
                        active_tasks[task_id]['logs'].append({
                            'level': 'INFO',
                            'message': '🔍 Commit SHA locked. Scanning for migration pipeline (ignoring branch-cut pipeline)...',
                            'timestamp': datetime.now().isoformat()
                        })
                        time.sleep(10)
                    
                        # Get pipeline ID — retry for up to ~5 minutes (15 retries × 20 s)
                        pipeline = None
                        for retry in range(15):
                            pipeline = ms.get_pipeline_for_commit(project_id, commit_sha)
                            if pipeline:
                                break
                            elapsed_s = (retry + 1) * 20
                            active_tasks[task_id]['logs'].append({
                                'level': 'INFO',
                                'message': f'Pipeline not visible yet — retrying... ({elapsed_s}s elapsed)',
                                'timestamp': datetime.now().isoformat()
                            })
                            if retry < 14:
                                time.sleep(20)
                    
                        if pipeline:
                            pipeline_id = pipeline.get('id')
                            pipeline_sha = pipeline.get('sha', '')
                            if not pipeline_sha or pipeline_sha != commit_sha:
                                sha_got = pipeline_sha[:8] if pipeline_sha else '(none)'
                                active_tasks[task_id]['logs'].append({
                                    'level': 'WARN',
                                    'message': (f'⚠️ Pipeline #{pipeline_id} SHA mismatch '
                                                f'(got {sha_got}, expected {commit_sha[:8]}) '
                                                f'— this is the branch-cut pipeline. Skipping...'),
                                    'timestamp': datetime.now().isoformat()
                                })
                                pipeline = None
                            else:
                                active_tasks[task_id]['pipeline_id'] = pipeline_id
                                update_pipeline_status(project_id, 'running', pipeline_id=pipeline_id,
                                                       commit_sha=commit_sha, committer_name=committer_name,
                                                       commit_message=commit_message, path_with_namespace=p_path)
                                active_tasks[task_id]['logs'].append({
                                    'level': 'INFO',
                                    'message': f'✅ SHA verified! Pipeline #{pipeline_id} matched commit {commit_sha[:8]} — monitoring...',
                                    'timestamp': datetime.now().isoformat()
                                })
                    
                        if pipeline:
                            _pid = project_id
                            _pipeline_id = pipeline_id
                            _commit_sha = commit_sha
                            _committer_name = committer_name
                            _commit_message = commit_message
                            _task_id = task_id
                        
                            def monitor_pipeline():
                                # ── Constants ─────────────────────────────────────────────
                                MAX_RETRIES       = 3      # total attempts including the first
                                PER_ATTEMPT_SECS  = 3000  # 50 minutes per attempt before forced cancel+retry
                                CHECK_INTERVAL    = 30    # seconds between API polls
                                HEARTBEAT_EVERY   = 300   # log "still running" every 5 min

                                def _log(level, msg):
                                    if _task_id in active_tasks:
                                        active_tasks[_task_id]['logs'].append({
                                            'level': level,
                                            'message': msg,
                                            'timestamp': datetime.now().isoformat()
                                        })

                                def _is_transient(pl_id):
                                    """Return True if any failed job in pl_id looks like a
                                    transient infrastructure error (docker pull, Maven flake, etc.).
                                    Uses job.failure_reason from the GitLab jobs list — no raw
                                    trace fetch required."""
                                    # GitLab failure_reason values that indicate runner/infra issues
                                    TRANSIENT_REASONS = {
                                        'runner_system_failure',
                                        'stuck_or_timeout_failure',
                                        'scheduler_failure',
                                        'api_failure',
                                        'stale_schedule',
                                        'runner_unsupported',
                                    }
                                    # Job names whose failures are usually transient
                                    # (docker image pull happens during these stages)
                                    TRANSIENT_JOB_NAMES = {
                                        'build', 'compile', 'package', 'test',
                                        'maven-build', 'mvn-build', 'docker-build',
                                    }
                                    try:
                                        jobs = ms.api_call(f"projects/{_pid}/pipelines/{pl_id}/jobs")
                                        if not isinstance(jobs, list):
                                            return False
                                        for job in jobs:
                                            if job.get('status') not in ('failed', 'canceled'):
                                                continue
                                            reason     = (job.get('failure_reason') or '').lower()
                                            job_name   = (job.get('name') or '').lower()
                                            job_stage  = (job.get('stage') or '').lower()

                                            if reason in TRANSIENT_REASONS:
                                                _log('WARN',
                                                     f'🔎 Transient failure detected in job #{job.get("id")} '
                                                     f'"{job.get("name","?")}" — failure_reason: {reason}')
                                                return True

                                            # runner_system_failure sometimes appears as script_failure
                                            # for docker pull; check if a build/compile job failed
                                            if reason == 'script_failure':
                                                for tname in TRANSIENT_JOB_NAMES:
                                                    if tname in job_name or tname in job_stage:
                                                        _log('WARN',
                                                             f'🔎 Build/compile job #{job.get("id")} '
                                                             f'"{job.get("name","?")}" failed (script_failure) — '
                                                             f'treating as potentially transient (docker/maven issue)')
                                                        return True
                                    except Exception as _je:
                                        print(f"[PIPELINE] Could not inspect jobs for pipeline {pl_id}: {_je}")
                                    return False

                                def _cancel_pipeline(pl_id):
                                    """Best-effort cancel — never raises."""
                                    try:
                                        ms.api_call(f"projects/{_pid}/pipelines/{pl_id}/cancel", "POST")
                                        _log('WARN', f'🛑 Pipeline #{pl_id} cancelled (50-min timeout exceeded)')
                                        print(f"[PIPELINE] Cancelled pipeline {pl_id} on project {_pid} due to timeout")
                                    except Exception as _ce:
                                        print(f"[PIPELINE] Could not cancel pipeline {pl_id}: {_ce}")

                                def _retry_pipeline(pl_id, attempt_num):
                                    """Trigger a GitLab retry and return the new pipeline id, or None."""
                                    try:
                                        resp = ms.api_call(
                                            f"projects/{_pid}/pipelines/{pl_id}/retry", "POST"
                                        )
                                        if isinstance(resp, dict) and resp.get('id') and not resp.get('error'):
                                            new_id = resp['id']
                                            _log('INFO',
                                                 f'🔁 Auto-retry #{attempt_num}: new pipeline #{new_id} triggered '
                                                 f'(retrying failed pipeline #{pl_id})')
                                            update_pipeline_status(
                                                _pid, 'running', pipeline_id=new_id,
                                                commit_sha=_commit_sha, committer_name=_committer_name,
                                                commit_message=_commit_message
                                            )
                                            return new_id
                                        else:
                                            err = resp.get('details', str(resp)) if isinstance(resp, dict) else str(resp)
                                            _log('ERROR', f'Could not trigger retry for pipeline #{pl_id}: {err}')
                                    except Exception as _re:
                                        _log('ERROR', f'Retry API call failed for pipeline #{pl_id}: {_re}')
                                    return None

                                def _poll_until_done(pl_id, attempt_num):
                                    """Poll pl_id until it finishes or the 50-min deadline expires.
                                    Returns dict with keys: status (success/failed/canceled/skipped/timeout)"""
                                    deadline      = time.time() + PER_ATTEMPT_SECS
                                    _last_hb      = time.time()
                                    _last_status  = None
                                    attempt_start = time.time()

                                    _log('INFO',
                                         f'🔍 [Attempt {attempt_num}/{MAX_RETRIES}] Monitoring pipeline #{pl_id} '
                                         f'(SHA {_commit_sha[:8]}) — timeout in 50 min...')
                                    update_pipeline_status(
                                        _pid, 'running', pipeline_id=pl_id,
                                        commit_sha=_commit_sha, committer_name=_committer_name,
                                        commit_message=_commit_message
                                    )

                                    while True:
                                        now = time.time()
                                        if now >= deadline:
                                            elapsed = int((now - attempt_start) // 60)
                                            _log('WARN',
                                                 f'⏰ Pipeline #{pl_id} exceeded 50-min limit '
                                                 f'({elapsed}m elapsed) — cancelling...')
                                            _cancel_pipeline(pl_id)
                                            return {'status': 'timeout'}

                                        try:
                                            pl_resp = ms.api_call(f"projects/{_pid}/pipelines/{pl_id}")
                                            if isinstance(pl_resp, dict) and not pl_resp.get('error'):
                                                pl_status = pl_resp.get('status', 'unknown')

                                                if pl_status != _last_status:
                                                    _last_status = pl_status
                                                    _last_hb     = now
                                                    _log('INFO', f'🔄 Pipeline #{pl_id} status → {pl_status}')

                                                elif now - _last_hb >= HEARTBEAT_EVERY:
                                                    elapsed    = int((now - attempt_start) // 60)
                                                    remaining  = max(0, int((deadline - now) // 60))
                                                    _log('INFO',
                                                         f'⏳ Pipeline #{pl_id} still {pl_status} — '
                                                         f'{elapsed}m elapsed, auto-cancel in {remaining}m')
                                                    _last_hb = now

                                                if pl_status in ('success', 'failed', 'canceled', 'skipped'):
                                                    return {'status': pl_status, 'pipeline': pl_resp}
                                            else:
                                                err = pl_resp.get('details', 'unknown') if isinstance(pl_resp, dict) else str(pl_resp)
                                                print(f"[PIPELINE] Poll error for {pl_id}: {err}")

                                        except Exception as _pe:
                                            print(f"[PIPELINE] Poll exception for {_pid}/{pl_id}: {_pe}")

                                        time.sleep(CHECK_INTERVAL)

                                # ── SHA guard (same as before) ─────────────────────────────
                                try:
                                    _verify_pl = ms.api_call(f"projects/{_pid}/pipelines/{_pipeline_id}")
                                    if isinstance(_verify_pl, dict) and not _verify_pl.get('error'):
                                        pl_sha = _verify_pl.get('sha', '')
                                        if not pl_sha or pl_sha != _commit_sha:
                                            sha_got = pl_sha[:8] if pl_sha else '(none)'
                                            print(f"[PIPELINE] SHA mismatch on pipeline {_pipeline_id}: "
                                                  f"expected {_commit_sha[:8]}, got {sha_got} — skipping monitor")
                                            _log('WARN',
                                                 f'⚠️ Pipeline #{_pipeline_id} belongs to a different commit '
                                                 f'(got {sha_got}, expected {_commit_sha[:8]}) — skipping monitor, '
                                                 f'waiting for the migration pipeline...')
                                            return
                                except Exception as _sha_err:
                                    print(f"[PIPELINE] SHA verification error: {_sha_err}")

                                # ── Retry loop ─────────────────────────────────────────────
                                active_pl_id = _pipeline_id

                                for attempt in range(1, MAX_RETRIES + 1):
                                    result = _poll_until_done(active_pl_id, attempt)
                                    final  = result.get('status', 'unknown')

                                    if final == 'success':
                                        _log('SUCCESS',
                                             f'✅ Pipeline #{active_pl_id} completed successfully'
                                             + (f' (after {attempt} attempt(s))' if attempt > 1 else '') + '!')
                                        update_pipeline_status(
                                            _pid, 'success', pipeline_id=active_pl_id,
                                            commit_sha=_commit_sha, committer_name=_committer_name,
                                            commit_message=_commit_message, workflow_stage='pipeline_success'
                                        )
                                        return  # ← all done

                                    # timeout OR failure — decide whether to retry
                                    if attempt < MAX_RETRIES:
                                        if final == 'timeout':
                                            reason = 'exceeded 50-min timeout'
                                            should_retry = True
                                        elif final == 'failed':
                                            is_transient = _is_transient(active_pl_id)
                                            reason = 'transient infrastructure error detected' if is_transient else 'non-transient failure'
                                            should_retry = is_transient
                                        else:
                                            reason = f'status={final}'
                                            should_retry = False

                                        if should_retry:
                                            _log('WARN',
                                                 f'🔁 Pipeline #{active_pl_id} {reason}. '
                                                 f'Auto-retrying ({attempt}/{MAX_RETRIES - 1} retries used)...')
                                            new_id = _retry_pipeline(active_pl_id, attempt + 1)
                                            if new_id:
                                                active_pl_id = new_id
                                                time.sleep(10)  # brief pause before polling new pipeline
                                                continue
                                            else:
                                                _log('ERROR', f'Retry could not be triggered — giving up.')
                                        else:
                                            _log('ERROR',
                                                 f'❌ Pipeline #{active_pl_id} {reason} — no auto-retry '
                                                 f'for this failure type. Check GitLab for details.')

                                    # Reached here: either max retries exhausted, non-retryable, or retry failed
                                    if final == 'timeout':
                                        _log('ERROR',
                                             f'⏰ Pipeline #{active_pl_id} timed out after all {attempt} attempt(s). '
                                             f'The last pipeline was cancelled. Check GitLab for details.')
                                    elif final != 'success':
                                        suffix = f' after {attempt} attempt(s)' if attempt > 1 else ''
                                        _log('ERROR',
                                             f'❌ Pipeline #{active_pl_id} ended with status: {final}{suffix}')

                                    update_pipeline_status(
                                        _pid, 'failed', pipeline_id=active_pl_id,
                                        commit_sha=_commit_sha, committer_name=_committer_name,
                                        commit_message=_commit_message, workflow_stage='committed'
                                    )
                                    return
                        
                            threading.Thread(target=monitor_pipeline, daemon=True).start()
                        else:
                            active_tasks[task_id]['logs'].append({
                                'level': 'WARN',
                                'message': '⚠️ Pipeline not found after 5 min — starting background discovery. '
                                           'The pipeline may still appear once GitLab\'s runner picks up the job.',
                                'timestamp': datetime.now().isoformat()
                            })
                            _late_pid = project_id
                            _late_sha = commit_sha
                            _late_task_id = task_id
                            _late_committer = committer_name
                            _late_message = commit_message
                            _late_path = p_path

                            def late_pipeline_discovery():
                                """Keep looking for the pipeline for up to 30 extra minutes."""
                                found_pipeline = None
                                for attempt in range(30):
                                    time.sleep(60)
                                    candidate = ms.get_pipeline_for_commit(_late_pid, _late_sha)
                                    if candidate:
                                        if candidate.get('sha', '') == _late_sha:
                                            found_pipeline = candidate
                                            break
                                        else:
                                            print(f"[PIPELINE] Late-discovery: candidate pipeline {candidate.get('id')} "
                                                  f"SHA {candidate.get('sha','?')[:8]} != expected {_late_sha[:8]} — skipping")
                                    print(f"[PIPELINE] Late-discovery attempt {attempt+1}/30 for {_late_pid} SHA {_late_sha[:8]}")

                                if not found_pipeline:
                                    print(f"[PIPELINE] Late-discovery gave up for project {_late_pid}")
                                    if _late_task_id in active_tasks:
                                        active_tasks[_late_task_id]['logs'].append({
                                            'level': 'ERROR',
                                            'message': '❌ Could not locate pipeline after 35 min — please check GitLab directly.',
                                            'timestamp': datetime.now().isoformat()
                                        })
                                    return

                                late_pid_id = found_pipeline.get('id')
                                print(f"[PIPELINE] Late-discovery found pipeline #{late_pid_id} for project {_late_pid}")
                                if _late_task_id in active_tasks:
                                    active_tasks[_late_task_id]['pipeline_id'] = late_pid_id
                                    active_tasks[_late_task_id]['logs'].append({
                                        'level': 'INFO',
                                        'message': f'🔍 Pipeline #{late_pid_id} found — now monitoring...',
                                        'timestamp': datetime.now().isoformat()
                                    })
                                update_pipeline_status(_late_pid, 'running', pipeline_id=late_pid_id,
                                                       commit_sha=_late_sha, committer_name=_late_committer,
                                                       commit_message=_late_message, path_with_namespace=_late_path)

                                try:
                                    result = ms.wait_for_pipeline_completion(_late_pid, late_pid_id,
                                                                              timeout=7200, check_interval=30)
                                except Exception as exc:
                                    result = {'status': 'timeout'}

                                final = result.get('status', 'unknown')
                                if final == 'success':
                                    update_pipeline_status(_late_pid, 'success', pipeline_id=late_pid_id,
                                                           commit_sha=_late_sha, workflow_stage='pipeline_success',
                                                           path_with_namespace=_late_path)
                                    if _late_task_id in active_tasks:
                                        active_tasks[_late_task_id]['logs'].append({
                                            'level': 'SUCCESS',
                                            'message': f'✅ Pipeline #{late_pid_id} completed successfully!',
                                            'timestamp': datetime.now().isoformat()
                                        })
                                else:
                                    update_pipeline_status(_late_pid, 'failed', pipeline_id=late_pid_id,
                                                           commit_sha=_late_sha, workflow_stage='committed',
                                                           path_with_namespace=_late_path)
                                    if _late_task_id in active_tasks:
                                        active_tasks[_late_task_id]['logs'].append({
                                            'level': 'ERROR',
                                            'message': f'❌ Pipeline #{late_pid_id} ended with status: {final}',
                                            'timestamp': datetime.now().isoformat()
                                        })

                            threading.Thread(target=late_pipeline_discovery, daemon=True).start()
                    
                        add_project_to_history(project_id, p_name, 'commit', 'success', {
                            'commit_sha': commit_sha,
                            'file_count': len(actions)
                        })
                    
                        # ── Per-run log files ────────────────────────────────────
                        save_run_migration_log(project_id, p_name, active_tasks[task_id]['logs'])
                        try:
                            snapshot = ms.create_rollback_snapshot(project_id, p_name, current_ref, files_to_backup)
                            rb_data = {
                                'created_at': datetime.now().isoformat(),
                                'commit_sha': commit_sha,
                                'project_id': project_id,
                                'project_name': p_name,
                                'branch': current_ref,
                                'snapshots': [snapshot]
                            }
                            save_run_rollback_log(project_id, p_name, rb_data)
                        except Exception as rb_err:
                            print(f"[WARN] Could not save per-run rollback copy: {rb_err}")
                        save_run_state_log(project_id, p_name, {
                            'operation': 'commit',
                            'status': 'success',
                            'commit_sha': commit_sha,
                            'files_changed': [a['file_path'] for a in actions],
                            'pipeline_id': active_tasks[task_id].get('pipeline_id'),
                            'workflow_stage': 'committed'
                        })
                        # ────────────────────────────────────────────────────────
                        _commit_succeeded = True
                    else:
                        error_msg = commit_resp.get('details', 'Unknown error')
                        active_tasks[task_id]['logs'].append({
                            'level': 'ERROR',
                            'message': f'Commit failed: {error_msg}',
                            'timestamp': datetime.now().isoformat()
                        })
                        active_tasks[task_id]['status'] = 'failed'
                        update_pipeline_status(project_id, 'commit_failed')
                        add_project_to_history(project_id, p_name, 'commit', 'failed', {'error': error_msg})
                    
                except Exception as e:
                    print(f"[ERROR] Commit thread error: {e}")
                    active_tasks[task_id]['logs'].append({
                        'level': 'ERROR',
                        'message': f'Exception: {str(e)}',
                        'timestamp': datetime.now().isoformat()
                    })
                    active_tasks[task_id]['status'] = 'failed'
                    update_pipeline_status(project_id, 'commit_failed')
            finally:
                if _acquired:
                    _queue_release(pid_str, succeeded=_commit_succeeded)
                _cleanup_active_tasks()
        
        threading.Thread(target=commit_thread, daemon=True).start()
        
        return jsonify({
            'success': True,
            'task_id': task_id,
            'message': 'Commit started'
        })
        
    except Exception as e:
        print(f"[ERROR] Commit endpoint error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/tasks/<task_id>', methods=['GET'])
def get_task_status(task_id):
    """Get status of a background task"""
    if task_id in active_tasks:
        task = active_tasks[task_id]
        return jsonify({
            'success': True,
            'task': {
                'status':        task['status'],
                'stage':         task.get('stage'),
                'deploy_stage':  task.get('deploy_stage'),   # tag_create | build | terminate | deploy
                'logs':          task['logs'],
                'commit_sha':    task.get('commit_sha'),
                'file_count':    task.get('file_count'),
                'pipeline_id':   task.get('pipeline_id'),
                'project_name':  task.get('project_name'),
                'operation':     task.get('operation'),
                'total':         task.get('total'),
                'done':          task.get('done'),
                'failed':        task.get('failed'),
            },
            'status':       task['status'],
            'stage':        task.get('stage'),
            'deploy_stage': task.get('deploy_stage'),
            'logs':         task['logs'],
            'commit_sha':   task.get('commit_sha'),
            'file_count':   task.get('file_count'),
            'pipeline_id':  task.get('pipeline_id'),
            'total':        task.get('total'),
            'done':         task.get('done'),
            'failed':       task.get('failed'),
        })
    else:
        return jsonify({'success': False, 'error': 'Task not found'}), 404


@app.route('/api/projects/<int:project_id>/mr', methods=['POST'])
def create_mr(project_id):
    """Create merge request for a project"""
    try:
        data = request.json
        branch_num = data.get('branch_num', '12938')

        # Compute locally — do not mutate ms globals in the request handler itself
        _feature_branch = _build_feature_branch(branch_num, data.get("branch_prefix","task-"), data.get("branch_suffix","java17-migration"))
        _mr_title = f"TASK-{branch_num}: java migration"
        _jira_id = branch_num

        task_id = f"{project_id}_mr_{int(time.time() * 1000)}"
        active_tasks[task_id] = {
            'status': 'running',
            'project_id': project_id,
            'operation': 'mr',
            'logs': []
        }

        def mr_thread():
            try:
                project_info = ms.api_call(f"projects/{project_id}")
                p_name = project_info.get('name', f"ID:{project_id}") if isinstance(project_info, dict) else f"ID:{project_id}"

                active_tasks[task_id]['logs'].append({
                    'level': 'INFO',
                    'message': f'Creating MR for {p_name}...',
                    'timestamp': datetime.now().isoformat()
                })

                # ms.create_mr_for_project reads ms.FEATURE_BRANCH, ms.SOURCE_BRANCH,
                # ms.MR_TITLE, ms.REVIEWER_USERNAMES, ms.ASSIGNEE_USERNAMES throughout its
                # body — hold the lock for the entire call so another concurrent request
                # cannot overwrite these globals mid-execution.
                with _ms_globals_lock:
                    ms.FEATURE_BRANCH = _feature_branch
                    ms.JIRA_ID = _jira_id
                    ms.MR_TITLE = _mr_title
                    result = ms.create_mr_for_project(project_id, p_name, {'snapshots': []})
                
                if result['success']:
                    active_tasks[task_id]['logs'].append({
                        'level': 'SUCCESS',
                        'message': f'✅ MR created successfully',
                        'timestamp': datetime.now().isoformat()
                    })
                    active_tasks[task_id]['status'] = 'success'
                    active_tasks[task_id]['mr_url'] = result.get('url')
                    update_pipeline_status(project_id, 'success', workflow_stage='mr_raised')
                    add_project_to_history(project_id, p_name, 'mr', 'success', {'url': result.get('url')})
                else:
                    active_tasks[task_id]['logs'].append({
                        'level': 'ERROR',
                        'message': f'❌ MR creation failed',
                        'timestamp': datetime.now().isoformat()
                    })
                    active_tasks[task_id]['status'] = 'failed'
                    add_project_to_history(project_id, p_name, 'mr', 'failed')
            except Exception as e:
                print(f"[ERROR] MR thread error: {e}")
                active_tasks[task_id]['logs'].append({
                    'level': 'ERROR',
                    'message': f'Exception: {str(e)}',
                    'timestamp': datetime.now().isoformat()
                })
                active_tasks[task_id]['status'] = 'failed'
            finally:
                _cleanup_active_tasks()
        
        threading.Thread(target=mr_thread, daemon=True).start()
        
        return jsonify({'success': True, 'task_id': task_id})
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<int:project_id>/check-develop', methods=['GET'])
def check_develop(project_id):
    """Check whether develop/master/main branch already has Java 17 in pom.xml
    AND the expected platform version in .elasticbeanstalk/config.yml."""
    try:
        source_branch = _resolve_source_branch(project_id)

        pom_res = ms.api_call(
            f"projects/{project_id}/repository/files/{urllib.parse.quote('pom.xml', safe='')}?ref={urllib.parse.quote(source_branch, safe='')}"
        )
        if not isinstance(pom_res, dict) or 'content' not in pom_res:
            return jsonify({'status': 'unknown', 'detail': f'pom.xml not found on {source_branch}', 'branch': source_branch})

        if pom_res.get('error'):
            return jsonify({'status': 'unknown', 'detail': pom_res.get('details', 'API error'), 'branch': source_branch})

        pom_content = base64.b64decode(pom_res['content']).decode('utf-8', errors='replace')

        _JAVA_TAGS = [
            r'java\.version',
            r'jdk\.version',
            r'maven\.compiler\.source',
            r'maven\.compiler\.target',
            r'maven\.compiler\.release',
        ]

        def _normalize_java_ver(ver_str):
            """Normalise 1.8 → 8, 1.11 → 11, 17 → 17 etc. Returns int or None."""
            ver_str = ver_str.strip()
            m = re.match(r'^1\.(\d+)$', ver_str)   # legacy 1.x style
            if m:
                return int(m.group(1))
            m = re.match(r'^(\d+)$', ver_str)        # plain integer
            if m:
                return int(m.group(1))
            return None

        def _find_java_ver(text, exact=None):
            """Return (re.Match, raw_ver_str, normalised_int) for the first matching tag.
            Handles plain integers (8, 11, 17) and legacy 1.x notation (1.8, 1.11)."""
            # Pattern captures both 1.x style and plain integers
            ver_pat = r'(1\.\d+|\d+)'
            for tag in _JAVA_TAGS:
                m = re.search(rf'<{tag}>\s*{ver_pat}\s*</{tag}>', text)
                if m:
                    raw = m.group(1)
                    norm = _normalize_java_ver(raw)
                    if exact is not None:
                        if norm == exact:
                            return m, raw, norm
                    else:
                        return m, raw, norm
            return None, None, None

        match, raw_ver, norm_ver = _find_java_ver(pom_content, exact=17)
        has_java17 = match is not None

        if not has_java17:
            match, raw_ver, norm_ver = _find_java_ver(pom_content)
            if match is not None:
                display = f'{raw_ver} (Java {norm_ver})' if norm_ver and str(norm_ver) != raw_ver.strip() else raw_ver
                return jsonify({
                    'status': 'needs_migration',
                    'detail': f'Found Java {display} on {source_branch} — migration needed',
                    'branch': source_branch
                })
            loose_pat = r'<(?:java\.version|jdk\.version|maven\.compiler\.(?:source|target|release))>'
            if re.search(loose_pat, pom_content, re.IGNORECASE):
                return jsonify({'status': 'needs_migration', 'detail': f'Java version property found on {source_branch} (version unclear — check manually)', 'branch': source_branch})
            return jsonify({'status': 'unknown', 'detail': f'No Java version property found in {source_branch} pom.xml (checked java.version, jdk.version and maven.compiler.source/target/release)', 'branch': source_branch})

        # Java 17 is present — now also verify the platform version in config.yml
        target_platform = (ms.NEW_DEFAULT_PLATFORM or '').strip()
        platform_ok   = False
        platform_found = None

        if target_platform:
            cfg_path = urllib.parse.quote('.elasticbeanstalk/config.yml', safe='')
            cfg_res  = ms.api_call(
                f"projects/{project_id}/repository/files/{cfg_path}?ref={urllib.parse.quote(source_branch, safe='')}"
            )
            if isinstance(cfg_res, dict) and 'content' in cfg_res and not cfg_res.get('error'):
                cfg_content = base64.b64decode(cfg_res['content']).decode('utf-8', errors='replace')
                m_plat = re.search(r'default_platform:\s*(.+)', cfg_content)
                if m_plat:
                    platform_found = m_plat.group(1).strip()
                    platform_ok = (platform_found == target_platform)

        if target_platform and not platform_ok:
            found_label = platform_found if platform_found else 'not found'
            return jsonify({
                'status': 'needs_migration',
                'detail': (
                    f'Java 17 ✓ on {source_branch} but platform version mismatch — '
                    f'found: "{found_label}", expected: "{target_platform}"'
                ),
                'branch': source_branch,
                'java_ok': True,
                'platform_mismatch': True,
                'platform_found': platform_found,
                'platform_expected': target_platform,
            })

        detail = f'Java 17 ✓ and platform "{target_platform}" ✓ on {source_branch}' if target_platform else f'Java 17 already present in {source_branch} pom.xml'
        return jsonify({'status': 'done', 'detail': detail, 'branch': source_branch})

    except Exception as e:
        return jsonify({'status': 'unknown', 'detail': str(e)}), 500


@app.route('/api/projects/<int:project_id>/branch-status', methods=['GET'])
def get_branch_status(project_id):
    """Check whether the feature branch still exists, and if MR is merged"""
    try:
        branch_num    = request.args.get('branch_num', '12938')
        branch_prefix = request.args.get('branch_prefix', 'task-')
        branch_suffix = request.args.get('branch_suffix', 'java17-migration')
        feature_branch = _build_feature_branch(branch_num, branch_prefix, branch_suffix)
        
        branch_resp = ms.api_call(f"projects/{project_id}/repository/branches/{urllib.parse.quote(feature_branch, safe='')}")
        branch_exists = isinstance(branch_resp, dict) and 'name' in branch_resp and not branch_resp.get('error')
        
        mr_resp = ms.api_call(f"projects/{project_id}/merge_requests?source_branch={urllib.parse.quote(feature_branch, safe='')}&state=opened&per_page=1")
        open_mrs = mr_resp if isinstance(mr_resp, list) else []
        
        merged_resp = ms.api_call(f"projects/{project_id}/merge_requests?source_branch={urllib.parse.quote(feature_branch, safe='')}&state=merged&per_page=1")
        merged_mrs = merged_resp if isinstance(merged_resp, list) else []
        
        existing = pipeline_status.get(str(project_id), {})
        current_stage = existing.get('workflow_stage', 'idle')
        
        if not branch_exists and current_stage in ('mr_raised', 'pipeline_success', 'deployed', 'committed'):
            if merged_mrs:
                update_pipeline_status(project_id, 'success', workflow_stage='merged')
                current_stage = 'merged'
            else:
                pipeline_status.pop(str(project_id), None)
                save_pipeline_state()
                print(f"[PIPELINE] Project {project_id} branch deleted — removed from status tracking")
                current_stage = 'removed'
        
        return jsonify({
            'success': True,
            'branch_exists': branch_exists,
            'branch_name': feature_branch,
            'open_mrs': len(open_mrs),
            'merged_mrs': len(merged_mrs),
            'workflow_stage': current_stage,
            'mr_url': open_mrs[0].get('web_url') if open_mrs else (merged_mrs[0].get('web_url') if merged_mrs else None)
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<int:project_id>/workflow-stage', methods=['POST'])
def set_workflow_stage(project_id):
    """Manually advance or reset the workflow stage for a project"""
    try:
        data = request.json
        stage = data.get('stage')
        valid_stages = ['idle', 'committed', 'pipeline_success', 'deployed', 'mr_raised', 'merged']
        if stage not in valid_stages:
            return jsonify({'success': False, 'error': f'Invalid stage. Must be one of: {valid_stages}'}), 400
        
        update_pipeline_status(project_id, pipeline_status.get(str(project_id), {}).get('status', 'unknown'), workflow_stage=stage)
        return jsonify({'success': True, 'stage': stage})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<int:project_id>/status', methods=['DELETE'])
def remove_project_status(project_id):
    """Remove a project's entry from pipeline status tracking"""
    try:
        removed = pipeline_status.pop(str(project_id), None)
        if removed is not None:
            save_pipeline_state()
            print(f"[PIPELINE] Removed status entry for project {project_id}")
            return jsonify({'success': True, 'removed': True})
        return jsonify({'success': True, 'removed': False})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ── Queue / Circuit-Breaker endpoints ─────────────────────────────────────────

@app.route('/api/queue-status', methods=['GET'])
def get_queue_status():
    """Return current queue state for all tracked projects."""
    with _queue_lock:
        snapshot = {
            pid: {
                'position': info['position'],
                'status': info['status'],
            }
            for pid, info in _queue_state.items()
        }
    running = sum(1 for v in snapshot.values() if v['status'] == 'running')
    queued  = sum(1 for v in snapshot.values() if v['status'] == 'queued')
    return jsonify({
        'projects': snapshot,
        'running': running,
        'queued': queued,
        'max_concurrent': MAX_CONCURRENT,
        'circuit_breaker': {
            'paused': _cb_state['paused'],
            'consecutive_failures': _cb_state['consecutive_failures'],
            'threshold': _cb_state['THRESHOLD'],
        }
    })


@app.route('/api/queue/stop-all', methods=['POST'])
def queue_stop_all():
    """Cancel all QUEUED (not yet running) projects."""
    cancelled = []
    with _queue_lock:
        for pid, info in _queue_state.items():
            if info['status'] == 'queued':
                info['cancel_event'].set()
                cancelled.append(pid)
    return jsonify({'success': True, 'cancelled': cancelled, 'count': len(cancelled)})


@app.route('/api/circuit-breaker/resume', methods=['POST'])
def circuit_breaker_resume():
    """Manually resume a paused circuit breaker."""
    _cb_state['paused'] = False
    _cb_state['consecutive_failures'] = 0
    _cb_state['resume_event'].set()
    return jsonify({'success': True, 'message': 'Circuit breaker resumed'})

# ──────────────────────────────────────────────────────────────────────────────


@app.route('/api/projects/<int:project_id>/stop', methods=['POST'])
def stop_project_migration(project_id):
    """Send a stop signal to an active migration task for a project."""
    try:
        pid_str = str(project_id)

        with _queue_lock:
            if pid_str in _queue_state and _queue_state[pid_str]['status'] == 'queued':
                _queue_state[pid_str]['cancel_event'].set()

        matching_task_id = None
        for tid, task in active_tasks.items():
            if task.get('project_id') == project_id and task.get('status') == 'running':
                matching_task_id = tid
                break

        if pid_str not in project_interrupt_flags:
            project_interrupt_flags[pid_str] = threading.Event()
        project_interrupt_flags[pid_str].set()

        # NOTE: We intentionally do NOT set the global ms.INTERRUPTED flag here.
        # That flag is process-wide and would abort pipeline monitoring threads for
        # ALL concurrently running projects, not just this one.
        # Per-project cancellation is handled by project_interrupt_flags and the
        # cancel_event stored in _queue_state.

        current_status = pipeline_status.get(pid_str, {})
        state_snapshot = {
            'project_id': project_id,
            'status': current_status.get('status', 'unknown'),
            'workflow_stage': current_status.get('workflow_stage', 'idle'),
            'commit_sha': current_status.get('commit_sha'),
            'pipeline_id': current_status.get('pipeline_id'),
            'stopped_by': 'user_request',
        }
        try:
            ms.save_state(state_snapshot)
        except Exception as save_err:
            print(f"[WARN] Could not invoke ms.save_state: {save_err}")

        update_pipeline_status(
            project_id,
            'failed',
            workflow_stage=current_status.get('workflow_stage', 'idle'),
            pipeline_id=current_status.get('pipeline_id'),
            commit_sha=current_status.get('commit_sha'),
            committer_name=current_status.get('committer_name'),
            commit_message=current_status.get('commit_message'),
            path_with_namespace=current_status.get('path_with_namespace'),
        )
        save_pipeline_state()

        if matching_task_id:
            active_tasks[matching_task_id]['status'] = 'stopped'

        print(f"[STOP] Stop signal sent for project {project_id}. State saved.")
        return jsonify({
            'success': True,
            'message': f'Stop signal sent for project {project_id}. Migration state saved to file.',
            'task_id': matching_task_id,
        })
    except Exception as e:
        print(f"[ERROR] Stop endpoint error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ═══════════════════════════════════════════════════════════════════════════════
# NEW ENDPOINT: Certificate Replace
# POST /api/projects/<id>/cert-replace
# Body: { cert_base64, commit_message?, branch_num?, branch_prefix?, branch_suffix?, create_mr? }
#
# Logic:
#   1. File path is always hardcoded to: src/main/resources/presto.jks
#   2. Resolve the feature branch; abort (with current pipeline status) if it does not exist
#      — commits are NEVER made to develop / master / main.
#   3. Verify presto.jks already exists on the feature branch — abort if missing.
#   4. Compare byte-for-byte (via base64) with the uploaded cert.
#   5. If identical → return {changed: false}, skip commit and MR entirely.
#   6. If different → commit the new cert to the feature branch.
#   7. If create_mr=true → open an MR targeting the source branch.
# ═══════════════════════════════════════════════════════════════════════════════
@app.route('/api/projects/<int:project_id>/cert-replace', methods=['POST'])
def cert_replace(project_id):
    """Replace a JKS certificate in the repo — only commits/raises MR when cert differs.

    Pre-conditions enforced:
      * The cert is always read from / written to: src/main/resources/presto.jks (hardcoded).
      * The feature branch must already exist; commits are never made to develop/master/main.
      * presto.jks must already be present on that branch before it can be replaced.

    Required body fields:
      cert_base64   – base64-encoded bytes of the new JKS file

    Optional body fields:
      commit_message  – custom commit message
      branch_num      – branch ticket number (default: '12938')
      branch_prefix   – branch name prefix  (default: 'task-')
      branch_suffix   – branch name suffix  (default: 'java17-migration')
      create_mr       – bool, whether to open an MR after commit (default: false)

    Note: target_path and target_filename are ignored — the path is always
          src/main/resources/presto.jks.
    """
    try:
        data = request.json or {}

        # ── Input validation ──────────────────────────────────────────────────
        cert_base64 = (data.get('cert_base64') or '').strip()
        if not cert_base64:
            return jsonify({'success': False, 'error': 'cert_base64 is required'}), 400

        try:
            new_cert_bytes = base64.b64decode(cert_base64)
        except Exception:
            return jsonify({'success': False, 'error': 'cert_base64 is not valid base64'}), 400

        if len(new_cert_bytes) == 0:
            return jsonify({'success': False, 'error': 'cert_base64 decodes to zero bytes — aborting'}), 400

        # Path and filename are hardcoded — the cert must always live at this location.
        target_filename   = 'presto.jks'
        target_path       = 'src/main/resources'
        file_path_in_repo = f"{target_path}/{target_filename}"   # src/main/resources/presto.jks

        commit_message  = (data.get('commit_message') or f'chore: update {target_filename}').strip()
        branch_num      = data.get('branch_num', '12938')
        branch_prefix   = data.get('branch_prefix', 'task-')
        branch_suffix   = data.get('branch_suffix', 'java17-migration')
        create_mr_flag  = bool(data.get('create_mr', False))

        # ── Resolve target branch ─────────────────────────────────────────────
        _PROTECTED_BRANCHES = {'develop', 'master', 'main'}
        feature_branch = _build_feature_branch(branch_num, branch_prefix, branch_suffix)
        br_check = ms.api_call(
            f"projects/{project_id}/repository/branches/{urllib.parse.quote(feature_branch, safe='')}"
        )
        if isinstance(br_check, dict) and br_check.get('name') and not br_check.get('error'):
            target_branch = feature_branch
            on_feature_branch = True
        else:
            # Feature branch does not exist — refuse to fall back to a protected branch.
            # Return the current pipeline status so the UI can still display it.
            current_pipeline = pipeline_status.get(str(project_id), {})
            return jsonify({
                'success': False,
                'error': (
                    f"Feature branch '{feature_branch}' does not exist in project {project_id}. "
                    f"The cert commit must be made on a feature branch and never on a "
                    f"protected branch (develop / master / main). "
                    f"Please create the feature branch first and retry."
                ),
                'pipeline_status': current_pipeline,
            }), 400

        # Extra safety net: never commit to a protected branch regardless of how
        # target_branch was resolved (e.g. if a protected branch name was inferred).
        if target_branch.lower() in _PROTECTED_BRANCHES:
            current_pipeline = pipeline_status.get(str(project_id), {})
            return jsonify({
                'success': False,
                'error': (
                    f"Refusing to commit cert to protected branch '{target_branch}'. "
                    f"Cert commits must always target a feature branch."
                ),
                'pipeline_status': current_pipeline,
            }), 400

        print(f"[CERT-REPLACE] project={project_id} file={file_path_in_repo} branch={target_branch}")

        # ── Fetch existing cert from repo ─────────────────────────────────────
        encoded_file_path = urllib.parse.quote(file_path_in_repo, safe='')
        existing_resp = ms.api_call(
            f"projects/{project_id}/repository/files/{encoded_file_path}"
            f"?ref={urllib.parse.quote(target_branch, safe='')}"
        )

        existing_cert_b64_raw = None
        file_exists_in_repo = False
        if isinstance(existing_resp, dict) and 'content' in existing_resp and not existing_resp.get('error'):
            # GitLab returns base64 content (possibly with newlines every 60 chars)
            existing_cert_b64_raw = existing_resp['content']
            file_exists_in_repo = True

        # ── Guard: presto.jks must already exist before we commit a replacement ─
        if not file_exists_in_repo:
            return jsonify({
                'success': False,
                'error': (
                    f"'{file_path_in_repo}' does not exist on branch '{target_branch}' "
                    f"in project {project_id}. "
                    f"The cert file (presto.jks) must already be present in the repository "
                    f"before it can be replaced. Please verify the target_path and ensure "
                    f"the file has been committed at least once."
                ),
            }), 400

        # ── Normalise both sides for comparison ───────────────────────────────
        # Strip all whitespace so line-wrapped base64 doesn't cause false mismatches
        def _normalise_b64(s: str) -> str:
            return re.sub(r'\s+', '', s)

        new_b64_norm      = _normalise_b64(cert_base64)
        existing_b64_norm = _normalise_b64(existing_cert_b64_raw) if existing_cert_b64_raw else None

        if existing_b64_norm and existing_b64_norm == new_b64_norm:
            print(f"[CERT-REPLACE] Cert is identical to existing — skipping commit")
            return jsonify({
                'success': True,
                'changed': False,
                'message': (
                    f'{target_filename} at {file_path_in_repo} on branch "{target_branch}" '
                    f'is byte-for-byte identical to the uploaded certificate. '
                    f'No commit or MR was created.'
                ),
                'branch': target_branch,
                'file_path': file_path_in_repo,
            })

        # ── Commit the new cert ───────────────────────────────────────────────
        # At this point file_exists_in_repo is always True (guarded above).
        file_action = 'update'
        commit_payload = {
            'branch': target_branch,
            'commit_message': commit_message,
            'actions': [{
                'action': file_action,
                'file_path': file_path_in_repo,
                'content': new_b64_norm,     # normalised (no newlines) base64
                'encoding': 'base64',
            }]
        }

        commit_resp = ms.api_call(
            f"projects/{project_id}/repository/commits", 'POST', commit_payload
        )

        if not (isinstance(commit_resp, dict) and not commit_resp.get('error')):
            err = commit_resp.get('details', 'Unknown error') if isinstance(commit_resp, dict) else str(commit_resp)
            print(f"[CERT-REPLACE] Commit failed: {err}")
            return jsonify({'success': False, 'error': f'Commit failed: {err}'}), 500

        commit_sha      = commit_resp.get('id', '')
        committer_name  = commit_resp.get('committer_name') or commit_resp.get('author_name', 'Unknown')
        print(f"[CERT-REPLACE] Committed {file_path_in_repo} → SHA {commit_sha[:8]} on {target_branch}")

        # ── Project info for history ──────────────────────────────────────────
        project_info = ms.api_call(f"projects/{project_id}")
        p_name = (
            project_info.get('name', f'ID:{project_id}')
            if isinstance(project_info, dict) and not project_info.get('error')
            else f'ID:{project_id}'
        )

        add_project_to_history(project_id, p_name, 'cert_replace', 'success', {
            'file': file_path_in_repo,
            'branch': target_branch,
            'commit_sha': commit_sha[:8] if commit_sha else '',
            'action': file_action,
            'cert_bytes': len(new_cert_bytes),
        })

        # ── Optionally create MR ──────────────────────────────────────────────
        mr_url   = None
        mr_iid   = None
        mr_error = None

        if on_feature_branch and create_mr_flag:
            source_branch = _resolve_source_branch(project_id)
            mr_resp = ms.api_call(
                f"projects/{project_id}/merge_requests", 'POST', {
                    'source_branch': target_branch,
                    'target_branch': source_branch,
                    'title': commit_message,
                    'remove_source_branch': False,
                }
            )
            if isinstance(mr_resp, dict) and not mr_resp.get('error'):
                mr_url  = mr_resp.get('web_url')
                mr_iid  = mr_resp.get('iid')
                print(f"[CERT-REPLACE] MR !{mr_iid} created: {mr_url}")
                update_pipeline_status(project_id, 'success', workflow_stage='mr_raised',
                                       commit_sha=commit_sha, committer_name=committer_name,
                                       commit_message=commit_message)
            else:
                mr_error = mr_resp.get('details', 'MR creation failed') if isinstance(mr_resp, dict) else str(mr_resp)
                print(f"[CERT-REPLACE] MR creation failed: {mr_error}")
        elif on_feature_branch:
            # Cert committed to feature branch but no MR requested — update stage to committed
            update_pipeline_status(project_id, 'syncing', commit_sha=commit_sha,
                                   committer_name=committer_name, commit_message=commit_message,
                                   workflow_stage='committed')

        return jsonify({
            'success': True,
            'changed': True,
            'message': (
                f'{target_filename} updated on branch "{target_branch}" '
                f'({"created" if file_action == "create" else "replaced"}).'
            ),
            'commit_sha': commit_sha,
            'short_sha': commit_sha[:8] if commit_sha else '',
            'branch': target_branch,
            'file_path': file_path_in_repo,
            'action': file_action,
            'cert_bytes': len(new_cert_bytes),
            'mr_url': mr_url,
            'mr_iid': mr_iid,
            'mr_error': mr_error,   # None when MR was not requested or succeeded
        })

    except Exception as e:
        print(f"[ERROR] cert-replace error for project {project_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


def save_run_state_log(project_id, p_name, run_info):
    """Save per-run state snapshot to state_logs/ folder"""
    try:
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        safe_name = re.sub(r'[^\w\-]', '_', p_name)
        filename = f"state_{safe_name}_{ts}.json"
        filepath = os.path.join('state_logs', filename)
        data = {
            'project_id': project_id,
            'project_name': p_name,
            'timestamp': datetime.now().isoformat(),
            **run_info
        }
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
        print(f"[STATE] Saved run state log: {filepath}")
        return filepath
    except Exception as e:
        print(f"[WARN] Could not save run state log: {e}")
        return None


def save_run_rollback_log(project_id, p_name, rollback_data):
    """Save per-run rollback snapshot to rollback_logs/ with timestamp"""
    try:
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        safe_name = re.sub(r'[^\w\-]', '_', p_name)
        filename = f"rollback_{safe_name}_{ts}.json"
        filepath = os.path.join('rollback_logs', filename)
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(rollback_data, f, indent=2)
        print(f"[ROLLBACK] Saved per-run rollback file: {filepath}")
        return filepath
    except Exception as e:
        print(f"[WARN] Could not save per-run rollback log: {e}")
        return None


def save_run_migration_log(project_id, p_name, log_lines):
    """Save per-run migration log to migration_logs/ folder"""
    try:
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        safe_name = re.sub(r'[^\w\-]', '_', p_name)
        filename = f"migration_{safe_name}_{ts}.log"
        filepath = os.path.join('migration_logs', filename)
        with open(filepath, 'w', encoding='utf-8') as f:
            for line in log_lines:
                ts_str = line.get('timestamp', datetime.now().isoformat())
                level = line.get('level', 'INFO')
                msg = line.get('message', '')
                f.write(f"[{ts_str}] [{level}] {msg}\n")
        print(f"[MIGRATION] Saved per-run migration log: {filepath}")
        return filepath
    except Exception as e:
        print(f"[WARN] Could not save per-run migration log: {e}")
        return None


@app.route('/api/projects/<int:project_id>/commit-sha', methods=['GET'])
def get_commit_sha(project_id):
    """Get the latest commit SHA for a project's feature branch (for tag creation)"""
    try:
        branch_num    = request.args.get('branch_num', '12938')
        branch_prefix = request.args.get('branch_prefix', 'task-')
        branch_suffix = request.args.get('branch_suffix', 'java17-migration')
        feature_branch = _build_feature_branch(branch_num, branch_prefix, branch_suffix)
        
        branch_info = ms.api_call(f"projects/{project_id}/repository/branches/{urllib.parse.quote(feature_branch, safe='')}")
        if isinstance(branch_info, dict) and 'commit' in branch_info:
            commit = branch_info['commit']
            return jsonify({
                'success': True,
                'commit_sha': commit.get('id', ''),
                'short_sha': commit.get('short_id', ''),
                'branch': feature_branch,
                'author': commit.get('author_name', 'Unknown'),
                'message': commit.get('title', ''),
                'date': commit.get('committed_date', '')
            })
        
        status = pipeline_status.get(str(project_id), {})
        if status.get('commit_sha'):
            return jsonify({
                'success': True,
                'commit_sha': status['commit_sha'],
                'short_sha': status['commit_sha'][:8],
                'branch': feature_branch,
                'author': status.get('committer_name', 'Unknown'),
                'message': status.get('commit_message', ''),
                'date': status.get('timestamp', '')
            })
        
        return jsonify({'success': False, 'error': 'No commit SHA found'}), 404
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<int:project_id>/tags', methods=['GET'])
def get_tags(project_id):
    """Get tags for a project"""
    try:
        tags_resp = ms.fetch_all_tags_for_project(project_id)
        
        if isinstance(tags_resp, dict) and tags_resp.get("error"):
            return jsonify({'success': False, 'error': tags_resp.get('details')}), 500
        
        tags = tags_resp if isinstance(tags_resp, list) else []
        filter_result = ms.filter_and_sort_deployment_tags(tags)
        
        return jsonify({
            'success': True,
            'tags': filter_result['sorted_tags'],
            'found_categories': filter_result['found_categories'],
            'missing_categories': filter_result['missing_categories']
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/bulk-tags', methods=['POST'])
def get_bulk_tags():
    """Load tags for multiple projects in a single request.

    Body:
        project_ids  list[int]  — projects to fetch tags for

    Returns:
        results  dict  {str(project_id): {tags, found_categories, error?}}
    """
    try:
        data = request.json or {}
        project_ids = [int(x) for x in (data.get('project_ids') or [])]
        if not project_ids:
            return jsonify({'success': False, 'error': 'No project_ids provided'}), 400

        results = {}

        def _fetch_one(pid):
            try:
                tags_resp = ms.fetch_all_tags_for_project(pid)
                tags = tags_resp if isinstance(tags_resp, list) else []
                filter_result = ms.filter_and_sort_deployment_tags(tags)
                results[str(pid)] = {
                    'tags': filter_result['sorted_tags'],
                    'found_categories': filter_result['found_categories'],
                    'missing_categories': filter_result['missing_categories'],
                }
            except Exception as exc:
                results[str(pid)] = {'tags': [], 'found_categories': [], 'missing_categories': [], 'error': str(exc)}

        threads = [threading.Thread(target=_fetch_one, args=(pid,), daemon=True) for pid in project_ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        return jsonify({'success': True, 'results': results})

    except Exception as e:
        print(f"[ERROR] bulk-tags: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<int:project_id>/tags', methods=['POST'])
def create_tag(project_id):
    """Create a new tag"""
    try:
        data = request.json
        tag_name = data.get('tag_name')
        ref = data.get('ref', 'develop')
        
        if not tag_name:
            return jsonify({'success': False, 'error': 'Tag name required'}), 400
        
        result = ms.api_call(f"projects/{project_id}/repository/tags", "POST", {
            "tag_name": tag_name,
            "ref": ref
        })
        
        if isinstance(result, dict) and not result.get("error"):
            return jsonify({'success': True, 'tag': result})
        else:
            return jsonify({'success': False, 'error': result.get('details', 'Unknown error')}), 500
            
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<int:project_id>/tags/<path:tag_name>', methods=['DELETE'])
def delete_tag(project_id, tag_name):
    """Delete a tag"""
    try:
        tag_name = urllib.parse.unquote(tag_name)
        result = ms.api_call(f"projects/{project_id}/repository/tags/{urllib.parse.quote(tag_name, safe='')}", "DELETE")
        if result is None or (isinstance(result, dict) and not result.get("error")):
            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'error': str(result)}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<int:project_id>/tags/<path:tag_name>/pipeline', methods=['GET'])
def get_tag_pipeline_status(project_id, tag_name):
    """Return the latest pipeline status for a given tag ref."""
    try:
        tag_name = urllib.parse.unquote(tag_name)
        result = ms.api_call(
            f"projects/{project_id}/pipelines?ref={urllib.parse.quote(tag_name, safe='')}&per_page=1"
        )
        if isinstance(result, list) and result:
            p = result[0]
            return jsonify({
                'success': True,
                'status': p.get('status'),
                'pipeline_id': p.get('id'),
                'web_url': p.get('web_url', '')
            })
        return jsonify({'success': True, 'status': 'not_found'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


def extract_env_from_tag(tag_name):
    """Extract the bare environment name from a tag that may include a project-name prefix."""
    KNOWN_ENVS = [
        'azure-dev', 'azure-test', 'azure-prod', 'azure-staging', 'azure-uat',
        'dev', 'test', 'prod', 'staging', 'uat',
    ]
    tag_lower = tag_name.lower()
    for env in KNOWN_ENVS:
        if tag_lower == env:
            return env
        if tag_lower.endswith('-' + env):
            return env
    return tag_name


@app.route('/api/projects/<int:project_id>/deploy', methods=['POST'])
def deploy_project(project_id):
    """Deploy project with selected tags — supports queueing via the shared semaphore."""
    try:
        data = request.json
        tags = data.get('tags', [])
        source_branch = data.get('source_branch', None)
        post_merge_mode = data.get('post_merge_mode', False)
        branch_num = data.get('branch_num', '12938')
        use_queue = data.get('use_queue', True)  # NEW: queue-aware deployment

        # Compute locally — never set ms globals in the Flask request handler itself
        _feature_branch = _build_feature_branch(branch_num, data.get("branch_prefix","task-"), data.get("branch_suffix","java17-migration"))
        _jira_id = branch_num
        
        if not tags:
            return jsonify({'success': False, 'error': 'No tags selected'}), 400

        # ── Choose a queue key based on operation type ─────────────────────
        # "pm_<id>" for post-merge develop→dev+test deploys
        # "md_<id>" for master→dev+test+perf deploys
        # These are separate from the migration queue (plain numeric keys).
        if post_merge_mode:
            queue_key = f"pm_{project_id}"
        elif source_branch in ('master', 'main') or (source_branch and source_branch not in ('develop',)):
            queue_key = f"md_{project_id}"
        else:
            queue_key = f"dp_{project_id}"

        task_id = f"{project_id}_deploy_{int(time.time() * 1000)}"
        active_tasks[task_id] = {
            'status': 'queued' if use_queue else 'running',
            'project_id': project_id,
            'operation': 'deploy',
            'logs': []
        }

        # Register in queue if requested
        cancel_ev = None
        if use_queue:
            cancel_ev = _queue_register(queue_key)
            update_pipeline_status(project_id, 'queued')
        
        def deploy_thread():
            deployment_successful = False
            _acquired = False
            def _log(level, message):
                active_tasks[task_id]['logs'].append({
                    'level': level,
                    'message': message,
                    'timestamp': datetime.now().isoformat()
                })

            try:
                # ── Wait for concurrency slot ─────────────────────────────────
                if use_queue:
                    ok = _queue_acquire(queue_key)
                    if not ok:
                        _log('WARN', 'Deploy cancelled while waiting in queue')
                        active_tasks[task_id]['status'] = 'cancelled'
                        update_pipeline_status(project_id, 'cancelled')
                        return
                    _acquired = True

                if post_merge_mode:
                    try:
                        feature_branch_enc = urllib.parse.quote(_feature_branch, safe='')
                        mrs = ms.api_call(
                            f"projects/{project_id}/merge_requests"
                            f"?state=merged&source_branch={feature_branch_enc}&target_branch=develop&per_page=5"
                        )
                        if not isinstance(mrs, list) or len(mrs) == 0:
                            _log('ERROR', (
                                f'Post-merge deploy aborted: no merged MR found for '
                                f'{_feature_branch} → develop. '
                                f'Please ensure the MR is fully merged before deploying.'
                            ))
                            active_tasks[task_id]['status'] = 'failed'
                            return
                        _log('INFO', f'✅ MR verified as merged (MR !{mrs[0].get("iid","?")}). Proceeding with post-merge deploy.')
                    except Exception as mr_exc:
                        _log('WARN', f'Could not verify MR merge status: {mr_exc}. Proceeding anyway.')

                try:
                    project_info = ms.api_call(f"projects/{project_id}")
                    p_name = project_info.get('name', f"ID:{project_id}") if isinstance(project_info, dict) else f"ID:{project_id}"
                    p_path = (project_info.get('path_with_namespace') or p_name) if isinstance(project_info, dict) and not project_info.get('error') else p_name
                    _project_web_url = f"{ms.BASE_URL.rstrip('/')}/{p_path}"
                    _log('INFO', f'Starting deployment for {p_name} — tags: {", ".join(tags)}')

                    deploy_branch = source_branch if source_branch else _feature_branch

                    branch_info = ms.api_call(
                        f"projects/{project_id}/repository/branches/{urllib.parse.quote(deploy_branch, safe='')}"
                    )
                    feature_head = None
                    if isinstance(branch_info, dict) and not branch_info.get('error'):
                        feature_head = (branch_info.get('commit') or {}).get('id')

                    if not feature_head:
                        _log('ERROR', f'Could not resolve HEAD of branch {deploy_branch}')
                        active_tasks[task_id]['status'] = 'failed'
                        return

                    tags_no_changes = 0  # count tags already at current HEAD

                    for tag_name in tags:
                        _log('INFO', f'▶ Processing tag: {tag_name}')
                        quoted_tag = urllib.parse.quote(tag_name, safe='')

                        all_tags_resp = ms.api_call(f"projects/{project_id}/repository/tags?per_page=100")
                        tag_obj = None
                        if isinstance(all_tags_resp, list):
                            tag_obj = next(
                                (t for t in all_tags_resp
                                 if isinstance(t, dict) and t.get('name') == tag_name),
                                None
                            )

                        if tag_obj and tag_obj.get('protected'):
                            _log('ERROR', f'Tag "{tag_name}" is PROTECTED — skipping')
                            continue

                        # ── Determine whether to create/recreate the tag or reuse existing pipeline ──
                        reuse_existing_pipeline = False   # if True, skip tag creation and reuse

                        if tag_obj:
                            existing_sha = ((tag_obj.get('commit') or {}).get('id') or '')
                            if existing_sha and existing_sha == feature_head:
                                # Tag already points to the same commit.
                                # Check whether a successful deploy job already ran for it.
                                _log('INFO',
                                     f'Tag "{tag_name}" already points to {feature_head[:8]} '
                                     f'({deploy_branch} HEAD). Checking deploy status...')
                                existing_pipelines = ms.api_call(
                                    f"projects/{project_id}/pipelines"
                                    f"?ref={urllib.parse.quote(tag_name, safe='')}&per_page=5"
                                )
                                already_deployed = False
                                reuse_pipeline_id = None
                                # Pipelines are returned newest-first. Walk them to find
                                # the most recent one that is usable.
                                if isinstance(existing_pipelines, list):
                                    for ep in existing_pipelines:
                                        ep_id     = ep.get('id')
                                        ep_status = ep.get('status', '')
                                        if not ep_id:
                                            continue
                                        ep_jobs = ms.get_pipeline_jobs(project_id, ep_id)
                                        # Case 1: deploy job already ran successfully → nothing to do
                                        deploy_ran = any(
                                            j.get('name', '').lower().startswith('eb-deploy')
                                            and j.get('status') == 'success'
                                            for j in ep_jobs
                                        )
                                        if deploy_ran:
                                            already_deployed = True
                                            _log('INFO',
                                                 f'✅ Tag "{tag_name}" already deployed via pipeline '
                                                 f'#{ep_id} — skipping.')
                                            break
                                        # Case 2: pipeline is running/pending/success → reuse for terminate+deploy
                                        if ep_status in ('success', 'running', 'pending', 'created',
                                                         'waiting_for_resource', 'preparing'):
                                            reuse_pipeline_id = ep_id
                                            _log('INFO',
                                                 f'Found reusable pipeline #{ep_id} '
                                                 f'(status: {ep_status}) for tag "{tag_name}".')
                                            break
                                        # Case 3: pipeline failed/cancelled → log and keep looking
                                        _log('INFO',
                                             f'Pipeline #{ep_id} for tag "{tag_name}" has status '
                                             f'"{ep_status}" — not reusable, checking older pipelines...')

                                if already_deployed:
                                    tags_no_changes += 1
                                    continue

                                if reuse_pipeline_id:
                                    _log('INFO',
                                         f'Tag "{tag_name}" not yet deployed. Reusing existing pipeline '
                                         f'#{reuse_pipeline_id} for terminate + deploy...')
                                    reuse_existing_pipeline = True
                                    pipeline_id = reuse_pipeline_id
                                else:
                                    # No usable pipeline found — recreate tag to trigger a fresh one
                                    _log('INFO',
                                         f'Tag "{tag_name}" at HEAD but no usable pipeline found. '
                                         f'Recreating tag to trigger a fresh build...')

                        if reuse_existing_pipeline:
                            # ── If the reused pipeline is still building, wait for it ──
                            # (Jumping straight to terminate+deploy while building would find
                            # all jobs in 'created' state, not 'manual', and silently skip them.)
                            reuse_pipeline_url = f"{_project_web_url}/-/pipelines/{pipeline_id}"
                            _log('INFO', f'Reused pipeline URL: {reuse_pipeline_url}')
                            reuse_pl_check = ms.api_call(f"projects/{project_id}/pipelines/{pipeline_id}")
                            reuse_pl_status = reuse_pl_check.get('status') if isinstance(reuse_pl_check, dict) else 'unknown'
                            if reuse_pl_status in ('running', 'pending', 'created', 'waiting_for_resource', 'preparing'):
                                _log('INFO',
                                     f'Reused pipeline #{pipeline_id} is still {reuse_pl_status}. '
                                     f'Waiting for build to complete before triggering deploy...')
                                active_tasks[task_id]['deploy_stage'] = 'build'
                                p_result = ms.wait_for_pipeline_completion(
                                    project_id, pipeline_id, timeout=1800, check_interval=30
                                )
                                if p_result['status'] == 'interrupted':
                                    _log('WARN', 'Reused pipeline monitoring interrupted — stopping deploy loop')
                                    break
                                if p_result['status'] != 'success':
                                    _log('ERROR',
                                         f'Reused pipeline #{pipeline_id} ended with: {p_result["status"]}. '
                                         f'Skipping terminate + deploy for "{tag_name}".')
                                    continue
                                _log('INFO', f'Reused pipeline #{pipeline_id} build complete ✓')
                            elif reuse_pl_status != 'success':
                                _log('ERROR',
                                     f'Reused pipeline #{pipeline_id} has unexpected status: {reuse_pl_status}. '
                                     f'Recreating tag to trigger a fresh build...')
                                reuse_existing_pipeline = False
                                # Fall through to the else block to recreate the tag

                        if reuse_existing_pipeline:
                            # Re-fetch jobs with fresh statuses now that pipeline is complete
                            jobs = ms.get_pipeline_jobs(project_id, pipeline_id)
                            _log('INFO', f'Jobs in reused pipeline: {[j.get("name") for j in jobs]}')
                        else:
                            # Normal path: (re)create the tag
                            if tag_obj and not reuse_existing_pipeline:
                                old_sha = ((tag_obj.get('commit') or {}).get('id') or '?')[:8]
                                _log('INFO', f'Deleting existing tag "{tag_name}" (was at {old_sha})...')
                                ms.api_call(
                                    f"projects/{project_id}/repository/tags/{quoted_tag}",
                                    method='DELETE'
                                )
                                time.sleep(1)

                            _log('INFO', f'Creating fresh tag "{tag_name}" → {deploy_branch} ({feature_head[:8]})...')
                            active_tasks[task_id]['deploy_stage'] = 'tag_create'
                            t_res = ms.api_call(
                                f"projects/{project_id}/repository/tags", 'POST',
                                {'tag_name': tag_name, 'ref': deploy_branch}
                            )
                            if not (isinstance(t_res, dict) and not t_res.get('error')):
                                err = t_res.get('details', str(t_res)) if isinstance(t_res, dict) else str(t_res)
                                _log('ERROR', f'Failed to create tag "{tag_name}": {err}')
                                continue

                            created_commit = (t_res.get('commit') or {}).get('id')
                            if not created_commit:
                                _log('ERROR', f'Tag "{tag_name}" created but no commit SHA returned — skipping')
                                continue

                            tag_url = f"{_project_web_url}/-/tags/{urllib.parse.quote(tag_name, safe='')}"
                            _log('INFO', f'Tag "{tag_name}" created at {created_commit[:8]}')
                            _log('INFO', f'Tag URL: {tag_url}')

                        if not reuse_existing_pipeline:
                            # ── Wait for GitLab to schedule the fresh pipeline ──────────
                            _log('INFO', 'Waiting for GitLab to schedule build pipeline...')
                            active_tasks[task_id]['deploy_stage'] = 'build'
                            time.sleep(12)
                            pipeline = None
                            for _attempt in range(15):
                                pipeline = ms.get_pipeline_for_commit(project_id, created_commit)
                                if pipeline:
                                    break
                                elapsed = 12 + (_attempt + 1) * 20
                                _log('INFO', f'  Pipeline not visible yet ({elapsed}s elapsed) — retrying...')
                                time.sleep(20)

                            if not pipeline:
                                _log('ERROR',
                                     f'No pipeline appeared for tag "{tag_name}" after 5 min. '
                                     f'Check GitLab CI config for this project.')
                                continue

                            pipeline_id  = pipeline.get('id')
                            pipeline_url = f"{_project_web_url}/-/pipelines/{pipeline_id}"
                            _log('INFO', f'Pipeline #{pipeline_id} detected. Waiting for build to finish (up to 30 min)...')
                            _log('INFO', f'Pipeline URL: {pipeline_url}')

                            p_result = ms.wait_for_pipeline_completion(
                                project_id, pipeline_id, timeout=1800, check_interval=30
                            )
                            if p_result['status'] == 'interrupted':
                                _log('WARN', 'Pipeline monitoring interrupted — stopping deploy loop')
                                break
                            if p_result['status'] != 'success':
                                _log('ERROR',
                                     f'Build pipeline #{pipeline_id} ended with: {p_result["status"]}. '
                                     f'Skipping terminate + deploy for "{tag_name}".')
                                continue

                            _log('INFO', f'Build pipeline #{pipeline_id} succeeded!')
                            jobs = ms.get_pipeline_jobs(project_id, pipeline_id)
                            _log('INFO', f'Jobs in pipeline: {[j.get("name") for j in jobs]}')

                        terminate_job = ms.find_job_by_name(jobs, 'eb-terminate')
                        if terminate_job:
                            if terminate_job.get('status') == 'manual':
                                env_name = extract_env_from_tag(tag_name)
                                _log('INFO', f'Triggering eb-terminate (ENVIRONMENT={env_name})...')
                                active_tasks[task_id]['deploy_stage'] = 'terminate'
                                _log('INFO', f'Job URL: {_project_web_url}/-/jobs/{terminate_job["id"]}')
                                term_trigger = ms.api_call(
                                    f"projects/{project_id}/jobs/{terminate_job['id']}/play",
                                    'POST',
                                    {'variables': [
                                        {'key': 'ENVIRONMENT', 'value': env_name},
                                        {'key': 'EB_ENV',      'value': env_name},
                                    ]}
                                )
                                if isinstance(term_trigger, dict) and term_trigger.get('error'):
                                    _log('WARN', 'play-with-variables failed — retrying without variables')
                                    ms.trigger_manual_job(project_id, terminate_job['id'])

                                term_res = ms.wait_for_job_completion(
                                    project_id, terminate_job['id'], timeout=900
                                )
                                if term_res['status'] == 'interrupted':
                                    _log('WARN', 'eb-terminate monitoring interrupted — stopping deploy loop')
                                    break
                                if term_res['status'] != 'success':
                                    _log('ERROR',
                                         f'eb-terminate ended with: {term_res["status"]}. '
                                         f'Deploy for "{tag_name}" is BLOCKED until terminate succeeds.')
                                    continue
                                _log('INFO', 'eb-terminate succeeded ✓')
                            else:
                                _log('INFO',
                                     f'eb-terminate is in state "{terminate_job.get("status")}" '
                                     f'(not manual) — skipping trigger, proceeding to deploy')
                        else:
                            _log('INFO', 'No eb-terminate job in pipeline — proceeding directly to deploy')

                        deploy_job_name = ms.map_tag_to_deploy_job(tag_name)
                        if not deploy_job_name:
                            _log('WARN', f'No deploy job mapped for tag "{tag_name}" — skipping')
                            continue

                        jobs = ms.get_pipeline_jobs(project_id, pipeline_id)
                        deploy_job = ms.find_job_by_name(jobs, deploy_job_name)

                        # ── Fallback: try common eb-deploy name variants if primary not found ──
                        if not deploy_job:
                            _log('INFO', f'Deploy job "{deploy_job_name}" not found — trying name variants...')
                            _deploy_name_candidates = [
                                'eb-deploy-dev-azure',
                                'eb-deploy',
                                'eb-deploy-dev',
                                'eb-deploy-azure',
                            ]
                            # Remove the one we already tried and prioritise remaining
                            _deploy_name_candidates = [c for c in _deploy_name_candidates if c != deploy_job_name]
                            for _candidate in _deploy_name_candidates:
                                _found = ms.find_job_by_name(jobs, _candidate)
                                if _found:
                                    _log('INFO', f'Found deploy job via fallback: "{_candidate}"')
                                    deploy_job      = _found
                                    deploy_job_name = _candidate
                                    break
                            # Last resort: any job whose name starts with 'eb-deploy'
                            if not deploy_job:
                                deploy_job = next(
                                    (j for j in jobs if j.get('name', '').lower().startswith('eb-deploy')),
                                    None
                                )
                                if deploy_job:
                                    deploy_job_name = deploy_job.get('name')
                                    _log('INFO', f'Found deploy job via prefix scan: "{deploy_job_name}"')

                        if not deploy_job:
                            _log('WARN', f'Deploy job "{deploy_job_name}" not found in pipeline (tried all variants). '
                                         f'Available jobs: {[j.get("name") for j in jobs]}')
                            continue

                        if deploy_job.get('status') != 'manual':
                            _log('INFO',
                                 f'Deploy job "{deploy_job_name}" is in state '
                                 f'"{deploy_job.get("status")}" — skipping trigger')
                            continue

                        _log('INFO', f'Triggering deploy job "{deploy_job_name}"...')
                        active_tasks[task_id]['deploy_stage'] = 'deploy'
                        _log('INFO', f'Job URL: {_project_web_url}/-/jobs/{deploy_job["id"]}')
                        ms.trigger_manual_job(project_id, deploy_job['id'])
                        dep_res = ms.wait_for_job_completion(
                            project_id, deploy_job['id'], timeout=1200
                        )
                        if dep_res['status'] == 'interrupted':
                            _log('WARN', f'"{deploy_job_name}" interrupted — stopping deploy loop')
                            break
                        if dep_res['status'] == 'success':
                            _log('SUCCESS', f'Deployment complete for tag "{tag_name}"!')
                            deployment_successful = True
                        else:
                            _log('ERROR', f'"{deploy_job_name}" ended with: {dep_res["status"]}')

                    if deployment_successful:
                        active_tasks[task_id]['status'] = 'success'
                        _log('SUCCESS', '✅ Deployment completed successfully')
                        final_stage = 'post_merge_deployed' if post_merge_mode else 'deployed'
                        update_pipeline_status(project_id, 'success', workflow_stage=final_stage)
                        add_project_to_history(project_id, p_name, 'deploy', 'success', {'tags': tags})
                    elif tags_no_changes == len(tags):
                        active_tasks[task_id]['status'] = 'no_changes'
                        _log('INFO',
                             '⚠️ Nothing deployed — all selected tags already point to the current '
                             f'branch HEAD ({feature_head[:8]}). Commit new code to {deploy_branch} '
                             'and deploy again.')
                        add_project_to_history(project_id, p_name, 'deploy', 'no_changes', {'tags': tags})
                        deployment_successful = True  # treat as non-failure so queue circuit-breaker doesn't penalise
                    else:
                        active_tasks[task_id]['status'] = 'failed'
                        _log('ERROR', '❌ Deployment did not complete — review logs above')
                        add_project_to_history(project_id, p_name, 'deploy', 'failed', {'tags': tags})

                except Exception as e:
                    print(f"[ERROR] Deploy thread error: {e}")
                    active_tasks[task_id]['logs'].append({
                        'level': 'ERROR',
                        'message': f'Exception: {str(e)}',
                        'timestamp': datetime.now().isoformat()
                    })
                    active_tasks[task_id]['status'] = 'failed'

            finally:
                if use_queue and _acquired:
                    _queue_release(queue_key, succeeded=deployment_successful)
                _cleanup_active_tasks()

        threading.Thread(target=deploy_thread, daemon=True).start()
        
        return jsonify({'success': True, 'task_id': task_id, 'queue_key': queue_key if use_queue else None})
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/bulk-master-deploy', methods=['POST'])
def bulk_master_deploy():
    """
    Deploy master/main branch → prod tags for a list of projects.
    Called after the developer manually merges develop → master.

    Body:
        project_ids   list[int]   — projects to deploy
        tags          list[str]   — optional tag override (e.g. ['azure-prod'])
                                    if omitted, auto-detects azure vs plain family
        skip_mr_check bool        — skip develop→master MR verification
                                    (default true — merge is done manually)
    Returns:
        task_ids  dict  {str(project_id): task_id}
    """
    try:
        data        = request.json or {}
        project_ids = [int(x) for x in (data.get('project_ids') or [])]
        custom_tags = list(data.get('tags') or [])
        skip_check  = bool(data.get('skip_mr_check', True))

        if not project_ids:
            return jsonify({'success': False, 'error': 'No project_ids provided'}), 400

        per_project_tids = {}

        for pid in project_ids:
            tid = f"{pid}_masterDeploy_{int(time.time() * 1000)}"
            active_tasks[tid] = {
                'status':     'running',
                'stage':      'starting',
                'project_id': pid,
                'operation':  'master_deploy',
                'logs':       []
            }
            per_project_tids[str(pid)] = tid

            def _master_deploy(project_id=pid, task_id=tid, override_tags=list(custom_tags)):
                def _log(level, msg):
                    active_tasks[task_id]['logs'].append({
                        'level': level, 'message': msg,
                        'timestamp': datetime.now().isoformat()
                    })

                try:
                    proj   = ms.api_call(f"projects/{project_id}")
                    p_name = proj.get('name', f"ID:{project_id}") if isinstance(proj, dict) else f"ID:{project_id}"
                    p_path = (proj.get('path_with_namespace') or p_name) if isinstance(proj, dict) else p_name
                    p_web  = f"{ms.BASE_URL.rstrip('/')}/{p_path}"
                    _log('INFO', f'[{p_name}] Starting master-branch deploy...')

                    # ── Optional MR verification ──────────────────────────────
                    if not skip_check:
                        merged_mr = None
                        for target in ('master', 'main'):
                            mrs = ms.api_call(
                                f"projects/{project_id}/merge_requests"
                                f"?state=merged&source_branch=develop&target_branch={target}&per_page=5"
                            )
                            if isinstance(mrs, list) and len(mrs) > 0:
                                merged_mr = mrs[0]
                                break
                        if not merged_mr:
                            _log('ERROR', 'No merged develop→master/main MR found. '
                                          'Ensure the MR is fully merged before deploying, '
                                          'or enable skip_mr_check.')
                            active_tasks[task_id]['status'] = 'failed'
                            active_tasks[task_id]['stage']  = 'failed'
                            return
                        _log('INFO', f'✅ MR !{merged_mr.get("iid","?")} verified as merged.')

                    # ── Resolve master/main ───────────────────────────────────
                    master_branch = None
                    for candidate in ('master', 'main'):
                        br = ms.api_call(f"projects/{project_id}/repository/branches/{candidate}")
                        if isinstance(br, dict) and br.get('name'):
                            master_branch = candidate
                            break
                    if not master_branch:
                        _log('ERROR', 'Neither master nor main branch found.')
                        active_tasks[task_id]['status'] = 'failed'
                        active_tasks[task_id]['stage']  = 'failed'
                        return

                    br_info     = ms.api_call(f"projects/{project_id}/repository/branches/{master_branch}")
                    master_head = (br_info.get('commit') or {}).get('id') if isinstance(br_info, dict) else None
                    if not master_head:
                        _log('ERROR', f'Could not resolve HEAD of {master_branch}.')
                        active_tasks[task_id]['status'] = 'failed'
                        active_tasks[task_id]['stage']  = 'failed'
                        return
                    _log('INFO', f'Deploying from {master_branch} @ {master_head[:8]}')

                    # ── Determine prod tags ───────────────────────────────────
                    if override_tags:
                        prod_tags = override_tags
                        _log('INFO', f'Using provided tags: {prod_tags}')
                    else:
                        all_tags_resp = ms.api_call(f"projects/{project_id}/repository/tags?per_page=100")
                        existing_names = []
                        if isinstance(all_tags_resp, list):
                            existing_names = [t.get('name', '').lower()
                                              for t in all_tags_resp if isinstance(t, dict)]
                        if any(n.startswith('azure-') for n in existing_names):
                            prod_tags = ['azure-prod']
                            _log('INFO', 'Auto-detected azure family → [azure-prod]')
                        else:
                            prod_tags = ['prod']
                            _log('INFO', 'Auto-detected plain family → [prod]')

                    active_tasks[task_id]['stage'] = 'deploying'
                    deployment_ok = False
                    tags_no_changes = 0  # track tags already at master HEAD

                    for tag_name in prod_tags:
                        quoted_tag = urllib.parse.quote(tag_name, safe='')

                        # Check if tag is protected
                        all_tags_resp = ms.api_call(f"projects/{project_id}/repository/tags?per_page=100")
                        tag_obj = None
                        if isinstance(all_tags_resp, list):
                            tag_obj = next((t for t in all_tags_resp
                                            if isinstance(t, dict) and t.get('name') == tag_name), None)
                        if tag_obj and tag_obj.get('protected'):
                            _log('ERROR', f'Tag "{tag_name}" is PROTECTED — cannot recreate. Skipping.')
                            continue

                        existing_commit = (tag_obj.get('commit') or {}).get('id') if tag_obj else None
                        if existing_commit and existing_commit == master_head:
                            _log('INFO',
                                 f'[NO CHANGES] "{tag_name}" already at {master_head[:8]} '
                                 f'— no new commits on {master_branch} since last deploy. Skipping.')
                            tags_no_changes += 1
                            continue

                        if tag_obj:
                            _log('INFO', f'Deleting existing tag "{tag_name}"...')
                            ms.api_call(f"projects/{project_id}/repository/tags/{quoted_tag}", 'DELETE')
                            time.sleep(0.5)

                        _log('INFO', f'Creating tag "{tag_name}" → {master_branch} ({master_head[:8]})...')
                        t_res = ms.api_call(
                            f"projects/{project_id}/repository/tags", 'POST',
                            {'tag_name': tag_name, 'ref': master_branch}
                        )
                        if not (isinstance(t_res, dict) and not t_res.get('error')):
                            err = t_res.get('details', 'unknown') if isinstance(t_res, dict) else str(t_res)
                            _log('ERROR', f'Failed to create tag "{tag_name}": {err}')
                            continue

                        created_commit = (t_res.get('commit') or {}).get('id', master_head)
                        _log('INFO', f'Tag "{tag_name}" created at {created_commit[:8]}')

                        # Wait for pipeline triggered by the tag
                        _log('INFO', 'Waiting 10 s for GitLab to schedule pipeline...')
                        time.sleep(10)
                        pipeline = None
                        for attempt in range(15):   # up to 5 min
                            pipeline = ms.get_pipeline_for_commit(project_id, created_commit)
                            if pipeline:
                                break
                            _log('INFO', f'Pipeline not visible yet ({(attempt+1)*20}s)...')
                            time.sleep(20)

                        if not pipeline:
                            _log('WARN', f'No pipeline detected for {created_commit[:8]} — tag created but CI not triggered. Check GitLab CI config.')
                            deployment_ok = True   # tag exists; pipeline may be intentionally absent
                            continue

                        pipeline_id  = pipeline.get('id')
                        pipeline_url = f"{p_web}/-/pipelines/{pipeline_id}"
                        _log('INFO', f'Pipeline #{pipeline_id} found — monitoring... {pipeline_url}')
                        update_pipeline_status(project_id, 'running', pipeline_id=pipeline_id,
                                               commit_sha=created_commit, workflow_stage='master_deploying',
                                               path_with_namespace=p_path)

                        p_result = ms.wait_for_pipeline_completion(
                            project_id, pipeline_id, timeout=3600, check_interval=30)
                        if p_result['status'] != 'success':
                            _log('ERROR', f'Pipeline #{pipeline_id} ended with: {p_result["status"]}')
                            update_pipeline_status(project_id, 'failed', pipeline_id=pipeline_id,
                                                   commit_sha=created_commit,
                                                   workflow_stage='master_deploy_failed',
                                                   path_with_namespace=p_path)
                            continue

                        _log('SUCCESS', f'✅ Pipeline #{pipeline_id} passed!')

                        # Trigger the deploy job
                        deploy_job_name = ms.map_tag_to_deploy_job(tag_name)
                        if not deploy_job_name:
                            _log('WARN', f'No deploy job mapped for "{tag_name}" — skipping job trigger')
                            deployment_ok = True
                            continue

                        jobs       = ms.get_pipeline_jobs(project_id, pipeline_id)
                        deploy_job = ms.find_job_by_name(jobs, deploy_job_name)
                        if deploy_job and deploy_job.get('status') == 'manual':
                            _log('INFO', f'Triggering deploy job "{deploy_job_name}"...')
                            ms.trigger_manual_job(project_id, deploy_job['id'])
                            dep_res = ms.wait_for_job_completion(project_id, deploy_job['id'], timeout=1800)
                            if dep_res['status'] == 'success':
                                _log('SUCCESS', f'🚀 "{deploy_job_name}" succeeded!')
                                deployment_ok = True
                                update_pipeline_status(project_id, 'success', pipeline_id=pipeline_id,
                                                       commit_sha=created_commit,
                                                       workflow_stage='master_deployed',
                                                       path_with_namespace=p_path)
                            else:
                                _log('ERROR', f'"{deploy_job_name}" ended with: {dep_res["status"]}')
                        else:
                            _log('WARN', f'Deploy job "{deploy_job_name}" not found or not in manual state')

                    if deployment_ok:
                        active_tasks[task_id]['status'] = 'success'
                        active_tasks[task_id]['stage']  = 'success'
                        add_project_to_history(project_id, p_name, 'master_deploy', 'success',
                                               {'tags': prod_tags, 'branch': master_branch})
                    elif tags_no_changes == len(prod_tags):
                        # All prod tags already point to the current master HEAD
                        active_tasks[task_id]['status'] = 'no_changes'
                        active_tasks[task_id]['stage']  = 'no_changes'
                        _log('INFO',
                             f'⚠️ Nothing deployed — all tags already point to {master_branch} '
                             f'HEAD ({master_head[:8]}). Merge new code to {master_branch} '
                             'and deploy again.')
                        add_project_to_history(project_id, p_name, 'master_deploy', 'no_changes',
                                               {'tags': prod_tags, 'branch': master_branch})
                    else:
                        active_tasks[task_id]['status'] = 'failed'
                        active_tasks[task_id]['stage']  = 'failed'
                        add_project_to_history(project_id, p_name, 'master_deploy', 'failed',
                                               {'tags': prod_tags, 'branch': master_branch})

                except Exception as e:
                    print(f"[ERROR] master_deploy thread: {e}")
                    active_tasks[task_id]['logs'].append({
                        'level': 'ERROR', 'message': f'Exception: {e}',
                        'timestamp': datetime.now().isoformat()
                    })
                    active_tasks[task_id]['status'] = 'failed'
                    active_tasks[task_id]['stage']  = 'failed'
                finally:
                    _cleanup_active_tasks()

            threading.Thread(target=_master_deploy, daemon=True).start()

        return jsonify({
            'success':  True,
            'task_ids': per_project_tids,
            'count':    len(project_ids),
        })

    except Exception as e:
        print(f"[ERROR] bulk_master_deploy: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/bulk-mr', methods=['POST'])
def bulk_create_mrs():
    """Create MRs for multiple projects — only allowed when pipeline has passed"""
    try:
        data = request.json
        project_ids = data.get('project_ids', [])
        branch_num  = data.get('branch_num', '12938')
        selected_assignees = data.get('assignees', [])
        selected_reviewers = data.get('reviewers', [])

        # Capture locally before thread spawn to avoid cross-request contamination
        _feature_branch = _build_feature_branch(branch_num, data.get("branch_prefix","task-"), data.get("branch_suffix","java17-migration"))
        _mr_title = f"TASK-{branch_num}: java migration"
        _jira_id = branch_num

        ALLOWED_STAGES = {'pipeline_success', 'deployed', 'mr_raised', 'merged'}
        blocked = []
        for pid in project_ids:
            ps = pipeline_status.get(str(pid), {})
            stage = ps.get('workflow_stage', 'idle')
            status = ps.get('status', 'unknown')
            project_name = PROJECT_NAMES.get(pid, f'Project {pid}')
            if status == 'running':
                blocked.append(f"{project_name}: pipeline still running")
            elif stage not in ALLOWED_STAGES:
                blocked.append(f"{project_name}: stage is '{stage}' — pipeline must pass first")
        if blocked:
            print(f"[BULK-MR] Blocked — pre-conditions not met: {blocked}")
            return jsonify({
                'success': False,
                'error': 'MR creation blocked — the following projects have not passed their pipeline:\n' + '\n'.join(blocked),
                'blocked': blocked
            }), 422
        
        task_id = f"bulk_mr_{int(time.time() * 1000)}"
        active_tasks[task_id] = {
            'status': 'running',
            'operation': 'bulk_mr',
            'logs': []
        }
        
        _selected_assignees = list(selected_assignees)
        _selected_reviewers = list(selected_reviewers)

        def bulk_mr_thread():
            try:
                # Hold the globals lock for the entire bulk_create_mrs call.
                # ms.bulk_create_mrs reads FEATURE_BRANCH, MR_TITLE, REVIEWER_USERNAMES
                # and ASSIGNEE_USERNAMES multiple times internally.  Without the lock,
                # a concurrent request could overwrite these between individual project MRs.
                with _ms_globals_lock:
                    ms.FEATURE_BRANCH = _feature_branch
                    ms.JIRA_ID = _jira_id
                    ms.MR_TITLE = _mr_title
                    ms.REVIEWER_USERNAMES = list(_selected_reviewers)
                    ms.ASSIGNEE_USERNAMES = list(_selected_assignees)
                    print(f"[BULK-MR] Using reviewers={_selected_reviewers}, assignees={_selected_assignees}")
                    results = ms.bulk_create_mrs(project_ids, {'snapshots': []}, state=None)

                active_tasks[task_id]['status'] = 'success'
                active_tasks[task_id]['logs'].append({
                    'level': 'SUCCESS',
                    'message': f'✅ Bulk MR creation completed: {results["success"]} succeeded, {results["failed"]} failed',
                    'timestamp': datetime.now().isoformat()
                })
            except Exception as e:
                print(f"[ERROR] Bulk MR thread error: {e}")
                active_tasks[task_id]['logs'].append({
                    'level': 'ERROR',
                    'message': f'Exception: {str(e)}',
                    'timestamp': datetime.now().isoformat()
                })
                active_tasks[task_id]['status'] = 'failed'
            finally:
                _cleanup_active_tasks()
        
        threading.Thread(target=bulk_mr_thread, daemon=True).start()
        
        return jsonify({'success': True, 'task_id': task_id})
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/history', methods=['GET'])
def get_history():
    """Get project history"""
    return jsonify({
        'success': True,
        'history': project_history
    })


@app.route('/api/pipeline-status', methods=['GET'])
def get_pipeline_status():
    """Get current pipeline status for all projects"""
    return jsonify({
        'success': True,
        'pipelines': pipeline_status
    })


@app.route('/api/logs', methods=['GET'])
def list_logs():
    """List all available log files"""
    try:
        log_dirs = {
            'migration_logs': '📝 Migration',
            'rollback_logs': '↩️ Rollback', 
            'state_logs': '💾 State',
            'api_audit_logs': '🔍 Audit'
        }
        all_logs = []
        
        for log_dir, display_name in log_dirs.items():
            if os.path.exists(log_dir):
                for filename in os.listdir(log_dir):
                    if filename.endswith('.log') or filename.endswith('.json'):
                        filepath = os.path.join(log_dir, filename)
                        try:
                            stat_info = os.stat(filepath)
                            size_kb = round(stat_info.st_size / 1024, 2)
                            modified_time = datetime.fromtimestamp(stat_info.st_mtime)
                            
                            all_logs.append({
                                'name': filename,
                                'path': filepath,
                                'size_kb': size_kb,
                                'timestamp': modified_time.isoformat(),
                                'date': modified_time.strftime('%Y-%m-%d %H:%M:%S'),
                                'type': log_dir,
                                'type_display': display_name
                            })
                        except Exception as e:
                            print(f"[ERROR] Error reading file {filepath}: {e}")
        
        all_logs.sort(key=lambda x: x['timestamp'], reverse=True)
        
        return jsonify({
            'success': True,
            'logs': all_logs,
            'count': len(all_logs),
            'directories': {dir_name: os.path.exists(dir_name) for dir_name in log_dirs.keys()}
        })
    except Exception as e:
        print(f"[ERROR] Error listing logs: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/logs/<path:log_path>', methods=['GET'])
def get_log_content(log_path):
    """Get content of a specific log file"""
    try:
        log_path = urllib.parse.unquote(log_path)
        log_path = log_path.replace('\\', '/')
        
        allowed_dirs = ['migration_logs', 'rollback_logs', 'state_logs', 'api_audit_logs']
        normalized_path = os.path.normpath(log_path)
        
        path_parts = normalized_path.replace('\\', '/').split('/')
        if not path_parts or path_parts[0] not in allowed_dirs:
            return jsonify({'success': False, 'error': 'Access denied'}), 403
        
        if not os.path.exists(normalized_path):
            return jsonify({'success': False, 'error': 'Log file not found'}), 404
        
        with open(normalized_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        
        return jsonify({
            'success': True,
            'content': content,
            'path': normalized_path
        })
    except Exception as e:
        print(f"[ERROR] Error reading log {log_path}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


def generate_diff(old_content, new_content):
    import difflib
    
    if not isinstance(old_content, str):
        old_content = str(old_content)
    if not isinstance(new_content, str):
        new_content = str(new_content)
    
    old_lines = old_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)
    
    diff = difflib.unified_diff(
        old_lines, 
        new_lines, 
        fromfile='before', 
        tofile='after', 
        lineterm=''
    )
    
    diff_text = '\n'.join(diff)
    
    print(f"[DEBUG] Generated diff with {len(diff_text)} characters")
    print(f"[DEBUG] Old content: {len(old_content)} chars, New content: {len(new_content)} chars")
    
    return diff_text


@app.route('/api/rollback', methods=['POST'])
def perform_rollback_from_ui():
    """Perform rollback using a rollback file"""
    try:
        data = request.json
        rollback_file = data.get('rollback_file')
        
        if not rollback_file:
            return jsonify({'success': False, 'error': 'No rollback file specified'}), 400
        
        rollback_file = urllib.parse.unquote(rollback_file)
        rollback_file = rollback_file.replace('\\', '/')
        
        allowed_dirs = ['rollback_logs']
        normalized_path = os.path.normpath(rollback_file)
        path_parts = normalized_path.replace('\\', '/').split('/')
        
        if not path_parts or path_parts[0] not in allowed_dirs:
            return jsonify({'success': False, 'error': 'Invalid rollback file path'}), 403
        
        if not os.path.exists(normalized_path):
            return jsonify({'success': False, 'error': 'Rollback file not found'}), 404
        
        print(f"[ROLLBACK] Starting rollback from: {rollback_file}")
        
        task_id = f"rollback_{int(time.time() * 1000)}"
        active_tasks[task_id] = {
            'status': 'running',
            'operation': 'rollback',
            'logs': []
        }
        
        def rollback_thread():
            try:
                active_tasks[task_id]['logs'].append({
                    'level': 'INFO',
                    'message': f'Loading rollback data from: {rollback_file}',
                    'timestamp': datetime.now().isoformat()
                })
                
                rollback_data = ms.load_rollback_data(rollback_file)
                
                if not rollback_data:
                    active_tasks[task_id]['logs'].append({
                        'level': 'ERROR',
                        'message': 'Failed to load rollback data',
                        'timestamp': datetime.now().isoformat()
                    })
                    active_tasks[task_id]['status'] = 'failed'
                    return
                
                snapshots = rollback_data.get('snapshots', [])
                
                active_tasks[task_id]['logs'].append({
                    'level': 'INFO',
                    'message': f'Found {len(snapshots)} project(s) to rollback',
                    'timestamp': datetime.now().isoformat()
                })
                
                success_count = 0
                fail_count = 0
                
                for snapshot in snapshots:
                    pid = snapshot['project_id']
                    p_name = snapshot['project_name']
                    branch = snapshot['branch']

                    # Guard: never roll back onto a protected branch
                    _PROTECTED_ROLLBACK = {'develop', 'master', 'main'}
                    if branch.lower() in _PROTECTED_ROLLBACK:
                        active_tasks[task_id]['logs'].append({
                            'level': 'ERROR',
                            'message': f'❌ Rollback for {p_name} refused — target branch "{branch}" is protected. '
                                       f'Rollback snapshots must target a feature branch.',
                            'timestamp': datetime.now().isoformat()
                        })
                        fail_count += 1
                        continue
                    
                    active_tasks[task_id]['logs'].append({
                        'level': 'INFO',
                        'message': f'Rolling back: {p_name}',
                        'timestamp': datetime.now().isoformat()
                    })
                    
                    project_success = True
                    for file_path, file_data in snapshot.get('files', {}).items():
                        if not file_data.get('exists'):
                            continue
                        
                        content = file_data.get('content')
                        if content is None:
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
                            
                            ms.api_call(f"projects/{pid}/repository/commits", "POST", commit_data)
                            
                            active_tasks[task_id]['logs'].append({
                                'level': 'SUCCESS',
                                'message': f'✅ Restored {file_path} in {p_name}',
                                'timestamp': datetime.now().isoformat()
                            })
                            
                        except Exception as e:
                            active_tasks[task_id]['logs'].append({
                                'level': 'ERROR',
                                'message': f'❌ Error restoring {file_path}: {str(e)}',
                                'timestamp': datetime.now().isoformat()
                            })
                            project_success = False
                    
                    if project_success:
                        success_count += 1
                        active_tasks[task_id]['logs'].append({
                            'level': 'SUCCESS',
                            'message': f'✅ Rollback completed for {p_name}',
                            'timestamp': datetime.now().isoformat()
                        })
                    else:
                        fail_count += 1
                        active_tasks[task_id]['logs'].append({
                            'level': 'WARN',
                            'message': f'⚠️ Rollback completed with errors for {p_name}',
                            'timestamp': datetime.now().isoformat()
                        })
                
                active_tasks[task_id]['logs'].append({
                    'level': 'INFO',
                    'message': f'Rollback complete: {success_count} successful, {fail_count} with errors',
                    'timestamp': datetime.now().isoformat()
                })
                
                active_tasks[task_id]['status'] = 'success' if fail_count == 0 else 'partial_success'
                active_tasks[task_id]['success_count'] = success_count
                active_tasks[task_id]['fail_count'] = fail_count
                
            except Exception as e:
                print(f"[ERROR] Rollback thread error: {e}")
                active_tasks[task_id]['logs'].append({
                    'level': 'ERROR',
                    'message': f'Exception: {str(e)}',
                    'timestamp': datetime.now().isoformat()
                })
                active_tasks[task_id]['status'] = 'failed'
            finally:
                _cleanup_active_tasks()

        threading.Thread(target=rollback_thread, daemon=True).start()

        return jsonify({
            'success': True,
            'task_id': task_id,
            'message': 'Rollback started'
        })
        
    except Exception as e:
        print(f"[ERROR] Rollback endpoint error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<int:project_id>/branches/<path:branch_name>', methods=['DELETE'])
def delete_branch(project_id, branch_name):
    """Delete a feature branch"""
    try:
        branch_name = urllib.parse.unquote(branch_name)

        # Guard: never allow deleting protected branches from this tool
        _PROTECTED_BRANCHES = {'develop', 'master', 'main'}
        if branch_name.lower() in _PROTECTED_BRANCHES:
            print(f"[SECURITY] Refused attempt to delete protected branch '{branch_name}' on project {project_id}")
            return jsonify({'success': False, 'error': f"Refusing to delete protected branch '{branch_name}'. This tool only deletes feature branches."}), 400

        result = ms.api_call(f"projects/{project_id}/repository/branches/{urllib.parse.quote(branch_name, safe='')}", "DELETE")
        
        if result is None or (isinstance(result, dict) and not result.get("error")):
            print(f"[BRANCH] Deleted branch {branch_name} from project {project_id}")
            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'error': str(result)}), 500
    except Exception as e:
        print(f"[ERROR] Branch delete error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<int:project_id>/pipelines/<int:pipeline_id>/retry', methods=['POST'])
def retry_pipeline(project_id, pipeline_id):
    """Retry a failed pipeline"""
    try:
        result = ms.api_call(f"projects/{project_id}/pipelines/{pipeline_id}/retry", "POST")
        
        if isinstance(result, dict) and not result.get("error"):
            print(f"[PIPELINE] Retried pipeline {pipeline_id} for project {project_id}")
            update_pipeline_status(project_id, 'running', pipeline_id=pipeline_id)
            return jsonify({'success': True, 'pipeline': result})
        else:
            return jsonify({'success': False, 'error': result.get('details', 'Unknown error')}), 500
    except Exception as e:
        print(f"[ERROR] Pipeline retry error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/reload-config', methods=['POST'])
def reload_config():
    """Re-read .env and reinitialise the HTTP session without restarting the server."""
    global PROJECT_NAMES
    try:
        raw = _read_env_file_robust()
        _apply_env_dict(raw)

        try:
            new_env = ms.load_env_config()
            if new_env.get('token') and not ms.TOKEN:
                ms.TOKEN = new_env['token']
            if new_env.get('projects'):
                PROJECT_NAMES = new_env['projects']
        except Exception:
            pass

        # Also refresh extended config (includes presto_project_ids)
        _new_ext = _load_extended_env_config()
        env_config['actuator_urls']         = _new_ext['actuator_urls']
        env_config['actuator_urls_dev']     = _new_ext['actuator_urls_dev']
        env_config['actuator_urls_test']    = _new_ext['actuator_urls_test']
        env_config['actuator_urls_perf']    = _new_ext['actuator_urls_perf']
        env_config['perf_urls']             = _new_ext['perf_urls']
        env_config['perf_urls_dev']         = _new_ext['perf_urls_dev']
        env_config['perf_urls_test']        = _new_ext['perf_urls_test']
        env_config['parent_pom_project_id'] = _new_ext['parent_pom_project_id']
        env_config['presto_project_ids']    = _new_ext['presto_project_ids']

        _ssl = raw.get('GITLAB_VERIFY_SSL', '').lower()
        _ssl_bool = (_ssl != 'false') if _ssl else True
        ms.SSL_VERIFY = _ssl_bool
        ms.setup_http_session(ssl_verify=_ssl_bool)
        print(f"[INFO] Config reloaded — {len(PROJECT_NAMES)} projects, token_set={bool(ms.TOKEN)}, base_url={ms.BASE_URL}")
        return jsonify({'success': True, 'projects': len(PROJECT_NAMES), 'token_set': bool(ms.TOKEN), 'base_url': ms.BASE_URL})
    except Exception as e:
        print(f"[ERROR] reload-config error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/env-debug', methods=['GET'])
def env_debug():
    """Diagnostic: show exactly what is parsed from .env"""
    env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    result = {
        'env_file_path': env_file,
        'env_file_exists': os.path.exists(env_file),
        'current_token_set': bool(ms.TOKEN),
        'current_token_length': len(ms.TOKEN) if ms.TOKEN else 0,
        'current_base_url': ms.BASE_URL,
        'current_projects_count': len(PROJECT_NAMES),
        'presto_project_ids': env_config.get('presto_project_ids', []),
    }
    if os.path.exists(env_file):
        try:
            raw_bytes = open(env_file, 'rb').read(4)
            result['has_bom'] = raw_bytes[:3] == b'\xef\xbb\xbf'
            result['first_bytes_hex'] = raw_bytes.hex()
            with open(env_file, encoding='utf-8-sig', errors='replace') as f:
                lines = f.read().splitlines()
            result['total_lines'] = len(lines)
            result['non_empty_non_comment_lines'] = len([l for l in lines if l.strip() and not l.strip().startswith('#')])
            parsed = _read_env_file_robust()
            safe = {}
            for k, v in parsed.items():
                if 'TOKEN' in k or 'PASSWORD' in k or 'SECRET' in k:
                    safe[k] = f'{"*" * min(len(v), 6)}... (length={len(v)})' if v else '(empty)'
                else:
                    safe[k] = v
            result['parsed_keys'] = safe
            result['token_key_found'] = any(k in parsed for k in ('GITLAB_TOKEN', 'TOKEN'))
            result['base_url_key_found'] = any(k in parsed for k in ('BASE_URL', 'GITLAB_BASE_URL', 'GITLAB_URL'))
        except Exception as ex:
            result['read_error'] = str(ex)
    return jsonify(result)


@app.route('/api/config/reload', methods=['POST'])
def config_reload():
    global PROJECT_NAMES
    try:
        new_env = ms.load_env_config()
        ms.BASE_URL = new_env['base_url']
        ms.TOKEN = new_env['token']
        ms.NEW_DEFAULT_PLATFORM = new_env['new_default_platform']
        ms.REVIEWER_USERNAMES = new_env['reviewer_usernames']
        ms.ASSIGNEE_USERNAMES = new_env['assignee_usernames']
        PROJECT_NAMES = new_env['projects']
        _ssl = new_env.get('ssl_verify')
        if _ssl is None:
            _ssl = True
        ms.SSL_VERIFY = _ssl
        ms.setup_http_session(ssl_verify=_ssl)

        token_info = {'valid': False, 'reason': 'unknown'}
        if not ms.TOKEN or ms.TOKEN.strip() == '':
            token_info['reason'] = 'no_token'
        elif not ms.BASE_URL or ms.BASE_URL.strip() == '':
            token_info['reason'] = 'no_base_url'
        elif ms.HTTP_SESSION is None:
            token_info['reason'] = 'session_not_initialised'
        else:
            try:
                user_resp = ms.api_call('user')
                if isinstance(user_resp, dict) and not user_resp.get('error'):
                    token_info['valid'] = True
                    token_info['reason'] = 'ok'
                    try:
                        token_resp = ms.api_call('personal_access_tokens/self')
                        if isinstance(token_resp, dict) and 'expires_at' in token_resp:
                            token_info['expires_at'] = token_resp['expires_at']
                    except Exception:
                        pass
                else:
                    err_detail = user_resp.get('details', '') if isinstance(user_resp, dict) else ''
                    if '401' in err_detail or 'Unauthorized' in err_detail:
                        token_info['reason'] = 'invalid'
                    elif '404' in err_detail or 'Not Found' in err_detail:
                        token_info['reason'] = 'wrong_base_url'
                    else:
                        token_info['reason'] = 'invalid'
            except Exception as _e:
                _emsg = str(_e)
                if 'NoneType' in _emsg or 'session' in _emsg.lower():
                    token_info['reason'] = 'session_not_initialised'
                else:
                    token_info['reason'] = 'connection_error'

        import concurrent.futures
        def _fetch_project_path(pid_pname):
            pid, pname = pid_pname
            try:
                proj_resp = ms.api_call(f"projects/{pid}")
                if isinstance(proj_resp, dict) and not proj_resp.get('error'):
                    path = proj_resp.get('path_with_namespace') or pname
                else:
                    path = pname
            except Exception:
                path = pname
            return {'id': pid, 'name': pname, 'path_with_namespace': path}

        projects_list = []
        if PROJECT_NAMES:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(10, len(PROJECT_NAMES))) as ex:
                projects_list = list(ex.map(_fetch_project_path, PROJECT_NAMES.items()))

        print(f"[INFO] /api/config/reload: {len(PROJECT_NAMES)} projects, token_valid={token_info['valid']}")
        return jsonify({
            'success': True,
            'projects': projects_list,
            'base_url': ms.BASE_URL,
            'assignees': ms.ASSIGNEE_USERNAMES,
            'reviewers': ms.REVIEWER_USERNAMES,
            'new_default_platform': ms.NEW_DEFAULT_PLATFORM,
            'token_info': token_info,
            'presto_project_ids': env_config.get('presto_project_ids', []),
        })
    except Exception as e:
        print(f"[ERROR] /api/config/reload error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<int:project_id>/cleanup', methods=['POST'])
def cleanup_project(project_id):
    try:
        data = request.json or {}
        branch_num = data.get('branch_num', '12938')
        feature_branch = _build_feature_branch(branch_num, data.get("branch_prefix","task-"), data.get("branch_suffix","java17-migration"))
        branch_result = ms.api_call(
            f"projects/{project_id}/repository/branches/{urllib.parse.quote(feature_branch, safe='')}",
            "DELETE"
        )
        branch_deleted = branch_result is None or (isinstance(branch_result, dict) and not branch_result.get('error'))
        removed = pipeline_status.pop(str(project_id), None)
        if removed is not None:
            save_pipeline_state()
        print(f"[CLEANUP] project {project_id}: branch_deleted={branch_deleted}, status_removed={removed is not None}")
        return jsonify({'success': True, 'branch_deleted': branch_deleted, 'status_removed': removed is not None})
    except Exception as e:
        print(f"[ERROR] cleanup_project error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


def _resolve_health_url(base_url: str) -> str:
    u = base_url.rstrip('/')
    if u.endswith('/health'):
        return u
    if u.endswith('/actuator'):
        return u + '/health'
    return u + '/actuator/health'


def _call_health_url(url: str) -> dict:
    import requests as _req

    def _try(u):
        resp = _req.get(u, timeout=8, verify=ms.SSL_VERIFY)
        return resp

    tried = [url]
    resp = _try(url)

    if resp.status_code == 404:
        if '/actuator/health' in url:
            alt = url.replace('/actuator/health', '/health')
        elif url.endswith('/health'):
            alt = url[:-len('/health')] + '/actuator/health'
        else:
            alt = None
        if alt and alt not in tried:
            tried.append(alt)
            resp = _try(alt)

    if resp.status_code == 404:
        return {
            'status': 'DOWN',
            'detail': f'404 Not Found — tried: {", ".join(tried)}. Check your ACTUATOR URL in .env',
            'url': tried[0], 'raw': {}
        }

    try:
        data = resp.json() if resp.text else {}
    except Exception:
        data = {}

    status = data.get('status', 'UNKNOWN').upper()
    components = data.get('components', {})
    detail_parts = [f"HTTP {resp.status_code}", f"status={status}"]
    if components:
        for comp, info in list(components.items())[:4]:
            detail_parts.append(f"{comp}={info.get('status','?')}")

    return {
        'status': 'UP' if status == 'UP' else 'DOWN',
        'detail': ' · '.join(detail_parts),
        'url': resp.url, 'raw': data
    }


@app.route('/api/projects/<int:project_id>/health', methods=['GET'])
def check_actuator_health(project_id):
    """Check Spring Boot Actuator health."""
    import requests as _req
    env = request.args.get('env', '').lower()
    pid_str = str(project_id)
    try:
        urls_dev   = env_config.get('actuator_urls_dev',  {})
        urls_test  = env_config.get('actuator_urls_test', {})
        urls_perf  = env_config.get('actuator_urls_perf', {})
        urls_plain = env_config.get('actuator_urls',      {})

        if env == 'dev':
            actuator_url = urls_dev.get(pid_str) or urls_plain.get(pid_str)
            env_label, key_hint = 'DEV', f'ACTUATOR_{project_id}_DEV'
        elif env == 'test':
            actuator_url = urls_test.get(pid_str) or urls_plain.get(pid_str)
            env_label, key_hint = 'TEST', f'ACTUATOR_{project_id}_TEST'
        elif env == 'perf':
            actuator_url = urls_perf.get(pid_str)
            env_label, key_hint = 'PERF', f'ACTUATOR_{project_id}_PERF'
        else:
            actuator_url = urls_plain.get(pid_str) or urls_dev.get(pid_str) or urls_test.get(pid_str)
            env_label, key_hint = '', f'ACTUATOR_{project_id}'

        if not actuator_url:
            return jsonify({'status': 'unconfigured', 'detail': f'No {key_hint} in .env', 'url': None, 'env': env_label})

        health_url = _resolve_health_url(actuator_url)
        try:
            result = _call_health_url(health_url)
            result['env'] = env_label
            return jsonify(result)
        except _req.exceptions.Timeout:
            return jsonify({'status': 'DOWN', 'detail': f'Timeout: {health_url}', 'url': health_url, 'env': env_label})
        except _req.exceptions.ConnectionError as ce:
            return jsonify({'status': 'DOWN', 'detail': f'Connection refused: {health_url}', 'url': health_url, 'env': env_label})
    except Exception as e:
        return jsonify({'status': 'unknown', 'detail': str(e), 'url': None, 'env': ''}), 500


@app.route('/api/projects/<int:project_id>/health/all', methods=['GET'])
def check_actuator_health_all(project_id):
    """Check DEV + TEST health simultaneously."""
    import requests as _req
    import concurrent.futures
    pid_str = str(project_id)
    urls_dev   = env_config.get('actuator_urls_dev',  {})
    urls_test  = env_config.get('actuator_urls_test', {})
    urls_perf  = env_config.get('actuator_urls_perf', {})
    urls_plain = env_config.get('actuator_urls',      {})

    def _check(url, label):
        if not url:
            return {'status': 'unconfigured', 'detail': f'No ACTUATOR_{project_id}_{label} in .env', 'env': label}
        health_url = _resolve_health_url(url)
        try:
            result = _call_health_url(health_url)
            result['env'] = label
            return result
        except _req.exceptions.Timeout:
            return {'status': 'DOWN', 'detail': f'Timeout', 'env': label}
        except Exception as ex:
            return {'status': 'DOWN', 'detail': str(ex), 'env': label}

    url_dev  = urls_dev.get(pid_str)  or urls_plain.get(pid_str)
    url_test = urls_test.get(pid_str) or urls_plain.get(pid_str)
    url_perf = urls_perf.get(pid_str)

    workers = 3 if url_perf else 2
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        fd = ex.submit(_check, url_dev,  'DEV')
        ft = ex.submit(_check, url_test, 'TEST')
        fp = ex.submit(_check, url_perf, 'PERF') if url_perf else None
    result = {'dev': fd.result(), 'test': ft.result()}
    if fp is not None:
        result['perf'] = fp.result()
    return jsonify(result)


@app.route('/api/projects/<int:project_id>/perf', methods=['GET'])
def check_perf_endpoint(project_id):
    """Check a custom /perf endpoint for a project."""
    import requests as _req
    env = request.args.get('env', '').lower()
    pid_str = str(project_id)
    try:
        urls_dev   = env_config.get('perf_urls_dev',  {})
        urls_test  = env_config.get('perf_urls_test', {})
        urls_plain = env_config.get('perf_urls',      {})

        if env == 'dev':
            perf_url = urls_dev.get(pid_str) or urls_plain.get(pid_str)
            env_label, key_hint = 'DEV', f'PERF_{project_id}_DEV'
        elif env == 'test':
            perf_url = urls_test.get(pid_str) or urls_plain.get(pid_str)
            env_label, key_hint = 'TEST', f'PERF_{project_id}_TEST'
        else:
            perf_url = urls_plain.get(pid_str) or urls_dev.get(pid_str) or urls_test.get(pid_str)
            env_label, key_hint = '', f'PERF_{project_id}'

        if not perf_url:
            return jsonify({'status': 'unconfigured', 'detail': f'No {key_hint} in .env', 'url': None, 'env': env_label})

        resolved = perf_url.rstrip('/')
        if not resolved.endswith('/perf'):
            resolved = resolved + '/perf'

        try:
            import requests as _r
            resp = _r.get(resolved, timeout=8, verify=ms.SSL_VERIFY)
            try:
                data = resp.json() if resp.text else {}
            except Exception:
                data = {}
            status = data.get('status', 'UP' if resp.status_code < 400 else 'DOWN').upper()
            detail_parts = [f"HTTP {resp.status_code}"]
            if isinstance(data, dict):
                for k in list(data.keys())[:5]:
                    detail_parts.append(f"{k}={data[k]}")
            return jsonify({
                'status': 'UP' if resp.status_code < 400 else 'DOWN',
                'detail': ' · '.join(detail_parts),
                'url': resolved, 'env': env_label, 'raw': data
            })
        except _req.exceptions.Timeout:
            return jsonify({'status': 'DOWN', 'detail': f'Timeout: {resolved}', 'url': resolved, 'env': env_label})
        except _req.exceptions.ConnectionError:
            return jsonify({'status': 'DOWN', 'detail': f'Connection refused: {resolved}', 'url': resolved, 'env': env_label})
    except Exception as e:
        return jsonify({'status': 'unknown', 'detail': str(e), 'url': None, 'env': ''}), 500


@app.route('/api/projects/<int:project_id>/branch-reset', methods=['POST'])
def branch_reset(project_id):
    """Delete the feature branch and recreate it fresh from develop/master/main."""
    try:
        data = request.json or {}
        branch_num    = data.get('branch_num', '12938')
        branch_prefix = data.get('branch_prefix', 'task-')
        branch_suffix = data.get('branch_suffix', 'java17-migration')
        feature_branch = _build_feature_branch(branch_num, branch_prefix, branch_suffix)

        source_branch = _resolve_source_branch(project_id)

        print(f"[BRANCH-RESET] project {project_id}: deleting '{feature_branch}', will recreate from '{source_branch}'")

        del_result = ms.api_call(
            f"projects/{project_id}/repository/branches/{urllib.parse.quote(feature_branch, safe='')}",
            "DELETE"
        )
        deleted_ok = del_result is None or (isinstance(del_result, dict) and not del_result.get('error'))
        print(f"[BRANCH-RESET] delete result: {del_result}")

        create_result = ms.api_call(
            f"projects/{project_id}/repository/branches",
            "POST",
            data={'branch': feature_branch, 'ref': source_branch}
        )
        if isinstance(create_result, dict) and create_result.get('error'):
            return jsonify({
                'success': False,
                'error': f"Branch recreated failed: {create_result.get('details', 'unknown')}",
                'branch': feature_branch,
                'source': source_branch
            }), 500

        pipeline_status.pop(str(project_id), None)
        save_pipeline_state()

        real_name = PROJECT_NAMES.get(project_id) or PROJECT_NAMES.get(str(project_id)) or f"project-{project_id}"
        add_project_to_history(project_id, real_name, 'branch_reset', 'success',
                               f"Branch {feature_branch} deleted and recreated from {source_branch}")

        return jsonify({
            'success': True,
            'branch': feature_branch,
            'source': source_branch,
            'detail': f"'{feature_branch}' deleted and recreated fresh from '{source_branch}'"
        })
    except Exception as e:
        print(f"[ERROR] branch_reset error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/branches/delete-all', methods=['POST'])
def delete_all_branches():
    """Delete the feature branch for every supplied project ID (no recreate)."""
    try:
        data          = request.json or {}
        project_ids   = data.get('project_ids', [])
        branch_num    = data.get('branch_num',    '12938')
        branch_prefix = data.get('branch_prefix', 'task-')
        branch_suffix = data.get('branch_suffix', 'java17-migration')
        feature_branch = _build_feature_branch(branch_num, branch_prefix, branch_suffix)

        if not project_ids:
            return jsonify({'success': False, 'error': 'No project_ids supplied'}), 400

        results = []
        for pid in project_ids:
            try:
                pid_int = int(pid)
                branch_enc = urllib.parse.quote(feature_branch, safe='')

                # Check if branch exists first
                br_check = ms.api_call(f"projects/{pid_int}/repository/branches/{branch_enc}")
                branch_exists = isinstance(br_check, dict) and 'name' in br_check

                if not branch_exists:
                    results.append({'project_id': pid_int, 'success': False,
                                    'not_found': True,
                                    'detail': f"Branch '{feature_branch}' does not exist"})
                    continue

                del_res = ms.api_call(
                    f"projects/{pid_int}/repository/branches/{branch_enc}",
                    "DELETE"
                )
                ok = del_res is None or (isinstance(del_res, dict) and not del_res.get('error'))
                pipeline_status.pop(str(pid_int), None)
                real_name_del = PROJECT_NAMES.get(pid_int) or PROJECT_NAMES.get(str(pid_int)) or f"project-{pid_int}"
                add_project_to_history(pid_int, real_name_del, 'branch_delete', 'success' if ok else 'failed',
                                       f"Branch {feature_branch} deleted" if ok else del_res.get('details','unknown'))
                results.append({'project_id': pid_int, 'success': ok,
                                 'not_found': False,
                                 'detail': f"'{feature_branch}' deleted" if ok else 'Delete failed'})
            except Exception as ex:
                results.append({'project_id': pid, 'success': False, 'not_found': False, 'detail': str(ex)})

        save_pipeline_state()
        succeeded  = sum(1 for r in results if r['success'])
        not_found  = sum(1 for r in results if r.get('not_found'))
        return jsonify({
            'success':   True,
            'branch':    feature_branch,
            'deleted':   succeeded,
            'not_found': not_found,
            'total':     len(results),
            'results':   results
        })
    except Exception as e:
        print(f"[ERROR] delete_all_branches: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/parent-pom-version', methods=['GET'])
def get_parent_pom_version():
    """Fetch the version of the custom parent pom from its own GitLab repository."""
    def _extract_project_version(pom_xml: str):
        stripped = re.sub(r'<parent>[\s\S]*?</parent>', '', pom_xml, flags=re.IGNORECASE)
        m = re.search(r'<version>\s*(.*?)\s*</version>', stripped, re.IGNORECASE)
        return m.group(1).strip() if m else None

    try:
        parent_pom_project_id = env_config.get('parent_pom_project_id')

        if not parent_pom_project_id:
            return jsonify({
                'version': ms.TARGET_PARENT_VERSION,
                'source': 'fallback',
                'detail': 'No PARENT_POM_PROJECT_ID in .env — using hardcoded default 1.8.3',
                'project_id': None
            })

        pom_content = None
        used_ref = None
        for ref in ('master', 'main', 'develop'):
            pom_res = ms.api_call(
                f"projects/{parent_pom_project_id}/repository/files/{urllib.parse.quote('pom.xml', safe='')}?ref={ref}"
            )
            if isinstance(pom_res, dict) and 'content' in pom_res:
                pom_content = base64.b64decode(pom_res['content']).decode('utf-8', errors='replace')
                used_ref = ref
                break

        if not pom_content:
            return jsonify({
                'version': ms.TARGET_PARENT_VERSION,
                'source': 'fallback',
                'detail': f'Could not fetch pom.xml from project {parent_pom_project_id} — check PARENT_POM_PROJECT_ID',
                'project_id': parent_pom_project_id
            })

        version = _extract_project_version(pom_content)
        if version:
            ms.TARGET_PARENT_VERSION = version
            return jsonify({
                'version': version,
                'source': 'repo',
                'detail': f'Project version from parent pom repo (id={parent_pom_project_id}, ref={used_ref})',
                'project_id': parent_pom_project_id
            })
        else:
            return jsonify({
                'version': ms.TARGET_PARENT_VERSION,
                'source': 'fallback',
                'detail': f'No top-level <version> found in pom.xml (project {parent_pom_project_id})',
                'project_id': parent_pom_project_id
            })
    except Exception as e:
        return jsonify({'version': ms.TARGET_PARENT_VERSION, 'source': 'fallback', 'detail': str(e), 'project_id': None}), 500


@app.route('/api/projects/detect-presto', methods=['POST'])
def detect_presto_projects():
    """
    Scan every loaded project and check whether src/main/resources/presto.jks
    exists on the specified branch.  Returns lists: detected, not_found, errors.
    """
    try:
        data = request.json or {}
        branch = data.get('branch', ms.SOURCE_BRANCH) or ms.SOURCE_BRANCH
        PRESTO_JKS_PATH = urllib.parse.quote("src/main/resources/presto.jks", safe='')

        import concurrent.futures

        def _check_one(pid_pname):
            pid, pname = pid_pname
            try:
                resp = ms.api_call(
                    f"projects/{pid}/repository/files/{PRESTO_JKS_PATH}?ref={urllib.parse.quote(branch, safe='')}"
                )
                if isinstance(resp, dict) and 'file_name' in resp:
                    return ('detected', pid, pname)
                return ('not_found', pid, pname)
            except Exception:
                return ('error', pid, pname)

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(10, max(1, len(PROJECT_NAMES)))) as ex:
            results = list(ex.map(_check_one, PROJECT_NAMES.items()))

        detected_list, not_found_list, errors_list = [], [], []
        for status, pid, pname in results:
            entry = {'id': pid, 'name': pname}
            if status == 'detected':
                detected_list.append(entry)
            elif status == 'not_found':
                not_found_list.append(entry)
            else:
                errors_list.append(entry)

        print(f"[PRESTO-DETECT] branch={branch} detected={len(detected_list)} not_found={len(not_found_list)} errors={len(errors_list)}")
        return jsonify({'success': True, 'branch': branch,
                        'detected': detected_list, 'not_found': not_found_list, 'errors': errors_list})
    except Exception as e:
        print(f"[ERROR] detect-presto: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500



# ── Bulk-cert orchestrator cancel handle ──────────────────────────────────────
# Only ONE bulk-cert run is allowed at a time.  A new run first cancels any
# existing one, then replaces this with a fresh Event.
_bulk_cert_cancel_ev: threading.Event = threading.Event()
_bulk_cert_cancel_ev.set()   # starts in "not running" state (set = idle)


@app.route('/api/bulk-cert/cancel', methods=['POST'])
def cancel_bulk_cert():
    """Gracefully stop the running bulk-cert orchestrator after its current project finishes."""
    _bulk_cert_cancel_ev.set()
    print("[BULK-CERT] Cancel requested by user")
    return jsonify({'success': True, 'message': 'Cancellation signal sent — current project will finish before stopping'})


def _run_cert_pipeline_full(pid, tid, fb, bn, settings, cancel_ev):
    """
    Full Commit → Build → Dev-Deploy pipeline for a single Presto project.

    Stages written to active_tasks[tid]['stage']:
        queued  → committing → building → deploying → success
                                                     → failed (at any stage)
                                                     → cancelled
                                                     → idempotent (cert unchanged)
    """
    cert_base64     = settings['cert_base64']
    target_filename = settings['target_filename']
    target_path     = settings['target_path']
    commit_msg      = settings['commit_msg']
    create_mr       = settings['create_mr']
    deploy_dev      = settings['deploy_dev']
    dev_tag_name    = settings['dev_tag_name']
    _PROTECTED      = {'develop', 'master', 'main'}
    CHECK_INTERVAL  = 30   # seconds between API polls

    def _log(level, msg):
        active_tasks[tid]['logs'].append({
            'level': level, 'message': msg, 'timestamp': datetime.now().isoformat()
        })

    def _fail(msg, stage_label=None):
        _log('ERROR', msg)
        active_tasks[tid]['status'] = 'failed'
        active_tasks[tid]['stage']  = 'failed'

    def _set_stage(s):
        active_tasks[tid]['stage'] = s

    try:
        # ── Resolve project ───────────────────────────────────────────────
        proj   = ms.api_call(f"projects/{pid}")
        p_name = proj.get('name', f"ID:{pid}") if isinstance(proj, dict) else f"ID:{pid}"
        p_path = (proj.get('path_with_namespace') or p_name) if isinstance(proj, dict) else p_name
        p_web  = f"{ms.BASE_URL.rstrip('/')}/{p_path}"

        # ══════════════════════════════════════════════════════════════════
        # STAGE 1 — COMMIT
        # ══════════════════════════════════════════════════════════════════
        _set_stage('committing')
        _log('INFO', f'[{p_name}] Starting cert pipeline...')

        if cancel_ev.is_set():
            _log('WARN', 'Cancelled before commit')
            active_tasks[tid]['status'] = 'cancelled'
            active_tasks[tid]['stage']  = 'cancelled'
            return

        if fb.lower() in _PROTECTED:
            return _fail(f'Refused: branch "{fb}" is protected')

        file_path_in_repo = f"{target_path}/{target_filename}"
        file_path_encoded = urllib.parse.quote(file_path_in_repo, safe='')

        try:
            cert_bytes = base64.b64decode(cert_base64)
        except Exception:
            return _fail('Invalid base64 cert data')

        # Ensure feature branch exists
        br = ms.api_call(f"projects/{pid}/repository/branches/{urllib.parse.quote(fb, safe='')}")
        if not (isinstance(br, dict) and 'name' in br):
            _log('INFO', f'Creating branch {fb}...')
            with _ms_globals_lock:
                ms.FEATURE_BRANCH = fb
                ms.JIRA_ID        = bn
                ms.create_feature_branch(pid, p_name)
            br = ms.api_call(f"projects/{pid}/repository/branches/{urllib.parse.quote(fb, safe='')}")
            if not (isinstance(br, dict) and 'name' in br):
                return _fail(f'Could not create branch {fb}')

        # Idempotency: skip if cert already matches
        existing = ms.api_call(
            f"projects/{pid}/repository/files/{file_path_encoded}"
            f"?ref={urllib.parse.quote(fb, safe='')}"
        )
        if isinstance(existing, dict) and 'content' in existing:
            try:
                if base64.b64decode(existing['content']) == cert_bytes:
                    _log('INFO', '⚠️ Certificate already matches on feature branch — no change needed')
                    active_tasks[tid]['status'] = 'idempotent'
                    active_tasks[tid]['stage']  = 'success'
                    update_pipeline_status(pid, 'success', workflow_stage='committed', path_with_namespace=p_path)
                    return
            except Exception:
                pass
            action = 'update'
        else:
            # Verify it exists on source branch (can't replace what isn't there)
            src = ms.api_call(
                f"projects/{pid}/repository/files/{file_path_encoded}"
                f"?ref={urllib.parse.quote(ms.SOURCE_BRANCH, safe='')}"
            )
            if not (isinstance(src, dict) and 'content' in src):
                return _fail(f'presto.jks not found on {ms.SOURCE_BRANCH} — cannot replace a non-existent file')
            action = 'update'

        # Commit
        commit_resp = ms.api_call(
            f"projects/{pid}/repository/commits", 'POST',
            {
                'branch': fb, 'commit_message': commit_msg,
                'actions': [{'action': action, 'file_path': file_path_in_repo,
                             'content': cert_base64, 'encoding': 'base64'}]
            }
        )
        if not (isinstance(commit_resp, dict) and not commit_resp.get('error')):
            err = commit_resp.get('details', 'unknown') if isinstance(commit_resp, dict) else str(commit_resp)
            return _fail(f'Commit failed: {err}')

        commit_sha = commit_resp.get('id', '')
        _log('SUCCESS', f'✅ Cert committed — SHA: {commit_sha[:8]} | Branch: {fb}')
        active_tasks[tid]['commit_sha'] = commit_sha
        add_project_to_history(pid, p_name, 'cert_replace', 'success',
                               {'file': file_path_in_repo, 'branch': fb})

        if create_mr:
            _log('INFO', 'Creating MR...')
            with _ms_globals_lock:
                ms.FEATURE_BRANCH = fb
                ms.JIRA_ID        = bn
                mr_r = ms.create_mr_for_project(pid, p_name, {'snapshots': []})
            if mr_r.get('success'):
                _log('SUCCESS', f'🔀 MR created: {mr_r.get("url", "")}')
            else:
                _log('WARN', 'MR creation failed — continuing with build')

        # If no dev deploy requested, we're done here
        if not deploy_dev:
            active_tasks[tid]['status'] = 'success'
            active_tasks[tid]['stage']  = 'success'
            update_pipeline_status(pid, 'success', workflow_stage='committed',
                                   commit_sha=commit_sha, path_with_namespace=p_path)
            return

        # ══════════════════════════════════════════════════════════════════
        # STAGE 2 — WAIT FOR CI BUILD
        # ══════════════════════════════════════════════════════════════════
        _set_stage('building')
        _log('INFO', '⏳ Waiting for CI build pipeline to appear on GitLab...')
        time.sleep(12)   # give GitLab time to schedule the pipeline

        build_pipeline = None
        for attempt in range(18):   # up to 6 min (18 × 20 s)
            if cancel_ev.is_set():
                _log('WARN', 'Cancelled while waiting for build pipeline')
                active_tasks[tid]['status'] = 'cancelled'
                active_tasks[tid]['stage']  = 'cancelled'
                return
            build_pipeline = ms.get_pipeline_for_commit(pid, commit_sha)
            if build_pipeline:
                break
            _log('INFO', f'Pipeline not visible yet ({(attempt + 1) * 20}s elapsed)...')
            time.sleep(20)

        if not build_pipeline:
            return _fail('❌ Build pipeline not found after 6 min — check GitLab CI configuration for this project')

        build_pl_id  = build_pipeline.get('id')
        build_pl_url = f"{p_web}/-/pipelines/{build_pl_id}"
        _log('INFO', f'🏗️ Build pipeline #{build_pl_id} found — monitoring... ({build_pl_url})')
        active_tasks[tid]['pipeline_id'] = build_pl_id
        update_pipeline_status(pid, 'running', pipeline_id=build_pl_id,
                               commit_sha=commit_sha, path_with_namespace=p_path)

        BUILD_DEADLINE = time.time() + 3600   # 1-hour safety cap
        last_build_status = None

        while True:
            if cancel_ev.is_set():
                _log('WARN', 'Cancelled during CI build')
                active_tasks[tid]['status'] = 'cancelled'
                active_tasks[tid]['stage']  = 'cancelled'
                return
            if time.time() >= BUILD_DEADLINE:
                return _fail(f'❌ Build pipeline #{build_pl_id} timed out after 60 min')

            pl_resp = ms.api_call(f"projects/{pid}/pipelines/{build_pl_id}")
            if isinstance(pl_resp, dict) and not pl_resp.get('error'):
                pl_status = pl_resp.get('status', 'unknown')
                if pl_status != last_build_status:
                    _log('INFO', f'🔄 Build pipeline #{build_pl_id} → {pl_status}')
                    last_build_status = pl_status
                if pl_status in ('success', 'failed', 'canceled', 'skipped'):
                    if pl_status != 'success':
                        update_pipeline_status(pid, 'failed', pipeline_id=build_pl_id,
                                               commit_sha=commit_sha, path_with_namespace=p_path)
                        return _fail(f'❌ Build pipeline ended with: {pl_status}')
                    break
            time.sleep(CHECK_INTERVAL)

        _log('SUCCESS', f'✅ Build pipeline #{build_pl_id} passed!')
        update_pipeline_status(pid, 'success', pipeline_id=build_pl_id, commit_sha=commit_sha,
                               workflow_stage='pipeline_success', path_with_namespace=p_path)

        # ══════════════════════════════════════════════════════════════════
        # STAGE 3 — DEPLOY TO DEV
        # ══════════════════════════════════════════════════════════════════
        _set_stage('deploying')
        _log('INFO', f'🚀 Starting dev deployment with tag "{dev_tag_name}"...')

        if cancel_ev.is_set():
            _log('WARN', 'Cancelled before deploy')
            active_tasks[tid]['status'] = 'cancelled'
            active_tasks[tid]['stage']  = 'cancelled'
            return

        # Resolve feature branch HEAD
        fb_info      = ms.api_call(f"projects/{pid}/repository/branches/{urllib.parse.quote(fb, safe='')}")
        feature_head = None
        if isinstance(fb_info, dict) and not fb_info.get('error'):
            feature_head = (fb_info.get('commit') or {}).get('id')
        if not feature_head:
            return _fail(f'Could not resolve HEAD of branch {fb}')

        # Delete or re-use existing dev tag
        quoted_tag   = urllib.parse.quote(dev_tag_name, safe='')
        existing_tag = ms.api_call(f"projects/{pid}/repository/tags/{quoted_tag}")
        if isinstance(existing_tag, dict) and 'name' in existing_tag:
            if existing_tag.get('protected'):
                return _fail(f'Tag "{dev_tag_name}" is protected — cannot recreate it')
            old_sha = (existing_tag.get('commit') or {}).get('id', '')
            if old_sha == feature_head:
                _log('INFO', f'[IDEMPOTENT] "{dev_tag_name}" already points to {feature_head[:8]} — no recreation needed')
            else:
                _log('INFO', f'Deleting existing "{dev_tag_name}" (was at {old_sha[:8]})...')
                ms.api_call(f"projects/{pid}/repository/tags/{quoted_tag}", 'DELETE')

        # Create dev tag on feature branch
        _log('INFO', f'Creating tag "{dev_tag_name}" → {fb} ({feature_head[:8]})...')
        tag_resp = ms.api_call(
            f"projects/{pid}/repository/tags", 'POST',
            {'tag_name': dev_tag_name, 'ref': fb}
        )
        if not (isinstance(tag_resp, dict) and not tag_resp.get('error')):
            err = tag_resp.get('details', 'unknown') if isinstance(tag_resp, dict) else str(tag_resp)
            return _fail(f'Tag creation failed: {err}')

        tag_commit = (tag_resp.get('commit') or {}).get('id', feature_head)
        _log('INFO', f'Tag "{dev_tag_name}" created at {tag_commit[:8]}')

        # Wait for tag pipeline
        _log('INFO', 'Waiting 12 s for GitLab to trigger the tag pipeline...')
        time.sleep(12)

        tag_pipeline = None
        for attempt in range(9):   # up to extra 3 min (9 × 20 s + 12 s already waited)
            tag_pipeline = ms.get_pipeline_for_commit(pid, tag_commit)
            if tag_pipeline:
                break
            _log('INFO', f'Waiting for tag pipeline ({(attempt + 1) * 20 + 12}s total)...')
            time.sleep(20)

        if not tag_pipeline:
            _log('WARN', '⚠️ Tag pipeline not found — cert committed and tag created but deploy could not be verified')
            active_tasks[tid]['status'] = 'partial_success'
            active_tasks[tid]['stage']  = 'success'
            return

        tag_pl_id  = tag_pipeline.get('id')
        tag_pl_url = f"{p_web}/-/pipelines/{tag_pl_id}"
        _log('INFO', f'Tag pipeline #{tag_pl_id} found — waiting for jobs to become available... ({tag_pl_url})')

        # Wait for the tag pipeline to either succeed or have manual jobs ready
        TAG_DEADLINE     = time.time() + 1800   # 30 min
        last_tag_status  = None
        deploy_job_name  = ms.map_tag_to_deploy_job(dev_tag_name)

        while True:
            if cancel_ev.is_set():
                _log('WARN', 'Cancelled during tag pipeline')
                active_tasks[tid]['status'] = 'cancelled'
                active_tasks[tid]['stage']  = 'cancelled'
                return
            if time.time() >= TAG_DEADLINE:
                return _fail('Tag pipeline timed out after 30 min')

            pl_resp = ms.api_call(f"projects/{pid}/pipelines/{tag_pl_id}")
            if isinstance(pl_resp, dict) and not pl_resp.get('error'):
                pl_st = pl_resp.get('status', 'unknown')
                if pl_st != last_tag_status:
                    _log('INFO', f'🔄 Tag pipeline #{tag_pl_id} → {pl_st}')
                    last_tag_status = pl_st

                if pl_st in ('success', 'failed', 'canceled', 'skipped'):
                    break   # pipeline finished — grab jobs below

                # Pipeline still running but deploy job may already be manual
                if deploy_job_name:
                    jobs_peek = ms.get_pipeline_jobs(pid, tag_pl_id)
                    dj_peek   = ms.find_job_by_name(jobs_peek, deploy_job_name)
                    if dj_peek and dj_peek.get('status') == 'manual':
                        _log('INFO', f'Deploy job "{deploy_job_name}" is ready (manual) — proceeding')
                        break

            time.sleep(CHECK_INTERVAL)

        # Fetch final job list
        jobs = ms.get_pipeline_jobs(pid, tag_pl_id)
        _log('INFO', f'Pipeline jobs: {[j.get("name") for j in jobs if isinstance(j, dict)]}')

        # Trigger eb-terminate if present and manual
        terminate_job = ms.find_job_by_name(jobs, 'eb-terminate')
        if terminate_job and terminate_job.get('status') == 'manual':
            env_name = dev_tag_name.lower().replace('azure-', '')
            _log('INFO', f"Triggering 'eb-terminate' (env={env_name})...")
            tr = ms.api_call(
                f"projects/{pid}/jobs/{terminate_job['id']}/play", 'POST',
                {'variables': [{'key': 'ENVIRONMENT', 'value': env_name},
                               {'key': 'EB_ENV',      'value': env_name}]}
            )
            if isinstance(tr, dict) and tr.get('error'):
                ms.trigger_manual_job(pid, terminate_job['id'])   # fallback: play without vars
            term_res = ms.wait_for_job_completion(pid, terminate_job['id'], timeout=600)
            if term_res.get('status') == 'success':
                _log('INFO', 'eb-terminate succeeded ✓')
            else:
                _log('WARN', f"eb-terminate ended with: {term_res.get('status')} — environment may not exist yet; proceeding anyway")

        # Trigger the actual deploy job
        if not deploy_job_name:
            _log('WARN', f'No deploy job mapped for tag "{dev_tag_name}" — skipping deploy trigger')
            active_tasks[tid]['status'] = 'success'
            active_tasks[tid]['stage']  = 'success'
            return

        deploy_job = ms.find_job_by_name(jobs, deploy_job_name)
        if not deploy_job:
            return _fail(f'Deploy job "{deploy_job_name}" not found in tag pipeline #{tag_pl_id}')

        dj_status = deploy_job.get('status')
        if dj_status != 'manual':
            _log('INFO', f'Deploy job "{deploy_job_name}" is already in status "{dj_status}" — no trigger needed')
            active_tasks[tid]['status'] = 'success'
            active_tasks[tid]['stage']  = 'success'
            return

        _log('INFO', f"Triggering '{deploy_job_name}'... ({p_web}/-/jobs/{deploy_job['id']})")
        ms.trigger_manual_job(pid, deploy_job['id'])

        dep_res = ms.wait_for_job_completion(pid, deploy_job['id'], timeout=1800)

        if dep_res.get('status') == 'success':
            _log('SUCCESS', f'✅ Dev deployment complete for {p_name}!')
            update_pipeline_status(pid, 'success', workflow_stage='deployed', path_with_namespace=p_path)
            active_tasks[tid]['status'] = 'success'
            active_tasks[tid]['stage']  = 'success'
            add_project_to_history(pid, p_name, 'deploy', 'success', {'tags': [dev_tag_name]})
        else:
            return _fail(f'❌ Deploy job "{deploy_job_name}" ended with: {dep_res.get("status")}')

    except Exception as exc:
        active_tasks[tid]['logs'].append({
            'level': 'ERROR', 'message': f'Exception: {exc}',
            'timestamp': datetime.now().isoformat()
        })
        active_tasks[tid]['status'] = 'failed'
        active_tasks[tid]['stage']  = 'failed'
        print(f"[BULK-CERT] Exception in cert pipeline for project {pid}: {exc}")
    finally:
        _cleanup_active_tasks()


@app.route('/api/projects/bulk-cert-replace', methods=['POST'])
def bulk_cert_replace():
    """
    Orchestrated bulk cert replacement.

    Projects are processed in batches of `batch_size` (default 1 = fully sequential).
    Each project goes through the full pipeline:
        Commit → Wait CI build → Create dev tag → eb-terminate → eb-deploy-dev-azure → Wait deploy

    The next batch only starts once every project in the current batch has fully
    completed.  This caps the number of simultaneous GitLab pipelines to `batch_size`,
    preventing runner saturation.

    Request body:
        project_ids      list[int]  — projects to process (in order)
        cert_base64      str        — base64-encoded certificate bytes
        target_filename  str        — filename to replace (default: presto.jks)
        target_path      str        — path in repo (default: src/main/resources)
        commit_message   str        — git commit message
        branch_num       str
        branch_prefix    str
        branch_suffix    str
        create_mr        bool       — create MR after commit (default false)
        deploy_dev       bool       — run full build+deploy pipeline (default true)
        dev_tag_name     str        — tag to create for dev deploy (default: azure-dev)
        batch_size       int 1-3    — concurrent projects per batch (default: 1)

    Returns:
        master_task_id   str        — poll to get overall progress (done/total)
        task_ids         dict       — {project_id: task_id} for per-project stage polling
    """
    global _bulk_cert_cancel_ev
    try:
        data = request.json or {}
        project_ids     = [int(x) for x in (data.get('project_ids') or [])]
        cert_base64     = data.get('cert_base64', '')
        target_filename = (data.get('target_filename') or 'presto.jks').strip()
        target_path     = (data.get('target_path')     or 'src/main/resources').strip()
        commit_msg      = (data.get('commit_message')  or f'chore: update {target_filename}').strip()
        branch_num      = (data.get('branch_num')      or '12938').strip()
        branch_prefix   = (data.get('branch_prefix')   or 'task-').strip()
        branch_suffix   = (data.get('branch_suffix')   or 'java17-migration').strip()
        create_mr       = bool(data.get('create_mr', False))
        deploy_dev      = bool(data.get('deploy_dev', True))
        dev_tag_name    = (data.get('dev_tag_name')    or 'azure-dev').strip()
        batch_size      = max(1, min(3, int(data.get('batch_size', 1))))

        if not project_ids:
            return jsonify({'success': False, 'error': 'No project_ids provided'}), 400
        if not cert_base64:
            return jsonify({'success': False, 'error': 'No cert_base64 provided'}), 400

        _feature_branch = _build_feature_branch(branch_num, branch_prefix, branch_suffix)

        # Cancel any still-running orchestrator from a previous call
        if not _bulk_cert_cancel_ev.is_set():
            print("[BULK-CERT] Cancelling previous orchestrator run before starting new one")
            _bulk_cert_cancel_ev.set()
            time.sleep(0.5)

        # Fresh cancel event for this run (clear = running, set = stop)
        _bulk_cert_cancel_ev = threading.Event()
        cancel_ev = _bulk_cert_cancel_ev

        # Create per-project tasks (all start as 'queued')
        per_project_tids = {}
        for pid in project_ids:
            tid = f"{pid}_cert_{int(time.time() * 1000)}"
            active_tasks[tid] = {
                'status':    'queued',
                'stage':     'queued',
                'project_id': pid,
                'operation': 'bulk_cert',
                'logs':      []
            }
            per_project_tids[str(pid)] = tid

        # Create orchestrator (master) task
        master_tid = f"bulk_cert_orch_{int(time.time() * 1000)}"
        active_tasks[master_tid] = {
            'status':    'running',
            'operation': 'cert_orchestrator',
            'total':     len(project_ids),
            'done':      0,
            'failed':    0,
            'logs':      []
        }

        settings = {
            'cert_base64':     cert_base64,
            'target_filename': target_filename,
            'target_path':     target_path,
            'commit_msg':      commit_msg,
            'create_mr':       create_mr,
            'deploy_dev':      deploy_dev,
            'dev_tag_name':    dev_tag_name,
        }

        def _orchestrator():
            remaining  = list(project_ids)
            done_count = 0
            fail_count = 0
            batch_num  = 0

            print(f"[BULK-CERT] Orchestrator started — {len(remaining)} projects, batch_size={batch_size}, "
                  f"deploy_dev={deploy_dev}, dev_tag={dev_tag_name}")

            while remaining:
                if cancel_ev.is_set():
                    print("[BULK-CERT] Orchestrator cancelled — stopping before next batch")
                    break

                batch      = remaining[:batch_size]
                remaining  = remaining[batch_size:]
                batch_num += 1

                print(f"[BULK-CERT] Batch {batch_num}: processing {[str(p) for p in batch]}")

                # Spawn one thread per project in this batch and WAIT for all to finish
                batch_threads = []
                for pid in batch:
                    tid = per_project_tids[str(pid)]
                    t   = threading.Thread(
                        target=_run_cert_pipeline_full,
                        args=(pid, tid, _feature_branch, branch_num, settings, cancel_ev),
                        daemon=True
                    )
                    t.start()
                    batch_threads.append(t)

                for t in batch_threads:
                    t.join()   # block until EVERY project in this batch fully finishes

                # Tally results
                for pid in batch:
                    tid    = per_project_tids[str(pid)]
                    status = active_tasks.get(tid, {}).get('status', 'failed')
                    if status in ('success', 'idempotent', 'partial_success'):
                        done_count += 1
                    else:
                        fail_count += 1

                active_tasks[master_tid]['done']   = done_count + fail_count
                active_tasks[master_tid]['failed']  = fail_count

                print(f"[BULK-CERT] Batch {batch_num} complete — "
                      f"running total: {done_count} ok, {fail_count} failed")

                # Pause between batches to let GitLab breathe
                if remaining and not cancel_ev.is_set():
                    print(f"[BULK-CERT] Pausing 5 s before next batch ({len(remaining)} projects remaining)...")
                    time.sleep(5)

            # Mark orchestrator terminal
            if cancel_ev.is_set() and remaining:
                active_tasks[master_tid]['status'] = 'cancelled'
                print(f"[BULK-CERT] Orchestrator cancelled with {len(remaining)} projects not started")
            elif fail_count == 0:
                active_tasks[master_tid]['status'] = 'success'
                print(f"[BULK-CERT] Orchestrator complete — all {done_count} project(s) succeeded")
            elif done_count > 0:
                active_tasks[master_tid]['status'] = 'partial_success'
                print(f"[BULK-CERT] Orchestrator complete — {done_count} ok, {fail_count} failed")
            else:
                active_tasks[master_tid]['status'] = 'failed'
                print(f"[BULK-CERT] Orchestrator complete — all {fail_count} project(s) failed")

        threading.Thread(target=_orchestrator, daemon=True).start()

        return jsonify({
            'success':        True,
            'master_task_id': master_tid,
            'task_ids':       per_project_tids,
            'project_count':  len(project_ids),
            'batch_size':     batch_size,
            'deploy_dev':     deploy_dev,
            'dev_tag_name':   dev_tag_name,
        })

    except Exception as e:
        print(f"[ERROR] bulk-cert-replace: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


if __name__ == '__main__':
    print("=" * 70)
    print("  GitLab Migration & Orchestrator Tool")
    print("  Server running on http://localhost:5000")
    print("  Logs saved to state_logs/project_state.json")
    print("=" * 70)
    print(f"  Projects loaded  : {len(PROJECT_NAMES)}")
    print(f"  GitLab base URL  : {ms.BASE_URL or '⚠️  NOT SET'}")
    print(f"  Token set        : {'✅ Yes' if ms.TOKEN else '❌ No — check GITLAB_TOKEN in .env'}")
    print(f"  SSL verify       : {ms.SSL_VERIFY}")
    print(f"  Max concurrent   : {MAX_CONCURRENT}  (set MAX_CONCURRENT_MIGRATIONS env var to change)")
    print(f"  Default branch   : {ms.SOURCE_BRANCH}")
    print(f"  Default platform : {ms.NEW_DEFAULT_PLATFORM[:60] + '...' if len(ms.NEW_DEFAULT_PLATFORM) > 60 else ms.NEW_DEFAULT_PLATFORM or '⚠️  NOT SET'}")
    print(f"  Reviewers        : {', '.join(ms.REVIEWER_USERNAMES) if ms.REVIEWER_USERNAMES else '(none configured)'}")
    print(f"  Assignees        : {', '.join(ms.ASSIGNEE_USERNAMES) if ms.ASSIGNEE_USERNAMES else '(none configured)'}")
    print("=" * 70)
    app.run(host='0.0.0.0', port=5000, debug=False)
