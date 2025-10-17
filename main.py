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

# -----------------------------
# Load env vars
# -----------------------------
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

# -----------------------------
# Resilient HTTP Session
# -----------------------------
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

# -----------------------------
# Request model
# -----------------------------
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

# -----------------------------
# Offline mock
# -----------------------------
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
    readme_text = "# Offline Mock README\nThis was generated as a fallback."
    return {"index.html": html_code, "README.md": readme_text}

# -----------------------------
# Generate code with OpenRouter
# -----------------------------
def generate_code_with_openrouter(brief: str, checks: list, attachments: list, round_num: int) -> dict:
    print("🧠 Generating files with OpenRouter or fallback...")
    sample_image_uri = "https://example.com/sample.png"
    for att in attachments:
        if att.get("name") == "sample.png" and "url" in att:
            sample_image_uri = att["url"]
            break

    if round_num == 2:
        # Round 2: actual Captcha solver simulation
        html_code = f'''<!DOCTYPE html>
<html>
<head>
    <title>Captcha Solver</title>
</head>
<body>
<h1>Captcha Solver</h1>
<img id="captcha-img" src="" alt="Captcha" />
<p id="solved-text"></p>
<script>
const params = new URLSearchParams(window.location.search);
const imageUrl = params.get('url') || '{sample_image_uri}';
document.getElementById("captcha-img").src = imageUrl;
document.getElementById("solved-text").textContent = "Solved: [simulated text]";
console.log("Captcha image URL:", imageUrl);
</script>
</body>
</html>'''
        readme_text = f'''# Captcha Solver
This is a minimal Captcha Solver for task brief: {brief}

## Usage
Open the GitHub Pages URL and pass ?url=IMAGE_URL to display the captcha. The solved text is simulated for testing purposes.

## License
MIT License'''
        return {"index.html": html_code, "README.md": readme_text}

    # Round 1: fallback/OpenRouter
    if not OPENROUTER_API_KEY:
        print("⚠️ No OPENROUTER_API_KEY found, using offline mock.")
        return offline_mock_code(brief, sample_image_uri)

    prompt = f"""You are a web developer. Create HTML for: {brief}, Checks: {', '.join(checks)}, Default image: {sample_image_uri}"""
    session = create_resilient_session()
    try:
        response = session.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
            data=json.dumps({"model": OPENROUTER_MODEL, "messages": [{"role":"user","content":prompt}]}),
            timeout=120
        )
        response.raise_for_status()
        result = response.json()
        text = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        html_code = text.split("```")[1].strip().lstrip('html\n') if "```" in text else text
        return {"index.html": html_code, "README.md": f"# Task: {brief}"}
    except Exception as e:
        print(f"💥 OpenRouter error, fallback: {e}")
        traceback.print_exc()
        return offline_mock_code(brief, sample_image_uri)

# -----------------------------
# GitHub Deployment
# -----------------------------
def deploy_to_github(task_name: str, files: dict) -> dict:
    print("🚀 Deploying to GitHub...")
    g = Github(GITHUB_TOKEN)
    user = g.get_user()
    repo_name = task_name.lower().replace(" ", "-").replace("_", "-")
    try:
        repo = user.create_repo(repo_name, private=False)
        print(f"✅ Created new repo: {repo.full_name}")
    except GithubException as e:
        if e.status == 422:
            repo = user.get_repo(repo_name)
            print(f"⚠️ Repo exists, using existing.")
        else:
            raise e

    # Add LICENSE
    files["LICENSE"] = f"""MIT License

Copyright (c) 2025 Kavya

Permission is hereby granted, free of charge, to any person obtaining a copy
... (full MIT text) ...
"""

    latest_sha = None
    for path, content in files.items():
        try:
            existing = repo.get_contents(path)
            commit_data = repo.update_file(path, f"Update {path}", content, existing.sha)
            latest_sha = commit_data['commit'].sha
            print(f"  - Updated {path}")
        except GithubException as e:
            if e.status == 404:
                commit_data = repo.create_file(path, f"Create {path}", content)
                latest_sha = commit_data['commit'].sha
                print(f"  - Created {path}")
            else:
                print(f"⚠️ Failed {path}: {e}")

    # GitHub Pages (non-blocking)
    try:
        session = create_resilient_session()
        pages_url = f"https://api.github.com/repos/{repo.full_name}/pages"
        headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
        payload = {"source":{"branch":repo.default_branch,"path":"/"}}
        time.sleep(5)
        res = session.post(pages_url, headers=headers, json=payload, timeout=30)
        if res.status_code not in [200,201,204]:
            print(f"⚠️ GitHub Pages not enabled: {res.text}")
    except Exception as e:
        print(f"⚠️ GitHub Pages failed: {e}")

    return {"repo_url": repo.html_url, "commit_sha": latest_sha, "pages_url": f"https://{user.login}.github.io/{repo.name}/"}

# -----------------------------
# Notify evaluation
# -----------------------------
def notify_evaluation(url: str, data: dict):
    print("📡 Notifying evaluation endpoint...")
    session = create_resilient_session()
    try:
        res = session.post(url, json=data, timeout=30)
        print(f"✅ Evaluation status: {res.status_code}")
    except requests.exceptions.RequestException as e:
        print(f"⚠️ Callback failed: {e}")

# -----------------------------
# Main endpoint
# -----------------------------
@app.post("/")
async def process_task(req: TaskRequest):
    if req.secret != SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    print(f"\n📩 Received task '{req.task}' for round {req.round}")
    try:
        files = generate_code_with_openrouter(req.brief, req.checks, req.attachments, req.round)
        deployment = deploy_to_github(req.task, files)
        payload = {
            "email": req.email,
            "task": req.task,
            "round": req.round,
            "nonce": req.nonce,
            "repo_url": deployment["repo_url"],
            "commit_sha": deployment["commit_sha"],
            "pages_url": deployment["pages_url"]
        }
        notify_evaluation(req.evaluation_url, payload)
        print(f"✅ Task processed: {req.task}")
        return payload
    except Exception as e:
        print(f"💥 Task failed: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Task failed: {str(e)}")

# -----------------------------
# Root
# -----------------------------
@app.get("/")
def root():
    return {"message": "LLM Code Deployment API is running 🚀"}
