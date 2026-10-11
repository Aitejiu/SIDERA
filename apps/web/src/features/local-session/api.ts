import type { components } from "@/shared/api/product-schema";
import { agentRequest } from "@/features/agents/api";

export type LocalSession = components["schemas"]["LocalSessionView"];

export const readLocalSession = (signal?: AbortSignal) => agentRequest<LocalSession>("/product/v1/session", signal ? { signal } : {});
export const exchangeLocalSession = (token: string) => agentRequest<components["schemas"]["BootstrapSessionView"]>("/product/v1/session/bootstrap", { method: "POST", body: JSON.stringify({ token }) });
export const logoutLocalSession = () => agentRequest<components["schemas"]["LogoutView"]>("/product/v1/session/logout", { method: "POST" });
