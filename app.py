from flask import Flask, render_template, request, jsonify, send_from_directory
import urllib.request
import urllib.parse
import urllib.error
import json
import base64
import re
import datetime
import os
import logging
from functools import wraps

app = Flask(__name__, static_folder='static', template_folder='.')

# Configuration
BASE_URL = os.getenv('GITLAB_BASE_URL', '').rstrip('/')
TOKEN = os.getenv('GITLAB_TOKEN', '')
FEATURE_BRANCH = os.getenv('FEATURE_BRANCH', 'task-1293-java17-migration')
SOURCE_BRANCH = os.getenv('SOURCE_BRANCH', 'develop')
TARGET_PARENT_VERSION = os.getenv('TARGET_PARENT_VERSION', '1.8.3')
NEW_DEFAULT_PLATFORM = os.getenv('NEW_DEFAULT_PLATFORM', '')

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)

# Load configuration from .env file
def load_env_config():
    """Load configuration from .env file"""
    config = {
        'base_url': '',
        'token': '',
        'projects': {},
        'new_default_platform': '',
        'reviewer_usernames': [],
        'assignee_usernames': []
    }
    
    script_dir = os.path.dirname(os.path.abspath(__file__))
    env_file = os.path.join(script_dir, '.env')
    
    if not os.path.exists(env_file):
        logger.warning(".env file not found")
        return config
    
    try:
        with open(env_file, 'r') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                
                if '=' in line:
                    key, value = line.split('=', 1)
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    
                    if key in ('BASE_URL', 'GITLAB_BASE_URL', 'GITLAB_URL'):
                        config['base_url'] = value.rstrip('/')
                    elif key in ('GITLAB_TOKEN', 'TOKEN'):
                        config['token'] = value
                    elif key == 'NEW_DEFAULT_PLATFORM':
                        config['new_default_platform'] = value
                    elif key == 'REVIEWER_USERNAMES':
                        config['reviewer_usernames'] = [x.strip() for x in value.split(',') if x.strip()]
                    elif key == 'ASSIGNEE_USERNAMES':
                        config['assignee_usernames'] = [x.strip() for x in value.split(',') if x.strip()]
                    elif key.startswith('PROJECT_'):
                        try:
                            project_id = int(key.replace('PROJECT_', ''))
                            config['projects'][project_id] = value
                        except ValueError:
                            pass
    except Exception as e:
        logger.error(f"Error reading .env file: {e}")
    
    return config

# Load config on startup
env_config = load_env_config()
if env_config['base_url']:
    BASE_URL = env_config['base_url']
if env_config['token']:
    TOKEN = env_config['token']
if env_config['new_default_platform']:
    NEW_DEFAULT_PLATFORM = env_config['new_default_platform']

PROJECT_NAMES = env_config['projects']

