import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { expect, it } from "vitest";

import { NativeTraceReader } from "./NativeTraceReader";
import type { NativeRunEvent } from "./types";

const events: NativeRunEvent[] = [
  { run_id: "80000000-0000-0000-0000-000000000001", sequence: 3, kind: "assistant.message",
    occurred_at: "2026-10-07T00:00:00Z", operation_id: null, response_id: null,
    data: { text: "Checking the dependency", raw: "<img src='https://untrusted.invalid/x' onerror='alert(1)'>" } },
  { run_id: "80000000-0000-0000-0000-000000000001", sequence: 8, kind: "tool.result",
    occurred_at: "2026-10-07T00:01:00Z", operation_id: "90000000-0000-0000-0000-000000000001",
    response_id: "90000000-0000-0000-0000-000000000002", data: { text: "Public tool output", value: 42 } },
];
let loads = 0;
function Reader({ items = events }: { items?: NativeRunEvent[] }) {
  const [selected, setSelected] = useState<number | null>(null);
  const [follow, setFollow] = useState(true);
  return <NativeTraceReader events={items} selectedSequence={follow ? items.at(-1)?.sequence ?? null : selected}
    onSelect={(event) => { setSelected(event.sequence); setFollow(false); }}
    onLoadNext={() => { loads += 1; }} hasNext isLoadingNext={false}
    followTail={follow} onFollowTail={() => setFollow(true)}
    onPauseFollow={() => { setSelected(items.at(-1)?.sequence ?? null); setFollow(false); }} />;
}

it("selects native kind/time/sequence and expands only escaped public data", async () => {
  const user = userEvent.setup(); render(<Reader />);
  let details = screen.getByRole("region", { name: "轨迹详情" });
  expect(details).toHaveTextContent("tool.result");
  expect(details).toHaveTextContent("#8"); expect(details).toHaveTextContent(events[1]!.operation_id!);
  await user.click(screen.getByRole("button", { name: /assistant.message.*#3/ }));
  details = screen.getByRole("region", { name: "轨迹详情" });
  expect(details).toHaveTextContent("assistant.message"); expect(details).toHaveTextContent("未提供");
  const data = within(details).getByLabelText("公开数据");
  expect(data).not.toBeVisible();
  await user.click(within(details).getByText("公开数据", { selector: "summary" }));
  expect(data).toBeVisible(); expect(data).toHaveTextContent("Checking the dependency");
  expect(data).toHaveTextContent("<img"); expect(document.querySelector("img")).toBeNull();
  expect(details.querySelector("time")).toHaveAttribute("dateTime", "2026-10-07T00:00:00Z");
  expect(details).not.toHaveTextContent("Checkpoint"); expect(details).not.toHaveTextContent("成功");
});

it("filters loaded kinds/text without fetching and hides unrelated selected detail", async () => {
  loads = 0; const user = userEvent.setup(); render(<Reader />);
  await user.type(screen.getByRole("searchbox", { name: "搜索已加载记录" }), "dependency");
  expect(screen.getByRole("button", { name: /assistant.message.*#3/ })).toBeVisible();
  expect(screen.queryByRole("button", { name: /tool.result.*#8/ })).not.toBeInTheDocument();
  expect(screen.getByRole("region", { name: "轨迹详情" })).not.toHaveTextContent("Public tool output");
  await user.selectOptions(screen.getByRole("combobox", { name: "记录类型" }), "tool.result");
  expect(screen.getByText("已加载记录中没有匹配项")).toBeVisible(); expect(loads).toBe(0);
  await user.click(screen.getByRole("button", { name: "清除筛选" }));
  expect(screen.getByRole("button", { name: /tool.result.*#8/ })).toBeVisible();
  expect(screen.getByRole("button", { name: "跟随尾部" })).toBeVisible();
});

it("keeps paused selection as new events arrive and resumes explicitly", async () => {
  const user = userEvent.setup(); const view = render(<Reader />);
  await user.click(screen.getByRole("button", { name: /assistant.message.*#3/ }));
  const newer = { ...events[1]!, sequence: 12, kind: "run.closed" };
  view.rerender(<Reader items={[...events, newer]} />);
  expect(screen.getByRole("heading", { name: "assistant.message" })).toBeVisible();
  await user.click(screen.getByRole("button", { name: "跟随尾部" }));
  expect(screen.getByRole("heading", { name: "run.closed" })).toBeVisible();
});

it("shows a truthful empty list", () => {
  render(<Reader items={[]} />);
  expect(screen.getByText("暂无轨迹记录")).toBeVisible();
  expect(screen.getByRole("region", { name: "轨迹详情" })).toHaveTextContent("选择一条记录查看详情");
});
