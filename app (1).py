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

active_tasks = {}
project_history = []
pipeline_status = {}  # Track pipeline status per project
STATE_FILE = 'project_state.json'
PIPELINE_FILE = 'pipeline_state.json'

def load_project_state():
    global project_history
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                project_history = json.load(f)
        except:
            project_history = []
    else:
        project_history = []

def save_project_state():
    with open(STATE_FILE, 'w') as f:
        json.dump(project_history, f, indent=2)

def load_pipeline_state():
    global pipeline_status
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

def update_pipeline_status(project_id, status, pipeline_id=None, commit_sha=None, committer_name=None, commit_message=None, workflow_stage=None):
    """Update pipeline status for a project, preserving existing details if not provided"""
    existing = pipeline_status.get(str(project_id), {})
    pipeline_status[str(project_id)] = {
        'status': status,
        'pipeline_id': pipeline_id if pipeline_id is not None else existing.get('pipeline_id'),
        'commit_sha': commit_sha if commit_sha is not None else existing.get('commit_sha'),
        'committer_name': committer_name if committer_name is not None else existing.get('committer_name', 'Unknown'),
        'commit_message': commit_message if commit_message is not None else existing.get('commit_message', ''),
        'timestamp': existing.get('timestamp') if status in ('success', 'failed') and existing.get('timestamp') else datetime.now().isoformat(),
        # Workflow stage: idle → committed → pipeline_success → deployed → mr_raised → merged
        'workflow_stage': workflow_stage if workflow_stage is not None else existing.get('workflow_stage', 'idle')
    }
    save_pipeline_state()
    print(f"[PIPELINE] Updated status for project {project_id}: {status} (stage: {pipeline_status[str(project_id)]['workflow_stage']})")

load_project_state()
load_pipeline_state()

# Ensure log directories exist
for log_dir in ['migration_logs', 'rollback_logs', 'state_logs']:
    os.makedirs(log_dir, exist_ok=True)
    print(f"[INFO] Ensured log directory exists: {log_dir}")

# Initialize file logging
try:
    log_file = ms.setup_file_logging()
    print(f"[INFO] File logging initialized: {log_file}")
except Exception as e:
    print(f"[WARN] Could not initialize file logging: {e}")

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
    # Return data at top level for easier access in JavaScript
    return jsonify({
        'success': True,
        'base_url': ms.BASE_URL,
        'projects': [
            {
                'id': pid,
                'name': pname,
                'path_with_namespace': pname  # Provide a default path
            } 
            for pid, pname in PROJECT_NAMES.items()
        ],
        'assignees': ms.ASSIGNEE_USERNAMES,
        'reviewers': ms.REVIEWER_USERNAMES,
        'new_default_platform': ms.NEW_DEFAULT_PLATFORM
    })