def retry(tries=3, delay=1, backoff=2):
    """Retry decorator for API calls"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            _tries, _delay = tries, delay
            while True:
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    _tries -= 1
                    if _tries <= 0:
                        raise
                    logger.warning(f"Retrying {func.__name__}: {e}")
                    import time
                    time.sleep(_delay)
                    _delay *= backoff
        return wrapper
    return decorator

@retry(tries=3, delay=1, backoff=2)
def api_call(endpoint, method="GET", data=None):
    """Make API call to GitLab"""
    url = f"{BASE_URL}/api/v4/{endpoint.lstrip('/')}"
    
    if not TOKEN:
        raise RuntimeError("GitLab token not configured")
    
    body = json.dumps(data).encode("utf-8") if data else None
    req = urllib.request.Request(url, method=method, data=body)
    req.add_header("PRIVATE-TOKEN", TOKEN)
    req.add_header("Content-Type", "application/json")
    
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            text = response.read().decode("utf-8")
            return json.loads(text) if text else {}
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8") if e.fp else ""
        logger.error(f"HTTP {e.code} for {url}: {error_body}")
        raise Exception(f"GitLab API error: {e.code}")
    except urllib.error.URLError as e:
        logger.error(f"URL error for {url}: {e}")
        raise Exception(f"Network error: {e}")

def update_parent_block(match):
    """Update parent POM version"""
    block = match.group(0)
    block = re.sub(r"<version>.*?</version>", f"<version>{TARGET_PARENT_VERSION}</version>", block)
    block = re.sub(r"(parent-pom-).*?(\.xml)", lambda m: m.group(1) + TARGET_PARENT_VERSION + m.group(2), block)
    return block

# Routes
@app.route('/')
def index():
    """Serve the main HTML page"""
    return send_from_directory('.', 'index.html')

@app.route('/api/projects', methods=['GET'])
def get_projects():
    """Get all configured projects with their details"""
    try:
        projects_list = []
        
        for project_id, project_name in PROJECT_NAMES.items():
            try:
                # Get project details from GitLab
                project_info = api_call(f"projects/{project_id}")
                
                # Check if feature branch exists
                branch_exists = False
                try:
                    branch_info = api_call(f"projects/{project_id}/repository/branches/{urllib.parse.quote(FEATURE_BRANCH, safe='')}")
                    branch_exists = 'name' in branch_info
                except:
                    pass
                
                projects_list.append({
                    'id': project_id,
                    'name': project_name,
                    'web_url': project_info.get('web_url', ''),
                    'branch_exists': branch_exists,
                    'default_branch': project_info.get('default_branch', 'main')
                })
            except Exception as e:
                logger.error(f"Error fetching project {project_id}: {e}")
                projects_list.append({
                    'id': project_id,
                    'name': project_name,
                    'web_url': '',
                    'branch_exists': False,
                    'error': str(e)
                })
        
        return jsonify({
            'success': True,
            'projects': projects_list,
            'feature_branch': FEATURE_BRANCH,
            'source_branch': SOURCE_BRANCH
        })
    except Exception as e:
        logger.error(f"Error in get_projects: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/changes/<int:project_id>', methods=['GET'])
def get_changes(project_id):
    """Get proposed changes for a project"""
    try:
        file_types = request.args.get('files', '1,2,3').split(',')
        
        # Determine which branch to use
        try:
            branch_check = api_call(f"projects/{project_id}/repository/branches/{urllib.parse.quote(FEATURE_BRANCH, safe='')}")
            current_ref = FEATURE_BRANCH if 'name' in branch_check else SOURCE_BRANCH
        except:
            current_ref = SOURCE_BRANCH
        
        changes = []
        
        # POM.xml changes
        if '1' in file_types:
            try:
                pom_response = api_call(f"projects/{project_id}/repository/files/{urllib.parse.quote('pom.xml', safe='')}?ref={urllib.parse.quote(current_ref, safe='')}")
                if 'content' in pom_response:
                    orig_content = base64.b64decode(pom_response['content']).decode('utf-8')
                    updated_content = re.sub(
                        r"<(java\.version|maven\.compiler\.(source|target|release))>11</\1>",
                        r"<\1>17</\1>",
                        orig_content
                    )
                    if "<parent>" in updated_content:
                        updated_content = re.sub(r"<parent>[\s\S]*?</parent>", update_parent_block, updated_content)
                    
                    if orig_content != updated_content:
                        changes.append({
                            'file_path': 'pom.xml',
                            'old_content': orig_content,
                            'new_content': updated_content
                        })
            except Exception as e:
                logger.error(f"Error getting pom.xml for project {project_id}: {e}")
        
        # .gitlab-ci.yml changes
        if '2' in file_types:
            try:
                ci_response = api_call(f"projects/{project_id}/repository/files/{urllib.parse.quote('.gitlab-ci.yml', safe='')}?ref={urllib.parse.quote(current_ref, safe='')}")
                if 'content' in ci_response:
                    orig_content = base64.b64decode(ci_response['content']).decode('utf-8')
                    updated_content = re.sub(r"^\s*image:.*(\n|$)", "", orig_content, flags=re.MULTILINE)
                    
                    if orig_content != updated_content:
                        changes.append({
                            'file_path': '.gitlab-ci.yml',
                            'old_content': orig_content,
                            'new_content': updated_content
                        })
            except Exception as e:
                logger.error(f"Error getting .gitlab-ci.yml for project {project_id}: {e}")
        
        # Elastic Beanstalk config changes
        if '3' in file_types:
            try:
                eb_response = api_call(f"projects/{project_id}/repository/files/{urllib.parse.quote('.elasticbeanstalk/config.yml', safe='')}?ref={urllib.parse.quote(current_ref, safe='')}")
                if 'content' in eb_response:
                    orig_content = base64.b64decode(eb_response['content']).decode('utf-8')
                    
                    if NEW_DEFAULT_PLATFORM:
                        updated_content = re.sub(
                            r"(default_platform:\s*).*$",
                            f"default_platform: {NEW_DEFAULT_PLATFORM}",
                            orig_content,
                            flags=re.MULTILINE
                        )
                        
                        if orig_content != updated_content:
                            changes.append({
                                'file_path': '.elasticbeanstalk/config.yml',
                                'old_content': orig_content,
                                'new_content': updated_content
                            })
            except Exception as e:
                logger.error(f"Error getting .elasticbeanstalk/config.yml for project {project_id}: {e}")
        
        return jsonify({
            'success': True,
            'changes': changes
        })
    except Exception as e:
        logger.error(f"Error in get_changes: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/commit', methods=['POST'])
def commit_changes():
    """Commit changes to GitLab"""
    try:
        data = request.get_json()
        project_id = data.get('project_id')
        branch = data.get('branch', FEATURE_BRANCH)
        actions = data.get('actions', [])
        
        if not project_id or not actions:
            return jsonify({'success': False, 'error': 'Missing project_id or actions'}), 400
        
        # Ensure feature branch exists
        try:
            branch_check = api_call(f"projects/{project_id}/repository/branches/{urllib.parse.quote(branch, safe='')}")
        except:
            # Create feature branch from source branch
            try:
                api_call(
                    f"projects/{project_id}/repository/branches",
                    "POST",
                    {"branch": branch, "ref": SOURCE_BRANCH}
                )
                logger.info(f"Created feature branch {branch} for project {project_id}")
            except Exception as e:
                return jsonify({'success': False, 'error': f'Failed to create branch: {str(e)}'}), 500
        
        # Prepare commit actions
        commit_actions = []
        for action in actions:
            commit_actions.append({
                'action': 'update',
                'file_path': action['file_path'],
                'content': action['content']
            })
        
        # Commit changes
        commit_payload = {
            "branch": branch,
            "commit_message": "fix: java17-migration",
            "actions": commit_actions
        }
        
        commit_response = api_call(f"projects/{project_id}/repository/commits", "POST", commit_payload)
        
        if 'id' in commit_response:
            return jsonify({
                'success': True,
                'commit_sha': commit_response['id'],
                'message': 'Changes committed successfully'
            })
        else:
            return jsonify({'success': False, 'error': 'Commit failed'}), 500
            
    except Exception as e:
        logger.error(f"Error in commit_changes: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/mr', methods=['POST'])
def create_merge_request():
    """Create merge request"""
    try:
        data = request.get_json()
        project_id = data.get('project_id')
        source_branch = data.get('source_branch', FEATURE_BRANCH)
        target_branch = data.get('target_branch', SOURCE_BRANCH)
        title = data.get('title', f'TASK-1293: java migration')
        
        if not project_id:
            return jsonify({'success': False, 'error': 'Missing project_id'}), 400
        
        # Check if MR already exists
        existing_mrs = api_call(f"projects/{project_id}/merge_requests?state=opened&source_branch={urllib.parse.quote(source_branch, safe='')}")
        
        if existing_mrs and len(existing_mrs) > 0:
            return jsonify({
                'success': True,
                'mr_url': existing_mrs[0].get('web_url'),
                'message': 'MR already exists'
            })
        
        # Create MR
        mr_payload = {
            "source_branch": source_branch,
            "target_branch": target_branch,
            "title": title
        }
        
        mr_response = api_call(f"projects/{project_id}/merge_requests", "POST", mr_payload)
        
        if 'web_url' in mr_response:
            return jsonify({
                'success': True,
                'mr_url': mr_response['web_url'],
                'message': 'MR created successfully'
            })
        else:
            return jsonify({'success': False, 'error': 'Failed to create MR'}), 500
            
    except Exception as e:
        logger.error(f"Error in create_merge_request: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/tags/<int:project_id>', methods=['GET', 'POST'])
def manage_tags(project_id):
    """Get or create tags for deployment"""
    if request.method == 'GET':
        try:
            tags = api_call(f"projects/{project_id}/repository/tags?per_page=100")
            
            # Filter deployment tags
            deployment_tags = []
            for tag in tags:
                tag_name = tag.get('name', '').lower()
                if any(env in tag_name for env in ['dev', 'test', 'performance']):
                    deployment_tags.append({
                        'name': tag.get('name'),
                        'commit_id': tag.get('commit', {}).get('id', ''),
                        'protected': tag.get('protected', False)
                    })
            
            return jsonify({
                'success': True,
                'tags': deployment_tags
            })
        except Exception as e:
            logger.error(f"Error getting tags: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500
    
    elif request.method == 'POST':
        try:
            data = request.get_json()
            tag_name = data.get('tag_name')
            ref = data.get('ref', FEATURE_BRANCH)
            
            if not tag_name:
                return jsonify({'success': False, 'error': 'Missing tag_name'}), 400
            
            # Delete existing tag if exists
            try:
                api_call(f"projects/{project_id}/repository/tags/{urllib.parse.quote(tag_name, safe='')}", "DELETE")
            except:
                pass
            
            # Create new tag
            tag_response = api_call(
                f"projects/{project_id}/repository/tags",
                "POST",
                {"tag_name": tag_name, "ref": ref}
            )
            
            if 'name' in tag_response:
                return jsonify({
                    'success': True,
                    'commit_sha': tag_response.get('commit', {}).get('id', ''),
                    'message': f'Tag {tag_name} created successfully'
                })
            else:
                return jsonify({'success': False, 'error': 'Failed to create tag'}), 500
                
        except Exception as e:
            logger.error(f"Error creating tag: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/pipelines/<int:project_id>', methods=['GET'])
def get_pipelines(project_id):
    """Get pipeline status for a project"""
    try:
        ref = request.args.get('ref', FEATURE_BRANCH)
        
        pipelines = api_call(f"projects/{project_id}/pipelines?ref={urllib.parse.quote(ref, safe='')}&per_page=1")
        
        if pipelines and len(pipelines) > 0:
            latest_pipeline = pipelines[0]
            return jsonify({
                'success': True,
                'pipeline': {
                    'id': latest_pipeline.get('id'),
                    'status': latest_pipeline.get('status'),
                    'ref': latest_pipeline.get('ref'),
                    'sha': latest_pipeline.get('sha'),
                    'web_url': latest_pipeline.get('web_url'),
                    'created_at': latest_pipeline.get('created_at'),
                    'updated_at': latest_pipeline.get('updated_at')
                }
            })
        else:
            return jsonify({
                'success': True,
                'pipeline': None
            })
            
    except Exception as e:
        logger.error(f"Error getting pipelines: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/deploy', methods=['POST'])
def trigger_deployment():
    """Trigger manual deployment job"""
    try:
        data = request.get_json()
        project_id = data.get('project_id')
        pipeline_id = data.get('pipeline_id')
        job_name = data.get('job_name')
        
        if not all([project_id, pipeline_id, job_name]):
            return jsonify({'success': False, 'error': 'Missing required parameters'}), 400
        
        # Get jobs for pipeline
        jobs = api_call(f"projects/{project_id}/pipelines/{pipeline_id}/jobs?per_page=100")
        
        # Find the job
        target_job = None
        for job in jobs:
            if job.get('name') == job_name:
                target_job = job
                break
        
        if not target_job:
            return jsonify({'success': False, 'error': f'Job {job_name} not found'}), 404
        
        # Trigger the job
        job_id = target_job.get('id')
        job_response = api_call(f"projects/{project_id}/jobs/{job_id}/play", "POST")
        
        if 'id' in job_response:
            return jsonify({
                'success': True,
                'job_id': job_response.get('id'),
                'status': job_response.get('status'),
                'message': f'Job {job_name} triggered successfully'
            })
        else:
            return jsonify({'success': False, 'error': 'Failed to trigger job'}), 500
            
    except Exception as e:
        logger.error(f"Error triggering deployment: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/logs', methods=['GET'])
def get_logs():
    """Get list of available log files"""
    try:
        log_dirs = ['migration_logs', 'rollback_logs', 'state_logs']
        all_logs = []
        
        for log_dir in log_dirs:
            if os.path.exists(log_dir):
                for filename in os.listdir(log_dir):
                    if filename.endswith('.log') or filename.endswith('.json'):
                        filepath = os.path.join(log_dir, filename)
                        stat = os.stat(filepath)
                        all_logs.append({
                            'name': filename,
                            'path': filepath,
                            'size_kb': round(stat.st_size / 1024, 2),
                            'timestamp': datetime.datetime.fromtimestamp(stat.st_mtime).isoformat(),
                            'date': datetime.datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S')
                        })
        
        return jsonify({
            'success': True,
            'logs': sorted(all_logs, key=lambda x: x['timestamp'], reverse=True)
        })
    except Exception as e:
        logger.error(f"Error getting logs: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/logs/<path:log_path>', methods=['GET'])
def get_log_content(log_path):
    """Get content of a specific log file"""
    try:
        if not os.path.exists(log_path):
            return jsonify({'success': False, 'error': 'Log file not found'}), 404
        
        with open(log_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        return jsonify({
            'success': True,
            'content': content
        })
    except Exception as e:
        logger.error(f"Error reading log: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    return jsonify({
        'status': 'healthy',
        'gitlab_url': BASE_URL,
        'token_configured': bool(TOKEN),
        'projects_count': len(PROJECT_NAMES)
    })

if __name__ == '__main__':
    if not TOKEN:
        logger.error("GitLab token not configured. Please set GITLAB_TOKEN in .env file")
    if not BASE_URL:
        logger.error("GitLab base URL not configured. Please set GITLAB_BASE_URL in .env file")
    
    logger.info(f"GitLab URL: {BASE_URL}")
    logger.info(f"Projects configured: {len(PROJECT_NAMES)}")
    logger.info(f"Feature branch: {FEATURE_BRANCH}")
    logger.info(f"Source branch: {SOURCE_BRANCH}")
    
    app.run(host='0.0.0.0', port=5000, debug=True)