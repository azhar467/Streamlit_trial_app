from flask import Flask, request, jsonify
from flask_cors import CORS
import threading
from datetime import datetime
import base64
import re
import time
import json
import os
import urllib.parse  # Added for URL encoding

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
pipeline_status = {}
STATE_FILE = 'project_state.json'

# Helper for URL encoding
def quote(s):
    return urllib.parse.quote(str(s), safe='')

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

def update_pipeline_status(project_id, status, pipeline_id=None, commit_sha=None):
    pipeline_status[project_id] = {
        'status': status,
        'pipeline_id': pipeline_id,
        'commit_sha': commit_sha,
        'timestamp': datetime.now().isoformat()
    }

load_project_state()


@app.route('/')
def index():
    with open('index.html', 'r', encoding='utf-8') as f:
        return f.read()


@app.route('/api/config', methods=['GET'])
def get_config():
    return jsonify({
        'success': True,
        'projects': [{'id': pid, 'name': name} for pid, name in sorted(PROJECT_NAMES.items())],
        'reviewers': env_config['reviewer_usernames'],
        'assignees': env_config['assignee_usernames']
    })


@app.route('/api/history', methods=['GET'])
def get_history():
    return jsonify({
        'success': True,
        'history': project_history
    })


@app.route('/api/pipeline-status', methods=['GET'])
def get_pipeline_status():
    return jsonify({
        'success': True,
        'pipelines': pipeline_status
    })


@app.route('/api/projects/<int:project_id>/pipeline-status', methods=['GET'])
def get_project_pipeline_status(project_id):
    if project_id in pipeline_status:
        return jsonify({
            'success': True,
            'pipeline': pipeline_status[project_id]
        })
    else:
        return jsonify({
            'success': True,
            'pipeline': None
        })


