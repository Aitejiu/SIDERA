import hashlib
import json
import re
import sys
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

from tests.support.agent_management_desktop import (
    FreshDesktopAdmission,
    FreshDesktopLab,
    begin_fresh_stage,
    complete_fresh_stage,
    create_cli_bootstrap,
    run_ego_browser,
)
from tests.support.agent_management_desktop import (
    management_desktop_lab as management_desktop_lab,
)
from tests.support.product_targets import ProductTargetError

_PROFILE_NAME = "N20 Fresh Native Harness"
_PROFILE_DESCRIPTION_R1 = "Fresh controlled profile revision one"
_PROFILE_DESCRIPTION_R2 = "Fresh controlled profile revision two"
_PROFILE_INSTRUCTIONS = "Use only the owned controlled harness for this test."
_PROFILE_TEST_INPUT = "N20 explicit Profile test; hold until its own Stop."
_UNRELATED_INPUT = "N20 separate conversation; remain active through Profile Stop."

_PROFILE_STOP_BODY = r"""
      const expectedRevision = config.profile_revision;
      if (!Number.isInteger(expectedRevision) || expectedRevision < 1) {
        throw new Error("fresh_profile_revision_refused");
      }
      const page2 = task.page(config.unrelated_page_label);
      const before = await readJson(
        page,
        `/main-agent/v1/threads/${encodeURIComponent(config.test_thread_id)}`,
      );
      if (!exactProfileRef(before.profileRef, config.profile_id, expectedRevision)
        || before.turns.length !== 1
        || before.turns[0].id !== config.test_turn_id) {
        throw new Error("fresh_original_identity_refused");
      }
      await assertProfileTestDialogPin(page, config.test_thread_id, expectedRevision);
      await page.click("loc=role:button[name='读取原测试结果']");
      await page.waitForFunction(() => {
        const dialog = document.querySelector("[role=dialog][aria-label='测试 Profile']");
        return (dialog?.textContent ?? "").includes("测试状态");
      }, undefined, { timeout: 20_000 });
      await page.click("loc=role:button[name='停止本次测试']");
      await page.waitForFunction(() => {
        const dialog = document.querySelector("[role=dialog][aria-label='测试 Profile']");
        if (!dialog) return false;
        if ((dialog.querySelector("[role=status]")?.textContent ?? "")
          .includes("本次测试清理已确认")) return true;
        return [...dialog.querySelectorAll("button")].some(button =>
          button.textContent?.trim() === "查询原停止结果" && !button.disabled);
      }, undefined, { timeout: 20_000 });
      const confirmed = await page.evaluate(() => {
        const dialog = document.querySelector("[role=dialog][aria-label='测试 Profile']");
        return (dialog?.querySelector("[role=status]")?.textContent ?? "")
          .includes("本次测试清理已确认");
      });
      if (!confirmed) {
        await assertProfileTestDialogPin(page, config.test_thread_id, expectedRevision);
        await page.click("loc=role:button[name='查询原停止结果']");
      }
      await page.waitForSelector("text=本次测试清理已确认", {
        state: "visible", timeout: 30_000,
      });
      const stopped = await readJson(
        page,
        `/main-agent/v1/threads/${encodeURIComponent(config.test_thread_id)}`,
      );
      if (!exactProfileRef(stopped.profileRef, config.profile_id, expectedRevision)
        || stopped.turns.length !== 1
        || stopped.turns[0].id !== config.test_turn_id
        || ["queued", "in_progress"].includes(stopped.turns[0].status)) {
        throw new Error("fresh_profile_stop_refused");
      }
      const unrelated = await readJson(
        page2,
        `/main-agent/v1/threads/${encodeURIComponent(config.unrelated_thread_id)}`,
      );
      if (!exactProfileRef(unrelated.profileRef, config.profile_id, expectedRevision)
        || unrelated.turns.length !== 1
        || unrelated.turns[0].id !== config.unrelated_turn_id
        || unrelated.turns[0].runId !== config.unrelated_run_id
        || !["queued", "in_progress"].includes(unrelated.turns[0].status)) {
        throw new Error("fresh_stop_isolation_refused");
      }
      return {
        test_thread_id: stopped.id,
        test_turn_id: stopped.turns[0].id,
        test_profile_revision: stopped.profileRef.revision,
        test_turn_status: stopped.turns[0].status,
        unrelated_thread_id: unrelated.id,
        unrelated_turn_id: unrelated.turns[0].id,
        unrelated_profile_revision: unrelated.profileRef.revision,
        unrelated_run_id: unrelated.turns[0].runId,
        unrelated_turn_status_after_test_stop: unrelated.turns[0].status,
      };
"""

_BROWSER_DRIVER = r"""
const config = __CONFIG__;
const task = await taskSpace(config.task_space_id);
const page = task.page("p1");
const backend = config.backend_url;
const workspace = config.workspace_id;
const agentBase = `/agents/v1/workspaces/${workspace}`;
const productBase = `/product/v1/workspaces/${workspace}`;

async function readJson(sourcePage, path) {
  const response = await sourcePage.fetch(path, {
    credentials: "include",
    cache: "no-store",
  });
  if (!response.ok) throw new Error("fresh_public_read_refused");
  return JSON.parse(response.body);
}

async function readThread(sourcePage, threadId, predicate) {
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    const thread = await readJson(
      sourcePage,
      `/main-agent/v1/threads/${encodeURIComponent(threadId)}`,
    );
    if (predicate(thread)) return thread;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error("fresh_original_thread_unconfirmed");
}

function exactProfileRef(value, profileId, revision) {
  return value?.workspace_id === workspace
    && value?.profile_id === profileId
    && value?.revision === revision;
}

async function profileTestThreadId(sourcePage) {
  const value = await sourcePage.evaluate(() => {
    const dialog = document.querySelector(
      '[role="dialog"][aria-label="测试 Profile"]',
    );
    if (!dialog) return null;
    const ids = Array.from(dialog.querySelectorAll("p"), (paragraph) => {
      const text = typeof paragraph.textContent === "string"
        ? paragraph.textContent.trim() : "";
      const match = text.match(
        /^会话\s+([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/i,
      );
      return match?.[1] ?? null;
    }).filter((id) => id !== null);
    return ids.length === 1 ? ids[0] : null;
  });
  if (
    typeof value !== "string"
    || !/^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value)
  ) {
    throw new Error("fresh_profile_test_thread_unavailable");
  }
  return value;
}

async function assertProfileTestDialogPin(sourcePage, expectedThreadId, expectedRevision) {
  const actualThreadId = await profileTestThreadId(sourcePage);
  const actualRevision = await sourcePage.evaluate(() => {
    const dialog = document.querySelector(
      '[role="dialog"][aria-label="测试 Profile"]',
    );
    if (!dialog) return null;
    const revisions = Array.from(dialog.querySelectorAll("strong"), (marker) => {
      const text = typeof marker.textContent === "string"
        ? marker.textContent.trim() : "";
      const match = text.match(/^Profile R([1-9][0-9]*)$/);
      return match === null ? null : Number(match[1]);
    }).filter((value) => value !== null);
    return revisions.length === 1 ? revisions[0] : null;
  });
  if (actualThreadId !== expectedThreadId || actualRevision !== expectedRevision) {
    throw new Error("fresh_test_dialog_repin_refused");
  }
}

function threadIdFromUrl(url) {
  const value = new URL(url).pathname.split("/").at(-1) ?? "";
  if (!/^[0-9a-f-]{36}$/i.test(value)) throw new Error("fresh_chat_thread_unavailable");
  return value;
}

let result;
try {
  result = await (async () => {
    __BODY__
  })();
} catch {
  process.exitCode = 1;
}
if (result !== undefined) {
  console.log(`FRESH_RESULT:${JSON.stringify(result)}`);
}
"""

