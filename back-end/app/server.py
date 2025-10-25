from fastapi import FastAPI, Body, Query, HTTPException, Request
from typing import Optional
from app.git_context_manager import Git_Context_Manager
from datetime import datetime
from dotenv import load_dotenv
import os
from app.llm_request_model import LLMRequest
from pydantic import BaseModel
from typing import List
import httpx
from datetime import datetime, timezone
from fastapi import Body, Query
import logging
from fastapi.middleware.cors import CORSMiddleware
from collections import defaultdict
from time import time

# Import routers
from app.routers import rewards, proposals
from app.config import get_config, StorageMode
from app.agents.spec_agent import SpecAgent
from app.middleware.abuse_detection import abuse_detector
from app.utils.workspace_manager import get_workspace_path
from app.repositories.proposal_repository import get_proposal_repository
import os
from typing import List, Dict
import json
from pathlib import Path

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("server.log", mode="w"), logging.StreamHandler()],
    force=True,
)
logger = logging.getLogger(__name__)

CONTEXT_PILOT_URL = "http://localhost:8000"

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

app = FastAPI(
    title="ContextPilot API",
    description="Manage long-term project scope using Git + LLMs + Web3 incentives. Stay aligned and intentional.",
    version="2.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# Simple in-memory rate limiter
rate_limit_store = defaultdict(list)


def check_rate_limit(
    client_ip: str, max_requests: int = 100, window_seconds: int = 3600
) -> bool:
    """
    Rate limiting: max_requests per window_seconds per IP.
    Default: 100 requests/hour per IP
    """
    now = time()
    cutoff = now - window_seconds

    # Clean old requests
    rate_limit_store[client_ip] = [
        timestamp for timestamp in rate_limit_store[client_ip] if timestamp > cutoff
    ]

    # Check limit
    if len(rate_limit_store[client_ip]) >= max_requests:
        return False

    # Add current request
    rate_limit_store[client_ip].append(now)
    return True


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    """Apply rate limiting and abuse detection to all requests"""
    client_ip = request.client.host if request.client else "unknown"

    # Skip rate limiting for health check
    if request.url.path == "/health":
        return await call_next(request)

    # Check for abuse patterns
    abuse_check = abuse_detector.check_request(request)
    if abuse_check["should_block"]:
        logger.error(f"🚫 Blocking request from {client_ip}: {abuse_check['reason']}")
        raise HTTPException(
            status_code=403, detail="Access denied due to suspicious activity."
        )

    if abuse_check["suspicious"]:
        logger.warning(
            f"⚠️ Suspicious request from {client_ip}: {abuse_check['reason']}"
        )

    # Check rate limit (10000 req/hour per IP for development)
    if not check_rate_limit(client_ip, max_requests=10000, window_seconds=3600):
        logger.warning(f"Rate limit exceeded for IP: {client_ip}")
        abuse_detector.record_error(client_ip, 429)
        raise HTTPException(
            status_code=429, detail="Rate limit exceeded. Try again later."
        )

    response = await call_next(request)

    # Record errors for abuse detection
    if response.status_code >= 400:
        abuse_detector.record_error(client_ip, response.status_code)

    return response


# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure properly in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(rewards.router)

# Proposals router: Only use in CLOUD mode
# In LOCAL mode, we use custom endpoints below with file storage
config = get_config()
if config.is_cloud_storage:
    logger.info("📊 Using Firestore proposals router (CLOUD mode)")
    app.include_router(proposals.router)
else:
    logger.info("📁 Using file-based proposals endpoints (LOCAL mode)")
    # Custom endpoints registered below

# app.include_router(events.router)


def get_manager(workspace_id: str = "default"):
    return Git_Context_Manager(workspace_id=workspace_id)


@app.get("/context")
def get_context(workspace_id: str = Query("default")):
    logger.info(f"GET /context called with workspace_id: {workspace_id}")

    try:
        # Try to read from PROJECT.md, GOAL.md, STATUS.md, MILESTONES.md
        from pathlib import Path
        import re

        project_root = Path("/app")
        logger.info(f"Project root: {project_root}")

        context = {
            "checkpoint": {
                "project_name": "ContextPilot - AI Development Assistant",
                "goal": "Transform development workflows with AI-powered multi-agent assistance",
                "current_status": "Core Functionality Complete",
                "milestones": [],
            }
        }

        # Try to read PROJECT.md (override defaults if exists)
        project_file = project_root / "PROJECT.md"
        if project_file.exists():
            try:
                content = project_file.read_text(encoding="utf-8")
                match = re.search(r"#\s+(.+?)(?:\n|$)", content)
                if match:
                    context["checkpoint"]["project_name"] = match.group(1).strip()
                    logger.info(f"Loaded project name from PROJECT.md")
            except Exception as e:
                logger.warning(f"Error reading PROJECT.md: {e}")

        # Try to read GOAL.md
        goal_file = project_root / "GOAL.md"
        if goal_file.exists():
            try:
                content = goal_file.read_text(encoding="utf-8")
                match = re.search(r"\*\*(.+?)\*\*", content)
                if match:
                    context["checkpoint"]["goal"] = match.group(1).strip()
                    logger.info(f"Loaded goal from GOAL.md")
            except Exception as e:
                logger.warning(f"Error reading GOAL.md: {e}")

        # Try to read STATUS.md
        status_file = project_root / "STATUS.md"
        if status_file.exists():
            try:
                content = status_file.read_text(encoding="utf-8")
                match = re.search(r"Current Status:\s+\*\*(.+?)\*\*", content)
                if match:
                    context["checkpoint"]["current_status"] = match.group(1).strip()
                    logger.info(f"Loaded status from STATUS.md")
            except Exception as e:
                logger.warning(f"Error reading STATUS.md: {e}")

        # Try to read MILESTONES.md
        milestones_file = project_root / "MILESTONES.md"
        logger.info(f"Looking for MILESTONES.md at: {milestones_file}")
        logger.info(f"MILESTONES.md exists: {milestones_file.exists()}")
        if milestones_file.exists():
            try:
                content = milestones_file.read_text(encoding="utf-8")
                logger.info(f"MILESTONES.md content length: {len(content)}")
                completed_matches = re.findall(
                    r"- ✅ \*\*(.+?)\*\*:?\s*(.+?)(?:\n|$)", content
                )
                milestones = []
                for name, desc in completed_matches:
                    milestones.append(
                        {
                            "name": name.strip(),
                            "status": "completed",
                            "due": "completed",
                        }
                    )
                context["checkpoint"]["milestones"] = milestones
                logger.info(f"Loaded {len(milestones)} milestones from MILESTONES.md")
            except Exception as e:
                logger.warning(f"Error reading MILESTONES.md: {e}")
        else:
            logger.warning(f"MILESTONES.md not found at {milestones_file}")

        logger.info(f"Context loaded from .md files: {context}")
        return context

    except Exception as e:
        logger.error(f"Error in context endpoint: {str(e)}", exc_info=True)
        # Return default context on error
        return {
            "checkpoint": {
                "project_name": "ContextPilot - AI Development Assistant",
                "goal": "Transform development workflows with AI-powered multi-agent assistance",
                "current_status": "Core Functionality Complete",
                "milestones": [],
            }
        }


@app.get("/context/milestones")
def get_milestones(workspace_id: str = Query("default")):
    manager = get_manager(workspace_id)
    print("context", manager.get_project_context())
    context = manager.get_project_context()
    checkpoint = context.get("checkpoint", {})
    milestones = checkpoint.get("milestones", [])
    print("milestones", milestones)
    return {"milestones": milestones}


@app.post("/commit")
def manual_commit(
    message: str = "Manual context update",
    agent: str = "manual",
    workspace_id: str = Query("default"),
):
    manager = get_manager(workspace_id)
    commit = manager.commit_changes(message=message, agent=agent)
    return {"status": "success", "commit": commit}


@app.post("/commit/llm")
def llm_commit(workspace_id: str = Query("default")):
    manager = get_manager(workspace_id)
    diff = manager.show_diff()
    try:
        summary = manager.summarize_diff_for_commit(diff)
    except Exception as e:
        summary = f"Error calling OpenAI: {str(e)}"
    commit = manager.commit_changes(message=summary, agent="llm")
    return {"status": "success", "commit": commit, "summary": summary}


@app.get("/log")
def get_log(workspace_id: str = Query("default")):
    manager = get_manager(workspace_id)
    return manager.get_project_context()["history"]


@app.get("/coach")
def get_coach_tip(workspace_id: str = Query("default")):
    logger.info(f"GET /coach called with workspace_id: {workspace_id}")

    try:
        logger.info(f"Creating Git_Context_Manager for workspace: {workspace_id}")
        manager = get_manager(workspace_id)

        logger.info("Getting project context...")
        state = manager.get_project_context()
        checkpoint = state.get("checkpoint")
        history = state.get("history", [])

        if not checkpoint:
            logger.info("No checkpoint data available")
            return {"tip": "No checkpoint data available yet."}

        today = datetime.today().date()
        milestones = checkpoint.get("milestones", [])
        status = checkpoint.get("current_status", "No current status recorded.")

        logger.info(f"Current status: {status}")
        logger.info(f"Number of milestones: {len(milestones)}")
        logger.info(f"Number of history entries: {len(history)}")

        if history:
            last_entry = history[-1]
            last_time = datetime.fromisoformat(last_entry["timestamp"]).date()
            days_inactive = (today - last_time).days
            logger.info(f"Days since last activity: {days_inactive}")

            if days_inactive >= 3:
                tip = (
                    f"You haven't made updates in {days_inactive} days.\n"
                    f"🔄 Review your current status: '{status}' and consider logging progress today!"
                )
                logger.info("Generated inactivity tip")
                return {"tip": tip}
            elif days_inactive == 0:
                logger.info("Generated momentum tip")
                return {"tip": "You're on a roll! Keep the momentum going 💪"}

        if milestones:
            next_milestone = sorted(milestones, key=lambda m: m["due"])[0]
            due_date = datetime.strptime(next_milestone["due"], "%Y-%m-%d").date()
            days_left = (due_date - today).days
            name = next_milestone["name"]
            logger.info(f"Next milestone: {name}, days left: {days_left}")

            if days_left < 0:
                tip = f"The milestone '{name}' was due {abs(days_left)} days ago. Consider updating your checkpoint."
                logger.info("Generated overdue milestone tip")
                return {"tip": tip}
            elif days_left == 0:
                logger.info("Generated deadline tip")
                return {
                    "tip": f"Today is the deadline for '{name}'! Time to wrap it up 🎯"
                }
            else:
                logger.info("Generated milestone reminder tip")
                return {
                    "tip": f"{days_left} day(s) left until the milestone '{name}'. Stay focused and make progress today!"
                }

        logger.info("No milestones found, generating default tip")
        return {
            "tip": "No milestones found. You can add one to help guide your progress!"
        }

    except Exception as e:
        logger.error(f"Error in coach endpoint: {str(e)}")
        return {"tip": f"Error generating coach tip: {str(e)}"}


@app.post("/update")
def update_checkpoint(
    project_name: str = Body(...),
    goal: str = Body(...),
    current_status: str = Body(...),
    milestones: list[dict] = Body(...),
    workspace_id: str = Query("default"),
):
    logger.info(f"POST /update called with workspace_id: {workspace_id}")
    logger.info(f"Project name: {project_name}")
    logger.info(f"Goal: {goal}")
    logger.info(f"Current status: {current_status}")
    logger.info(f"Number of milestones: {len(milestones)}")

    try:
        logger.info(f"Creating Git_Context_Manager for workspace: {workspace_id}")
        manager = get_manager(workspace_id)

        logger.info("Getting current project context...")
        state = manager.get_project_context()

        logger.info("Updating checkpoint data...")
        state["checkpoint"].update(
            {
                "project_name": project_name,
                "goal": goal,
                "current_status": current_status,
                "milestones": milestones,
            }
        )

        logger.info("Writing updated context to files...")
        manager.write_context(state)

        logger.info("Committing changes...")
        commit = manager.commit_changes(
            message="Checkpoint updated via API", agent="update"
        )
        logger.info(f"Commit successful with hash: {commit}")

        response = {"status": "updated", "commit": commit}
        logger.info("Update endpoint completed successfully")
        return response

    except Exception as e:
        logger.error(f"Error in update endpoint: {str(e)}")
        return {"status": "error", "message": f"Failed to update checkpoint: {str(e)}"}


@app.post("/push-to-llm")
def push_context_to_llm(request: LLMRequest, workspace_id: str = Query("default")):
    manager = get_manager(workspace_id)
    try:
        context = manager.get_project_context()
        result = manager.query_llm(prompt=request.prompt, context=context)
        return {"response": result}
    except Exception as e:
        return {"error": str(e)}


@app.get("/summary")
def get_summary(workspace_id: str = Query("default")):
    manager = get_manager(workspace_id)
    context = manager.get_project_context()
    checkpoint = context.get("checkpoint", {})
    milestones = checkpoint.get("milestones", [])
    remaining = [m["name"] for m in milestones if "due" in m]

    return {
        "project_name": checkpoint.get("project_name"),
        "goal": checkpoint.get("goal"),
        "status": checkpoint.get("current_status"),
        "milestones": remaining,
    }


@app.post("/reflect")
def reflect(
    style: LLMRequest = Body(default="motivational"),
    workspace_id: str = Query("default"),
):
    manager = get_manager(workspace_id)
    prompt = (
        f"Gere uma reflexão de coaching no estilo '{style}' com base no contexto atual."
    )
    context = manager.get_project_context()
    try:
        result = manager.query_llm(prompt=prompt, context=context)
        return {"reflection": result}
    except Exception as e:
        return {"error": f"Erro ao gerar reflexão: {str(e)}"}


@app.post("/plan")
def plan(workspace_id: str = Query("default")):
    manager = get_manager(workspace_id)
    prompt = "Com base no contexto atual e nos marcos definidos, gere um plano de ação prático com etapas claras."
    context = manager.get_project_context()
    try:
        result = manager.query_llm(prompt=prompt, context=context)
        return {"plan": result}
    except Exception as e:
        return {"error": f"Erro ao gerar plano: {str(e)}"}


@app.get("/llm-history")
def llm_history(workspace_id: str = Query("default")):
    manager = get_manager(workspace_id)
    context = manager.get_project_context()
    llm_commits = [
        entry for entry in context.get("history", []) if entry.get("agent") == "llm"
    ]
    return {"llm_commits": llm_commits}


@app.post("/validate-goal")
def validate_goal(workspace_id: str = Query("default")):
    manager = get_manager(workspace_id)
    context = manager.get_project_context()
    goal = context.get("checkpoint", {}).get("goal", "")
    if not goal:
        return {
            "valid": False,
            "feedback": "Nenhum objetivo encontrado no contexto atual.",
        }

    prompt = f"Avalie se este objetivo é claro, mensurável e realista: '{goal}'"
    try:
        result = manager.query_llm(prompt=prompt, context=context)
        return {"valid": True, "feedback": result}
    except Exception as e:
        return {"valid": False, "feedback": f"Erro ao consultar LLM: {str(e)}"}


class Milestone(BaseModel):
    name: str
    due: str  # Format: YYYY-MM-DD


class ContextPayload(BaseModel):
    project_name: str
    goal: str
    initial_status: str
    milestones: List[Milestone]


@app.post("/generate-context")
async def generate_context(payload: ContextPayload, workspace_id: str = "default"):
    logger.info(f"POST /generate-context called with workspace_id: {workspace_id}")
    logger.info(f"Project name: {payload.project_name}")
    logger.info(f"Goal: {payload.goal}")
    logger.info(f"Initial status: {payload.initial_status}")
    logger.info(f"Number of milestones: {len(payload.milestones)}")

    try:
        logger.info(f"Creating Git_Context_Manager for workspace: {workspace_id}")
        manager = get_manager(workspace_id)

        # Create initial context structure
        context = {
            "checkpoint": {
                "project_name": payload.project_name,
                "goal": payload.goal,
                "current_status": payload.initial_status,
                "milestones": [milestone.dict() for milestone in payload.milestones],
            },
            "history": [],
        }
        logger.info("Context structure created successfully")

        # Write the context to files
        logger.info("Writing context to files...")
        manager.write_context(context)
        logger.info("Context written to files successfully")

        # Commit the changes
        commit_message = (
            f"Initial context generated for project: {payload.project_name}"
        )
        logger.info(f"Committing changes with message: {commit_message}")
        commit = manager.commit_changes(
            message=commit_message, agent="generate-context"
        )
        logger.info(f"Commit successful with hash: {commit}")

        response = {
            "status": "success",
            "message": f"Context generated successfully for project: {payload.project_name}",
            "commit": commit,
            "context": context,
        }
        logger.info("Generate context endpoint completed successfully")
        return response

    except Exception as e:
        logger.error(f"Error in generate-context endpoint: {str(e)}")
        return {"status": "error", "message": f"Failed to generate context: {str(e)}"}


@app.post("/context/commit-task")
def commit_task(
    task_name: str = Body(...),
    agent: str = Body(...),
    notes: str = Body(...),
    workspace_id: str = Query("default"),
):
    logger.info(f"POST /context/commit-task called with workspace_id: {workspace_id}")
    logger.info(f"Task name: {task_name}")
    logger.info(f"Agent: {agent}")
    logger.info(f"Notes: {notes}")

    try:
        logger.info(f"Creating Git_Context_Manager for workspace: {workspace_id}")
        manager = get_manager(workspace_id)

        # Log to history
        logger.info("Logging task history...")
        manager.log_history(message=notes, agent=agent)

        # ✅ Criar commit
        logger.info("Checking if there are changes to commit...")
        if not manager.repo.is_dirty(untracked_files=True):
            logger.warning("No changes detected. Skipping commit.")
            return {"status": "error", "message": "No changes detected to commit."}

        commit_message = f"Agent {agent} updated task: {task_name}"
        logger.info(f"Committing changes with message: {commit_message}")
        commit = manager.commit_changes(message=commit_message, agent=agent)
        logger.info(f"Commit successful with hash: {commit}")

        response = {"status": "success", "commit": commit}
        logger.info("Commit task endpoint completed successfully")
        return response

    except Exception as e:
        logger.error(f"Error in commit-task endpoint: {str(e)}")
        return {"status": "error", "message": f"Failed to commit task: {str(e)}"}


@app.post("/context/push")
def push_context(
    workspace_id: str = Query("default"),
    remote_name: str = Query("origin"),
    branch: str = Query("main"),
    auto_commit: bool = Query(False),
    commit_message: str = Query("Auto final sync"),
    agent: str = Query("manual"),
):
    logger.info(f"POST /context/push called for workspace_id: {workspace_id}")
    try:
        manager = get_manager(workspace_id)

        if auto_commit:
            final_commit_hash = manager.commit_changes(
                message=commit_message, agent=agent
            )
            logger.info(f"Final commit hash before push: {final_commit_hash}")

            # 🔒 Safety check extra
            if manager.repo.is_dirty(untracked_files=True):
                logger.info(
                    "Extra changes detected even after final commit. Forcing last safety commit..."
                )
                manager.repo.git.add(A=True)
                extra_message = f"Final safety auto-commit before push by {agent}"
                extra_commit = manager.repo.index.commit(extra_message)
                logger.info(
                    f"✅ Safety commit created with hash: {extra_commit.hexsha}"
                )

        res = manager.push_changes(remote_name=remote_name, branch=branch)

        if res["status"] == "success":
            return {
                "status": "success",
                "message": "Changes pushed successfully",
                "push_details": res["details"],
                "commit": final_commit_hash,
            }
        else:
            return {"status": "error", "message": res["message"]}
    except Exception as e:
        logger.error(f"Error in push endpoint: {str(e)}")
        return {"status": "error", "message": str(e)}


@app.post("/context/close-cycle")
def close_cycle(workspace_id: str = Query("default")):
    logger.info(f"POST /context/close-cycle called for workspace_id: {workspace_id}")
    try:
        manager = get_manager(workspace_id)
        success = manager.close_cycle()
        if success:
            return {"status": "success", "message": "Cycle closed successfully."}
        else:
            return {"status": "error", "message": "Failed to close cycle."}
    except Exception as e:
        logger.error(f"Error closing cycle: {str(e)}")
        return {"status": "error", "message": str(e)}


# ===== ENDPOINTS FOR EXTENSION (MOCK FOR TESTING) =====


@app.get("/health")
def health_check():
    """Health check for extension connectivity"""
    logger.info("Health check called")
    config = get_config()

    return {
        "status": "ok",
        "version": "2.1.0",
        "config": {
            "environment": config.environment,
            "storage_mode": config.storage_mode.value,
            "rewards_mode": config.rewards_mode.value,
            "event_bus_mode": config.event_bus_mode.value,
        },
        "agents": [
            "spec",
            "git",
            "development",
            "context",
            "coach",  # Strategy Coach Agent (unified)
            "milestone",
            "retrospective",
        ],
    }


@app.post("/admin/clear-blacklist")
def clear_blacklist():
    """Clear the IP blacklist (for development)"""
    abuse_detector.blacklist.clear()
    abuse_detector.error_counts.clear()
    logger.info("🧹 Blacklist cleared")
    return {"message": "Blacklist cleared successfully"}


@app.get("/admin/abuse-stats")
def get_abuse_stats():
    """
    Get abuse detection statistics (admin endpoint)

    In production, this should require authentication!
    """
    logger.info("GET /admin/abuse-stats called")
    return abuse_detector.get_stats()


@app.get("/agents/status")
def get_agents_status(workspace_id: str = Query(default="default")):
    """
    Get real-time status of all agents from AgentOrchestrator.

    Returns metrics from actual agent state files.
    """
    logger.info(f"GET /agents/status called for workspace: {workspace_id}")

    try:
        from app.agents.agent_orchestrator import AgentOrchestrator
        from datetime import datetime, timezone

        # Get workspace path
        workspace_path = get_workspace_path(workspace_id)

        # Initialize orchestrator
        orchestrator = AgentOrchestrator(
            workspace_id=workspace_id, workspace_path=str(workspace_path)
        )

        # Initialize all agents to get their state
        orchestrator.initialize_agents()

        # Get real metrics from agents
        agent_metrics = orchestrator.get_agent_metrics()

        # Agent metadata (names, emojis)
        agent_info = {
            "spec": {"name": "Spec Agent", "emoji": "📋"},
            "git": {"name": "Git Agent", "emoji": "🔧"},
            "development": {"name": "Development Agent", "emoji": "💻"},
            "context": {"name": "Context Agent", "emoji": "📦"},
            "coach": {"name": "Strategy Coach Agent", "emoji": "🎯"},
            "milestone": {"name": "Milestone Agent", "emoji": "🏁"},
            "retrospective": {"name": "Retrospective Agent", "emoji": "🔄"},
        }

        # Build response with real data
        result = []
        for agent_id, info in agent_info.items():
            metrics = agent_metrics.get(agent_id, {})

            # Determine status based on metrics
            events_processed = metrics.get("events_processed", 0)
            errors = metrics.get("errors", 0)

            if errors > 0:
                status = "error"
            elif events_processed == 0:
                status = "idle"
            else:
                status = "active"

            # Get last activity from agent state
            agent = orchestrator.agents.get(agent_id)
            last_activity = "Never"
            if agent and hasattr(agent, "state"):
                last_updated = agent.state.get("last_updated")
                if last_updated:
                    try:
                        last_time = datetime.fromisoformat(
                            last_updated.replace("Z", "+00:00")
                        )
                        now = datetime.now(timezone.utc)
                        delta = now - last_time.replace(tzinfo=timezone.utc)

                        if delta.total_seconds() < 60:
                            last_activity = "Just now"
                        elif delta.total_seconds() < 3600:
                            mins = int(delta.total_seconds() / 60)
                            last_activity = (
                                f"{mins} minute{'s' if mins > 1 else ''} ago"
                            )
                        elif delta.total_seconds() < 86400:
                            hours = int(delta.total_seconds() / 3600)
                            last_activity = (
                                f"{hours} hour{'s' if hours > 1 else ''} ago"
                            )
                        else:
                            days = int(delta.total_seconds() / 86400)
                            last_activity = f"{days} day{'s' if days > 1 else ''} ago"
                    except Exception as e:
                        logger.warning(
                            f"Error parsing last_updated for {agent_id}: {e}"
                        )

            result.append(
                {
                    "agent_id": agent_id,
                    "name": info["name"],
                    "status": status,
                    "last_activity": last_activity,
                    "metrics": {
                        "events_processed": events_processed,
                        "events_published": metrics.get("events_published", 0),
                        "errors": errors,
                    },
                }
            )

        # Cleanup
        orchestrator.shutdown_agents()

        logger.info(f"[agents/status] Returned status for {len(result)} agents")
        return result

    except Exception as e:
        logger.error(f"Error getting agent status: {e}", exc_info=True)
        # Fallback to basic structure if orchestrator fails
        return [
            {
                "agent_id": "spec",
                "name": "Spec Agent",
                "status": "unknown",
                "last_activity": "Unknown",
            },
            {
                "agent_id": "git",
                "name": "Git Agent",
                "status": "unknown",
                "last_activity": "Unknown",
            },
            {
                "agent_id": "development",
                "name": "Development Agent",
                "status": "unknown",
                "last_activity": "Unknown",
            },
            {
                "agent_id": "context",
                "name": "Context Agent",
                "status": "unknown",
                "last_activity": "Unknown",
            },
            {
                "agent_id": "coach",
                "name": "Strategy Coach Agent",
                "status": "unknown",
                "last_activity": "Unknown",
            },
            {
                "agent_id": "milestone",
                "name": "Milestone Agent",
                "status": "unknown",
                "last_activity": "Unknown",
            },
            {
                "agent_id": "retrospective",
                "name": "Retrospective Agent",
                "status": "unknown",
                "last_activity": "Unknown",
            },
        ]


@app.post("/agents/coach/ask")
async def coach_ask(
    user_id: str = Body(...),
    question: str = Body(...),
    workspace_id: str = Body(default="default"),
):
    """Ask the Strategy Coach Agent a question (integrated with real agent)"""
    logger.info(
        f"POST /agents/coach/ask - user: {user_id}, workspace: {workspace_id}, question: {question}"
    )

    try:
        from app.agents.coach_agent import CoachAgent

        workspace_path = str(get_workspace_path(workspace_id))
        project_id = os.getenv(
            "GCP_PROJECT_ID",
            os.getenv("GOOGLE_CLOUD_PROJECT", os.getenv("GCP_PROJECT", "local")),
        )

        # Initialize Coach Agent
        coach = CoachAgent(
            workspace_path=workspace_path,
            workspace_id=workspace_id,
            project_id=project_id,
        )

        # Get answer from coach (using Gemini API if available)
        gemini_api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")

        if gemini_api_key:
            # Use LLM for intelligent response
            try:
                import google.generativeai as genai

                genai.configure(api_key=gemini_api_key)
                model = genai.GenerativeModel("gemini-1.5-flash")

                # Build context-aware prompt
                context_info = f"User question: {question}\n\n"
                context_info += "As the Strategy Coach Agent, provide strategic, technical, and motivational guidance. "
                context_info += "Focus on: 1) Strategic direction, 2) Code quality best practices, 3) Practical next steps."

                response = model.generate_content(context_info)
                answer = response.text

            except Exception as llm_error:
                logger.warning(f"LLM generation failed, using fallback: {llm_error}")
                answer = _fallback_coach_response(question)
        else:
            logger.warning("No Gemini API key found, using fallback response")
            answer = _fallback_coach_response(question)

        return {"answer": answer, "agent_id": "coach", "workspace_id": workspace_id}

    except Exception as e:
        logger.error(f"Error in coach agent: {str(e)}", exc_info=True)
        # Fallback to helpful error message
        return {
            "answer": f"I encountered an issue processing your question. Here's what I'd generally recommend: {_fallback_coach_response(question)}",
            "error": str(e),
        }


def _fallback_coach_response(question: str) -> str:
    """Generate a helpful fallback response when LLM is unavailable"""
    question_lower = question.lower()

    if any(
        word in question_lower
        for word in ["test", "testing", "qa", "quality assurance"]
    ):
        return (
            "Great question about testing! I recommend: 1) Start with unit tests for critical logic, "
            "2) Use integration tests for API endpoints, 3) Consider TDD for complex features. "
            "What specific area would you like to test first?"
        )
    elif any(word in question_lower for word in ["refactor", "clean", "improve"]):
        return (
            "Excellent focus on code quality! For refactoring: 1) Identify code smells (long functions, duplicates), "
            "2) Write tests before refactoring, 3) Refactor in small steps with frequent commits. "
            "Would you like me to analyze your codebase for issues?"
        )
    elif any(
        word in question_lower for word in ["architecture", "design", "structure"]
    ):
        return (
            "Great architectural question! Consider: 1) Separation of concerns (keep business logic separate), "
            "2) SOLID principles, 3) Ports and Adapters pattern for external dependencies. "
            "What part of the architecture are you working on?"
        )
    elif any(word in question_lower for word in ["start", "begin", "first", "next"]):
        return (
            "Let's break this down! Here's a strategic approach: 1) Define clear success criteria, "
            "2) Break into smaller, testable milestones, 3) Start with the riskiest/most uncertain part. "
            "What's the most important outcome you're aiming for?"
        )
    else:
        return (
            f"For '{question}', I recommend: 1) Break it into smaller, concrete tasks, "
            "2) Identify any risks or unknowns early, 3) Define what 'done' looks like. "
            "Would you like me to help you create a specific action plan?"
        )


# Mock endpoint removed - no more fallbacks to mask real issues


# ===== GIT AGENT ENDPOINTS =====


@app.post("/git/event")
async def trigger_git_event(
    workspace_id: str = Query("default"),
    event_type: str = Body(...),
    data: dict = Body(...),
    source: str = Body("manual"),
):
    """
    Trigger Git Agent to handle an event

    Example:
    ```json
    {
        "event_type": "spec.created",
        "data": {
            "file_name": "git_agent.py",
            "description": "Created Git Agent MVP"
        },
        "source": "developer"
    }
    ```
    """
    from app.agents.git_agent import commit_via_agent

    logger.info(f"POST /git/event - workspace: {workspace_id}, type: {event_type}")

    try:
        commit_hash = await commit_via_agent(
            workspace_id=workspace_id, event_type=event_type, data=data, source=source
        )

        if commit_hash:
            return {
                "status": "success",
                "message": "Git Agent processed event and created commit",
                "commit_hash": commit_hash,
            }
        else:
            return {
                "status": "skipped",
                "message": "Git Agent decided not to commit (changes not significant)",
            }
    except Exception as e:
        logger.error(f"Error processing git event: {str(e)}")
        return {"status": "error", "message": str(e)}


# ===== SPEC AGENT ENDPOINTS =====


@app.post("/agents/spec/analyze")
async def spec_analyze(workspace_id: str = Query("default")):
    """Analyze documentation consistency and return issues (Spec Agent)."""
    logger.info(f"POST /agents/spec/analyze - workspace: {workspace_id}")
    try:
        workspace_path = str(get_workspace_path(workspace_id))
        project_id = os.getenv(
            "GCP_PROJECT_ID",
            os.getenv("GOOGLE_CLOUD_PROJECT", os.getenv("GCP_PROJECT", "local")),
        )
        agent = SpecAgent(workspace_path=workspace_path, project_id=project_id)
        issues = await agent.validate_docs()
        return {"issues": issues, "count": len(issues)}
    except Exception as e:
        logger.error(f"Error analyzing docs: {str(e)}")
        return {"issues": [], "count": 0, "error": str(e)}


@app.get("/agents/spec/proposals")
async def spec_get_proposals(workspace_id: str = Query("default")):
    """Create in-memory change proposals from SpecAgent issues (no commit).

    The approval will be a separate step that triggers the Git Agent.
    """
    logger.info(f"GET /agents/spec/proposals - workspace: {workspace_id}")
    try:
        workspace_path = str(get_workspace_path(workspace_id))
        project_id = os.getenv(
            "GCP_PROJECT_ID",
            os.getenv("GOOGLE_CLOUD_PROJECT", os.getenv("GCP_PROJECT", "local")),
        )
        agent = SpecAgent(workspace_path=workspace_path, project_id=project_id)
        issues: List[Dict] = await agent.validate_docs()

        # Map issues -> lightweight proposals (not persisted)
        proposals: List[Dict] = []
        for idx, issue in enumerate(issues, start=1):
            title = f"Docs issue: {issue.get('file', 'unknown')}"
            description = issue.get("message", issue.get("type", "issue"))
            proposals.append(
                {
                    "id": f"spec-{idx}",
                    "agent_id": "spec",
                    "title": title,
                    "description": description,
                    "proposed_changes": [
                        {
                            "file_path": issue.get("file", "docs/"),
                            "change_type": "update",
                            "description": description,
                        }
                    ],
                    "status": "pending",
                }
            )

        return {"proposals": proposals, "count": len(proposals)}
    except Exception as e:
        logger.error(f"Error generating spec proposals: {str(e)}")
        return {"proposals": [], "count": 0, "error": str(e)}


@app.post("/proposals/approve-mvp")
async def approve_proposal_mvp(
    workspace_id: str = Query("default"),
    proposal_id: str = Body(...),
    summary: str = Body(...),
):
    """Approval trigger that calls the Git Agent (MVP, no persistence)."""
    logger.info(
        f"POST /proposals/approve-mvp - {proposal_id} (workspace: {workspace_id})"
    )
    try:
        from app.agents.git_agent import commit_via_agent

        commit_hash = await commit_via_agent(
            workspace_id=workspace_id,
            event_type="proposal.approved",
            data={"proposal_id": proposal_id, "changes_summary": summary},
            source="spec-agent",
        )
        return {"status": "approved", "commit_hash": commit_hash}
    except Exception as e:
        logger.error(f"Error approving proposal: {str(e)}")
        return {"status": "error", "error": str(e)}


# ===== PROPOSALS PERSISTENCE (MVP - JSON + MD in workspace) =====


def _proposals_paths(workspace_id: str) -> Dict[str, Path]:
    ws = Path(get_workspace_path(workspace_id))
    proposals_dir = ws / "proposals"
    proposals_dir.mkdir(parents=True, exist_ok=True)
    return {
        "json": ws / "proposals.json",
        "dir": proposals_dir,
    }


def _read_proposals(json_path: Path) -> List[Dict]:
    """Read proposals from proposals.json (legacy)"""
    if json_path.exists():
        try:
            return json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def _read_proposals_from_dir(proposals_dir: Path) -> List[Dict]:
    """Read all proposals from proposals/ directory (new format with diffs)"""
    if not proposals_dir.exists():
        return []

    proposals = []
    for json_file in proposals_dir.glob("*.json"):
        try:
            with open(json_file, "r") as f:
                proposal = json.load(f)
                proposals.append(proposal)
        except Exception as e:
            logger.error(f"Error reading proposal {json_file}: {e}")

    # Sort by created_at (newest first)
    proposals.sort(key=lambda p: p.get("created_at", ""), reverse=True)
    return proposals


def _write_proposals(json_path: Path, proposals: List[Dict]):
    json_path.write_text(json.dumps(proposals, indent=2), encoding="utf-8")


def _auto_approve_enabled() -> bool:
    val = os.getenv(
        "CONTEXTPILOT_AUTO_APPROVE_PROPOSALS",
        os.getenv("CP_AUTO_APPROVE_PROPOSALS", "false"),
    )
    return str(val).lower() in ("1", "true", "yes", "on")


async def _trigger_github_workflow(proposal_id: str, proposal: Dict) -> bool:
    """
    Trigger GitHub Actions workflow to apply proposal.

    Uses repository_dispatch event to trigger the workflow.
    """
    github_token = os.getenv("GITHUB_TOKEN")
    github_repo = os.getenv(
        "GITHUB_REPO", "fsegall/google-context-pilot"
    )  # Format: owner/repo

    if not github_token:
        logger.warning("[API] GITHUB_TOKEN not set, skipping GitHub trigger")
        return False

    url = f"https://api.github.com/repos/{github_repo}/dispatches"
    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json",
    }

    payload = {
        "event_type": "proposal-approved",
        "client_payload": {
            "proposal_id": proposal_id,
            "title": proposal.get("title", ""),
            "description": proposal.get("description", ""),
            "workspace_id": proposal.get("workspace_id", "contextpilot"),
        },
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                url, json=payload, headers=headers, timeout=10.0
            )
            response.raise_for_status()
            logger.info(
                f"[API] GitHub Actions triggered successfully for {proposal_id}"
            )
            return True
    except Exception as e:
        logger.error(f"[API] Failed to trigger GitHub Actions: {e}")
        return False


