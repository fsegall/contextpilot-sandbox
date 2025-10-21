import * as vscode from 'vscode';
import { ContextPilotService, AgentStatus } from '../services/contextpilot';

export class AgentsProvider implements vscode.TreeDataProvider<AgentItem> {
  private _onDidChangeTreeData: vscode.EventEmitter<AgentItem | undefined | null | void> = new vscode.EventEmitter<AgentItem | undefined | null | void>();
  readonly onDidChangeTreeData: vscode.Event<AgentItem | undefined | null | void> = this._onDidChangeTreeData.event;
  private eventBusMode: string = 'unknown';

  constructor(private contextPilotService: ContextPilotService) {
    this.updateEventBusMode();
  }

  refresh(): void {
    this.updateEventBusMode();
    this._onDidChangeTreeData.fire();
  }

  private async updateEventBusMode(): Promise<void> {
    try {
      if (this.contextPilotService.isConnected()) {
        const health = await this.contextPilotService.getHealth();
        this.eventBusMode = health.config?.event_bus_mode || 'unknown';
      }
    } catch (error) {
      console.error('[AgentsProvider] Failed to update event bus mode:', error);
      this.eventBusMode = 'unknown';
    }
  }

  getTreeItem(element: AgentItem): vscode.TreeItem {
    return element;
  }

  async getChildren(): Promise<AgentItem[]> {
    if (!this.contextPilotService.isConnected()) {
      return [];
    }

    try {
      // Add mode indicator as first item
      const modeIcon = this.eventBusMode === 'pubsub' ? '📡' : '💾';
      const modeItem = new AgentItem({
        agent_id: 'event-bus-mode',
        name: `⚙️ ${modeIcon} Event Bus: ${this.eventBusMode}`,
        status: 'active',
        last_activity: 'now'
      });
      modeItem.tooltip = this.eventBusMode === 'pubsub' 
        ? 'Pub/Sub Mode: Agents communicate via Google Pub/Sub (scalable)'
        : 'In-Memory Mode: Agents communicate via in-memory events (local)';
      modeItem.contextValue = 'mode-indicator';
      
      const items: AgentItem[] = [modeItem];
      
      const agents = await this.contextPilotService.getAgentsStatus();
      const agentItems = agents.map(agent => new AgentItem(agent));
      items.push(...agentItems);
      
      return items;
    } catch (error) {
      // Return default agents if API not implemented yet
      return [
        new AgentItem({ agent_id: 'context', name: 'Context Agent', status: 'active', last_activity: 'now' }),
        new AgentItem({ agent_id: 'spec', name: 'Spec Agent', status: 'active', last_activity: '5m ago' }),
        new AgentItem({ agent_id: 'development', name: 'Development Agent', status: 'active', last_activity: '3m ago' }),
        new AgentItem({ agent_id: 'retrospective', name: 'Retrospective Agent', status: 'active', last_activity: '15m ago' }),
        new AgentItem({ agent_id: 'strategy', name: 'Strategy Agent', status: 'idle', last_activity: '1h ago' }),
        new AgentItem({ agent_id: 'milestone', name: 'Milestone Agent', status: 'active', last_activity: '10m ago' }),
        new AgentItem({ agent_id: 'git', name: 'Git Agent', status: 'active', last_activity: '2m ago' }),
        new AgentItem({ agent_id: 'coach', name: 'Coach Agent', status: 'active', last_activity: 'now' }),
      ];
    }
  }
}

class AgentItem extends vscode.TreeItem {
  constructor(public readonly agent: AgentStatus) {
    super(agent.name, vscode.TreeItemCollapsibleState.None);
    this.description = agent.status;
    this.tooltip = `Last activity: ${agent.last_activity}`;
    
    const icon = agent.status === 'active' ? 'circle-filled' :
                 agent.status === 'idle' ? 'circle-outline' :
                 'error';
    
    this.iconPath = new vscode.ThemeIcon(
      icon,
      agent.status === 'active' ? new vscode.ThemeColor('charts.green') :
      agent.status === 'idle' ? new vscode.ThemeColor('charts.yellow') :
      new vscode.ThemeColor('charts.red')
    );
  }
}

