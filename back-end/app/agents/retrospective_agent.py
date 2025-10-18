"""
Retrospective Agent - Agent Meetings & Cross-Agent Learning

Facilitates periodic retrospectives where agents:
- Share learnings from their work
- Identify coordination bottlenecks
- Propose process improvements
- Generate actionable insights

Triggered at cycle boundaries (e.g., end of milestone, manual trigger).
"""

import os
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
from pathlib import Path
import json

from app.agents.base_agent import BaseAgent
from app.services.event_bus import EventTypes, Topics, get_event_bus
from app.utils.workspace_manager import get_workspace_path

logger = logging.getLogger(__name__)


class RetrospectiveAgent(BaseAgent):
    """
    Retrospective Agent - Facilitates cross-agent learning and reflection.

    Core responsibilities:
    - Collect agent metrics and learnings
    - Synthesize insights from multiple agents
    - Identify workflow improvements
    - Generate retrospective summaries
    - Propose action items
    """

    def __init__(self, workspace_id: str = "default", project_id: Optional[str] = None):
        super().__init__(
            workspace_id=workspace_id, agent_id="retrospective", project_id=project_id
        )

        # Subscribe to cycle completion events
        self.subscribe_to_event(EventTypes.MILESTONE_COMPLETE)

        logger.info(f"[RetrospectiveAgent] Initialized for workspace: {workspace_id}")

    async def handle_event(self, event_type: str, data: Dict) -> None:
        """Handle incoming events"""
        logger.info(f"[RetrospectiveAgent] Received event: {event_type}")

        try:
            if event_type == EventTypes.MILESTONE_COMPLETE:
                # Trigger retrospective on milestone completion
                await self.conduct_retrospective(trigger="milestone_complete")

            self.increment_metric("events_processed")
        except Exception as e:
            logger.error(f"[RetrospectiveAgent] Error handling {event_type}: {e}")
            self.increment_metric("errors")

    async def conduct_retrospective(
        self, trigger: str = "manual", gemini_api_key: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Conduct a retrospective meeting between agents.

        Args:
            trigger: What triggered the retrospective (e.g., "manual", "milestone_complete", "cycle_end")
            gemini_api_key: Optional Gemini API key for LLM synthesis

        Returns:
            Retrospective summary with agent insights and action items
        """
        logger.info(f"[RetrospectiveAgent] Starting retrospective (trigger: {trigger})")

        # 1. Gather agent metrics
        agent_metrics = self._collect_agent_metrics()

        # 2. Gather agent learnings (from state files)
        agent_learnings = self._collect_agent_learnings()

        # 3. Analyze event history
        event_summary = self._analyze_event_history()

        # 4. Generate insights
        insights = self._generate_insights(
            agent_metrics, agent_learnings, event_summary
        )

        # 5. Propose action items
        action_items = self._propose_action_items(insights)

        # 6. (Optional) Use LLM to synthesize a narrative
        llm_summary = None
        if gemini_api_key:
            llm_summary = await self._synthesize_with_llm(
                agent_metrics, agent_learnings, insights, action_items, gemini_api_key
            )

        # 7. Create retrospective report
        retrospective = {
            "retrospective_id": f"retro-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "trigger": trigger,
            "agent_metrics": agent_metrics,
            "agent_learnings": agent_learnings,
            "event_summary": event_summary,
            "insights": insights,
            "action_items": action_items,
            "llm_summary": llm_summary,
        }

        # 8. Save retrospective to workspace
        self._save_retrospective(retrospective)
        
        # 9. Create improvement proposal from action items
        proposal_id = await self._create_improvement_proposal(retrospective)
        if proposal_id:
            retrospective["proposal_id"] = proposal_id
            logger.info(f"[RetrospectiveAgent] Created improvement proposal: {proposal_id}")
        
        # 10. Publish retrospective event
        await self.publish_event(
            topic=Topics.RETROSPECTIVE_EVENTS,
            event_type="retrospective.summary.v1",
            data={
                "retrospective_id": retrospective["retrospective_id"],
                "workspace_id": self.workspace_id,
                "insights_count": len(insights),
                "action_items_count": len(action_items),
                "proposal_id": proposal_id,
            },
        )
        
        logger.info(
            f"[RetrospectiveAgent] Retrospective completed: {retrospective['retrospective_id']}"
        )
        return retrospective

    def _collect_agent_metrics(self) -> Dict[str, Dict]:
        """Collect metrics from all agent state files"""
        metrics = {}
        state_dir = Path(self.workspace_path) / ".agent_state"

        if not state_dir.exists():
            logger.warning("[RetrospectiveAgent] No agent state directory found")
            return metrics

        for state_file in state_dir.glob("*_state.json"):
            agent_id = state_file.stem.replace("_state", "")
            try:
                with open(state_file, "r") as f:
                    state = json.load(f)
                    metrics[agent_id] = state.get("metrics", {})
            except Exception as e:
                logger.error(
                    f"[RetrospectiveAgent] Error reading {agent_id} state: {e}"
                )

        return metrics

    def _collect_agent_learnings(self) -> Dict[str, Any]:
        """Collect learnings from agent memory"""
        learnings = {}
        state_dir = Path(self.workspace_path) / ".agent_state"

        if not state_dir.exists():
            return learnings

        for state_file in state_dir.glob("*_state.json"):
            agent_id = state_file.stem.replace("_state", "")
            try:
                with open(state_file, "r") as f:
                    state = json.load(f)
                    memory = state.get("memory", {})

                    # Extract learnings-related memory keys
                    agent_learnings = {
                        k: v
                        for k, v in memory.items()
                        if "learning" in k.lower() or "insight" in k.lower()
                    }

                    if agent_learnings:
                        learnings[agent_id] = agent_learnings
            except Exception as e:
                logger.error(
                    f"[RetrospectiveAgent] Error reading {agent_id} learnings: {e}"
                )

        return learnings

    def _analyze_event_history(self) -> Dict[str, Any]:
        """Analyze recent event bus activity"""
        try:
            event_bus = get_event_bus(
                project_id=self.project_id,
                force_in_memory=os.getenv("USE_PUBSUB", "false").lower() != "true",
            )

            # Only works with InMemoryEventBus (has get_event_log)
            if hasattr(event_bus, "get_event_log"):
                events = event_bus.get_event_log()

                # Analyze event patterns
                event_types = {}
                for event in events:
                    event_type = event.get("event_type", "unknown")
                    event_types[event_type] = event_types.get(event_type, 0) + 1

                return {
                    "total_events": len(events),
                    "event_types": event_types,
                    "most_active_agent": self._find_most_active_agent(events),
                }
            else:
                return {"note": "Event history only available in development mode"}
        except Exception as e:
            logger.error(f"[RetrospectiveAgent] Error analyzing events: {e}")
            return {}

    def _find_most_active_agent(self, events: List[Dict]) -> str:
        """Find which agent published the most events"""
        agent_counts = {}
        for event in events:
            source = event.get("source", "unknown")
            agent_counts[source] = agent_counts.get(source, 0) + 1

        if agent_counts:
            return max(agent_counts, key=agent_counts.get)
        return "none"

    def _generate_insights(
        self, agent_metrics: Dict, agent_learnings: Dict, event_summary: Dict
    ) -> List[str]:
        """Generate insights from collected data"""
        insights = []

        # Insight 1: Agent activity levels
        total_events_processed = sum(
            m.get("events_processed", 0) for m in agent_metrics.values()
        )
        if total_events_processed > 0:
            insights.append(
                f"Agents processed {total_events_processed} events in this cycle."
            )

        # Insight 2: Error rates
        total_errors = sum(m.get("errors", 0) for m in agent_metrics.values())
        if total_errors > 0:
            insights.append(
                f"⚠️ {total_errors} errors occurred across all agents. Review error logs."
            )

        # Insight 3: Agent collaboration
        if event_summary.get("total_events", 0) > 0:
            most_active = event_summary.get("most_active_agent", "unknown")
            insights.append(
                f"Most active agent: {most_active}. Strong cross-agent communication observed."
            )

        # Insight 4: Learnings present
        if agent_learnings:
            agents_with_learnings = list(agent_learnings.keys())
            insights.append(
                f"Agents {', '.join(agents_with_learnings)} recorded learnings for future reference."
            )

        # Insight 5: Idle agents (no events processed)
        idle_agents = [
            agent_id
            for agent_id, metrics in agent_metrics.items()
            if metrics.get("events_processed", 0) == 0
        ]
        if idle_agents:
            insights.append(
                f"⏸️ Idle agents: {', '.join(idle_agents)}. Consider reviewing their triggers."
            )

        return insights

    def _propose_action_items(self, insights: List[str]) -> List[Dict[str, str]]:
        """Propose action items based on insights"""
        action_items = []

        # Parse insights for actionable items
        for insight in insights:
            if "errors" in insight.lower():
                action_items.append(
                    {
                        "priority": "high",
                        "action": "Review error logs and fix agent error handling",
                        "assigned_to": "developer",
                    }
                )

            if "idle agents" in insight.lower():
                action_items.append(
                    {
                        "priority": "medium",
                        "action": "Review event subscriptions for idle agents",
                        "assigned_to": "developer",
                    }
                )

            if "learnings" in insight.lower():
                action_items.append(
                    {
                        "priority": "low",
                        "action": "Document agent learnings in project retrospective notes",
                        "assigned_to": "team",
                    }
                )

        # Default action: continue with good performance
        if not action_items:
            action_items.append(
                {
                    "priority": "low",
                    "action": "Continue current workflow - agents performing well",
                    "assigned_to": "team",
                }
            )

        return action_items

    async def _synthesize_with_llm(
        self,
        agent_metrics: Dict,
        agent_learnings: Dict,
        insights: List[str],
        action_items: List[Dict],
        gemini_api_key: str,
    ) -> str:
        """Use Gemini to create a narrative retrospective summary"""
        try:
            import google.generativeai as genai

            genai.configure(api_key=gemini_api_key)

            model = genai.GenerativeModel("gemini-1.5-flash")

            prompt = f"""
You are a retrospective facilitator for a multi-agent development system.

**Agent Metrics:**
{json.dumps(agent_metrics, indent=2)}

**Agent Learnings:**
{json.dumps(agent_learnings, indent=2)}

**Insights:**
{chr(10).join(f"- {i}" for i in insights)}

**Action Items:**
{json.dumps(action_items, indent=2)}

Please synthesize a concise retrospective summary in the following format:

## Retrospective Summary

**What went well:**
- [List 2-3 positive observations]

**What could be improved:**
- [List 2-3 areas for improvement]

**Key learnings:**
- [List 2-3 key insights]

**Next cycle focus:**
- [List 1-2 priorities for next cycle]

Keep the tone encouraging and constructive. Maximum 200 words.
"""

            response = model.generate_content(prompt)
            return response.text

        except Exception as e:
            logger.error(f"[RetrospectiveAgent] LLM synthesis failed: {e}")
            return "LLM synthesis unavailable. See raw insights above."

    def _save_retrospective(self, retrospective: Dict) -> None:
        """Save retrospective report to workspace"""
        retro_dir = Path(self.workspace_path) / "retrospectives"
        retro_dir.mkdir(exist_ok=True)

        retro_id = retrospective["retrospective_id"]

        # Save JSON
        json_path = retro_dir / f"{retro_id}.json"
        with open(json_path, "w") as f:
            json.dump(retrospective, f, indent=2)

        # Save Markdown summary
        md_path = retro_dir / f"{retro_id}.md"
        md_content = self._format_retrospective_md(retrospective)
        with open(md_path, "w") as f:
            f.write(md_content)

        logger.info(f"[RetrospectiveAgent] Saved retrospective to {retro_dir}")

    def _format_retrospective_md(self, retrospective: Dict) -> str:
        """Format retrospective as Markdown"""
        md = f"""# Agent Retrospective
**ID:** {retrospective['retrospective_id']}  
**Date:** {retrospective['timestamp']}  
**Trigger:** {retrospective['trigger']}

---

## Agent Metrics

"""
        for agent_id, metrics in retrospective["agent_metrics"].items():
            md += f"### {agent_id.upper()}\n"
            for key, value in metrics.items():
                md += f"- **{key}:** {value}\n"
            md += "\n"

        md += f"""---

## Insights

"""
        for insight in retrospective["insights"]:
            md += f"- {insight}\n"

        md += f"""
---

## Action Items

"""
        for item in retrospective["action_items"]:
            md += f"- [{item['priority'].upper()}] {item['action']} (Assigned: {item['assigned_to']})\n"

        if retrospective.get("llm_summary"):
            md += f"""
---

## LLM Summary

{retrospective['llm_summary']}
"""

        md += f"""
---

*Generated by RetrospectiveAgent*
"""

        return md
    
    async def _create_improvement_proposal(self, retrospective: Dict) -> Optional[str]:
        """
        Create a change proposal from retrospective action items.
        
        This closes the feedback loop: retrospective → insights → proposal → code changes
        """
        action_items = retrospective.get("action_items", [])
        
        if not action_items:
            logger.info("[RetrospectiveAgent] No action items, skipping proposal creation")
            return None
        
        # Get high priority actions
        high_priority_actions = [
            item for item in action_items if item.get("priority") == "high"
        ]
        
        # If no high priority, use all actions
        actions_to_propose = high_priority_actions if high_priority_actions else action_items[:3]
        
        if not actions_to_propose:
            return None
        
        # Build proposal content
        proposal_title = "Agent System Improvements (from Retrospective)"
        
        proposal_description = f"""# Agent System Improvements

**Generated from Retrospective:** {retrospective['retrospective_id']}
**Date:** {retrospective['timestamp']}

## Background

After analyzing agent performance and collaboration patterns, the following improvements have been identified:

"""
        
        # Add insights
        for insight in retrospective.get("insights", [])[:3]:
            proposal_description += f"- {insight}\n"
        
        proposal_description += "\n## Proposed Changes\n\n"
        
        # Add action items
        for i, item in enumerate(actions_to_propose, 1):
            proposal_description += f"### {i}. {item['action']}\n\n"
            proposal_description += f"**Priority:** {item['priority'].upper()}\n"
            proposal_description += f"**Assigned to:** {item['assigned_to']}\n\n"
            
            # Add implementation guidance
            if "error" in item['action'].lower():
                proposal_description += "**Implementation:**\n"
                proposal_description += "- Review error logs in `.agent_state/` files\n"
                proposal_description += "- Add try-catch blocks around agent operations\n"
                proposal_description += "- Improve error reporting to event bus\n\n"
            elif "subscribe" in item['action'].lower() or "event" in item['action'].lower():
                proposal_description += "**Implementation:**\n"
                proposal_description += "- Update agent `__init__` to subscribe to new events\n"
                proposal_description += "- Add handler method for new event type\n"
                proposal_description += "- Test event flow with demo script\n\n"
            elif "document" in item['action'].lower():
                proposal_description += "**Implementation:**\n"
                proposal_description += "- Update relevant README or docs files\n"
                proposal_description += "- Add code comments\n"
                proposal_description += "- Create examples if needed\n\n"
            else:
                proposal_description += "**Implementation:**\n"
                proposal_description += "- Review relevant agent code\n"
                proposal_description += "- Make incremental changes\n"
                proposal_description += "- Test with existing workflows\n\n"
        
        proposal_description += """
## Benefits

Implementing these changes will:
- Improve agent coordination and collaboration
- Reduce errors and edge cases
- Enhance system reliability
- Better developer experience

## Next Steps

1. Review this proposal
2. Approve to implement changes
3. Test with existing workflows
4. Monitor agent metrics in next retrospective

---

*This proposal was automatically generated by the Retrospective Agent based on agent performance analysis.*
"""
        
        # Create the proposal using the proposal repository
        try:
            from app.repositories.proposal_repository import get_proposal_repository
            
            # Check if Firestore is enabled
            if os.getenv("FIRESTORE_ENABLED", "false").lower() == "true":
                repo = get_proposal_repository()
                
                proposal_data = {
                    "workspace_id": self.workspace_id,
                    "agent_id": "retrospective",
                    "title": proposal_title,
                    "description": proposal_description,
                    "proposed_changes": [
                        {
                            "file_path": f"docs/agent_improvements_{retrospective['retrospective_id']}.md",
                            "change_type": "create",
                            "description": "Agent improvement action plan",
                            "after": proposal_description,
                        }
                    ],
                    "status": "pending",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "metadata": {
                        "retrospective_id": retrospective["retrospective_id"],
                        "action_items_count": len(actions_to_propose),
                    },
                }
                
                proposal_id = repo.create(proposal_data)
                logger.info(f"[RetrospectiveAgent] Proposal created in Firestore: {proposal_id}")
                return proposal_id
            else:
                # Fallback: save to local file
                proposal_id = f"retro-proposal-{retrospective['retrospective_id']}"
                proposals_dir = Path(self.workspace_path) / "proposals"
                proposals_dir.mkdir(exist_ok=True)
                
                proposal_data = {
                    "id": proposal_id,
                    "workspace_id": self.workspace_id,
                    "agent_id": "retrospective",
                    "title": proposal_title,
                    "description": proposal_description,
                    "proposed_changes": [
                        {
                            "file_path": f"docs/agent_improvements_{retrospective['retrospective_id']}.md",
                            "change_type": "create",
                            "description": "Agent improvement action plan",
                            "after": proposal_description,
                        }
                    ],
                    "status": "pending",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "metadata": {
                        "retrospective_id": retrospective["retrospective_id"],
                        "action_items_count": len(actions_to_propose),
                    },
                }
                
                # Save JSON
                with open(proposals_dir / f"{proposal_id}.json", "w") as f:
                    json.dump(proposal_data, f, indent=2)
                
                # Save MD
                with open(proposals_dir / f"{proposal_id}.md", "w") as f:
                    f.write(f"# {proposal_title}\n\n{proposal_description}")
                
                logger.info(f"[RetrospectiveAgent] Proposal saved locally: {proposal_id}")
                return proposal_id
                
        except Exception as e:
            logger.error(f"[RetrospectiveAgent] Failed to create proposal: {e}")
            return None


# Standalone helper function for API endpoint
async def trigger_retrospective(
    workspace_id: str = "default",
    trigger: str = "manual",
    gemini_api_key: Optional[str] = None,
) -> Dict:
    """
    Trigger a retrospective (used by API endpoint).

    Args:
        workspace_id: Workspace identifier
        trigger: What triggered the retrospective
        gemini_api_key: Optional Gemini API key

    Returns:
        Retrospective summary
    """
    project_id = os.getenv(
        "GCP_PROJECT_ID",
        os.getenv("GOOGLE_CLOUD_PROJECT", os.getenv("GCP_PROJECT", "local")),
    )

    agent = RetrospectiveAgent(workspace_id=workspace_id, project_id=project_id)
    retrospective = await agent.conduct_retrospective(
        trigger=trigger, gemini_api_key=gemini_api_key
    )

    return retrospective
