import {
  createBrowserRouter,
  createMemoryRouter,
  Navigate,
  type RouteObject,
} from "react-router-dom";

import {
  App,
  TaskRoutePlaceholder,
} from "./App";
import { SubtaskPage } from "@/features/subtasks/SubtaskPage";
import { TaskDetailPage } from "@/features/tasks/TaskDetailPage";
import { TaskListPage } from "@/features/tasks/TaskListPage";
import { PlanDiffPage } from "@/features/tasks/PlanDiffPage";
import { PlanGenerationPage } from "@/features/tasks/PlanGenerationPage";
import { ArtifactPage } from "@/features/trace/ArtifactPage";
import { WorkerTracePage } from "@/features/trace/WorkerTracePage";
import { ExecutionPage } from "@/features/trace/ExecutionPage";
import { WorkerItemPage } from "@/features/worker-items/WorkerItemPage";
import { CapabilitiesPage, ChatPage, HistoryPage, InboxPage } from "@/features/workspace/WorkspacePages";
import { PlanningBriefForm } from "@/features/planning/PlanningBriefForm";
import { PlanningPage } from "@/features/planning/PlanningPage";

const DEFAULT_WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";

export const routes: RouteObject[] = [
  {
    path: "/",
    element: <Navigate to={`/workspaces/${DEFAULT_WORKSPACE_ID}/tasks`} replace />,
  },
  {
    element: <App />,
    children: [
      {
        path: "/workspaces/:workspaceId/tasks",
        element: <TaskListPage />,
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
        path: "/workspaces/:workspaceId/tasks/:rootTaskId/subtasks/:subtaskKey",
        element: <SubtaskPage />,
      },
      {
        path: "/workspaces/:workspaceId/worker-items/:workerItemId",
        element: <WorkerItemPage />,
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
        element: <TaskRoutePlaceholder />,
      },
    ],
  },
];

export const router = createBrowserRouter(routes);

export function createTestRouter(initialEntries: string[]) {
  return createMemoryRouter(routes, { initialEntries });
}
