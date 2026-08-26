import type { components } from "@/shared/api/knowledge-schema";

export type KnowledgeSearchHit = {
  page_id: string;
  revision_id: string;
  section_key: string;
  revision_number: number;
  revision_hash: string;
  section_hash: string;
  title: string;
  summary: string;
  tags: string[];
  source_ids: string[];
  source_types: string[];
  source_availability: string[];
  trust: string;
  section_byte_count: number;
  rank: number;
  rank_explanation: string;
};

export type KnowledgeSearchResponse = {
  items: KnowledgeSearchHit[];
  next_cursor: string | null;
};

export type KnowledgeRevisionSummary = {
  revision_id: string;
  page_id: string;
  workspace_id: string;
  revision_number: number;
  title: string;
  summary: string;
  tags: string[];
  content_hash: string;
  change_reason: string;
  created_by: string;
  created_at: string;
  accepted_candidate_id: string | null;
};

export type KnowledgeSection = {
  section_key: string;
  heading: string;
  summary: string;
  body_markdown: string;
  ordinal: number;
  section_hash: string;
  content_byte_count: number;
};

export type KnowledgeRelation = {
  relation_key: string;
  relation_kind: string;
  target_page_id: string;
  target_revision_id: string | null;
};

export type KnowledgeSource = {
  source_id: string;
  workspace_id: string;
  source_type: string;
  label: string;
  uri: string | null;
  workspace_relative_path: string | null;
  git_revision: string | null;
  digest: string | null;
  metadata: Record<string, unknown>;
  availability: string;
  safe_reason_code: string | null;
  last_checked_at: string | null;
  version: number;
};

export type KnowledgeRevision = KnowledgeRevisionSummary & {
  sections: KnowledgeSection[];
  relations: KnowledgeRelation[];
  source_refs: string[];
  sources: KnowledgeSource[];
};

export type KnowledgePageSummary = {
  page_id: string;
  workspace_id: string;
  canonical_title: string;
  slug: string;
  status: string;
  version: number;
  current_revision: KnowledgeRevisionSummary | null;
  updated_at: string;
};

export type KnowledgePageDetail = {
  summary: KnowledgePageSummary;
  current_revision: KnowledgeRevision | null;
  revision_history: KnowledgeRevisionSummary[];
};

export type KnowledgePageList = {
  items: KnowledgePageSummary[];
  next_cursor: string | null;
};

export type KnowledgeCandidateSummary = {
  candidate_id: string;
  workspace_id: string;
  operation: string;
  target_page_id: string | null;
  expected_revision_id: string | null;
  expected_revision_hash: string | null;
  proposed_title: string;
  proposed_slug: string;
  candidate_hash: string;
  status: string;
  producer_type: string;
  producer_id: string;
  task_id: string | null;
  subtask_id: string | null;
  worker_item_id: string | null;
  execution_id: string | null;
  supersedes_candidate_id: string | null;
  version: number;
  created_at: string;
  updated_at: string;
};

export type KnowledgeCandidateList = {
  items: KnowledgeCandidateSummary[];
  next_cursor: string | null;
};

export type KnowledgeRevisionDiff = {
  from_revision_id: string;
  to_revision_id: string;
  title_changed: boolean;
  summary_changed: boolean;
  tags_added: string[];
  tags_removed: string[];
  sections_added: string[];
  sections_removed: string[];
  sections_changed: Array<{
    section_key: string;
    before_hash: string;
    after_hash: string;
  }>;
  relations_added: string[];
  relations_removed: string[];
  relations_changed: string[];
  sources_added: string[];
  sources_removed: string[];
  body_unified_diff: string;
  truncated: boolean;
};

export type KnowledgeCandidateDetail = {
  summary: KnowledgeCandidateSummary;
  proposed_snapshot: Record<string, unknown>;
  provenance: Record<string, unknown>;
  sources: KnowledgeSource[];
  observed_target_revision: KnowledgeRevisionSummary | null;
  current_target_revision: KnowledgeRevisionSummary | null;
  target_is_stale: boolean;
  diff: KnowledgeRevisionDiff | null;
};

export type KnowledgeSourceList = {
  items: KnowledgeSource[];
  next_cursor: string | null;
};

export type KnowledgeCommandResponse = {
  command_id: string;
  status: string;
  [key: string]: unknown;
};

export type KnowledgeCommandStatus = {
  command_id: string;
  status: "pending" | "applied" | "rejected";
  result?: KnowledgeCommandResponse | null;
  error_code?: string | null;
};

export type SubmitCandidatePayload = components["schemas"]["SubmitCandidatePayload"];
export type SubmitKnowledgeCandidateCommand = components["schemas"]["SubmitCommandRequest"];
export type ReviewCandidatePayload = components["schemas"]["ReviewCandidatePayload"];
export type ReviewKnowledgeCandidateCommand = components["schemas"]["ReviewCommandRequest"];
export type WithdrawCandidatePayload = components["schemas"]["WithdrawCandidatePayload"];
export type WithdrawKnowledgeCandidateCommand = components["schemas"]["WithdrawCommandRequest"];

export type KnowledgeProposedSnapshot = {
  title: string;
  slug: string;
  summary: string;
  tags: string[];
  sections: KnowledgeSection[];
  relations: KnowledgeRelation[];
  source_refs: string[];
};