@app.get("/proposals/list")
async def list_proposals_local(
    workspace_id: str = Query("default"),
    user_id: str = Query(...),
    status: Optional[str] = Query(None),
):
    """List proposals (LOCAL mode - file-based storage)"""
    config = get_config()

    if config.is_cloud_storage:
        raise HTTPException(
            status_code=501,
            detail="This endpoint is for LOCAL mode. Use Firestore router in CLOUD mode.",
        )

    paths = _proposals_paths(workspace_id)
    proposals = _read_proposals_from_dir(paths["dir"])

    if not proposals:
        proposals = _read_proposals(paths["json"])

    # Filter by status if provided
    if status:
        proposals = [p for p in proposals if p.get("status") == status]

    return {"proposals": proposals, "total": len(proposals)}


@app.get("/proposals/{proposal_id}")
async def get_proposal_local(proposal_id: str, workspace_id: str = Query("default")):
    """Get proposal by ID (LOCAL mode - file-based storage)"""
    config = get_config()

    if config.is_cloud_storage:
        raise HTTPException(
            status_code=501,
            detail="This endpoint is for LOCAL mode. Use Firestore router in CLOUD mode.",
        )

    paths = _proposals_paths(workspace_id)
    proposal_file = paths["dir"] / f"{proposal_id}.json"

    if proposal_file.exists():
        with open(proposal_file, "r") as f:
            return json.load(f)

    # Fallback to proposals.json
    proposals = _read_proposals(paths["json"])
    for p in proposals:
        if p.get("id") == proposal_id:
            return p

    raise HTTPException(status_code=404, detail="Proposal not found")