_BROWSER_SPACE_DRIVER = r"""
const config = __CONFIG__;
try {
  const task = await taskSpace(config.task_space_name);
  const page = task.page("p1");
  if (!Number.isInteger(task.spaceId) || task.spaceId < 1 || page.label !== "p1") {
    throw new Error("fresh_browser_space_refused");
  }
  console.log(`FRESH_RESULT:${JSON.stringify({ space_id: task.spaceId, page_label: page.label })}`);
} catch {
  process.exitCode = 1;
}
"""


def _browser_script(
    lab: FreshDesktopLab,
    body: str,
    *,
    task_space_id: int,
    screenshots: dict[str, str] | None = None,
    variables: dict[str, object] | None = None,
) -> str:
    if type(task_space_id) is not int or task_space_id < 1:
        raise ProductTargetError("fresh_browser_config_refused")
    config = {
        "task_space_id": task_space_id,
        "backend_url": lab.backend_url,
        "web_url": lab.web_url,
        "workspace_id": str(lab.workspace_id),
        "screenshots": screenshots or {},
    }
    if variables is not None:
        if type(variables) is not dict or set(config).intersection(variables):
            raise ProductTargetError("fresh_browser_config_refused")
        config.update(variables)
    return _BROWSER_DRIVER.replace(
        "__CONFIG__", json.dumps(config, separators=(",", ":"))
    ).replace("__BODY__", body)


def _require_result(
    value: object, fields: set[str], error_code: str
) -> dict[str, object]:
    if type(value) is not dict or set(value) != fields:
        raise ProductTargetError(error_code)
    return value


def _profile_ref(
    workspace_id: UUID, profile_id: str, revision: int
) -> dict[str, object]:
    try:
        selected = UUID(profile_id)
    except ValueError:
        raise ProductTargetError("fresh_profile_identity_refused") from None
    return {
        "workspace_id": str(workspace_id),
        "profile_id": str(selected),
        "revision": revision,
    }


def _run_stage(
    lab: FreshDesktopLab,
    body: str,
    *,
    task_space_id: int,
    variables: dict[str, object],
    screenshots: dict[str, str] | None = None,
) -> dict[str, object]:
    return run_ego_browser(
        _browser_script(
            lab,
            body,
            task_space_id=task_space_id,
            screenshots=screenshots,
            variables=variables,
        )
    )


def _capture_recent_run_overview(
    lab: FreshDesktopLab,
    *,
    task_space_id: int,
    profile_id: str,
    profile_test_result: dict[str, object],
    unrelated_result: dict[str, object],
) -> dict[str, object]:
    if (
        type(task_space_id) is not int
        or task_space_id < 1
        or getattr(lab, "browser_space_id", None) != task_space_id
        or type(profile_id) is not str
    ):
        raise ProductTargetError("fresh_recent_overview_context_refused")
    try:
        selected_profile_id = str(UUID(profile_id))
    except ValueError:
        raise ProductTargetError("fresh_recent_overview_context_refused") from None
    if selected_profile_id != profile_id:
        raise ProductTargetError("fresh_recent_overview_context_refused")

    if (
        type(profile_test_result) is not dict
        or type(unrelated_result) is not dict
        or unrelated_result.get("page_label") in (None, "", "p1")
        or type(getattr(lab, "owned", None)) is not dict
    ):
        raise ProductTargetError("fresh_recent_overview_context_refused")
    identities = (
        profile_test_result.get("run_id"),
        unrelated_result.get("run_id"),
        profile_test_result.get("thread_id"),
        unrelated_result.get("thread_id"),
    )
    if (
        any(type(value) is not str for value in identities)
        or profile_test_result.get("run_id") == unrelated_result.get("run_id")
        or profile_test_result.get("thread_id") == unrelated_result.get("thread_id")
    ):
        raise ProductTargetError("fresh_recent_overview_context_refused")
    try:
        for identity in identities:
            if str(UUID(identity)) != identity:
                raise ValueError
    except ValueError:
        raise ProductTargetError("fresh_recent_overview_context_refused") from None

    widths = (1280, 1440, 1600)
    artifact_root = lab.settings.artifact_root
    screenshots = {
        str(width): str(artifact_root / f"profile-r2-recent-{width}.png")
        for width in widths
    }
    run_ids = [profile_test_result["run_id"], unrelated_result["run_id"]]
    thread_ids = [profile_test_result["thread_id"], unrelated_result["thread_id"]]
    body = r"""
      const page2 = task.page(config.unrelated_page_label);
      await page2.goto(`${config.web_url}/workspaces/${config.workspace_id}/agents/${config.profile_id}`);
      await page2.waitForSelector(`loc=role:heading[name='${config.profile_name}']`, { state: "visible" });
      await page2.waitForSelector("loc=role:heading[name='最近运行']", { state: "visible" });
      const regionId = `profile-recent-runs-${config.profile_id}`;
      await page2.waitForSelector(`section.agent-work[aria-labelledby='${regionId}']`, { state: "visible" });
      await page2.waitForFunction(({ regionId: expectedRegionId, runIds: expectedRunIds }) => {
        const region = document.querySelector(`section.agent-work[aria-labelledby="${expectedRegionId}"]`);
        if (!region) return false;
        const rows = [...region.querySelectorAll(".agent-work-row")];
        return expectedRunIds.every((runId) => rows.some((row) => row.textContent?.includes(runId)));
      }, { regionId, runIds: config.run_ids }, { timeout: 30_000 });

      const profileRevision = await page2.evaluate(() =>
        document.querySelector(".agent-identity strong")?.textContent?.trim() ?? null,
      );
      const rows = await page2.evaluate(() => {
        const region = document.querySelector("section.agent-work[aria-labelledby^='profile-recent-runs-']");
        return [...(region?.querySelectorAll(".agent-work-row") ?? [])].map((row) => ({
          text: row.textContent ?? "",
          links: [...row.querySelectorAll("a")].map((link) => new URL(link.href).pathname),
        }));
      });
      if (profileRevision !== "R2") throw new Error("fresh_recent_overview_revision_refused");
      for (let index = 0; index < config.run_ids.length; index += 1) {
        const row = rows.find((candidate) => candidate.text.includes(config.run_ids[index]));
        const expectedPath = `/workspaces/${config.workspace_id}/chat/${config.thread_ids[index]}`;
        if (!row || !row.text.includes("Profile R1") || !row.links.includes(expectedPath)) {
          throw new Error("fresh_recent_overview_identity_refused");
        }
      }

      const geometry = [];
      for (const width of [1280, 1440, 1600]) {
        await page2.cdp("Emulation.setDeviceMetricsOverride", {
          width,
          height: 1000,
          deviceScaleFactor: 1,
          mobile: false,
          dontSetVisibleSize: true,
        });
        await page2.waitForFunction(({ width: expectedWidth }) =>
          window.innerWidth === expectedWidth && window.innerHeight === 1000,
        { width }, { timeout: 10_000 });
        await page2.snapshot();
        const facts = await page2.evaluate(() => {
          const root = document.documentElement;
          const panel = document.querySelector("section.agent-page");
          const bounds = panel?.getBoundingClientRect();
          return {
            viewport_width: window.innerWidth,
            viewport_height: window.innerHeight,
            document_overflow_x: root.scrollWidth > window.innerWidth,
            panel_bounds_finite: bounds !== undefined && bounds !== null
              && Number.isFinite(bounds.left) && Number.isFinite(bounds.right)
              && Number.isFinite(bounds.top) && Number.isFinite(bounds.bottom)
              && Number.isFinite(bounds.width) && Number.isFinite(bounds.height),
          };
        });
        if (facts.viewport_width !== width || facts.viewport_height !== 1000
          || facts.panel_bounds_finite !== true || facts.document_overflow_x !== false) {
          throw new Error("fresh_recent_overview_geometry_refused");
        }
        await page2.screenshot({ path: config.screenshots[String(width)] });
        geometry.push({
          width,
          viewport_width: facts.viewport_width,
          viewport_height: facts.viewport_height,
          panel_bounds_finite: facts.panel_bounds_finite,
          document_overflow_x: facts.document_overflow_x,
        });
      }
      return {
        profile_id: config.profile_id,
        revision: 2,
        run_ids: config.run_ids,
        screenshots: config.screenshots,
        geometry,
      };
"""
    result = _run_stage(
        lab,
        body,
        task_space_id=task_space_id,
        screenshots=screenshots,
        variables={
            "profile_id": selected_profile_id,
            "profile_name": _PROFILE_NAME,
            "run_ids": run_ids,
            "thread_ids": thread_ids,
            "unrelated_page_label": unrelated_result["page_label"],
        },
    )
    result = _require_result(
        result,
        {"profile_id", "revision", "run_ids", "screenshots", "geometry"},
        "fresh_recent_overview_result_refused",
    )
    if (
        result.get("profile_id") != selected_profile_id
        or result.get("revision") != 2
        or result.get("run_ids") != run_ids
        or result.get("screenshots") != screenshots
    ):
        raise ProductTargetError("fresh_recent_overview_result_refused")
    geometry = result.get("geometry")
    if type(geometry) is not list or len(geometry) != len(widths):
        raise ProductTargetError("fresh_recent_overview_geometry_refused")
    expected_geometry_fields = {
        "width",
        "viewport_width",
        "viewport_height",
        "panel_bounds_finite",
        "document_overflow_x",
    }
    for width, facts in zip(widths, geometry, strict=True):
        if (
            type(facts) is not dict
            or set(facts) != expected_geometry_fields
            or facts.get("width") != width
            or facts.get("viewport_width") != width
            or facts.get("viewport_height") != 1000
            or facts.get("panel_bounds_finite") is not True
            or facts.get("document_overflow_x") is not False
        ):
            raise ProductTargetError("fresh_recent_overview_geometry_refused")
    lab.owned["recent_run_overview"] = result
    return result


