import type { components } from "@/shared/api/schema";

export type PlanningProfile = components["schemas"]["PlanningProfileResponse"];
export type PlanningProfilePage = components["schemas"]["PlanningProfilePageResponse"];
export type PlanningRequest = components["schemas"]["PlanningRequestDetailResponse"];
export type PlanningBriefPage = components["schemas"]["PlanningBriefPageResponse"];
export type PlanningRunPage = components["schemas"]["PlanningRunPageResponse"];
export type PlanningRun = components["schemas"]["PlanningRunResponse"];
export type PlanningClarificationPage = components["schemas"]["PlanningClarificationPageResponse"];
export type PlanningProposal = components["schemas"]["PlanningProposalResponse"];
export type PlanningMaterialization = components["schemas"]["PlanningMaterializationResponse"];
export type SubmitPlanningRequestPayload = components["schemas"]["SubmitPlanningRequestPayload"];
export type CancelPlanningRequestPayload = components["schemas"]["CancelPlanningRequestPayload"];
export type PlanningClarificationAnswer = components["schemas"]["PlanningClarificationAnswerPayload"];
export type AnswerPlanningClarificationPayload = components["schemas"]["AnswerPlanningClarificationPayload"];
export type RequestProposalRevisionPayload = components["schemas"]["RequestProposalRevisionPayload"];
export type RejectPlanningProposalPayload = components["schemas"]["RejectPlanningProposalPayload"];
export type PlanningCommandRequest = components["schemas"]["CommandRequest"];
export type PlanningCommandResponse = components["schemas"]["CommandResponse"];
