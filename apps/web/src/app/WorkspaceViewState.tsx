import { createContext, useCallback, useContext, useEffect, useLayoutEffect, useRef, useState, type ReactNode, type RefObject } from "react";
import { useLocation } from "react-router-dom";

type Position = { x: number; y: number };
type ReadingPosition = {
  tabId: string | null;
  window: Position;
  regions: Record<string, Position>;
};
type ViewMemory = {
  positions: Map<string, ReadingPosition>;
  details: Map<string, boolean>;
};

const ViewMemoryContext = createContext<ViewMemory | null>(null);

export function WorkspaceViewStateProvider({ openTabIds, children }: {
  openTabIds: readonly string[];
  children: ReactNode;
}) {
  const [memory] = useState<ViewMemory>(() => ({ positions: new Map(), details: new Map() }));
  const previousIds = useRef(openTabIds);

  useEffect(() => {
    const removed = new Set(previousIds.current.filter(id => !openTabIds.includes(id)));
    for (const id of removed) memory.details.delete(id);
    for (const [route, position] of memory.positions) {
      if (position.tabId !== null && removed.has(position.tabId)) memory.positions.delete(route);
    }
    previousIds.current = openTabIds;
  }, [memory, openTabIds]);

  return <ViewMemoryContext.Provider value={memory}>{children}</ViewMemoryContext.Provider>;
}

export function useTaskDetailsState(rootTaskId: string) {
  return useDetailsState(`root-task:${rootTaskId}`);
}

export function useSubtaskDetailsState(rootTaskId: string, subtaskKey: string) {
  return useDetailsState(`subtask:${rootTaskId}:${subtaskKey}`);
}

function useDetailsState(key: string) {
  const memory = useContext(ViewMemoryContext);
  const [open, setOpen] = useState(() => memory?.details.get(key) ?? false);
  const update = useCallback((next: boolean) => {
    memory?.details.set(key, next);
    setOpen(next);
  }, [key, memory]);
  return [open, update] as const;
}

export function useWorkerTimelineState(workerItemId: string) {
  const memory = useContext(ViewMemoryContext);
  const { pathname, search } = useLocation();
  const key = `worker-item:${workerItemId}`;
  const [initialScrollTop] = useState(() =>
    memory?.positions.get(`${pathname}${search}`)?.regions["worker-timeline"]?.y ?? null,
  );
  const [showSystemRecords, setShowSystemRecords] = useState(() => memory?.details.get(key) ?? false);
  const update = (next: boolean) => {
    memory?.details.set(key, next);
    setShowSystemRecords(next);
  };
  return { initialScrollTop, showSystemRecords, onSystemRecordsChange: update };
}

// Keep reading state, not mounted pages: hidden workers must not keep effects alive.
export function useWorkspaceReadingPosition(frameRef: RefObject<HTMLDivElement | null>, tabId: string | null) {
  const memory = useContext(ViewMemoryContext);
  const { pathname, search } = useLocation();
  const route = `${pathname}${search}`;

  useEffect(() => {
    const previous = window.history.scrollRestoration;
    window.history.scrollRestoration = "manual";
    return () => { window.history.scrollRestoration = previous; };
  }, []);

  useLayoutEffect(() => {
    const frame = frameRef.current;
    if (!memory || !frame) return;
    const saved = memory.positions.get(route);
    const target = saved ?? { tabId, window: { x: 0, y: 0 }, regions: {} };
    let restoring = true;
    let resizeObserver: ResizeObserver | undefined;

    const regions = () => Array.from(frame.querySelectorAll<HTMLElement>("[data-workspace-scroll]"));
    const capture = () => {
      if (restoring) return;
      const positions: Record<string, Position> = {};
      for (const element of regions()) {
        positions[element.dataset.workspaceScroll!] = { x: element.scrollLeft, y: element.scrollTop };
      }
      memory.positions.set(route, { tabId, window: { x: window.scrollX, y: window.scrollY }, regions: positions });
    };
    const stopRestoring = () => {
      restoring = false;
      resizeObserver?.disconnect();
      mutationObserver.disconnect();
    };
    const restore = () => {
      if (!restoring) return;
      let complete = true;
      const elements = regions();
      for (const [key, position] of Object.entries(target.regions)) {
        const element = elements.find(item => item.dataset.workspaceScroll === key);
        if (!element) {
          complete = false;
          continue;
        }
        element.scrollLeft = position.x;
        element.scrollTop = position.y;
        complete = complete && Math.abs(element.scrollTop - position.y) < 1 && Math.abs(element.scrollLeft - position.x) < 1;
      }
      if (window.scrollX !== target.window.x || window.scrollY !== target.window.y) {
        window.scrollTo({ left: target.window.x, top: target.window.y, behavior: "instant" });
      }
      complete = complete && Math.abs(window.scrollY - target.window.y) < 1 && Math.abs(window.scrollX - target.window.x) < 1;
      if (complete) stopRestoring();
    };
    const onIntent = () => {
      stopRestoring();
      capture();
    };
    const onScroll = (event: Event) => {
      if (event.target === document || event.target === window || (event.target instanceof Node && frame.contains(event.target))) capture();
    };

    // Async projections and virtualized activity may not have their final height yet.
    const mutationObserver = new MutationObserver(restore);
    mutationObserver.observe(frame, { childList: true, subtree: true, attributes: true, attributeFilter: ["style", "open"] });
    if (typeof ResizeObserver === "function") {
      resizeObserver = new ResizeObserver(restore);
      resizeObserver.observe(frame);
    }
    restore();
    document.addEventListener("scroll", onScroll, { capture: true, passive: true });
    const intentEvents = ["wheel", "touchstart", "pointerdown", "keydown"] as const;
    for (const event of intentEvents) document.addEventListener(event, onIntent, { capture: true, passive: true });
    return () => {
      stopRestoring();
      document.removeEventListener("scroll", onScroll, true);
      for (const event of intentEvents) document.removeEventListener(event, onIntent, true);
    };
  }, [frameRef, memory, route, tabId]);
}