@app.post("/proposals/create")
async def create_proposals_local(workspace_id: str = Query("default")):
    """Generate proposals from SpecAgent (LOCAL mode - file-based storage)"""
    config = get_config()

    if config.is_cloud_storage:
        raise HTTPException(
            status_code=501,
            detail="This endpoint is for LOCAL mode. Use Firestore router in CLOUD mode.",
        )

    logger.info(f"POST /proposals/create - workspace: {workspace_id}")

    try:
        workspace_path = str(get_workspace_path(workspace_id))
        agent = SpecAgent(
            workspace_path=workspace_path,
            workspace_id=workspace_id,
            project_id=config.gcp_project_id,
        )
        issues: List[Dict] = await agent.validate_docs()

        # Create proposals using agent's method
        new_proposals = []
        for issue in issues:
            proposal_id = await agent._create_proposal_for_issue(issue)
            if proposal_id:
                new_proposals.append(proposal_id)

        # Count total proposals
        paths = _proposals_paths(workspace_id)
        proposals = _read_proposals_from_dir(paths["dir"])
        if not proposals:
            proposals = _read_proposals(paths["json"])
        total = len(proposals)

        return {"created": len(new_proposals), "total": total}
    except Exception as e:
        logger.error(f"Error creating proposals: {str(e)}")
        return {"created": 0, "error": str(e)}