@app.route('/api/projects/<int:project_id>/preview', methods=['POST'])
def preview_changes(project_id):
    try:
        data = request.json
        choices = data.get('choices', [])
        branch = data.get('branch', 'develop')
        
        # URL encode the branch
        encoded_branch = quote(branch)
        
        print(f"\n[PREVIEW] Starting preview for project {project_id}")
        
        actions = []
        
        if '1' in choices:
            print(f"[PREVIEW] Processing pom.xml...")
            # Use quoted path and branch
            path = quote("pom.xml")
            res = ms.api_call(f"projects/{project_id}/repository/files/{path}?ref={encoded_branch}")
            if isinstance(res, dict) and "content" in res:
                orig = base64.b64decode(res['content']).decode('utf-8')
                upd = re.sub(r"<(java\.version|maven\.compiler\.(source|target|release))>11</\1>", r"<\1>17</\1>", orig)
                if "<parent>" in upd:
                    upd = re.sub(r"<parent>[\s\S]*?</parent>", ms.update_parent_block, upd)
                
                if orig != upd:
                    last_commit_id = res.get('last_commit_id', 'N/A')
                    commit_author = 'N/A'
                    commit_date = 'N/A'
                    
                    if last_commit_id != 'N/A':
                        commit_info = ms.api_call(f"projects/{project_id}/repository/commits/{last_commit_id}")
                        if isinstance(commit_info, dict):
                            commit_author = commit_info.get('author_name', 'N/A')
                            commit_date = commit_info.get('committed_date', 'N/A')
                    
                    diff_text = generate_diff(orig, upd)
                    actions.append({
                        'file_path': 'pom.xml',
                        'old_content': orig,
                        'new_content': upd,
                        'diff': diff_text,
                        'last_commit_id': last_commit_id,
                        'commit_author': commit_author,
                        'commit_date': commit_date
                    })
        
        if '2' in choices:
            print(f"[PREVIEW] Processing .gitlab-ci.yml...")
            path = quote(".gitlab-ci.yml")
            res = ms.api_call(f"projects/{project_id}/repository/files/{path}?ref={encoded_branch}")
            if isinstance(res, dict) and "content" in res:
                orig = base64.b64decode(res['content']).decode('utf-8')
                upd = re.sub(r"^\s*image:.*(\n|$)", "", orig, flags=re.MULTILINE)
                
                if orig != upd:
                    last_commit_id = res.get('last_commit_id', 'N/A')
                    commit_author = 'N/A'
                    commit_date = 'N/A'
                    if last_commit_id != 'N/A':
                        commit_info = ms.api_call(f"projects/{project_id}/repository/commits/{last_commit_id}")
                        if isinstance(commit_info, dict):
                            commit_author = commit_info.get('author_name', 'N/A')
                            commit_date = commit_info.get('committed_date', 'N/A')
                    
                    diff_text = generate_diff(orig, upd)
                    actions.append({
                        'file_path': '.gitlab-ci.yml',
                        'old_content': orig,
                        'new_content': upd,
                        'diff': diff_text,
                        'last_commit_id': last_commit_id,
                        'commit_author': commit_author,
                        'commit_date': commit_date
                    })
        
        if '3' in choices:
            print(f"[PREVIEW] Processing .elasticbeanstalk/config.yml...")
            raw_path = ".elasticbeanstalk/config.yml"
            encoded_path = quote(raw_path)
            res = ms.api_call(f"projects/{project_id}/repository/files/{encoded_path}?ref={encoded_branch}")
            if isinstance(res, dict) and "content" in res:
                orig = base64.b64decode(res['content']).decode('utf-8')
                upd = re.sub(r"(default_platform:\s*).*$", f"default_platform: {ms.NEW_DEFAULT_PLATFORM}", orig, flags=re.MULTILINE)
                
                if orig != upd:
                    last_commit_id = res.get('last_commit_id', 'N/A')
                    commit_author = 'N/A'
                    commit_date = 'N/A'
                    if last_commit_id != 'N/A':
                        commit_info = ms.api_call(f"projects/{project_id}/repository/commits/{last_commit_id}")
                        if isinstance(commit_info, dict):
                            commit_author = commit_info.get('author_name', 'N/A')
                            commit_date = commit_info.get('committed_date', 'N/A')
                    
                    diff_text = generate_diff(orig, upd)
                    actions.append({
                        'file_path': raw_path,
                        'old_content': orig,
                        'new_content': upd,
                        'diff': diff_text,
                        'last_commit_id': last_commit_id,
                        'commit_author': commit_author,
                        'commit_date': commit_date
                    })
        
        return jsonify({'success': True, 'actions': actions})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


