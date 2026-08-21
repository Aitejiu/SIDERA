import {
  createBrowserRouter,
  createMemoryRouter,
  Navigate,
  type RouteObject,
} from "react-router-dom";

import { App, TaskRoutePlaceholder } from "./App";
import { TaskDetailPage } from "@/features/tasks/TaskDetailPage";
import { TaskListPage } from "@/features/tasks/TaskListPage";

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
        path: "/workspaces/:workspaceId/tasks/:rootTaskId",
        element: <TaskDetailPage />,
      },
      {
        path: "/workspaces/:workspaceId/activity",
        element: <TaskRoutePlaceholder />,
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
