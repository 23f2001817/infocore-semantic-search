import os
import time
import requests
import json
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv
from github import Github  # pip install PyGithub if needed (but avoid extras per constraints)
import base64  # For handling attachments

load_dotenv()

# Secrets from .env
SECRET = os.getenv("SECRET")  # Your chosen secret for Google Form
GH_PAT = os.getenv("GH_PAT")  # GitHub Personal Access Token (generate at github.com/settings/tokens with 'repo' scope)
HF_TOKEN = os.getenv("HF_TOKEN")  # Hugging Face token (regenerate if error)

if not all([SECRET, GH_PAT, HF_TOKEN]):
    raise RuntimeError("Missing env vars")

# LLM model for code generation (use free HF model)
LLM_MODEL = "meta-llama/Llama-2-7b-chat-hf"  # Or another free one if limits hit
HF_API_URL = f"https://api-inference.huggingface.co/models/{LLM_MODEL}"

app = FastAPI()

class TaskRequest(BaseModel):
    email: str
    secret: str
    task: str
    round: int
    nonce: str
    brief: str
    checks: list[str]
    evaluation_url: str
    attachments: list[dict] = []  # e.g., [{"name": "sample.png", "url": "data:image/png;base64,..."}]

def generate_code_with_llm(brief: str, attachments: list[dict], round: int) -> dict:
    # Prompt LLM to generate index.html and README.md
    prompt = f"Generate a minimal static web app (index.html with JS/CSS via CDN) for: {brief}. Use attachments if needed (e.g., default image data URI). For round {round}. Include Bootstrap, handle ?url=. Output as JSON: {{'index.html': '...code...', 'README.md': '...markdown...'}}"
    headers = {"Authorization": f"Bearer {HF_TOKEN}"}
    payload = {"inputs": prompt}
    response = requests.post(HF_API_URL, headers=headers, json=payload)
    if response.status_code != 200:
        raise HTTPException(500, "LLM error")
    generated = response.json()[0]['generated_text']  # Parse as JSON
    return json.loads(generated)  # Assume LLM outputs valid JSON

def create_and_deploy_repo(task: str, files: dict, round: int):
    g = Github(GH_PAT)
    user = g.get_user()
    repo_name = task  # e.g., "captcha-solver-abc123"
    try:
        repo = user.create_repo(repo_name, private=False)  # Public
    except:
        raise HTTPException(500, "Repo creation failed")
    
    # Add files: index.html, README.md, LICENSE (MIT text)
    mit_license = "MIT License\n\nCopyright (c) 2025 [Your Name]\n\n..."  # Full MIT text from opensource.org/licenses/MIT
    repo.create_file("LICENSE", "Add MIT license", mit_license)
    for path, content in files.items():
        repo.create_file(path, f"Add {path} for round {round}", content)
    
    # Enable GitHub Pages (via API—set branch to main, source to root)
    # Note: API may not directly enable; use requests to GitHub API endpoint or manual if needed
    headers = {"Authorization": f"token {GH_PAT}", "Accept": "application/vnd.github.switcheroo-preview+json"}
    requests.patch(f"https://api.github.com/repos/{user.login}/{repo_name}/pages", headers=headers, json={"source": {"branch": "main", "path": "/"}})
    
    # Wait for deploy (poll for 200 OK)
    pages_url = f"https://{user.login}.github.io/{repo_name}/"
    for _ in range(10):  # Up to 2 min
        if requests.head(pages_url).status_code == 200:
            break
        time.sleep(12)
    else:
        raise HTTPException(500, "Pages not live")
    
    # Get commit SHA
    commit_sha = repo.get_branch("main").commit.sha
    return repo.html_url, commit_sha, pages_url

@app.post("/")
async def build_task(req: TaskRequest, request: Request):
    if req.secret != SECRET:
        raise HTTPException(403, "Invalid secret")
    
    print(f"Received request: {req.dict()}")  # Log for nonce/evaluation_url
    
    # Generate files with LLM
    files = generate_code_with_llm(req.brief, req.attachments, req.round)
    
    # Deploy
    repo_url, commit_sha, pages_url = create_and_deploy_repo(req.task, files, req.round)
    
    # Notify evaluation_url with retries
    payload = {
        "email": req.email,
        "task": req.task,
        "round": req.round,
        "nonce": req.nonce,
        "repo_url": repo_url,
        "commit_sha": commit_sha,
        "pages_url": pages_url
    }
    delay = 1
    while True:
        response = requests.post(req.evaluation_url, json=payload)
        if response.status_code == 200:
            break
        time.sleep(delay)
        delay *= 2  # Exponential backoff
        if delay > 32:
            raise HTTPException(500, "Notify failed")
    
    return {"status": "success"}

# For round 2: Same endpoint handles (check req.round == 2, update existing repo instead of create)
# In create_and_deploy_repo, if round > 1, get existing repo and update files