def perform_single_commit(project_id, choices):
    task_id = f"commit_{project_id}_{int(datetime.now().timestamp())}"
    
    def run_commit():
        logs = []
        try:
            p_name = PROJECT_NAMES.get(project_id, f"Project {project_id}")
            logs.append({'level': 'INFO', 'message': f'Starting commit for {p_name}'})
            
            update_pipeline_status(project_id, 'committing')
            
            # Create feature branch
            ms.create_feature_branch(project_id, p_name)
            logs.append({'level': 'INFO', 'message': f'Feature branch: {ms.FEATURE_BRANCH}'})
            
            actions = []
            # Quote the feature branch for URL
            encoded_ref = quote(ms.FEATURE_BRANCH)
            
            if '1' in choices:
                path = quote("pom.xml")
                res = ms.api_call(f"projects/{project_id}/repository/files/{path}?ref={encoded_ref}")
                if isinstance(res, dict) and "content" in res:
                    orig = base64.b64decode(res['content']).decode('utf-8')
                    upd = re.sub(r"<(java\.version|maven\.compiler\.(source|target|release))>11</\1>", r"<\1>17</\1>", orig)
                    if "<parent>" in upd:
                        upd = re.sub(r"<parent>[\s\S]*?</parent>", ms.update_parent_block, upd)
                    if orig != upd:
                        actions.append({"action": "update", "file_path": "pom.xml", "content": upd})
                        logs.append({'level': 'INFO', 'message': 'POM.xml updated'})
            
            if '2' in choices:
                path = quote(".gitlab-ci.yml")
                res = ms.api_call(f"projects/{project_id}/repository/files/{path}?ref={encoded_ref}")
                if isinstance(res, dict) and "content" in res:
                    orig = base64.b64decode(res['content']).decode('utf-8')
                    upd = re.sub(r"^\s*image:.*(\n|$)", "", orig, flags=re.MULTILINE)
                    if orig != upd:
                        actions.append({"action": "update", "file_path": ".gitlab-ci.yml", "content": upd})
                        logs.append({'level': 'INFO', 'message': '.gitlab-ci.yml updated'})
            
            if '3' in choices:
                path = ".elasticbeanstalk/config.yml"
                encoded_path = quote(path)
                res = ms.api_call(f"projects/{project_id}/repository/files/{encoded_path}?ref={encoded_ref}")
                if isinstance(res, dict) and "content" in res:
                    orig = base64.b64decode(res['content']).decode('utf-8')
                    upd = re.sub(r"(default_platform:\s*).*$", f"default_platform: {ms.NEW_DEFAULT_PLATFORM}", orig, flags=re.MULTILINE)
                    if orig != upd:
                        actions.append({"action": "update", "file_path": path, "content": upd})
                        logs.append({'level': 'INFO', 'message': 'Config updated'})
            
            if actions:
                commit_payload = {
                    "branch": ms.FEATURE_BRANCH,
                    "commit_message": f"fix: {ms.UPGRADE_TYPE}",
                    "actions": actions
                }
                
                logs.append({'level': 'INFO', 'message': f'Committing {len(actions)} file(s)...'})
                commit_resp = ms.api_call(f"projects/{project_id}/repository/commits", "POST", commit_payload)
                
                if isinstance(commit_resp, dict) and not commit_resp.get("error"):
                    commit_sha = commit_resp.get('id')
                    logs.append({'level': 'SUCCESS', 'message': 'Commit successful!'})
                    logs.append({'level': 'INFO', 'message': f'SHA: {commit_sha[:8]}'})
                    
                    time.sleep(5)
                    pipeline = ms.get_pipeline_for_commit(project_id, commit_sha)
                    
                    if pipeline:
                        pipeline_id = pipeline.get('id')
                        logs.append({'level': 'INFO', 'message': f'Pipeline {pipeline_id} triggered'})
                        update_pipeline_status(project_id, 'running', pipeline_id, commit_sha)
                        
                        def monitor_pipeline():
                            result = ms.wait_for_pipeline_completion(project_id, pipeline_id, timeout=1800, check_interval=30)
                            if result['status'] == 'success':
                                update_pipeline_status(project_id, 'success', pipeline_id, commit_sha)
                            else:
                                update_pipeline_status(project_id, 'failed', pipeline_id, commit_sha)
                        
                        monitor_thread = threading.Thread(target=monitor_pipeline)
                        monitor_thread.daemon = True
                        monitor_thread.start()
                    else:
                        logs.append({'level': 'WARN', 'message': 'No pipeline triggered yet'})
                        update_pipeline_status(project_id, 'no_pipeline', None, commit_sha)
                    
                    add_project_to_history(project_id, p_name, 'commit', 'success', {'commit_sha': commit_sha})
                    active_tasks[task_id] = {'status': 'completed', 'message': 'Committed successfully', 'logs': logs, 'commit_sha': commit_sha}
                else:
                    err_det = commit_resp.get('details', 'unknown')
                    logs.append({'level': 'ERROR', 'message': f"Commit failed: {err_det}"})
                    update_pipeline_status(project_id, 'commit_failed')
                    add_project_to_history(project_id, p_name, 'commit', 'failed')
                    active_tasks[task_id] = {'status': 'failed', 'message': 'Commit failed', 'logs': logs}
            else:
                logs.append({'level': 'INFO', 'message': 'No changes to commit'})
                active_tasks[task_id] = {'status': 'completed', 'message': 'No changes to commit', 'logs': logs}
                
        except Exception as e:
            logs.append({'level': 'ERROR', 'message': f'Error: {str(e)}'})
            update_pipeline_status(project_id, 'error')
            add_project_to_history(project_id, PROJECT_NAMES.get(project_id, ''), 'commit', 'failed')
            active_tasks[task_id] = {'status': 'failed', 'message': str(e), 'logs': logs}

    thread = threading.Thread(target=run_commit)
    thread.daemon = True
    thread.start()
    
    active_tasks[task_id] = {
        'status': 'running',
        'message': 'Committing...',
        'logs': []
    }
    return task_id


