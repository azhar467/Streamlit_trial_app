import urllib.request, json, base64, re, urllib.parse, datetime, time

# --- CONFIGURATION ---
COMPANY_DOMAIN = "lfg" 
BASE_URL = f"https://gitlab.{COMPANY_DOMAIN}.com/api/v4"
TOKEN = "glpat-" 
PROJECT_IDS = [] 

JIRA_ID = "4323"
UPGRADE_TYPE = "java17-migration"
FEATURE_BRANCH = f"task-{JIRA_ID}-{UPGRADE_TYPE}"
SOURCE_BRANCH = "develop"
MR_TITLE = f"TASK-{JIRA_ID}: java migration"

TARGET_PARENT_VERSION = "1.8.3"
NEW_DEFAULT_PLATFORM = "arn:aws:elasticbeanstalk:us-east-1::platform/Corretto 17 running on 64bit Amazon Linux 2/3.10.1"

# ---------------------------------------------------------

def log(msg, level="INFO"):
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{timestamp}] [{level}] {msg}")

def api_call(endpoint, method="GET", data=None):
    url = f"{BASE_URL}/{endpoint}"
    req = urllib.request.Request(url, method=method)
    req.add_header("PRIVATE-TOKEN", TOKEN)
    req.add_header("Content-Type", "application/json")
    body = json.dumps(data).encode("utf-8") if data else None
    try:
        with urllib.request.urlopen(req, data=body) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:
        return {"error": True}

def update_parent_block(match):
    block = match.group(0)
    block = re.sub(r"<version>.*?</version>", f"<version>{TARGET_PARENT_VERSION}</version>", block)
    block = re.sub(r"(parent-pom-).*?(\.xml)", rf"\1{TARGET_PARENT_VERSION}\2", block)
    return block

def wait_and_trigger(pid, pipeline_id, target_job_name):
    log(f"   ⏳ Monitoring Pipeline {pipeline_id} for success...")
    for _ in range(30):
        pipe = api_call(f"projects/{pid}/pipelines/{pipeline_id}")
        status = pipe.get("status")
        if status == "success":
            log(f"   ✅ Success! Finding '{target_job_name}'...")
            jobs = api_call(f"projects/{pid}/pipelines/{pipeline_id}/jobs")
            for job in jobs:
                if job['name'] == target_job_name and job['status'] == 'manual':
                    api_call(f"projects/{pid}/jobs/{job['id']}/play", method="POST")
                    log(f"   🚀 Triggered {target_job_name} successfully.")
                    return True
            return False
        elif status in ["failed", "canceled"]:
            return False
        time.sleep(30)
    return False

def main():
    if not PROJECT_IDS: return log("Please add PROJECT_IDS.", "ERROR")
    
    dry_run = input("\nEnable Dry Run? (y/n): ").lower() == 'y'
    choices = input("Select Updates (1:POM, 2:CI, 3:EB): ").replace(" ", "").split(',')
    do_orch = input("Manage Tags & Orchestrate? (y/n): ").lower() == 'y'

    for pid in PROJECT_IDS:
        # Fetch Project Name
        project_info = api_call(f"projects/{pid}")
        p_name = project_info.get('name', f"ID:{pid}")
        
        log(f"--- Processing Project: {p_name} ({pid}) ---")
        actions = []
        
        br_check = api_call(f"projects/{pid}/repository/branches/{FEATURE_BRANCH}")
        current_ref = FEATURE_BRANCH if "name" in br_check else SOURCE_BRANCH
        
        # --- File Logic (POM, CI, EB) ---
        if '1' in choices:
            res = api_call(f"projects/{pid}/repository/files/pom.xml?ref={current_ref}")
            if "content" in res:
                orig = base64.b64decode(res['content']).decode('utf-8')
                upd = re.sub(r"<java\.version>.*?</java\.version>", "<java.version>17</java.version>", orig)
                upd = re.sub(r"<maven\.compiler\.(source|target)>.*?</maven\.compiler\.(source|target)>", r"<maven.compiler.\1>17</maven.compiler.\1>", upd)
                if "<parent>" in upd:
                    upd = re.sub(r"<parent>[\s\S]*?</parent>", update_parent_block, upd)
                if orig != upd: actions.append({"action": "update", "file_path": "pom.xml", "content": upd})

        if '2' in choices:
            res = api_call(f"projects/{pid}/repository/files/.gitlab-ci.yml?ref={current_ref}")
            if "content" in res:
                orig = base64.b64decode(res['content']).decode('utf-8')
                upd = re.sub(r"^\s*image:.*(\n|$)", "", orig, flags=re.MULTILINE)
                if orig != upd: actions.append({"action": "update", "file_path": ".gitlab-ci.yml", "content": upd})

        if '3' in choices:
            path = urllib.parse.quote(".elasticbeanstalk/config.yml", safe='')
            res = api_call(f"projects/{pid}/repository/files/{path}?ref={current_ref}")
            if "content" in res:
                orig = base64.b64decode(res['content']).decode('utf-8')
                upd = re.sub(r"(default_platform:\s*).*$", f"default_platform: {NEW_DEFAULT_PLATFORM}", orig, flags=re.MULTILINE)
                if orig != upd: actions.append({"action": "update", "file_path": ".elasticbeanstalk/config.yml", "content": upd})

        # --- EXECUTION: Commits ---
        if actions and not dry_run:
            if "name" not in br_check:
                api_call(f"projects/{pid}/repository/branches", "POST", {"branch": FEATURE_BRANCH, "ref": SOURCE_BRANCH})
            api_call(f"projects/{pid}/repository/commits", "POST", {"branch": FEATURE_BRANCH, "commit_message": f"fix: {UPGRADE_TYPE}", "actions": actions})
            log(f"   [SUCCESS] Changes pushed to {FEATURE_BRANCH}")
        elif not actions:
            log(f"   ✨ {p_name} is code-compliant.")

        # --- EXECUTION: Smart MR Management ---
        if not dry_run:
            br_status = api_call(f"projects/{pid}/repository/branches/{FEATURE_BRANCH}")
            if "name" in br_status:
                # Check if MR already exists
                existing_mrs = api_call(f"projects/{pid}/merge_requests?state=opened&source_branch={FEATURE_BRANCH}")
                if existing_mrs and len(existing_mrs) > 0:
                    log(f"   ℹ️ MR already exists for {p_name}: {existing_mrs[0]['web_url']}")
                else:
                    if input(f"   ❓ No MR found for {p_name}. Raise one now? (y/n): ").lower() == 'y':
                        api_call(f"projects/{pid}/merge_requests", "POST", {"source_branch": FEATURE_BRANCH, "target_branch": SOURCE_BRANCH, "title": MR_TITLE})
                        log(f"   ✅ MR raised for {p_name}.")

        # --- EXECUTION: Orchestration ---
        if do_orch and not dry_run:
            for tag in ["dev", "azure-dev"]:
                exists = api_call(f"projects/{pid}/repository/tags/{tag}")
                if isinstance(exists, dict) and "name" in exists:
                    api_call(f"projects/{pid}/repository/tags/{tag}", method="DELETE")
                    t_res = api_call(f"projects/{pid}/repository/tags", "POST", {"tag_name": tag, "ref": FEATURE_BRANCH})
                    pipe_id = t_res.get('commit', {}).get('last_pipeline', {}).get('id')
                    if pipe_id and wait_and_trigger(pid, pipe_id, "eb-terminate"):
                        wait_and_trigger(pid, pipe_id, f"eb-deploy-{tag}")

    log("🏁 Job finished.")

if __name__ == "__main__":
    main()
