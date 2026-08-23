import type { components } from "@/shared/api/schema";

export type PlanningProfile = components["schemas"]["PlanningProfileResponse"];
export type PlanningProfilePage = components["schemas"]["PlanningProfilePageResponse"];
export type PlanningRequest = components["schemas"]["PlanningRequestDetailResponse"];
export type PlanningBriefPage = components["schemas"]["PlanningBriefPageResponse"];
export type PlanningRunPage = components["schemas"]["PlanningRunPageResponse"];
export type PlanningClarificationPage = components["schemas"]["PlanningClarificationPageResponse"];
export type PlanningProposal = components["schemas"]["PlanningProposalResponse"];
export type PlanningMaterialization = components["schemas"]["PlanningMaterializationResponse"];
export type SubmitPlanningRequestPayload = components["schemas"]["SubmitPlanningRequestPayload"];
export type PlanningCommandRequest = components["schemas"]["CommandRequest"];
export type PlanningCommandResponse = components["schemas"]["CommandResponse"];