@app.route('/api/projects/<int:project_id>/commit', methods=['POST'])
def commit_changes(project_id):
    try:
        data = request.json
        choices = data.get('choices', [])
        branch_num = data.get('branch_num', '12938')
        
        ms.JIRA_ID = branch_num
        ms.FEATURE_BRANCH = f"task-{branch_num}-{ms.UPGRADE_TYPE}"
        ms.MR_TITLE = f"TASK-{branch_num}: java migration"
        
        task_id = perform_single_commit(project_id, choices)
        return jsonify({'success': True, 'task_id': task_id})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/bulk-commit', methods=['POST'])
def bulk_commit():
    try:
        data = request.json
        project_ids = data.get('project_ids', [])
        choices = data.get('choices', [])
        branch_num = data.get('branch_num', '12938')
        
        ms.JIRA_ID = branch_num
        ms.FEATURE_BRANCH = f"task-{branch_num}-{ms.UPGRADE_TYPE}"
        ms.MR_TITLE = f"TASK-{branch_num}: java migration"
        
        # We start a task that processes all commits sequentially or in parallel.
        # For simplicity, we'll return the task_id of the first project 
        # (or create a wrapper task, but here we'll just loop and fire threads).
        # A better approach for bulk is to have one parent task, but given structure,
        # let's trigger them and return the list of task IDs.
        
        tasks = []
        for pid in project_ids:
            # We must set MS variables globally, which isn't thread safe if we run parallel requests 
            # with different params, but here params are same for bulk.
            task_id = perform_single_commit(pid, choices)
            tasks.append({'project_id': pid, 'task_id': task_id})
            
        return jsonify({'success': True, 'tasks': tasks, 'task_id': tasks[0]['task_id'] if tasks else None})
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<int:project_id>/mr', methods=['POST'])
def create_mr(project_id):
    try:
        data = request.json
        branch_num = data.get('branch_num', '12938')
        
        ms.JIRA_ID = branch_num
        ms.FEATURE_BRANCH = f"task-{branch_num}-{ms.UPGRADE_TYPE}"
        ms.MR_TITLE = f"TASK-{branch_num}: java migration"
        
        task_id = f"mr_{project_id}_{int(datetime.now().timestamp())}"
        
        def run_mr():
            logs = []
            try:
                p_name = PROJECT_NAMES.get(project_id, f"Project {project_id}")
                logs.append({'level': 'INFO', 'message': f'Creating MR for {p_name}'})
                
                result = ms.create_mr_for_project(project_id, p_name, {'snapshots': []})
                
                if result['success']:
                    logs.append({'level': 'SUCCESS', 'message': f"MR created: {result.get('url', 'N/A')}"})
                    add_project_to_history(project_id, p_name, 'mr', 'success', {'url': result.get('url')})
                    active_tasks[task_id] = {
                        'status': 'completed',
                        'message': f"MR created: {result.get('url', 'N/A')}",
                        'logs': logs,
                        'url': result.get('url')
                    }
                else:
                    logs.append({'level': 'ERROR', 'message': 'Failed to create MR'})
                    add_project_to_history(project_id, p_name, 'mr', 'failed')
                    active_tasks[task_id] = {
                        'status': 'failed',
                        'message': 'Failed to create MR',
                        'logs': logs
                    }
            except Exception as e:
                logs.append({'level': 'ERROR', 'message': f'Error: {str(e)}'})
                add_project_to_history(project_id, PROJECT_NAMES.get(project_id, ''), 'mr', 'failed')
                active_tasks[task_id] = {'status': 'failed', 'message': str(e), 'logs': logs}
        
        thread = threading.Thread(target=run_mr)
        thread.daemon = True
        thread.start()
        
        active_tasks[task_id] = {'status': 'running', 'message': 'Creating MR...', 'logs': []}
        return jsonify({'success': True, 'task_id': task_id})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<int:project_id>/tags', methods=['GET', 'POST'])