def _ensure_browser_space(lab: FreshDesktopLab) -> dict[str, object]:
    intent = {"task_space_name": "Sidera N20 native-harness"}
    started, saved = begin_fresh_stage(lab.admission, "browser_space", intent)
    if started:
        config = json.dumps(intent, separators=(",", ":"))
        script = _BROWSER_SPACE_DRIVER.replace("__CONFIG__", config)
        result = run_ego_browser(script)
        result = _require_result(
            result, {"space_id", "page_label"}, "fresh_browser_space_refused"
        )
        if (
            type(result["space_id"]) is not int
            or result["space_id"] < 1
            or result["page_label"] != "p1"
        ):
            raise ProductTargetError("fresh_browser_space_refused")
        complete_fresh_stage(lab.admission, "browser_space", intent, result)
    elif type(saved) is dict:
        result = _require_result(
            saved, {"space_id", "page_label"}, "fresh_stage_result_refused"
        )
        if (
            type(result["space_id"]) is not int
            or result["space_id"] < 1
            or result["page_label"] != "p1"
        ):
            raise ProductTargetError("fresh_stage_result_refused")
    else:
        raise ProductTargetError("fresh_stage_result_refused")
    lab.browser_space_id = result["space_id"]
    lab.owned["browser_space"] = result
    return result


def _authorize_browser(lab: FreshDesktopLab, space_id: int) -> dict[str, object]:
    bootstrap = create_cli_bootstrap(lab.settings, resource_owner=lab)
    browser_body = r"""
      const fs = await import("node:fs/promises");
      await page.goto(`${config.web_url}/workspaces/${workspace}/agents`);
      await page.waitForSelector("loc=role:heading[name='智能体']", { state: "visible" });
      const initial = await readJson(page, "/product/v1/status");
      if (initial.lifecycle !== "ready" || initial.execution?.ready !== false) {
        throw new Error("fresh_initial_status_refused");
      }
      await page.click("loc=role:button[name='本地会话']");
      await page.waitForSelector("loc=role:dialog[name='本地会话']", { state: "visible" });
      const loginView = await page.snapshot();
      if (!loginView.includes("CLI 令牌")) throw new Error("fresh_existing_session_refused");
      const bootstrap = JSON.parse(await fs.readFile(config.bootstrap_file, "utf8"));
      if (typeof bootstrap.token !== "string" || bootstrap.token.length === 0) {
        throw new Error("fresh_cli_token_refused");
      }
      let token = bootstrap.token;
      const tokenSelector = "loc=css:[role='dialog'][aria-labelledby='local-session-title'] input[type='password']";
      await page.fill(tokenSelector, token);
      token = "";
      bootstrap.token = "";
      await page.click("loc=role:button[name='连接']");
      await page.waitForSelector(tokenSelector, {
        state: "hidden",
        timeout: 20_000,
      });
      await page.click("loc=role:button[name='关闭本地会话']");
      await page.waitForSelector("loc=role:dialog[name='本地会话']", { state: "hidden" });
      const health = await readJson(page, "/main-agent/v1/health");
      if (health.provider !== "unavailable") throw new Error("fresh_pre_registration_health_refused");
      const authorized = await readJson(page, `${agentBase}/runtimes`);
      if (!Array.isArray(authorized.items)) throw new Error("fresh_authorization_refused");
      return {
        workspace_id: workspace,
        initial_lifecycle: initial.lifecycle,
        initial_execution_ready: initial.execution.ready,
        pre_registration_provider: health.provider,
        authorized_runtime_count: authorized.items.length,
        bootstrap_expires_at: config.bootstrap_expires_at,
      };
"""
    browser_result = _run_stage(
        lab,
        browser_body,
        task_space_id=space_id,
        variables={
            "bootstrap_file": bootstrap["output"],
            "bootstrap_expires_at": bootstrap["expires_at"],
        },
    )
    browser_result = _require_result(
        browser_result,
        {
            "workspace_id",
            "initial_lifecycle",
            "initial_execution_ready",
            "pre_registration_provider",
            "authorized_runtime_count",
            "bootstrap_expires_at",
        },
        "fresh_browser_result_refused",
    )
    workspace = str(lab.workspace_id)
    if (
        browser_result["workspace_id"] != workspace
        or browser_result["initial_lifecycle"] != "ready"
        or browser_result["initial_execution_ready"] is not False
        or browser_result["pre_registration_provider"] != "unavailable"
        or browser_result["authorized_runtime_count"] != 0
        or browser_result["bootstrap_expires_at"] != bootstrap["expires_at"]
    ):
        raise ProductTargetError("fresh_browser_result_refused")
    return browser_result


def _ensure_unrelated_page(lab: FreshDesktopLab, space_id: int) -> str:
    intent = {"space_id": space_id, "primary_page_label": "p1"}
    started, saved = begin_fresh_stage(lab.admission, "unrelated_page", intent)
    if started:
        result = _run_stage(
            lab,
            "const page2 = await task.newPage(); return { page_label: page2.label };",
            task_space_id=space_id,
            variables={},
        )
        result = _require_result(result, {"page_label"}, "fresh_unrelated_page_refused")
        if (
            type(result["page_label"]) is not str
            or re.fullmatch(r"p[2-9][0-9]*", result["page_label"]) is None
        ):
            raise ProductTargetError("fresh_unrelated_page_refused")
        complete_fresh_stage(lab.admission, "unrelated_page", intent, result)
    elif type(saved) is dict:
        result = _require_result(saved, {"page_label"}, "fresh_stage_result_refused")
        if (
            type(result["page_label"]) is not str
            or re.fullmatch(r"p[2-9][0-9]*", result["page_label"]) is None
        ):
            raise ProductTargetError("fresh_stage_result_refused")
    else:
        raise ProductTargetError("fresh_stage_result_refused")
    return result["page_label"]


