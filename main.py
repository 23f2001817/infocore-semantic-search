import os
import time
import traceback
import requests
import json
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from github import Github, GithubException
from dotenv import load_dotenv

# -------------------------------------------------------------------------
# ✅ LOAD ENV & CONFIGURATION
# -------------------------------------------------------------------------
load_dotenv()

SECRET = os.getenv("SECRET")
GITHUB_TOKEN = os.getenv("GH_PAT")
HF_API_TOKEN = os.getenv("HF_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

HF_MODEL = "deepseek-ai/deepseek-coder-6.7b-instruct"
OPENROUTER_MODEL = "deepseek/deepseek-coder"

if not all([SECRET, GITHUB_TOKEN]):
    raise RuntimeError("Missing required env vars: SECRET or GH_PAT")

app = FastAPI(title="LLM Code Deployment API", version="0.4.0")

# -------------------------------------------------------------------------
# ✅ RESILIENT HTTP SESSION
# -------------------------------------------------------------------------
def create_resilient_session():
    session = requests.Session()
    retry_strategy = Retry(
        total=5,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "POST", "PUT", "DELETE", "OPTIONS"]
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session

# -------------------------------------------------------------------------
# ✅ REQUEST MODEL
# -------------------------------------------------------------------------
class TaskRequest(BaseModel):
    email: str
    secret: str
    task: str
    round: int
    nonce: str
    brief: str
    checks: list[str]
    evaluation_url: str
    attachments: list = []

# -------------------------------------------------------------------------
# 🧠 GENERATE CODE WITH OPENROUTER
# -------------------------------------------------------------------------
def generate_code_with_openrouter(brief: str, checks: list, attachments: list) -> dict:
    print("🧠 Step 1: Generating files with OpenRouter...")
    if not OPENROUTER_API_KEY:
        print("⚠️ No OPENROUTER_API_KEY found, falling back to offline mock.")
        return offline_mock_code(brief, "")

    sample_image_uri = "https://example.com/sample.png"
    for attachment in attachments:
        if attachment.get("name") == "sample.png" and "url" in attachment:
            sample_image_uri = attachment["url"]
            break

    prompt = f"""
You are an expert web developer. Create a single HTML file for this brief:
{brief}
Checks: {", ".join(checks)}
Default image URL: {sample_image_uri}
"""

    session = create_resilient_session()
    try:
        response = session.post(
            url="https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json"
            },
            data=json.dumps({
                "model": OPENROUTER_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.7,
                "max_tokens": 2000
            }),
            timeout=120
        )
        response.raise_for_status()
        result = response.json()
        print("OpenRouter raw response:", result)

        choices = result.get("choices", [])
        if not choices or 'message' not in choices[0]:
            raise RuntimeError("No valid choices returned from OpenRouter")

        generated_text = choices[0]['message'].get('content', "")
        # Extract HTML inside triple backticks if present
        if "```" in generated_text:
            html_code = generated_text.split("```")[1].strip()
            if html_code.lower().startswith("html"):
                html_code = html_code[4:].strip()
        else:
            html_code = generated_text.strip()

        readme_content = f"# Task: {brief}\n\nGenerated using OpenRouter model."
        return {"index.html": html_code, "README.md": readme_content}

    except Exception as e:
        print(f"💥 OpenRouter error, falling back to offline mock. Error: {e}")
        traceback.print_exc()
        return offline_mock_code(brief, sample_image_uri)

# -------------------------------------------------------------------------
# 💾 OFFLINE MOCK
# -------------------------------------------------------------------------
def offline_mock_code(brief: str, sample_image_uri: str) -> dict:
    html_code = f'''<!DOCTYPE html>
<html>
<head>
    <title>Sample Page</title>
</head>
<body>
<script>
const params = new URLSearchParams(window.location.search);
const imageUrl = params.get('url') || '{sample_image_uri}';
console.log("Using image URL:", imageUrl);
</script>
</body>
</html>'''
    readme_text = "# Offline Mock README\n\nThis was generated as a fallback."
    return {"index.html": html_code, "README.md": readme_text}