def handle_tags(project_id):
    if request.method == 'GET':
        try:
            tags_resp = ms.fetch_all_tags_for_project(project_id)
            if isinstance(tags_resp, dict) and tags_resp.get("error"):
                return jsonify({'success': False, 'error': tags_resp.get('details')}), 500
            
            filter_result = ms.filter_and_sort_deployment_tags(tags_resp)
            return jsonify({'success': True, 'tags': filter_result['sorted_tags']})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
    
    elif request.method == 'POST':
        try:
            data = request.json
            tag_name = data.get('tag_name')
            ref = data.get('ref', 'develop')
            
            if not tag_name:
                return jsonify({'success': False, 'error': 'tag_name is required'}), 400
            
            # Use URL encoding for tag and ref
            # Note: create tag endpoint is POST /projects/:id/repository/tags
            # Body: tag_name, ref
            
            result = ms.api_call(f"projects/{project_id}/repository/tags", "POST", {
                "tag_name": tag_name,
                "ref": ref
            })
            
            if isinstance(result, dict) and result.get("error"):
                return jsonify({'success': False, 'error': result.get('message', 'Unknown error')}), 500
            
            return jsonify({'success': True, 'tag': result})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/projects/<int:project_id>/deploy', methods=['POST'])
def deploy(project_id):
    try:
        data = request.json
        selected_tags = data.get('tags', [])
        create_mr_on_success = data.get('create_mr_on_success', False)
        branch_num = data.get('branch_num', '12938')
        
        ms.JIRA_ID = branch_num
        ms.FEATURE_BRANCH = f"task-{branch_num}-{ms.UPGRADE_TYPE}"
        ms.MR_TITLE = f"TASK-{branch_num}: java migration"
        
        task_id = f"deploy_{project_id}_{int(datetime.now().timestamp())}"
        
        def run_deploy():
            logs = []
            try:
                p_name = PROJECT_NAMES.get(project_id, f"Project {project_id}")
                logs.append({'level': 'INFO', 'message': f'Starting deployment for {p_name}'})
                
                # Check branch existence using Quoted Branch Name
                encoded_branch = quote(ms.FEATURE_BRANCH)
                branch_info = ms.api_call(f"projects/{project_id}/repository/branches/{encoded_branch}")
                
                feature_head = (branch_info.get("commit") or {}).get("id") if isinstance(branch_info, dict) else None
                
                if not feature_head:
                    logs.append({'level': 'ERROR', 'message': f'Feature branch {ms.FEATURE_BRANCH} not found'})
                    add_project_to_history(project_id, p_name, 'deploy', 'failed')
                    active_tasks[task_id] = {'status': 'failed', 'message': 'Feature branch not found', 'logs': logs}
                    return
                
                all_builds_successful = True
                deployment_results = []
                
                for tag_name in selected_tags:
                    logs.append({'level': 'INFO', 'message': f'Processing tag: {tag_name}'})
                    
                    tags_resp = ms.fetch_all_tags_for_project(project_id)
                    tag_obj = next((t for t in tags_resp if isinstance(t, dict) and t.get('name') == tag_name), None) if isinstance(tags_resp, list) else None
                    
                    if tag_obj and not tag_obj.get('protected'):
                        # Delete tag: Encode tag name
                        encoded_tag = quote(tag_name)
                        ms.api_call(f"projects/{project_id}/repository/tags/{encoded_tag}", method="DELETE")
                        logs.append({'level': 'INFO', 'message': f'Deleted existing tag {tag_name}'})
                    
                    logs.append({'level': 'INFO', 'message': f'Creating tag {tag_name}'})
                    t_res = ms.api_call(f"projects/{project_id}/repository/tags", "POST", {"tag_name": tag_name, "ref": ms.FEATURE_BRANCH})
                    
                    if isinstance(t_res, dict) and not t_res.get("error"):
                        created_commit = (t_res.get('commit') or {}).get('id')
                        time.sleep(10)
                        
                        pipeline = ms.get_pipeline_for_commit(project_id, created_commit)
                        if pipeline:
                            pipeline_id = pipeline.get('id')
                            logs.append({'level': 'INFO', 'message': f'Pipeline {pipeline_id} triggered'})
                            
                            result = ms.wait_for_pipeline_completion(project_id, pipeline_id, timeout=1800, check_interval=30)
                            
                            if result['status'] == 'success':
                                logs.append({'level': 'SUCCESS', 'message': f'Build succeeded for {tag_name}'})
                                
                                jobs = ms.get_pipeline_jobs(project_id, pipeline_id)
                                terminate_job = ms.find_job_by_name(jobs, 'eb-terminate')
                                if terminate_job:
                                    ms.trigger_manual_job(project_id, terminate_job.get('id'))
                                    ms.wait_for_job_completion(project_id, terminate_job.get('id'), timeout=900)
                                
                                deploy_job_name = ms.map_tag_to_deploy_job(tag_name)
                                if deploy_job_name:
                                    jobs = ms.get_pipeline_jobs(project_id, pipeline_id)
                                    deploy_job = ms.find_job_by_name(jobs, deploy_job_name)
                                    if deploy_job:
                                        ms.trigger_manual_job(project_id, deploy_job.get('id'))
                                        deploy_result = ms.wait_for_job_completion(project_id, deploy_job.get('id'), timeout=1200)
                                        
                                        if deploy_result['status'] == 'success':
                                            logs.append({'level': 'SUCCESS', 'message': f'Deployment SUCCESS for {tag_name}'})
                                            deployment_results.append({'tag': tag_name, 'success': True})
                                        else:
                                            logs.append({'level': 'ERROR', 'message': f'Deployment FAILED for {tag_name}'})
                                            deployment_results.append({'tag': tag_name, 'success': False})
                                            all_builds_successful = False
                            else:
                                logs.append({'level': 'ERROR', 'message': f'Build FAILED for {tag_name}'})
                                deployment_results.append({'tag': tag_name, 'success': False})
                                all_builds_successful = False
                
                if all_builds_successful:
                    add_project_to_history(project_id, p_name, 'deploy', 'success', {'tags': selected_tags})
                else:
                    add_project_to_history(project_id, p_name, 'deploy', 'failed', {'tags': selected_tags})
                
                logs.append({'level': 'INFO', 'message': 'Deployment process completed'})
                active_tasks[task_id] = {'status': 'completed', 'message': 'Deployment completed', 'logs': logs}
                
            except Exception as e:
                logs.append({'level': 'ERROR', 'message': f'Error: {str(e)}'})
                active_tasks[task_id] = {'status': 'failed', 'message': str(e), 'logs': logs}
        
        thread = threading.Thread(target=run_deploy)
        thread.daemon = True
        thread.start()
        
        active_tasks[task_id] = {'status': 'running', 'message': 'Starting deployment...', 'logs': []}
        return jsonify({'success': True, 'task_id': task_id})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/bulk-mr', methods=['POST'])
