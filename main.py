import os
import time
import traceback
import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from github import Github, GithubException
from dotenv import load_dotenv

# -------------------------------
# ✅ Load environment variables
# -------------------------------
load_dotenv()
SECRET = os.getenv("SECRET", "jackie")
GITHUB_TOKEN = os.getenv("GH_PAT")
if not GITHUB_TOKEN:
    raise RuntimeError("Missing GH_PAT in environment variables")

# -------------------------------
# ✅ FastAPI app
# -------------------------------
app = FastAPI(title="Captcha Solver Deployment API", version="0.2.0")

# -------------------------------
# ✅ Request model
# -------------------------------
class TaskRequest(BaseModel):
    email: str
    secret: str
    task: str
    round: int
    nonce: str
    brief: str
    checks: list[str]
    evaluation_url: str
    attachments: list[dict] = []

# -------------------------------
# ✅ HTML template for captcha solver
# -------------------------------
def get_captcha_html(sample_image_url: str) -> str:
    return f"""<!DOCTYPE html>
<html>
<head>
    <title>Captcha Solver</title>
</head>
<body>
<h1>Captcha Solver</h1>
<img id="captcha-img" src="" alt="Captcha" />
<p id="solved-text">Solved: [simulated]</p>
<script>
const params = new URLSearchParams(window.location.search);
const imgUrl = params.get('url') || '{sample_image_url}';
document.getElementById('captcha-img').src = imgUrl;
document.getElementById('solved-text').textContent = "Solved: [simulated text]";
console.log("Captcha URL:", imgUrl);
</script>
</body>
</html>
"""

# -------------------------------
# ✅ Deploy to GitHub
# -------------------------------
def deploy_to_github(task_name: str, files: dict) -> dict:
    g = Github(GITHUB_TOKEN)
    user = g.get_user()
    repo_name = task_name.lower().replace(" ", "-").replace("_", "-")

    # Create or fetch repo
    try:
        repo = user.create_repo(repo_name, private=False)
        print(f"✅ Created new repo: {repo.full_name}")
    except GithubException as e:
        if e.status == 422:  # Repo exists
            repo = user.get_repo(repo_name)
            print(f"⚠️ Repo exists, using: {repo.full_name}")
        else:
            raise e

    # Add files to repo
    latest_commit_sha = None
    for path, content in files.items():
        try:
            existing = repo.get_contents(path)
            commit_data = repo.update_file(path, f"Update {path}", content, existing.sha)
        except GithubException as e:
            if e.status == 404:
                commit_data = repo.create_file(path, f"Create {path}", content)
            else:
                raise e
        latest_commit_sha = commit_data['commit'].sha

    # Add LICENSE if missing
    try:
        repo.get_contents("LICENSE")
    except GithubException:
        repo.create_file("LICENSE", "Add MIT License", "MIT License\n\nCopyright (c) 2025 Kavya")

    # GitHub Pages URL
    pages_url = f"https://{user.login}.github.io/{repo.name}/"
    return {"repo_url": repo.html_url, "commit_sha": latest_commit_sha, "pages_url": pages_url}

# -------------------------------
# ✅ Notify evaluation
# -------------------------------
def notify_evaluation(url: str, payload: dict):
    try:
        res = requests.post(url, json=payload, timeout=30)
        print(f"📡 Evaluation notified, status: {res.status_code}")
    except Exception as e:
        print(f"⚠️ Evaluation callback failed: {e}")

# -------------------------------
# ✅ Main endpoint
# -------------------------------
@app.post("/")
async def process_task(req: TaskRequest):
    if req.secret != SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")

    print(f"\n📩 Received task '{req.task}' for round {req.round}")

    # Determine sample image
    sample_image_url = "https://example.com/sample.png"
    for att in req.attachments:
        if att.get("name") == "sample.png" and att.get("url"):
            sample_image_url = att["url"]
            break

    # Generate files
    html_content = get_captcha_html(sample_image_url)
    readme_content = "# Captcha Solver\n\nOpen GitHub Pages URL and pass ?url=IMAGE_URL\n\nLicense: MIT"

    files = {"index.html": html_content, "README.md": readme_content}

    # Deploy to GitHub
    try:
        deployment = deploy_to_github(req.task, files)
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Deployment failed: {e}")

    # Notify evaluation
    payload = {
        "email": req.email,
        "task": req.task,
        "round": req.round,
        "nonce": req.nonce,
        "repo_url": deployment["repo_url"],
        "commit_sha": deployment["commit_sha"],
        "pages_url": deployment["pages_url"],
    }
    notify_evaluation(req.evaluation_url, payload)
    print(f"✅ Task '{req.task}' processed successfully")

    return payload

# -------------------------------
# ✅ Root endpoint
# -------------------------------
@app.get("/")
def root():
    return {"message": "Captcha Solver Deployment API running 🚀"}
