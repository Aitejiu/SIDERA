import type { components } from "@/shared/api/product-schema";
import type { components as ControlPlane } from "@/shared/api/schema";

export type ProfileDraft = components["schemas"]["ProfileDraft-Input"];
export type ProfileRef = components["schemas"]["ProfileRef"];
export type ProfileView = components["schemas"]["ProfileView"];
export type ProfileList = components["schemas"]["ProfileListView"];
export type ProfileStatus = components["schemas"]["ProfileStatus"];
export type ResourceDraft = components["schemas"]["CapabilityResourceDraft-Input"];
export type ResourceRef = components["schemas"]["ResourceRef"];
export type ResourceView = components["schemas"]["ResourceView"];
export type ResourceList = components["schemas"]["ResourceListView"];
export type ResourceStatus = components["schemas"]["ResourceStatus"];
export type RuntimeRef = components["schemas"]["RuntimeRef"];
export type RuntimeSnapshot = components["schemas"]["RuntimeSnapshot"];
export type RuntimeDescriptor = components["schemas"]["RuntimeDescriptor"];
export type RuntimeCandidate = components["schemas"]["RuntimeCandidate"];
export type ConfigField = components["schemas"]["ConfigField"];
export type ConfigValidation = components["schemas"]["ConfigValidation"];
export type ConfigurationResult = components["schemas"]["ConfigurationResult"];
export type ConfigurationRef = components["schemas"]["ConfigurationRef"];
export type ProfileWork = components["schemas"]["ProfileWorkView"];
export type ProfileWorkRow = components["schemas"]["ProfileWorkRow"];
export type NativeRunSummary = ControlPlane["schemas"]["NativeRunSummary"];
export type Defaults = components["schemas"]["DefaultsView"];
export type DefaultsBody = components["schemas"]["DefaultsBody"];
export type Credential = components["schemas"]["CredentialSummarySchema"];
export type CredentialWrite = components["schemas"]["CredentialWriteSummarySchema"];
export type CredentialWriteInput = components["schemas"]["CredentialWriteInput"];
export type CredentialRevocation = components["schemas"]["CredentialRevocationSchema"];
export type SecretBinding = components["schemas"]["SecretBinding"];
export type Connection = components["schemas"]["ConnectionSummarySchema"];
export type ToolConnection = components["schemas"]["ToolConnectionView"];

export function confirmed(result: ConfigurationResult): boolean {
  return result.state === "succeeded" || result.state === "reconciled";
}

export function sameRef(left: ConfigurationRef, right: ConfigurationRef): boolean {
  return left.workspace_id === right.workspace_id && left.revision === right.revision
    && ("profile_id" in left && "profile_id" in right ? left.profile_id === right.profile_id
      : "resource_id" in left && "resource_id" in right && left.resource_id === right.resource_id);
}
