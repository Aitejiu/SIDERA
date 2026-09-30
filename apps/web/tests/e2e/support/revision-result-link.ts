import { type Locator, type Page } from "@playwright/test";

export function revisionResultLink(page: Page, rootTaskId: string): Locator {
  const route = `/tasks/${encodeURIComponent(rootTaskId)}/revisions/`;
  return page.locator(".tool-result-card")
    .filter({ hasText: "sidera_create_plan_revision" })
    .locator(`a[href*="${route}"]`);
}
