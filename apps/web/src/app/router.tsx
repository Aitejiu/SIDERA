import {
  createBrowserRouter,
  createMemoryRouter,
  Navigate,
  type RouteObject,
} from "react-router-dom";

import {
  App,
} from "./App";
import { DEFAULT_WORKSPACE_ID } from "./runtimeConfig";
import { KnowledgeInboxPage } from "@/features/knowledge/KnowledgeInboxPage";
import { KnowledgeImportPage } from "@/features/knowledge/KnowledgeImportPage";
import { KnowledgeLibraryPage } from "@/features/knowledge/KnowledgeLibraryPage";
import { KnowledgeArticlePage } from "@/features/knowledge/KnowledgeArticlePage";
import { KnowledgeCandidatePage } from "@/features/knowledge/KnowledgeCandidatePage";
import { SubtaskPage } from "@/features/subtasks/SubtaskPage";
import { TaskDetailPage } from "@/features/tasks/TaskDetailPage";
import { TaskListPage } from "@/features/tasks/TaskListPage";
import { PlanDiffPage } from "@/features/tasks/PlanDiffPage";
import { PlanGenerationPage } from "@/features/tasks/PlanGenerationPage";
import { ArtifactPage } from "@/features/trace/ArtifactPage";
import { WorkerTracePage } from "@/features/trace/WorkerTracePage";
import { ExecutionPage } from "@/features/trace/ExecutionPage";
import { WorkerItemPage } from "@/features/worker-items/WorkerItemPage";
import { WorkerNodePage } from "@/features/workers/WorkerNodePage";
import { WorkerNodesPage } from "@/features/workers/WorkerNodesPage";
import { ChatPage } from "@/features/chat/ChatPage";
import { InboxPage, NotificationSettingsPage } from "@/features/notifications";
import { CapabilitiesPage, HistoryPage } from "@/features/workspace/WorkspacePages";
import { PlanningBriefForm } from "@/features/planning/PlanningBriefForm";
import { PlanningPage } from "@/features/planning/PlanningPage";
import { PlanRevisionPage } from "@/features/planning/PlanRevisionPage";
import { ContextPackagePage } from "@/features/context/ContextPackagePage";
import { CollaborationDetailPage } from "@/features/collaboration/CollaborationDetailPage";
import { AgentListPage } from "@/features/agents/AgentListPage";
import { AgentDetailPage } from "@/features/agents/AgentDetailPage";
import { RuntimeSettings } from "@/features/agents/RuntimeSettings";

export const routes: RouteObject[] = [
  {
    path: "/",
    element: <Navigate to={`/workspaces/${DEFAULT_WORKSPACE_ID}/tasks`} replace />,
  },
  {
    element: <App />,
    children: [
      {
        path: "/workspaces/:workspaceId/agents",
        element: <AgentListPage />,
      },
      {
        path: "/workspaces/:workspaceId/agents/:profileId",
        element: <AgentDetailPage />,
      },
      {
        path: "/workspaces/:workspaceId/runtimes",
        element: <RuntimeSettings />,
      },
      {
        path: "/workspaces/:workspaceId/runtimes/:runtimeId",
        element: <RuntimeSettings />,
      },
      {
        path: "/workspaces/:workspaceId/tasks",
        element: <TaskListPage />,
      },
      {
        path: "/workspaces/:workspaceId/knowledge",
        element: <KnowledgeLibraryPage />,
      },
      {
        path: "/workspaces/:workspaceId/knowledge/inbox",
        element: <KnowledgeInboxPage />,
      },
      {
        path: "/workspaces/:workspaceId/knowledge/import",
        element: <KnowledgeImportPage />,
      },
      {
        path: "/workspaces/:workspaceId/knowledge/pages/:pageId",
        element: <KnowledgeArticlePage />,
      },
      {
        path: "/workspaces/:workspaceId/knowledge/candidates/:candidateId",
        element: <KnowledgeCandidatePage />,
      },
      {
        path: "/workspaces/:workspaceId/context-packages/:packageId",
        element: <ContextPackagePage />,
      },
      {
        path: "/workspaces/:workspaceId/planning/new",
        element: <PlanningBriefForm />,
      },
      {
        path: "/workspaces/:workspaceId/planning/:planningRequestId",
        element: <PlanningPage />,
      },
      {
        path: "/workspaces/:workspaceId/tasks/:rootTaskId",
        element: <TaskDetailPage />,
      },
      {
        path: "/workspaces/:workspaceId/tasks/:rootTaskId/collaboration",
        element: <CollaborationDetailPage />,
      },
      {
        path: "/workspaces/:workspaceId/tasks/:rootTaskId/revisions/:planningRequestId",
        element: <PlanRevisionPage />,
      },
      {
        path: "/workspaces/:workspaceId/tasks/:rootTaskId/subtasks/:subtaskKey",
        element: <SubtaskPage />,
      },
      {
        path: "/workspaces/:workspaceId/worker-items/:workerItemId",
        element: <WorkerItemPage />,
      },
      {
        path: "/workspaces/:workspaceId/workers",
        element: <WorkerNodesPage />,
      },
      {
        path: "/workspaces/:workspaceId/workers/:workerId",
        element: <WorkerNodePage />,
      },
      {
        path: "/workspaces/:workspaceId/tasks/:rootTaskId/plan/:generation",
        element: <PlanGenerationPage />,
      },
      {
        path: "/workspaces/:workspaceId/tasks/:rootTaskId/compare/:left/:right",
        element: <PlanDiffPage />,
      },
      {
        path: "/workspaces/:workspaceId/tasks/:rootTaskId/executions/:executionId/sessions/:sessionId",
        element: <ExecutionPage />,
      },
      {
        path: "/workspaces/:workspaceId/tasks/:rootTaskId/executions/:executionId/sessions/:sessionId/trace",
        element: <WorkerTracePage />,
      },
      {
        path: "/workspaces/:workspaceId/artifacts/:artifactId",
        element: <ArtifactPage />,
      },
      {
        path: "/workspaces/:workspaceId/inbox",
        element: <InboxPage />,
      },
      {
        path: "/workspaces/:workspaceId/chat",
        element: <ChatPage />,
      },
      {
        path: "/workspaces/:workspaceId/chat/:threadId",
        element: <ChatPage />,
      },
      {
        path: "/workspaces/:workspaceId/history",
        element: <HistoryPage />,
      },
      {
        path: "/workspaces/:workspaceId/capabilities",
        element: <CapabilitiesPage />,
      },
      {
        path: "/workspaces/:workspaceId/activity",
        element: <Navigate to="../inbox" replace />,
      },
      {
        path: "/workspaces/:workspaceId/settings",
        element: <NotificationSettingsPage />,
      },
    ],
  },
];

export const router = createBrowserRouter(routes);

export function createTestRouter(initialEntries: string[]) {
  return createMemoryRouter(routes, { initialEntries });
}