# -------------------------------------------------------------------------
# 🚀 DEPLOY TO GITHUB
# -------------------------------------------------------------------------
def deploy_to_github(task_name: str, files: dict) -> dict:
    print("🚀 Step 2: Deploying to GitHub...")
    g = Github(GITHUB_TOKEN)
    user = g.get_user()
    repo_name = task_name.lower().replace(" ", "-").replace("_", "-")

    try:
        repo = user.create_repo(repo_name, private=False)
        print(f"✅ Created new repo: {repo.full_name}")
    except GithubException as e:
        if e.status == 422:
            print(f"⚠️ Repo '{repo_name}' already exists, using existing repo.")
            repo = user.get_repo(repo_name)
        else:
            raise e

    files["LICENSE"] = "MIT License text here..."

    latest_commit_sha = None
    for path, content in files.items():
        try:
            existing_content = repo.get_contents(path)
            commit_data = repo.update_file(path, f"Update {path}", content, existing_content.sha)
            latest_commit_sha = commit_data['commit'].sha
            print(f"  - Updated file: {path}")
        except GithubException as e:
            if e.status == 404:
                commit_data = repo.create_file(path, f"Create {path}", content)
                latest_commit_sha = commit_data['commit'].sha
                print(f"  - Created file: {path}")
            else:
                print(f"⚠️ Failed to update/create file {path}: {e}")

    # Enable GitHub Pages
    try:
        session = create_resilient_session()
        pages_url = f"https://api.github.com/repos/{repo.full_name}/pages"
        headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
        pages_payload = {"source": {"branch": repo.default_branch, "path": "/"}}
        time.sleep(5)
        response = session.post(pages_url, headers=headers, json=pages_payload, timeout=30)
        if response.status_code not in [200, 201, 204]:
            print(f"⚠️ Could not enable GitHub Pages. Response: {response.text}")
    except Exception as e:
        print(f"⚠️ GitHub Pages enabling failed: {e}")

    return {
        "repo_url": repo.html_url,
        "commit_sha": latest_commit_sha,
        "pages_url": f"https://{user.login}.github.io/{repo.name}/"
    }

# -------------------------------------------------------------------------
# 📨 NOTIFY EVALUATION
# -------------------------------------------------------------------------
def notify_evaluation(url: str, data: dict):
    print("📡 Step 3: Notifying evaluation endpoint...")
    session = create_resilient_session()
    try:
        res = session.post(url, json=data, timeout=30)
        print(f"✅ Evaluation response status: {res.status_code}")
        res.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"⚠️ Callback to evaluation URL failed (non-blocking): {e}")

# -------------------------------------------------------------------------
# 🧩 MAIN ENDPOINT
# -------------------------------------------------------------------------
@app.post("/")
async def process_task(req: TaskRequest):
    if req.secret != SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")

    print(f"\n📩 Received task '{req.task}' for round {req.round}")
    print(f"Received request: {req.dict()}")

    try:
        files = generate_code_with_openrouter(req.brief, req.checks, req.attachments)
        deployment_details = deploy_to_github(req.task, files)
        evaluation_payload = {
            "email": req.email,
            "task": req.task,
            "round": req.round,
            "nonce": req.nonce,
            "repo_url": deployment_details["repo_url"],
            "commit_sha": deployment_details["commit_sha"],
            "pages_url": deployment_details["pages_url"],
        }
        notify_evaluation(req.evaluation_url, evaluation_payload)
        print(f"✅ Successfully processed task: {req.task}")
        return evaluation_payload

    except Exception as e:
        print(f"💥 Task processing failed: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Task processing failed: {str(e)}")

# -------------------------------------------------------------------------
# ✅ ROOT
# -------------------------------------------------------------------------
@app.get("/")
def root():
    return {"message": "LLM Code Deployment API is running 🚀"}