def _run_fresh_input_stage(
    admission: FreshDesktopAdmission,
    stage: str,
    intent: dict[str, object],
    start: Callable[[], dict[str, object]],
) -> tuple[bool, dict[str, object]]:
    if stage not in {"profile_test", "unrelated_conversation"}:
        raise ProductTargetError("fresh_input_stage_refused")
    started, saved = begin_fresh_stage(admission, stage, intent)
    if started:
        result = start()
        if type(result) is not dict:
            raise ProductTargetError("fresh_input_stage_refused")
        return True, result
    if type(saved) is not dict:
        raise ProductTargetError("fresh_stage_result_refused")
    return False, saved


def _active_run(lab: FreshDesktopLab, run_id: str) -> bool:
    try:
        selected = UUID(run_id)
    except ValueError:
        return False
    with lab.native.lock:
        inspection = lab.native.inspections.get(selected)
        return (
            inspection is not None
            and inspection.process_state == "alive"
            and inspection.response_state == "active"
        )


def test_fresh_desktop_public_lifecycle_and_individual_profile_stop(
    management_desktop_lab: FreshDesktopLab,
) -> None:
    lab = management_desktop_lab
    workspace_id = lab.workspace_id
    workspace = str(workspace_id)
    runtime_status = lab.runtime.status()
    if (
        runtime_status["lifecycle"] != "ready"
        or not lab.backend_url.startswith("http://127.0.0.1:")
        or not lab.web_url.startswith("http://[::1]:")
    ):
        raise ProductTargetError("fresh_initial_lifecycle_refused")
    browser_space = _ensure_browser_space(lab)
    space_id = browser_space["space_id"]

    browser_intent = {
        "workspace_id": workspace,
        "web_origin": lab.web_url,
        # Legacy checkpoint identity only; the CLI metadata supplies the actual secure file.
        "bootstrap_file": str(lab.bootstrap_file),
    }
    browser_started, browser_saved = begin_fresh_stage(
        lab.admission, "browser_authorization", browser_intent
    )
    if browser_started:
        browser_result = _authorize_browser(lab, space_id)
        complete_fresh_stage(
            lab.admission,
            "browser_authorization",
            browser_intent,
            browser_result,
        )
    elif type(browser_saved) is dict:
        browser_result = _require_result(
            browser_saved,
            {
                "workspace_id",
                "initial_lifecycle",
                "initial_execution_ready",
                "pre_registration_provider",
                "authorized_runtime_count",
                "bootstrap_expires_at",
            },
            "fresh_stage_result_refused",
        )
        if (
            browser_result.get("workspace_id") != workspace
            or browser_result.get("initial_lifecycle") != "ready"
            or browser_result.get("initial_execution_ready") is not False
            or browser_result.get("pre_registration_provider") != "unavailable"
            or browser_result.get("authorized_runtime_count") != 0
            or type(browser_result.get("bootstrap_expires_at")) is not str
            or not browser_result["bootstrap_expires_at"]
        ):
            raise ProductTargetError("fresh_stage_result_refused")
    else:
        raise ProductTargetError("fresh_stage_result_refused")

    runtime_intent = {
        "workspace_id": workspace,
        "harness_id": lab.native.harness_id,
        "executable": sys.executable,
        "max_active": 2,
        "max_sessions": 2,
    }
    runtime_started, runtime_saved = begin_fresh_stage(
        lab.admission, "runtime_registration", runtime_intent
    )
    if runtime_started:
        runtime_body = r"""
      await page.goto(`${config.web_url}/workspaces/${workspace}/runtimes`);
      await page.waitForSelector("loc=role:heading[name='运行环境']", { state: "visible" });
      await page.selectOption("loc=role:combobox[name='Harness']", {
        label: "Owned controlled product harness",
      });
      await page.fill("loc=role:textbox[name='可执行文件绝对路径']", config.executable);
      await page.fill("loc=role:spinbutton[name='活跃 Run 上限']", "2");
      await page.fill("loc=role:spinbutton[name='原生会话上限']", "2");
      await page.click("loc=role:button[name='登记运行环境']");
      await page.waitForSelector("text=登记结果已返回", { state: "visible" });
      const listed = await readJson(page, `${agentBase}/runtimes`);
      const matches = listed.items.filter((item) => item.harness_id === config.harness_id);
      if (matches.length !== 1 || matches[0].installed !== true || matches[0].enabled !== true) {
        throw new Error("fresh_runtime_registration_refused");
      }
      const status = await readJson(page, "/product/v1/status");
      const health = await readJson(page, "/main-agent/v1/health");
      if (status.execution?.ready !== true || health.provider !== "available") {
        throw new Error("fresh_post_registration_health_refused");
      }
      return {
        runtime_ref: matches[0].ref,
        harness_id: matches[0].harness_id,
        installed: matches[0].installed,
        enabled: matches[0].enabled,
        execution_ready: status.execution.ready,
        provider: health.provider,
      };
"""
        runtime_result = _run_stage(
            lab,
            runtime_body,
            task_space_id=space_id,
            variables={
                "harness_id": lab.native.harness_id,
                "executable": sys.executable,
            },
        )
        runtime_result = _require_result(
            runtime_result,
            {
                "runtime_ref",
                "harness_id",
                "installed",
                "enabled",
                "execution_ready",
                "provider",
            },
            "fresh_runtime_result_refused",
        )
        if (
            type(runtime_result["runtime_ref"]) is not dict
            or runtime_result["runtime_ref"].get("workspace_id") != workspace
            or type(runtime_result["runtime_ref"].get("runtime_id")) is not str
            or runtime_result["harness_id"] != lab.native.harness_id
            or runtime_result["installed"] is not True
            or runtime_result["enabled"] is not True
            or runtime_result["execution_ready"] is not True
            or runtime_result["provider"] != "available"
        ):
            raise ProductTargetError("fresh_runtime_result_refused")
        if lab.backend is None or not lab.backend.thread.is_alive():
            raise ProductTargetError("fresh_backend_restart_refused")
        lab.owned["backend_thread_ident_after_registration"] = lab.backend.thread.ident
        complete_fresh_stage(
            lab.admission,
            "runtime_registration",
            runtime_intent,
            runtime_result,
        )
    elif type(runtime_saved) is dict:
        runtime_result = _require_result(
            runtime_saved,
            {
                "runtime_ref",
                "harness_id",
                "installed",
                "enabled",
                "execution_ready",
                "provider",
            },
            "fresh_stage_result_refused",
        )
        current_body = r"""
      const listed = await readJson(page, `${agentBase}/runtimes`);
      const matches = listed.items.filter((item) => item.harness_id === config.harness_id);
      const status = await readJson(page, "/product/v1/status");
      const health = await readJson(page, "/main-agent/v1/health");
      if (matches.length !== 1) throw new Error("fresh_runtime_resume_refused");
      return {
        runtime_ref: matches[0].ref,
        harness_id: matches[0].harness_id,
        installed: matches[0].installed,
        enabled: matches[0].enabled,
        execution_ready: status.execution?.ready,
        provider: health.provider,
      };
"""
        current = _run_stage(
            lab,
            current_body,
            task_space_id=space_id,
            variables={"harness_id": lab.native.harness_id},
        )
        if current != runtime_result:
            raise ProductTargetError("fresh_runtime_resume_refused")
    else:
        raise ProductTargetError("fresh_stage_result_refused")
    runtime_ref = runtime_result.get("runtime_ref")
    if (
        type(runtime_ref) is not dict
        or runtime_ref.get("workspace_id") != workspace
        or type(runtime_ref.get("runtime_id")) is not str
    ):
        raise ProductTargetError("fresh_stage_result_refused")
    runtime_id = runtime_ref["runtime_id"]

    profile_intent = {
        "workspace_id": workspace,
        "runtime_id": runtime_id,
        "name": _PROFILE_NAME,
        "description": _PROFILE_DESCRIPTION_R1,
        "instructions_sha256": hashlib.sha256(
            _PROFILE_INSTRUCTIONS.encode("utf-8")
        ).hexdigest(),
        "secret_refs": [],
        "resource_refs": [],
    }
    profile_started, profile_saved = begin_fresh_stage(
        lab.admission, "profile_create", profile_intent
    )
    if profile_started:
        profile_body = r"""
      await page.goto(`${config.web_url}/workspaces/${workspace}/agents/new`);
      await page.waitForSelector("loc=role:heading[name='新建 Profile']", { state: "visible" });
      await page.fill("loc=role:textbox[name='名称']", config.profile_name);
      await page.fill("loc=role:textbox[name='描述']", config.profile_description);
      await page.selectOption("loc=role:combobox[name='运行环境']", {
        value: config.runtime_id,
      });
      await page.fill("loc=role:textbox[name='指令']", config.instructions);
      await page.fill("loc=role:spinbutton[name='并行安排上限']", "2");
      await page.click("loc=role:button[name='保存 Profile']");
      await page.waitForSelector("text=配置已保存 · 仅用于新安排", { state: "visible" });
      await page.click(`loc=css:.breadcrumb a[href='/workspaces/${workspace}/agents']`);
      await page.waitForSelector(`loc=role:link[name='${config.profile_name}']`, { state: "visible" });
      await page.click(`loc=role:link[name='${config.profile_name}']`);
      await page.waitForURL((url) => /\/agents\/[0-9a-f-]{36}$/i.test(url.pathname));
      const profileId = new URL(await page.url()).pathname.split("/").at(-1);
      const profile = await readJson(
        page,
        `${agentBase}/profiles/${encodeURIComponent(profileId)}?revision=1`,
      );
      if (profile.profile.ref.workspace_id !== workspace
        || profile.profile.ref.profile_id !== profileId
        || profile.profile.ref.revision !== 1
        || profile.profile.draft.secret_refs.length !== 0
        || profile.profile.draft.resource_refs.length !== 0) {
        throw new Error("fresh_profile_creation_refused");
      }
      await page.goto(`${config.web_url}/workspaces/${workspace}/agents/${profileId}?view=settings`);
      await page.waitForSelector("loc=role:button[name='配置凭据引用']", { state: "visible" });
      await page.click("loc=role:button[name='配置凭据引用']");
      await page.waitForSelector("text=尚无凭据", { state: "visible" });
      await page.goto(`${config.web_url}/workspaces/${workspace}/agents/${profileId}?view=capabilities&capability=instructions`);
      await page.waitForSelector("loc=role:link[name='Skills']", { state: "visible" });
      const capabilityViews = [];
      for (const [key, label] of [["skill", "Skills"], ["mcp", "MCP"], ["integration", "Integrations"]]) {
        await page.click(`loc=role:link[name='${label}']`);
        await page.waitForURL((url) => url.searchParams.get("capability") === key);
        await page.snapshot();
        capabilityViews.push(key);
      }
      await page.goto(`${config.web_url}/workspaces/${workspace}/agents/${profileId}`);
      await page.waitForSelector(`loc=role:heading[name='${config.profile_name}']`, { state: "visible" });
      const geometry = [];
      for (const width of [1280, 1440, 1600]) {
        await page.cdp("Emulation.setDeviceMetricsOverride", {
          width,
          height: 1000,
          deviceScaleFactor: 1,
          mobile: false,
          dontSetVisibleSize: true,
        });
        await page.waitForFunction(({ width: expectedWidth }) =>
          window.innerWidth === expectedWidth && window.innerHeight === 1000,
        { width }, { timeout: 10_000 });
        await page.snapshot();
        const facts = await page.evaluate(() => {
          const root = document.documentElement;
          const panel = document.querySelector("section.agent-page");
          const bounds = panel?.getBoundingClientRect();
          return {
            viewport_width: window.innerWidth,
            document_overflow_x: root.scrollWidth > window.innerWidth,
            panel_bounds_finite: bounds !== undefined && bounds !== null
              && Number.isFinite(bounds.left) && Number.isFinite(bounds.right)
              && Number.isFinite(bounds.top) && Number.isFinite(bounds.bottom),
          };
        });
        if (facts.viewport_width !== width || facts.panel_bounds_finite !== true) {
          throw new Error("fresh_profile_geometry_refused");
        }
        const screenshot = config.screenshots[String(width)];
        await page.screenshot({ path: screenshot });
        geometry.push({ width, document_overflow_x: facts.document_overflow_x });
      }
      return {
        profile_id: profileId,
        revision: 1,
        workspace_id: workspace,
        runtime_id: config.runtime_id,
        credential_metadata_empty: true,
        capability_views: ["instructions", ...capabilityViews],
        screenshots: config.screenshots,
        geometry,
      };
        """
        screenshot_dir = lab.settings.artifact_root
        screenshot_paths = {
            str(width): str(screenshot_dir / f"profile-r1-{width}.png")
            for width in (1280, 1440, 1600)
        }
        profile_result = _run_stage(
            lab,
            profile_body,
            task_space_id=space_id,
            screenshots=screenshot_paths,
            variables={
                "runtime_id": runtime_id,
                "profile_name": _PROFILE_NAME,
                "profile_description": _PROFILE_DESCRIPTION_R1,
                "instructions": _PROFILE_INSTRUCTIONS,
            },
        )
        profile_result = _require_result(
            profile_result,
            {
                "profile_id",
                "revision",
                "workspace_id",
                "runtime_id",
                "credential_metadata_empty",
                "capability_views",
                "screenshots",
                "geometry",
            },
            "fresh_profile_result_refused",
        )
        profile_id = profile_result.get("profile_id")
        if (
            type(profile_id) is not str
            or profile_result.get("revision") != 1
            or profile_result.get("workspace_id") != workspace
            or profile_result.get("runtime_id") != runtime_id
            or profile_result.get("credential_metadata_empty") is not True
            or profile_result.get("capability_views")
            != ["instructions", "skill", "mcp", "integration"]
            or profile_result.get("screenshots") != screenshot_paths
            or type(profile_result.get("geometry")) is not list
            or len(profile_result["geometry"]) != 3
        ):
            raise ProductTargetError("fresh_profile_result_refused")
        for path in screenshot_paths.values():
            if not Path(path).is_file() or Path(path).stat().st_size == 0:
                raise ProductTargetError("fresh_screenshot_refused")
        profile_ref = _profile_ref(workspace_id, profile_id, 1)
        complete_fresh_stage(
            lab.admission,
            "profile_create",
            profile_intent,
            profile_result,
        )
    elif type(profile_saved) is dict:
        profile_result = profile_saved
        profile_ref = _profile_ref(
            workspace_id, str(profile_result.get("profile_id")), 1
        )
        profile_id = profile_ref["profile_id"]
    else:
        raise ProductTargetError("fresh_stage_result_refused")
    if type(profile_id) is not str:
        raise ProductTargetError("fresh_stage_result_refused")

    defaults_intent = {
        "workspace_id": workspace,
        "profile_ref": profile_ref,
        "roles": ["main_profile_id", "planner_profile_id", "worker_profile_id"],
    }
    defaults_started, defaults_saved = begin_fresh_stage(
        lab.admission, "defaults_save", defaults_intent
    )
    if defaults_started:
        defaults_body = r"""
      await page.goto(`${config.web_url}/workspaces/${workspace}/agents`);
      await page.waitForSelector("loc=role:button[name='默认安排']", { state: "visible" });
      await page.click("loc=role:button[name='默认安排']");
      await page.waitForSelector("loc=css:section.agent-defaults", { state: "visible" });
      for (const role of ["默认 Main", "默认 Planner", "默认 Worker"]) {
        await page.selectOption(`loc=role:combobox[name='${role}']`, {
          label: `${config.profile_name} · R1`,
        });
      }
      await page.click("loc=role:button[name='保存默认安排']");
      await page.waitForSelector("text=默认安排已保存 · 仅用于新安排", { state: "visible" });
      const defaults = await readJson(page, `${productBase}/defaults`);
      if (defaults.main_profile_id !== config.profile_id
        || defaults.planner_profile_id !== config.profile_id
        || defaults.worker_profile_id !== config.profile_id) {
        throw new Error("fresh_defaults_result_refused");
      }
      return {
        workspace_id: workspace,
        version: defaults.version,
        main_profile_id: defaults.main_profile_id,
        planner_profile_id: defaults.planner_profile_id,
        worker_profile_id: defaults.worker_profile_id,
      };
"""
        defaults_result = _run_stage(
            lab,
            defaults_body,
            task_space_id=space_id,
            variables={"profile_id": profile_id, "profile_name": _PROFILE_NAME},
        )
        defaults_result = _require_result(
            defaults_result,
            {
                "workspace_id",
                "version",
                "main_profile_id",
                "planner_profile_id",
                "worker_profile_id",
            },
            "fresh_defaults_result_refused",
        )
        if (
            defaults_result.get("workspace_id") != workspace
            or type(defaults_result.get("version")) is not int
            or any(
                defaults_result.get(key) != profile_id
                for key in (
                    "main_profile_id",
                    "planner_profile_id",
                    "worker_profile_id",
                )
            )
        ):
            raise ProductTargetError("fresh_defaults_result_refused")
        complete_fresh_stage(
            lab.admission,
            "defaults_save",
            defaults_intent,
            defaults_result,
        )
    elif type(defaults_saved) is dict:
        defaults_result = defaults_saved
    else:
        raise ProductTargetError("fresh_stage_result_refused")

    _exercise_profile_stop_isolation(lab, space_id=space_id, profile_ref=profile_ref)


