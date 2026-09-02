import type { components } from "@/shared/api/schema";

export type RootCollaborationSummary =
  components["schemas"]["RootCollaborationSummaryResponse"];
export type CollaborationDetail =
  components["schemas"]["CollaborationDetailResponse"];
export type CollaborationDecision =
  components["schemas"]["CollaborationDecisionResponse"];
export type CollaborationDecisionPage =
  components["schemas"]["CollaborationDecisionPageResponse"];
export type CollaborationStatus = RootCollaborationSummary["status"];