def bulk_mr():
    try:
        data = request.json
        project_ids = data.get('project_ids', [])
        branch_num = data.get('branch_num', '12938')
        assignees = data.get('assignees', [])
        reviewers = data.get('reviewers', [])
        
        ms.JIRA_ID = branch_num
        ms.FEATURE_BRANCH = f"task-{branch_num}-{ms.UPGRADE_TYPE}"
        ms.MR_TITLE = f"TASK-{branch_num}: java migration"
        
        if assignees:
            ms.ASSIGNEE_USERNAMES = assignees
        if reviewers:
            ms.REVIEWER_USERNAMES = reviewers
        
        task_id = f"bulk_mr_{int(datetime.now().timestamp())}"
        
        def run_bulk():
            logs = []
            try:
                logs.append({'level': 'INFO', 'message': f'Creating MRs for {len(project_ids)} projects'})
                
                for pid in project_ids:
                    p_name = PROJECT_NAMES.get(pid, f"Project {pid}")
                    logs.append({'level': 'INFO', 'message': f'Processing {p_name}'})
                    
                    result = ms.create_mr_for_project(pid, p_name, {'snapshots': []})
                    
                    if result['success']:
                        logs.append({'level': 'SUCCESS', 'message': f'MR created for {p_name}'})
                        add_project_to_history(pid, p_name, 'mr', 'success', {'url': result.get('url')})
                    else:
                        logs.append({'level': 'ERROR', 'message': f'Failed for {p_name}'})
                        add_project_to_history(pid, p_name, 'mr', 'failed')
                
                logs.append({'level': 'INFO', 'message': 'Bulk MR creation completed'})
                active_tasks[task_id] = {'status': 'completed', 'message': 'Bulk MR completed', 'logs': logs}
                
            except Exception as e:
                logs.append({'level': 'ERROR', 'message': f'Error: {str(e)}'})
                active_tasks[task_id] = {'status': 'failed', 'message': str(e), 'logs': logs}
        
        thread = threading.Thread(target=run_bulk)
        thread.daemon = True
        thread.start()
        
        active_tasks[task_id] = {'status': 'running', 'message': 'Creating MRs...', 'logs': []}
        return jsonify({'success': True, 'task_id': task_id})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/tasks/<task_id>', methods=['GET'])
