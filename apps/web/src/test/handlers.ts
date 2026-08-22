import { http, HttpResponse, type HttpHandler } from "msw";

export function taskOverviewHandler(
  workspaceId: string,
  body: Parameters<typeof HttpResponse.json>[0],
): HttpHandler {
  return http.get(`/api/v1/workspaces/${workspaceId}/task-overviews`, () =>
    HttpResponse.json(body),
  );
}
