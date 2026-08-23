import { http, HttpResponse, type HttpHandler } from "msw";

export function taskOverviewHandler(
  workspaceId: string,
  body: Parameters<typeof HttpResponse.json>[0],
): HttpHandler {
  return http.get(`/api/v1/workspaces/${workspaceId}/task-overviews`, () =>
    HttpResponse.json(body),
  );
}

export function planningProfilesHandler(
  workspaceId: string,
  body: Parameters<typeof HttpResponse.json>[0],
): HttpHandler {
  return http.get(`/api/v1/workspaces/${workspaceId}/planning-profiles`, () =>
    HttpResponse.json(body),
  );
}

export function commandHandler(
  body: Parameters<typeof HttpResponse.json>[0],
  status = 201,
): HttpHandler {
  return http.post("/api/v1/commands", () =>
    HttpResponse.json(body, { status }),
  );
}
