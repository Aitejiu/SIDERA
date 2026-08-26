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

export function workersHandler(
  body: Parameters<typeof HttpResponse.json>[0],
): HttpHandler {
  return http.get("/api/v1/workers", () => HttpResponse.json(body));
}

export function workerHandler(
  workerId: string,
  body: Parameters<typeof HttpResponse.json>[0],
): HttpHandler {
  return http.get(`/api/v1/workers/${workerId}`, () => HttpResponse.json(body));
}

export function modulesHandler(
  moduleType: "worker_adapter" | "workspace_provider",
  body: Parameters<typeof HttpResponse.json>[0],
): HttpHandler {
  return http.get("/api/v1/modules", ({ request }) => {
    if (new URL(request.url).searchParams.get("module_type") !== moduleType) {
      return new HttpResponse(null, { status: 404 });
    }
    return HttpResponse.json(body);
  });
}

export function knowledgePagesHandler(
  workspaceId: string,
  body: Parameters<typeof HttpResponse.json>[0],
): HttpHandler {
  return http.get(
    `/knowledge-api/api/v1/workspaces/${workspaceId}/knowledge/pages`,
    () => HttpResponse.json(body),
  );
}

export function knowledgeCandidatesHandler(
  workspaceId: string,
  body: Parameters<typeof HttpResponse.json>[0],
): HttpHandler {
  return http.get(
    `/knowledge-api/api/v1/workspaces/${workspaceId}/knowledge/candidates`,
    () => HttpResponse.json(body),
  );
}

export function knowledgeCommandHandler(
  body: Parameters<typeof HttpResponse.json>[0],
  status = 201,
): HttpHandler {
  return http.post("/knowledge-api/api/v1/commands", () => HttpResponse.json(body, { status }));
}