@app.get("/context/summary")
async def get_context_summary(
    proposal_type: str = Query("general"), workspace_id: str = Query("default")
):
    """Generate intelligent context summary for new chat sessions."""
    try:
        from app.agents.spec_agent import SpecAgent

        # Get workspace path
        workspace_path = get_workspace_path(workspace_id)
        if not workspace_path:
            raise HTTPException(status_code=404, detail="Workspace not found")

        # Initialize Spec Agent
        spec_agent = SpecAgent(
            workspace_path=workspace_path,
            workspace_id=workspace_id,
            project_id=os.getenv("GCP_PROJECT_ID"),
        )

        # Generate context summary
        context = spec_agent.generate_context_summary(proposal_type)

        return {
            "context": context,
            "proposal_type": proposal_type,
            "workspace_id": workspace_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    except Exception as e:
        logger.error(f"Error generating context summary: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/proposals/{proposal_id}/approve")
async def approve_proposal_local(
    proposal_id: str, workspace_id: str = Query("default")
):
    """Approve proposal (LOCAL mode - file-based storage)"""
    config = get_config()

    if config.is_cloud_storage:
        raise HTTPException(
            status_code=501,
            detail="This endpoint is for LOCAL mode. Use Firestore router in CLOUD mode.",
        )

    # LOCAL mode: file-based storage
    paths = _proposals_paths(workspace_id)

    # Try individual file first
    proposal_file = paths["dir"] / f"{proposal_id}.json"
    if proposal_file.exists():
        with open(proposal_file, "r") as f:
            prop = json.load(f)
    else:
        # Fallback to proposals.json
        proposals = _read_proposals(paths["json"])
        prop = next((p for p in proposals if p.get("id") == proposal_id), None)

    if not prop:
        return {"status": "not_found"}

    summary = prop.get("description", "")
    try:
        commit_hash = None
        if _auto_approve_enabled():
            try:
                from app.agents.git_agent import commit_via_agent

                commit_hash = await commit_via_agent(
                    workspace_id=workspace_id,
                    event_type="proposal.approved",
                    data={"proposal_id": proposal_id, "changes_summary": summary},
                    source="spec-agent",
                )
            except Exception as git_error:
                logger.warning(f"[API] Git commit failed (non-fatal): {git_error}")

        # Update proposal status
        prop["status"] = "approved"
        if commit_hash:
            prop["commit_hash"] = commit_hash

        # Save
        if proposal_file.exists():
            with open(proposal_file, "w") as f:
                json.dump(prop, f, indent=2)
        else:
            _write_proposals(paths["json"], proposals)

        # Update MD file
        md_path = paths["dir"] / f"{proposal_id}.md"
        if md_path.exists():
            md_content = md_path.read_text(encoding="utf-8")
            md_content += f"\n\n---\n**Status:** approved\n"
            if commit_hash:
                md_content += f"**Commit:** {commit_hash}\n"
            md_path.write_text(md_content, encoding="utf-8")

        return {
            "status": "approved",
            "commit_hash": commit_hash,
            "auto_committed": bool(commit_hash),
        }
    except Exception as e:
        logger.error(f"Error approving proposal: {str(e)}")
        return {"status": "error", "error": str(e)}


@app.post("/proposals/{proposal_id}/reject")
async def reject_proposal_local(
    proposal_id: str, workspace_id: str = Query("default"), reason: str = Body("")
):
    """Reject proposal (LOCAL mode - file-based storage)"""
    config = get_config()

    if config.is_cloud_storage:
        raise HTTPException(
            status_code=501,
            detail="This endpoint is for LOCAL mode. Use Firestore router in CLOUD mode.",
        )

    paths = _proposals_paths(workspace_id)
    proposals = _read_proposals(paths["json"])
    prop = next((p for p in proposals if p.get("id") == proposal_id), None)

    if not prop:
        return {"status": "not_found"}

    prop["status"] = "rejected"
    prop["reason"] = reason
    _write_proposals(paths["json"], proposals)

    md_path = paths["dir"] / f"{proposal_id}.md"
    if md_path.exists():
        md_content = md_path.read_text(encoding="utf-8")
        md_content += f"\nStatus: rejected\nReason: {reason}\n"
        md_path.write_text(md_content, encoding="utf-8")

    return {"status": "rejected"}


# ===== RETROSPECTIVE AGENT ENDPOINTS =====


@app.post("/agents/retrospective/trigger")
async def trigger_agent_retrospective(
    workspace_id: str = Query("default"),
    trigger: str = Body("manual"),
    use_llm: bool = Body(False),
    trigger_topic: Optional[str] = Body(None),
):
    """
    Trigger a retrospective meeting between agents.

    This endpoint facilitates cross-agent learning by:
    - Collecting metrics from all agents
    - Analyzing event history
    - Generating insights and action items
    - (Optional) Synthesizing with LLM

    Args:
        workspace_id: Workspace identifier
        trigger: What triggered the retrospective (e.g., "manual", "milestone_complete", "cycle_end")
        use_llm: Whether to use Gemini LLM for narrative synthesis
        trigger_topic: Optional topic for agent discussion

    Returns:
        Retrospective summary with agent insights
    """
    logger.info(
        f"POST /agents/retrospective/trigger - workspace: {workspace_id}, trigger: {trigger}, topic: {trigger_topic}"
    )

    try:
        from app.agents.retrospective_agent import trigger_retrospective
        from app.utils.workspace_manager import get_workspace_path, create_workspace

        # Ensure workspace exists, create if not
        try:
            workspace_path = get_workspace_path(workspace_id)
        except FileNotFoundError:
            logger.info(f"Workspace '{workspace_id}' not found, creating it...")
            create_workspace(workspace_id, f"{workspace_id.title()} Workspace")
            workspace_path = get_workspace_path(workspace_id)
            logger.info(f"Workspace '{workspace_id}' created successfully")

        # Get Gemini API key if LLM synthesis requested
        gemini_api_key = None
        if use_llm:
            gemini_api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
            if not gemini_api_key:
                logger.warning("[API] LLM synthesis requested but no API key found")

        retrospective = await trigger_retrospective(
            workspace_id=workspace_id,
            trigger=trigger,
            gemini_api_key=gemini_api_key,
            trigger_topic=trigger_topic,
        )

        return {"status": "success", "retrospective": retrospective}

    except Exception as e:
        logger.error(f"Error triggering retrospective: {str(e)}")
        return {"status": "error", "message": str(e)}


@app.get("/agents/retrospective/list")
async def list_retrospectives(workspace_id: str = Query("default")):
    """
    List all retrospectives for a workspace.

    Returns:
        List of retrospective summaries
    """
    logger.info(f"GET /agents/retrospective/list - workspace: {workspace_id}")

    try:
        workspace_path = get_workspace_path(workspace_id)
        retro_dir = Path(workspace_path) / "retrospectives"

        if not retro_dir.exists():
            return {"retrospectives": [], "count": 0}

        retrospectives = []
        for json_file in sorted(retro_dir.glob("*.json"), reverse=True):
            try:
                with open(json_file, "r") as f:
                    retro = json.load(f)
                    # Return summary only (not full data)
                    retrospectives.append(
                        {
                            "retrospective_id": retro.get("retrospective_id"),
                            "timestamp": retro.get("timestamp"),
                            "trigger": retro.get("trigger"),
                            "insights_count": len(retro.get("insights", [])),
                            "action_items_count": len(retro.get("action_items", [])),
                        }
                    )
            except Exception as e:
                logger.error(f"Error reading retrospective {json_file}: {e}")

        return {"retrospectives": retrospectives, "count": len(retrospectives)}

    except Exception as e:
        logger.error(f"Error listing retrospectives: {str(e)}")
        return {"retrospectives": [], "count": 0, "error": str(e)}


@app.get("/agents/retrospective/{retrospective_id}")
async def get_retrospective(
    retrospective_id: str, workspace_id: str = Query("default")
):
    """
    Get a specific retrospective by ID.

    Returns:
        Full retrospective data
    """
    logger.info(
        f"GET /agents/retrospective/{retrospective_id} - workspace: {workspace_id}"
    )

    try:
        workspace_path = get_workspace_path(workspace_id)
        retro_file = (
            Path(workspace_path) / "retrospectives" / f"{retrospective_id}.json"
        )

        if not retro_file.exists():
            raise HTTPException(status_code=404, detail="Retrospective not found")

        with open(retro_file, "r") as f:
            retrospective = json.load(f)

        return retrospective

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error reading retrospective: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# ========== Development Agent Endpoints ==========


@app.post("/agents/development/implement")
async def development_implement_feature(
    workspace_id: str = Query("default"),
    description: str = Body(..., embed=True),
    target_files: Optional[List[str]] = Body(None, embed=True),
):
    """
    Generate code implementation for a feature using the Development Agent.

    Args:
        workspace_id: Workspace identifier
        description: Feature description or action item
        target_files: Optional list of files to modify (will be inferred if not provided)

    Returns:
        Created proposal ID and details
    """
    logger.info(f"POST /agents/development/implement - workspace: {workspace_id}")
    logger.info(f"Feature description: {description[:100]}...")

    try:
        from app.agents.development_agent import DevelopmentAgent

        workspace_path = str(get_workspace_path(workspace_id))
        project_id = os.getenv(
            "GCP_PROJECT_ID",
            os.getenv("GOOGLE_CLOUD_PROJECT", os.getenv("GCP_PROJECT", "local")),
        )

        agent = DevelopmentAgent(
            workspace_path=workspace_path,
            workspace_id=workspace_id,
            project_id=project_id,
        )

        proposal_id = await agent.implement_feature(
            description=description, target_files=target_files
        )

        if proposal_id:
            return {
                "success": True,
                "proposal_id": proposal_id,
                "message": f"Implementation proposal created: {proposal_id}",
            }
        else:
            return {"success": False, "error": "Failed to generate implementation"}

    except Exception as e:
        logger.error(f"Error in development agent: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/agents/development/implement-from-retrospective")
async def development_implement_from_retrospective(
    workspace_id: str = Query("default"),
    retrospective_id: str = Body(..., embed=True),
    action_item_indices: Optional[List[int]] = Body(None, embed=True),
):
    """
    Generate code implementations from retrospective action items.

    Args:
        workspace_id: Workspace identifier
        retrospective_id: Retrospective identifier
        action_item_indices: Optional list of action item indices to implement (0-based).
                             If None, implements all high/medium priority items.

    Returns:
        List of created proposal IDs
    """
    logger.info(
        f"POST /agents/development/implement-from-retrospective - workspace: {workspace_id}"
    )
    logger.info(f"Retrospective ID: {retrospective_id}")

    try:
        from app.agents.development_agent import implement_from_retrospective

        workspace_path = str(get_workspace_path(workspace_id))
        project_id = os.getenv(
            "GCP_PROJECT_ID",
            os.getenv("GOOGLE_CLOUD_PROJECT", os.getenv("GCP_PROJECT", "local")),
        )

        # Load retrospective
        retro_file = (
            Path(workspace_path) / "retrospectives" / f"{retrospective_id}.json"
        )
        if not retro_file.exists():
            raise HTTPException(status_code=404, detail="Retrospective not found")

        with open(retro_file, "r") as f:
            retrospective = json.load(f)

        action_items = retrospective.get("action_items", [])
        if not action_items:
            return {"success": False, "error": "No action items in retrospective"}

        # Filter action items if indices provided
        if action_item_indices is not None:
            action_items = [
                action_items[i] for i in action_item_indices if i < len(action_items)
            ]
        else:
            # Default: only high/medium priority
            action_items = [
                item
                for item in action_items
                if item.get("priority", "").lower() in ["high", "medium"]
            ]

        if not action_items:
            return {"success": False, "error": "No action items match the criteria"}

        # Generate implementations
        proposal_ids = await implement_from_retrospective(
            workspace_id=workspace_id,
            retrospective_id=retrospective_id,
            action_items=action_items,
            workspace_path=workspace_path,
            project_id=project_id,
        )

        return {
            "success": True,
            "proposal_ids": proposal_ids,
            "count": len(proposal_ids),
            "message": f"Generated {len(proposal_ids)} implementation proposal(s)",
        }

    except Exception as e:
        logger.error(f"Error implementing from retrospective: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/workspace/create")
async def create_workspace_endpoint(
    workspace_id: str = Body("default"),
    workspace_name: str = Body("Default Workspace")
):
    """Create a new workspace."""
    logger.info(f"POST /workspace/create - workspace: {workspace_id}")
    
    try:
        from app.utils.workspace_manager import create_workspace
        
        # Create workspace directory
        workspace_path = os.path.join("/app", ".contextpilot", "workspaces", workspace_id)
        os.makedirs(workspace_path, exist_ok=True)
        
        # Create basic workspace files
        meta_file = os.path.join(workspace_path, "meta.json")
        with open(meta_file, "w") as f:
            json.dump({
                "workspace_id": workspace_id,
                "name": workspace_name,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "status": "active"
            }, f, indent=2)
        
        logger.info(f"Workspace created: {workspace_path}")
        return {
            "status": "success",
            "workspace_id": workspace_id,
            "workspace_path": workspace_path,
            "message": f"Workspace '{workspace_id}' created successfully"
        }
        
    except Exception as e:
        logger.error(f"Error creating workspace: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