@app.route('/api/projects/<int:project_id>/preview', methods=['POST'])
def preview_changes(project_id):
    """Generate preview of changes before committing"""
    try:
        data = request.json
        choices = data.get('choices', [])
        branch_num = data.get('branch_num', '1293')
        
        # Set branch configuration
        ms.JIRA_ID = branch_num
        ms.FEATURE_BRANCH = f"task-{branch_num}-java17-migration"
        
        # Get project info
        project_info = ms.api_call(f"projects/{project_id}")
        p_name = project_info.get('name', f"ID:{project_id}") if isinstance(project_info, dict) else f"ID:{project_id}"
        
        # Check which branch to use for preview
        br_check = ms.api_call(f"projects/{project_id}/repository/branches/{urllib.parse.quote(ms.FEATURE_BRANCH, safe='')}")
        current_ref = ms.FEATURE_BRANCH if isinstance(br_check, dict) and "name" in br_check else ms.SOURCE_BRANCH
        
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
                    upd = re.sub(r"<(java\.version|maven\.compiler\.(source|target|release))>11</\1>", r"<\1>17</\1>", orig)
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
            'branch': ms.FEATURE_BRANCH
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
        branch_num = data.get('branch_num', '1293')
        
        # Set branch configuration
        ms.JIRA_ID = branch_num
        ms.FEATURE_BRANCH = f"task-{branch_num}-java17-migration"
        ms.MR_TITLE = f"TASK-{branch_num}: java migration"
        
        # Generate a unique task ID
        task_id = f"{project_id}_{int(time.time() * 1000)}"
        
        # Mark task as running
        active_tasks[task_id] = {
            'status': 'running',
            'project_id': project_id,
            'operation': 'commit',
            'logs': []
        }
        
        # Update pipeline status to "committing"
        update_pipeline_status(project_id, 'committing')
        
        # Start background thread
        def commit_thread():
            try:
                project_info = ms.api_call(f"projects/{project_id}")
                p_name = project_info.get('name', f"ID:{project_id}") if isinstance(project_info, dict) else f"ID:{project_id}"
                
                active_tasks[task_id]['project_name'] = p_name
                active_tasks[task_id]['logs'].append({
                    'level': 'INFO',
                    'message': f'Starting commit for {p_name}...',
                    'timestamp': datetime.now().isoformat()
                })
                
                # Check if branch exists, create if needed
                br_check = ms.api_call(f"projects/{project_id}/repository/branches/{urllib.parse.quote(ms.FEATURE_BRANCH, safe='')}")
                
                if not (isinstance(br_check, dict) and "name" in br_check):
                    active_tasks[task_id]['logs'].append({
                        'level': 'INFO',
                        'message': f'Creating branch {ms.FEATURE_BRANCH}...',
                        'timestamp': datetime.now().isoformat()
                    })
                    ms.create_feature_branch(project_id, p_name)
                
                # Prepare commit actions
                actions = []
                current_ref = ms.FEATURE_BRANCH if isinstance(br_check, dict) and "name" in br_check else ms.SOURCE_BRANCH
                files_to_backup = []
                
                if '1' in choices:  # POM
                    res = ms.api_call(f"projects/{project_id}/repository/files/{urllib.parse.quote('pom.xml', safe='')}?ref={urllib.parse.quote(current_ref, safe='')}")
                    if isinstance(res, dict) and "content" in res:
                        orig = base64.b64decode(res['content']).decode('utf-8')
                        upd = re.sub(r"<(java\.version|maven\.compiler\.(source|target|release))>11</\1>", r"<\1>17</\1>", orig)
                        if "<parent>" in upd:
                            upd = re.sub(r"<parent>[\s\S]*?</parent>", ms.update_parent_block, upd)
                        if orig != upd:
                            actions.append({"action": "update", "file_path": "pom.xml", "content": upd})
                            files_to_backup.append('pom.xml')
                
                if '2' in choices:  # CI
                    res = ms.api_call(f"projects/{project_id}/repository/files/{urllib.parse.quote('.gitlab-ci.yml', safe='')}?ref={urllib.parse.quote(current_ref, safe='')}")
                    if isinstance(res, dict) and "content" in res:
                        orig = base64.b64decode(res['content']).decode('utf-8')
                        upd = re.sub(r"^\s*image:.*(\n|$)", "", orig, flags=re.MULTILINE)
                        if orig != upd:
                            actions.append({"action": "update", "file_path": ".gitlab-ci.yml", "content": upd})
                            files_to_backup.append('.gitlab-ci.yml')
                
                if '3' in choices:  # EB Config
                    path = urllib.parse.quote(".elasticbeanstalk/config.yml", safe='')
                    res = ms.api_call(f"projects/{project_id}/repository/files/{path}?ref={urllib.parse.quote(current_ref, safe='')}")
                    if isinstance(res, dict) and "content" in res:
                        orig = base64.b64decode(res['content']).decode('utf-8')
                        upd = re.sub(r"(default_platform:\s*).*$", f"default_platform: {ms.NEW_DEFAULT_PLATFORM}", orig, flags=re.MULTILINE)
                        if orig != upd:
                            actions.append({"action": "update", "file_path": ".elasticbeanstalk/config.yml", "content": upd})
                            files_to_backup.append('.elasticbeanstalk/config.yml')
                
                if not actions:
                    active_tasks[task_id]['logs'].append({
                        'level': 'INFO',
                        'message': 'No changes detected - files may already be updated',
                        'timestamp': datetime.now().isoformat()
                    })
                    active_tasks[task_id]['status'] = 'no_changes'
                    update_pipeline_status(project_id, 'no_changes')
                    add_project_to_history(project_id, p_name, 'commit', 'no_changes')
                    return
                
                # Check if files already match
                all_match, details = ms.check_files_already_match(project_id, actions, ms.FEATURE_BRANCH)
                
                if all_match:
                    active_tasks[task_id]['logs'].append({
                        'level': 'INFO',
                        'message': 'All files already match desired state - skipping commit',
                        'timestamp': datetime.now().isoformat()
                    })
                    active_tasks[task_id]['status'] = 'idempotent'
                    update_pipeline_status(project_id, 'success')
                    add_project_to_history(project_id, p_name, 'commit', 'idempotent')
                    return
                
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
                
                # Commit changes
                active_tasks[task_id]['logs'].append({
                    'level': 'INFO',
                    'message': f'Committing {len(actions)} file(s) to {ms.FEATURE_BRANCH}...',
                    'timestamp': datetime.now().isoformat()
                })
                
                commit_payload = {
                    "branch": ms.FEATURE_BRANCH,
                    "commit_message": f"fix: java17-migration",
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
                    
                    # Update pipeline status with committer info
                    update_pipeline_status(project_id, 'running', commit_sha=commit_sha,
                                           committer_name=committer_name, commit_message=commit_message,
                                           workflow_stage='committed')
                    
                    # Wait for pipeline to be created
                    active_tasks[task_id]['logs'].append({
                        'level': 'INFO',
                        'message': 'Waiting for pipeline to be created...',
                        'timestamp': datetime.now().isoformat()
                    })
                    time.sleep(10)
                    
                    # Get pipeline ID (retry up to 3 times in case pipeline is slow to appear)
                    pipeline = None
                    for retry in range(3):
                        pipeline = ms.get_pipeline_for_commit(project_id, commit_sha)
                        if pipeline:
                            break
                        if retry < 2:
                            time.sleep(15)
                    
                    if pipeline:
                        pipeline_id = pipeline.get('id')
                        active_tasks[task_id]['pipeline_id'] = pipeline_id
                        update_pipeline_status(project_id, 'running', pipeline_id=pipeline_id,
                                               commit_sha=commit_sha, committer_name=committer_name,
                                               commit_message=commit_message)
                        
                        active_tasks[task_id]['logs'].append({
                            'level': 'INFO',
                            'message': f'Pipeline #{pipeline_id} created - monitoring status...',
                            'timestamp': datetime.now().isoformat()
                        })
                        
                        # Capture variables for closure
                        _pid = project_id
                        _pipeline_id = pipeline_id
                        _commit_sha = commit_sha
                        _committer_name = committer_name
                        _commit_message = commit_message
                        _task_id = task_id
                        
                        # Monitor pipeline in background (non-daemon so it survives page reload)
                        def monitor_pipeline():
                            result = ms.wait_for_pipeline_completion(_pid, _pipeline_id, timeout=1800, check_interval=30)
                            final_status = result.get('status', 'unknown')
                            
                            if final_status == 'success':
                                if _task_id in active_tasks:
                                    active_tasks[_task_id]['logs'].append({
                                        'level': 'SUCCESS',
                                        'message': f'✅ Pipeline #{_pipeline_id} completed successfully!',
                                        'timestamp': datetime.now().isoformat()
                                    })
                                update_pipeline_status(_pid, 'success', pipeline_id=_pipeline_id,
                                                       commit_sha=_commit_sha, committer_name=_committer_name,
                                                       commit_message=_commit_message, workflow_stage='pipeline_success')
                            else:
                                if _task_id in active_tasks:
                                    active_tasks[_task_id]['logs'].append({
                                        'level': 'ERROR',
                                        'message': f'❌ Pipeline #{_pipeline_id} status: {final_status}',
                                        'timestamp': datetime.now().isoformat()
                                    })
                                update_pipeline_status(_pid, 'failed', pipeline_id=_pipeline_id,
                                                       commit_sha=_commit_sha, committer_name=_committer_name,
                                                       commit_message=_commit_message, workflow_stage='committed')
                        
                        threading.Thread(target=monitor_pipeline, daemon=True).start()
                    else:
                        active_tasks[task_id]['logs'].append({
                            'level': 'WARN',
                            'message': 'Pipeline not found yet - check GitLab manually',
                            'timestamp': datetime.now().isoformat()
                        })
                    
                    add_project_to_history(project_id, p_name, 'commit', 'success', {
                        'commit_sha': commit_sha,
                        'file_count': len(actions)
                    })
                    
                    # ── Per-run log files ────────────────────────────────────
                    # 1. Migration log
                    save_run_migration_log(project_id, p_name, active_tasks[task_id]['logs'])
                    # 2. Rollback log (already saved above via ms.save_rollback_data,
                    #    but also save a timestamped copy here for per-run tracking)
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
                    # 3. State log
                    save_run_state_log(project_id, p_name, {
                        'operation': 'commit',
                        'status': 'success',
                        'commit_sha': commit_sha,
                        'files_changed': [a['file_path'] for a in actions],
                        'pipeline_id': active_tasks[task_id].get('pipeline_id'),
                        'workflow_stage': 'committed'
                    })
                    # ────────────────────────────────────────────────────────
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
        # Return both flat and nested format for compatibility
        return jsonify({
            'success': True,
            'task': {
                'status': task['status'],
                'logs': task['logs'],
                'commit_sha': task.get('commit_sha'),
                'file_count': task.get('file_count'),
                'pipeline_id': task.get('pipeline_id'),
                'project_name': task.get('project_name'),
                'operation': task.get('operation')
            },
            # also flat for any old callers
            'status': task['status'],
            'logs': task['logs'],
            'commit_sha': task.get('commit_sha'),
            'file_count': task.get('file_count'),
            'pipeline_id': task.get('pipeline_id')
        })
    else:
        return jsonify({'success': False, 'error': 'Task not found'}), 404


@app.route('/api/projects/<int:project_id>/mr', methods=['POST'])
def create_mr(project_id):
    """Create merge request for a project"""
    try:
        data = request.json
        branch_num = data.get('branch_num', '1293')
        
        # Set branch configuration
        ms.JIRA_ID = branch_num
        ms.FEATURE_BRANCH = f"task-{branch_num}-java17-migration"
        ms.MR_TITLE = f"TASK-{branch_num}: java migration"
        
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
                
                result = ms.create_mr_for_project(project_id, p_name, {'snapshots': []})
                
                if result['success']:
                    active_tasks[task_id]['logs'].append({
                        'level': 'SUCCESS',
                        'message': f'✅ MR created successfully',
                        'timestamp': datetime.now().isoformat()
                    })
                    active_tasks[task_id]['status'] = 'success'
                    active_tasks[task_id]['mr_url'] = result.get('url')
                    # Advance workflow to mr_raised — branch is still open
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
        
        threading.Thread(target=mr_thread, daemon=True).start()
        
        return jsonify({'success': True, 'task_id': task_id})
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<int:project_id>/branch-status', methods=['GET'])
def get_branch_status(project_id):
    """Check whether the feature branch still exists, and if MR is merged"""
    try:
        branch_num = request.args.get('branch_num', '1293')
        feature_branch = f"task-{branch_num}-java17-migration"
        
        # Check if branch exists
        branch_resp = ms.api_call(f"projects/{project_id}/repository/branches/{urllib.parse.quote(feature_branch, safe='')}")
        branch_exists = isinstance(branch_resp, dict) and 'name' in branch_resp and not branch_resp.get('error')
        
        # Check for open MRs on this branch
        mr_resp = ms.api_call(f"projects/{project_id}/merge_requests?source_branch={urllib.parse.quote(feature_branch, safe='')}&state=opened&per_page=1")
        open_mrs = mr_resp if isinstance(mr_resp, list) else []
        
        # Check for merged MRs on this branch
        merged_resp = ms.api_call(f"projects/{project_id}/merge_requests?source_branch={urllib.parse.quote(feature_branch, safe='')}&state=merged&per_page=1")
        merged_mrs = merged_resp if isinstance(merged_resp, list) else []
        
        existing = pipeline_status.get(str(project_id), {})
        current_stage = existing.get('workflow_stage', 'idle')
        
        # Auto-detect: if branch is gone and we had MR raised → it was likely merged
        if not branch_exists and current_stage in ('mr_raised', 'pipeline_success', 'deployed', 'committed'):
            if merged_mrs:
                # MR was merged — keep entry but mark merged
                update_pipeline_status(project_id, 'success', workflow_stage='merged')
                current_stage = 'merged'
            else:
                # Branch deleted without merging — REMOVE from pipeline_status entirely
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
    """Remove a project's entry from pipeline status tracking (e.g. when branch deleted)"""
    try:
        removed = pipeline_status.pop(str(project_id), None)
        if removed is not None:
            save_pipeline_state()
            print(f"[PIPELINE] Removed status entry for project {project_id}")
            return jsonify({'success': True, 'removed': True})
        return jsonify({'success': True, 'removed': False})
    except Exception as e:
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
        branch_num = request.args.get('branch_num', '1293')
        feature_branch = f"task-{branch_num}-java17-migration"
        
        # First try the feature branch
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
        
        # Fallback to pipeline status
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
        
        # Filter deployment tags
        filter_result = ms.filter_and_sort_deployment_tags(tags)
        
        return jsonify({
            'success': True,
            'tags': filter_result['sorted_tags'],
            'found_categories': filter_result['found_categories'],
            'missing_categories': filter_result['missing_categories']
        })
        
    except Exception as e:
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


@app.route('/api/projects/<int:project_id>/deploy', methods=['POST'])
def deploy_project(project_id):
    """Deploy project with selected tags"""
    try:
        data = request.json
        tags = data.get('tags', [])
        branch_num = data.get('branch_num', '1293')
        
        ms.JIRA_ID = branch_num
        ms.FEATURE_BRANCH = f"task-{branch_num}-java17-migration"
        
        if not tags:
            return jsonify({'success': False, 'error': 'No tags selected'}), 400
        
        task_id = f"{project_id}_deploy_{int(time.time() * 1000)}"
        active_tasks[task_id] = {
            'status': 'running',
            'project_id': project_id,
            'operation': 'deploy',
            'logs': []
        }
        
        def deploy_thread():
            try:
                project_info = ms.api_call(f"projects/{project_id}")
                p_name = project_info.get('name', f"ID:{project_id}") if isinstance(project_info, dict) else f"ID:{project_id}"
                
                active_tasks[task_id]['logs'].append({
                    'level': 'INFO',
                    'message': f'Starting deployment for {p_name}...',
                    'timestamp': datetime.now().isoformat()
                })
                
                # Simplified deployment process
                result = ms.handle_deployment(project_id, p_name, state=None)
                
                if result['success']:
                    active_tasks[task_id]['status'] = 'success'
                    active_tasks[task_id]['logs'].append({
                        'level': 'SUCCESS',
                        'message': '✅ Deployment completed successfully',
                        'timestamp': datetime.now().isoformat()
                    })
                    # Advance workflow stage to deployed
                    update_pipeline_status(project_id, 'success', workflow_stage='deployed')
                    add_project_to_history(project_id, p_name, 'deploy', 'success', {'tags': tags})
                else:
                    active_tasks[task_id]['status'] = 'failed'
                    active_tasks[task_id]['logs'].append({
                        'level': 'ERROR',
                        'message': '❌ Deployment failed',
                        'timestamp': datetime.now().isoformat()
                    })
                    add_project_to_history(project_id, p_name, 'deploy', 'failed', {'tags': tags})
                    
            except Exception as e:
                print(f"[ERROR] Deploy thread error: {e}")
                active_tasks[task_id]['logs'].append({
                    'level': 'ERROR',
                    'message': f'Exception: {str(e)}',
                    'timestamp': datetime.now().isoformat()
                })
                active_tasks[task_id]['status'] = 'failed'
        
        threading.Thread(target=deploy_thread, daemon=True).start()
        
        return jsonify({'success': True, 'task_id': task_id})
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/bulk-mr', methods=['POST'])
def bulk_create_mrs():
    """Create MRs for multiple projects — only allowed when pipeline has passed"""
    try:
        data = request.json
        project_ids = data.get('project_ids', [])
        branch_num  = data.get('branch_num', '1293')
        # Only use what the user actually selected in the UI.
        # An empty list means "no reviewers/assignees" — don't fall back to .env defaults.
        selected_assignees = data.get('assignees', [])   # list of usernames or []
        selected_reviewers = data.get('reviewers', [])   # list of usernames or []

        ms.JIRA_ID = branch_num
        ms.FEATURE_BRANCH = f"task-{branch_num}-java17-migration"
        ms.MR_TITLE = f"TASK-{branch_num}: java migration"

        # ── Server-side gate: pipeline must be at pipeline_success or beyond ──
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
        # ─────────────────────────────────────────────────────────────────────
        
        task_id = f"bulk_mr_{int(time.time() * 1000)}"
        active_tasks[task_id] = {
            'status': 'running',
            'operation': 'bulk_mr',
            'logs': []
        }
        
        # Capture selected lists in closure variables before the thread starts
        _selected_assignees = list(selected_assignees)
        _selected_reviewers = list(selected_reviewers)

        def bulk_mr_thread():
            # Temporarily override the global reviewer/assignee lists so that
            # create_mr_for_project() uses only what the user selected.
            # If the user selected nothing the lists are [], which suppresses auto-fill.
            original_reviewers = ms.REVIEWER_USERNAMES
            original_assignees = ms.ASSIGNEE_USERNAMES
            try:
                ms.REVIEWER_USERNAMES = _selected_reviewers
                ms.ASSIGNEE_USERNAMES = _selected_assignees
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
                # Always restore the originals so other operations are unaffected
                ms.REVIEWER_USERNAMES = original_reviewers
                ms.ASSIGNEE_USERNAMES = original_assignees
        
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
            'state_logs': '💾 State'
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
        
        # Sort by timestamp (newest first)
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
        # URL-decode the path in case it was encoded
        log_path = urllib.parse.unquote(log_path)
        # Normalize separators for Windows compatibility
        log_path = log_path.replace('\\', '/')
        
        # Security: ensure the path is within our log directories
        allowed_dirs = ['migration_logs', 'rollback_logs', 'state_logs']
        normalized_path = os.path.normpath(log_path)
        
        # Check if path starts with any allowed directory (handle both / and \ separators)
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
    
    # Ensure we have strings
    if not isinstance(old_content, str):
        old_content = str(old_content)
    if not isinstance(new_content, str):
        new_content = str(new_content)
    
    # Split into lines
    old_lines = old_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)
    
    # Generate unified diff
    diff = difflib.unified_diff(
        old_lines, 
        new_lines, 
        fromfile='before', 
        tofile='after', 
        lineterm=''
    )
    
    diff_text = '\n'.join(diff)
    
    # Debug: log diff length
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
        
        # URL-decode in case it was encoded
        rollback_file = urllib.parse.unquote(rollback_file)
        rollback_file = rollback_file.replace('\\', '/')
        
        # Security check
        allowed_dirs = ['rollback_logs']
        normalized_path = os.path.normpath(rollback_file)
        path_parts = normalized_path.replace('\\', '/').split('/')
        
        if not path_parts or path_parts[0] not in allowed_dirs:
            return jsonify({'success': False, 'error': 'Invalid rollback file path'}), 403
        
        if not os.path.exists(normalized_path):
            return jsonify({'success': False, 'error': 'Rollback file not found'}), 404
        
        print(f"[ROLLBACK] Starting rollback from: {rollback_file}")
        
        # Perform rollback in background thread
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
                
                # Load rollback data
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
                
                # Perform rollback for each project
                for snapshot in snapshots:
                    pid = snapshot['project_id']
                    p_name = snapshot['project_name']
                    branch = snapshot['branch']
                    
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
                
                # Final summary
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
        
        threading.Thread(target=rollback_thread, daemon=True).start()
        
        return jsonify({
            'success': True,
            'task_id': task_id,
            'message': 'Rollback started'
        })
        
    except Exception as e:
        print(f"[ERROR] Rollback endpoint error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


if __name__ == '__main__':
    print("=" * 70)
    print("  GitLab Migration & Orchestrator Tool")
    print("  Lincoln Financial Group")
    print("  Server running on http://localhost:5000")
    print("  Logs saved to project_state.json")
    print("=" * 70)
    app.run(host='0.0.0.0', port=5000, debug=False)