def _exercise_profile_stop_isolation(
    lab: FreshDesktopLab,
    *,
    space_id: int,
    profile_ref: dict[str, object],
    unrelated_page_label: str | None = None,
    capture_overview: bool = True,
) -> None:
    workspace_id = lab.workspace_id
    workspace = str(workspace_id)
    profile_id = profile_ref.get("profile_id")
    if (
        type(profile_id) is not str
        or profile_ref != _profile_ref(workspace_id, profile_id, 1)
        or type(space_id) is not int
        or space_id < 1
        or type(capture_overview) is not bool
        or unrelated_page_label is not None
        and (
            type(unrelated_page_label) is not str
            or re.fullmatch(r"p[2-9][0-9]*", unrelated_page_label) is None
        )
    ):
        raise ProductTargetError("fresh_stop_suffix_context_refused")

    profile_test_intent = {
        "profile_ref": profile_ref,
        "input_sha256": hashlib.sha256(_PROFILE_TEST_INPUT.encode("utf-8")).hexdigest(),
        "context_refs": [],
        "turn_count": 1,
    }
    test_body = r"""
      await page.goto(`${config.web_url}/workspaces/${workspace}/agents/${config.profile_id}`);
      await page.waitForSelector("loc=role:button[name='测试已保存 Profile']", { state: "visible" });
      await page.click("loc=role:button[name='测试已保存 Profile']");
      await page.waitForSelector("loc=role:dialog[name='测试 Profile']", { state: "visible" });
      await page.fill("loc=role:textbox[name='测试文本']", config.test_input);
      await page.click("loc=role:button[name='开始测试']");
      await page.waitForFunction(() => {
        const dialog = document.querySelector("[role=dialog][aria-label='测试 Profile']");
        return /会话\s+[0-9a-f-]{36}/i.test(dialog?.textContent ?? "");
      }, undefined, { timeout: 30_000 });
      const threadId = await profileTestThreadId(page);
      const thread = await readThread(page, threadId, (value) =>
        Array.isArray(value.turns) && value.turns.length === 1 && value.turns[0].runId !== null,
      );
      const turn = thread.turns[0];
      if (thread.contextRefs.length !== 0
        || !exactProfileRef(thread.profileRef, config.profile_id, 1)
        || !exactProfileRef(turn.profileRef, config.profile_id, 1)
        || typeof turn.runId !== "string"
        || typeof turn.operationId !== "string") {
        throw new Error("fresh_profile_test_pin_refused");
      }
      return {
        workspace_id: workspace,
        profile_id: config.profile_id,
        profile_revision: 1,
        thread_id: thread.id,
        turn_id: turn.id,
        run_id: turn.runId,
        operation_id: turn.operationId,
        response_id: turn.responseId,
        context_refs: thread.contextRefs,
        turn_count: thread.turns.length,
        status: turn.status,
        selected_model: config.selected_model,
        observed_model: null,
      };
"""
    profile_test_started, profile_test_result = _run_fresh_input_stage(
        lab.admission,
        "profile_test",
        profile_test_intent,
        lambda: _run_stage(
            lab,
            test_body,
            task_space_id=space_id,
            variables={
                "profile_id": profile_id,
                "test_input": _PROFILE_TEST_INPUT,
                "selected_model": None,
            },
        ),
    )
    profile_test_result = _require_result(
        profile_test_result,
        {
            "workspace_id",
            "profile_id",
            "profile_revision",
            "thread_id",
            "turn_id",
            "run_id",
            "operation_id",
            "response_id",
            "context_refs",
            "turn_count",
            "status",
            "selected_model",
            "observed_model",
        },
        "fresh_profile_test_result_refused",
    )
    if (
        profile_test_result.get("workspace_id") != workspace
        or profile_test_result.get("profile_id") != profile_id
        or profile_test_result.get("profile_revision") != 1
        or type(profile_test_result.get("thread_id")) is not str
        or type(profile_test_result.get("turn_id")) is not str
        or type(profile_test_result.get("run_id")) is not str
        or type(profile_test_result.get("operation_id")) is not str
        or profile_test_result.get("context_refs") != []
        or profile_test_result.get("turn_count") != 1
        or profile_test_result.get("selected_model") is not None
        or profile_test_result.get("observed_model") is not None
    ):
        raise ProductTargetError("fresh_profile_test_result_refused")
    test_run_id = UUID(profile_test_result["run_id"])
    if not lab.native.wait_for_prompt(test_run_id):
        raise ProductTargetError("fresh_profile_test_not_entered")
    if profile_test_started:
        if not _active_run(lab, profile_test_result["run_id"]):
            raise ProductTargetError("fresh_profile_test_not_active")
        complete_fresh_stage(
            lab.admission,
            "profile_test",
            profile_test_intent,
            profile_test_result,
        )

    unrelated_intent = {
        "profile_ref": profile_ref,
        "input_sha256": hashlib.sha256(_UNRELATED_INPUT.encode("utf-8")).hexdigest(),
        "context_refs": [],
        "turn_count": 1,
    }
    if unrelated_page_label is None:
        unrelated_page_label = _ensure_unrelated_page(lab, space_id)
    unrelated_body = r"""
      const page2 = task.page(config.unrelated_page_label);
      await page2.goto(`${config.web_url}/workspaces/${workspace}/chat?${new URLSearchParams({
        profile_id: config.profile_id,
        profile_revision: "1",
      })}`);
      await page2.waitForSelector("loc=role:button[name='使用该 Profile 新建对话']", { state: "visible" });
      await page2.click("loc=role:button[name='使用该 Profile 新建对话']");
      await page2.waitForURL((url) => /\/chat\/[0-9a-f-]{36}$/i.test(url.pathname));
      const threadId = threadIdFromUrl(await page2.url());
      await page2.fill("loc=role:textbox[name='给 Main Agent 的消息']", config.unrelated_input);
      await page2.click("loc=role:button[name='发送消息']");
      await page2.waitForSelector("text=TURN ACTIVE", { state: "visible", timeout: 30_000 });
      const thread = await readThread(page2, threadId, (value) =>
        Array.isArray(value.turns) && value.turns.length === 1 && value.turns[0].runId !== null,
      );
      const turn = thread.turns[0];
      if (thread.contextRefs.length !== 0
        || !exactProfileRef(thread.profileRef, config.profile_id, 1)
        || !exactProfileRef(turn.profileRef, config.profile_id, 1)
        || typeof turn.runId !== "string"
        || typeof turn.operationId !== "string") {
        throw new Error("fresh_unrelated_pin_refused");
      }
      return {
        workspace_id: workspace,
        page_label: page2.label,
        profile_id: config.profile_id,
        profile_revision: 1,
        thread_id: thread.id,
        turn_id: turn.id,
        run_id: turn.runId,
        operation_id: turn.operationId,
        context_refs: thread.contextRefs,
        turn_count: thread.turns.length,
        status: turn.status,
      };
"""
    unrelated_started, unrelated_result = _run_fresh_input_stage(
        lab.admission,
        "unrelated_conversation",
        unrelated_intent,
        lambda: _run_stage(
            lab,
            unrelated_body,
            task_space_id=space_id,
            variables={
                "profile_id": profile_id,
                "unrelated_input": _UNRELATED_INPUT,
                "unrelated_page_label": unrelated_page_label,
            },
        ),
    )
    unrelated_result = _require_result(
        unrelated_result,
        {
            "workspace_id",
            "page_label",
            "profile_id",
            "profile_revision",
            "thread_id",
            "turn_id",
            "run_id",
            "operation_id",
            "context_refs",
            "turn_count",
            "status",
        },
        "fresh_unrelated_result_refused",
    )
    if (
        unrelated_result.get("workspace_id") != workspace
        or unrelated_result.get("page_label") != unrelated_page_label
        or unrelated_result.get("profile_id") != profile_id
        or unrelated_result.get("profile_revision") != 1
        or type(unrelated_result.get("thread_id")) is not str
        or type(unrelated_result.get("turn_id")) is not str
        or type(unrelated_result.get("run_id")) is not str
        or type(unrelated_result.get("operation_id")) is not str
        or unrelated_result.get("context_refs") != []
        or unrelated_result.get("turn_count") != 1
    ):
        raise ProductTargetError("fresh_unrelated_result_refused")
    unrelated_run_id = UUID(unrelated_result["run_id"])
    if unrelated_run_id == test_run_id or not lab.native.wait_for_prompt(
        unrelated_run_id
    ):
        raise ProductTargetError("fresh_unrelated_not_entered")
    if unrelated_started:
        if not _active_run(lab, unrelated_result["run_id"]):
            raise ProductTargetError("fresh_unrelated_not_active")
        complete_fresh_stage(
            lab.admission,
            "unrelated_conversation",
            unrelated_intent,
            unrelated_result,
        )

    revision_intent = {
        "parent_ref": profile_ref,
        "description": _PROFILE_DESCRIPTION_R2,
        "description_sha256": hashlib.sha256(
            _PROFILE_DESCRIPTION_R2.encode("utf-8")
        ).hexdigest(),
    }
    revision_started, revision_saved = begin_fresh_stage(
        lab.admission, "profile_revision", revision_intent
    )
    if revision_started:
        revision_body = r"""
      const page2 = task.page(config.unrelated_page_label);
      await page2.goto(`${config.web_url}/workspaces/${workspace}/agents/${config.profile_id}?view=settings`);
      await page2.waitForSelector("loc=role:textbox[name='描述']", { state: "visible" });
      await page2.fill("loc=role:textbox[name='描述']", config.description_r2);
      await page2.click("loc=role:button[name='保存 Profile']");
      await page2.waitForSelector("text=配置已保存 · 仅用于新安排", { state: "visible" });
      const profile = await readJson(
        page2,
        `${agentBase}/profiles/${encodeURIComponent(config.profile_id)}`,
      );
      if (profile.profile.ref.workspace_id !== workspace
        || profile.profile.ref.profile_id !== config.profile_id
        || profile.profile.ref.revision !== 2
        || profile.profile.draft.description !== config.description_r2) {
        throw new Error("fresh_profile_revision_refused");
      }
      const originalThread = await readJson(
        page2,
        `/main-agent/v1/threads/${encodeURIComponent(config.test_thread_id)}`,
      );
      if (!exactProfileRef(originalThread.profileRef, config.profile_id, 1)
        || originalThread.turns.length !== 1
        || originalThread.turns[0].id !== config.test_turn_id
        || !exactProfileRef(originalThread.turns[0].profileRef, config.profile_id, 1)) {
        throw new Error("fresh_original_pin_changed");
      }
      await assertProfileTestDialogPin(page, config.test_thread_id, 1);
      const conversation = await readJson(
        page2,
        `/main-agent/v1/threads/${encodeURIComponent(config.unrelated_thread_id)}`,
      );
      if (!exactProfileRef(conversation.profileRef, config.profile_id, 1)
        || conversation.turns.length !== 1
        || conversation.turns[0].id !== config.unrelated_turn_id) {
        throw new Error("fresh_unrelated_pin_changed");
      }
      return {
        workspace_id: workspace,
        profile_id: profile.profile.ref.profile_id,
        revision: profile.profile.ref.revision,
        parent_revision: 1,
        original_thread_id: originalThread.id,
        original_thread_revision: originalThread.profileRef.revision,
        original_turn_id: originalThread.turns[0].id,
        unrelated_thread_id: conversation.id,
        unrelated_thread_revision: conversation.profileRef.revision,
      };
"""
        revision_result = _run_stage(
            lab,
            revision_body,
            task_space_id=space_id,
            variables={
                "profile_id": profile_id,
                "description_r2": _PROFILE_DESCRIPTION_R2,
                "test_thread_id": profile_test_result["thread_id"],
                "test_turn_id": profile_test_result["turn_id"],
                "unrelated_thread_id": unrelated_result["thread_id"],
                "unrelated_turn_id": unrelated_result["turn_id"],
                "unrelated_page_label": unrelated_page_label,
            },
        )
        revision_result = _require_result(
            revision_result,
            {
                "workspace_id",
                "profile_id",
                "revision",
                "parent_revision",
                "original_thread_id",
                "original_thread_revision",
                "original_turn_id",
                "unrelated_thread_id",
                "unrelated_thread_revision",
            },
            "fresh_revision_result_refused",
        )
        if (
            revision_result.get("workspace_id") != workspace
            or revision_result.get("profile_id") != profile_id
            or revision_result.get("revision") != 2
            or revision_result.get("parent_revision") != 1
            or revision_result.get("original_thread_id")
            != profile_test_result["thread_id"]
            or revision_result.get("original_thread_revision") != 1
            or revision_result.get("original_turn_id") != profile_test_result["turn_id"]
            or revision_result.get("unrelated_thread_id")
            != unrelated_result["thread_id"]
            or revision_result.get("unrelated_thread_revision") != 1
        ):
            raise ProductTargetError("fresh_revision_result_refused")
        if not _active_run(lab, profile_test_result["run_id"]):
            raise ProductTargetError("fresh_profile_test_lost_before_stop")
        if not _active_run(lab, unrelated_result["run_id"]):
            raise ProductTargetError("fresh_unrelated_lost_before_stop")
        complete_fresh_stage(
            lab.admission,
            "profile_revision",
            revision_intent,
            revision_result,
        )
    elif type(revision_saved) is dict:
        revision_result = revision_saved
    else:
        raise ProductTargetError("fresh_stage_result_refused")

    final_intent = {
        "test_thread_id": profile_test_result["thread_id"],
        "test_turn_id": profile_test_result["turn_id"],
        "test_run_id": profile_test_result["run_id"],
        "unrelated_thread_id": unrelated_result["thread_id"],
        "unrelated_turn_id": unrelated_result["turn_id"],
        "unrelated_run_id": unrelated_result["run_id"],
        "space_id": space_id,
    }
    final_started, final_saved = begin_fresh_stage(
        lab.admission, "final_cleanup", final_intent
    )
    if final_started:
        stop_result = _run_stage(
            lab,
            _PROFILE_STOP_BODY,
            task_space_id=space_id,
            variables={
                "profile_id": profile_id,
                "profile_revision": profile_ref["revision"],
                "test_thread_id": profile_test_result["thread_id"],
                "test_turn_id": profile_test_result["turn_id"],
                "unrelated_thread_id": unrelated_result["thread_id"],
                "unrelated_turn_id": unrelated_result["turn_id"],
                "unrelated_run_id": unrelated_result["run_id"],
                "unrelated_page_label": unrelated_page_label,
            },
        )
        stop_result = _require_result(
            stop_result,
            {
                "test_thread_id",
                "test_turn_id",
                "test_profile_revision",
                "test_turn_status",
                "unrelated_thread_id",
                "unrelated_turn_id",
                "unrelated_profile_revision",
                "unrelated_run_id",
                "unrelated_turn_status_after_test_stop",
            },
            "fresh_final_stop_result_refused",
        )
        if (
            stop_result.get("test_thread_id") != profile_test_result["thread_id"]
            or stop_result.get("test_turn_id") != profile_test_result["turn_id"]
            or stop_result.get("test_profile_revision") != 1
            or stop_result.get("unrelated_thread_id") != unrelated_result["thread_id"]
            or stop_result.get("unrelated_turn_id") != unrelated_result["turn_id"]
            or stop_result.get("unrelated_profile_revision") != 1
            or stop_result.get("unrelated_run_id") != unrelated_result["run_id"]
            or stop_result.get("unrelated_turn_status_after_test_stop")
            not in {"queued", "in_progress"}
        ):
            raise ProductTargetError("fresh_final_stop_result_refused")
        if not lab.native.wait_for_finish(test_run_id):
            raise ProductTargetError("fresh_profile_stop_not_finished")
        if _active_run(lab, profile_test_result["run_id"]):
            raise ProductTargetError("fresh_profile_stop_still_active")
        if not _active_run(lab, unrelated_result["run_id"]):
            raise ProductTargetError("fresh_stop_isolation_refused")
        with lab.native.lock:
            if test_run_id not in lab.native.cancelled_runs:
                raise ProductTargetError("fresh_profile_cancel_unconfirmed")
        if not lab.native.release_run(unrelated_run_id):
            raise ProductTargetError("fresh_unrelated_release_refused")
        if not lab.native.wait_for_finish(unrelated_run_id):
            raise ProductTargetError("fresh_unrelated_finish_refused")

        if capture_overview:
            _capture_recent_run_overview(
                lab,
                task_space_id=space_id,
                profile_id=profile_id,
                profile_test_result=profile_test_result,
                unrelated_result=unrelated_result,
            )

        finish_body = r"""
      const page2 = task.page(config.unrelated_page_label);
      const completed = await readThread(page2, config.unrelated_thread_id, (value) =>
        Array.isArray(value.turns) && value.turns.length === 1
          && !["queued", "in_progress"].includes(value.turns[0].status),
      );
      if (!exactProfileRef(completed.profileRef, config.profile_id, 1)
        || completed.turns[0].id !== config.unrelated_turn_id) {
        throw new Error("fresh_unrelated_cleanup_refused");
      }
      await page2.close();
      await page.goto("about:blank");
      await page.waitForURL("about:blank");
      const tabs = await task.tabs();
      if (await page.url() !== "about:blank"
        || tabs.some((tab) => tab.label === config.unrelated_page_label)) {
        throw new Error("fresh_browser_page_cleanup_refused");
      }
      return {
        space_id: task.spaceId,
        retained_page: page.label,
        retained_url: await page.url(),
        closed_unrelated_page: true,
        unrelated_thread_id: completed.id,
        unrelated_turn_id: completed.turns[0].id,
        unrelated_turn_status: completed.turns[0].status,
      };
"""
        finish_result = _run_stage(
            lab,
            finish_body,
            task_space_id=space_id,
            variables={
                "profile_id": profile_id,
                "unrelated_thread_id": unrelated_result["thread_id"],
                "unrelated_turn_id": unrelated_result["turn_id"],
                "unrelated_page_label": unrelated_page_label,
            },
        )
        finish_result = _require_result(
            finish_result,
            {
                "space_id",
                "retained_page",
                "retained_url",
                "closed_unrelated_page",
                "unrelated_thread_id",
                "unrelated_turn_id",
                "unrelated_turn_status",
            },
            "fresh_final_cleanup_result_refused",
        )
        if (
            finish_result.get("space_id") != space_id
            or finish_result.get("retained_page") != "p1"
            or finish_result.get("retained_url") != "about:blank"
            or finish_result.get("closed_unrelated_page") is not True
            or finish_result.get("unrelated_thread_id") != unrelated_result["thread_id"]
            or finish_result.get("unrelated_turn_id") != unrelated_result["turn_id"]
            or finish_result.get("unrelated_turn_status") != "completed"
        ):
            raise ProductTargetError("fresh_final_cleanup_result_refused")
        if _active_run(lab, unrelated_result["run_id"]):
            raise ProductTargetError("fresh_unrelated_still_active")
        final_result = {
            **stop_result,
            "profile_test_stop_confirmed": True,
            "profile_test_native_finished": True,
            "unrelated_remained_active_after_profile_stop": True,
            "retained_task_space_id": space_id,
            "retained_page": "p1",
            "retained_url": "about:blank",
            "unrelated_turn_status_after_cleanup": finish_result[
                "unrelated_turn_status"
            ],
        }
        complete_fresh_stage(
            lab.admission,
            "final_cleanup",
            final_intent,
            final_result,
        )
    elif type(final_saved) is dict:
        final_result = final_saved
    else:
        raise ProductTargetError("fresh_stage_result_refused")

    lab.owned["browser_space_id"] = space_id
    lab.owned["profile_ref_r1"] = profile_ref
    lab.owned["profile_ref_r2"] = _profile_ref(workspace_id, profile_id, 2)
    lab.owned["profile_test"] = {
        "thread_id": profile_test_result["thread_id"],
        "turn_id": profile_test_result["turn_id"],
        "run_id": profile_test_result["run_id"],
        "operation_id": profile_test_result["operation_id"],
        "profile_revision": 1,
        "selected_model": None,
        "observed_model": None,
    }
    lab.owned["unrelated_conversation"] = {
        "thread_id": unrelated_result["thread_id"],
        "turn_id": unrelated_result["turn_id"],
        "run_id": unrelated_result["run_id"],
        "operation_id": unrelated_result["operation_id"],
        "profile_revision": 1,
    }
    lab.owned["final_cleanup"] = final_result
    assert not re.search(r"token|secret_value", json.dumps(lab.owned), re.IGNORECASE)