def get_task(task_id):
    if task_id in active_tasks:
        return jsonify({'success': True, 'task': active_tasks[task_id]})
    else:
        return jsonify({'success': False, 'error': 'Task not found'}), 404


@app.route('/api/logs', methods=['GET'])
def list_logs():
    try:
        log_dirs = ['migration_logs', 'rollback_logs', 'state_logs']
        all_logs = []
        
        for log_dir in log_dirs:
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
                                'type': log_dir
                            })
                        except Exception as e:
                            print(f"Error reading file {filepath}: {e}")
        
        return jsonify({'success': True, 'logs': all_logs, 'count': len(all_logs)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/logs/<path:log_path>', methods=['GET'])
def get_log_content(log_path):
    try:
        allowed_dirs = ['migration_logs', 'rollback_logs', 'state_logs']
        normalized_path = os.path.normpath(log_path)
        
        if not any(normalized_path.startswith(d) for d in allowed_dirs):
            return jsonify({'success': False, 'error': 'Access denied'}), 403
        
        if not os.path.exists(normalized_path):
            return jsonify({'success': False, 'error': 'Log file not found'}), 404
        
        with open(normalized_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        return jsonify({'success': True, 'content': content, 'path': normalized_path})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


def generate_diff(old_content, new_content):
    import difflib
    if not isinstance(old_content, str): old_content = str(old_content)
    if not isinstance(new_content, str): new_content = str(new_content)
    
    old_lines = old_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)
    
    diff = difflib.unified_diff(old_lines, new_lines, fromfile='before', tofile='after', lineterm='')
    return '\n'.join(diff)


if __name__ == '__main__':
    print("=" * 60)
    print("  GitLab Migration Tool - Enhanced Version")
    print("  Server running on http://localhost:5000")
    print("=" * 60)
    app.run(host='0.0.0.0', port=5000, debug=False)
