import { useInfiniteQuery } from "@tanstack/react-query";
import { useLayoutEffect, useMemo, useRef } from "react";

import {
  NativeTraceUnavailableError,
  nativeTracePinKey,
  readNativeTracePage,
  traceReadError,
  validateNativeTracePage,
  type NativeTracePin,
} from "./nativeTrace";
import type { NativeRunEvent } from "./types";

export function useNativeTrace({ pin }: { pin: NativeTracePin | null }) {
  const scopeKey = pin === null ? null : nativeTracePinKey(pin);
  const activeScope = useRef<string | null>(scopeKey);
  useLayoutEffect(() => {
    activeScope.current = scopeKey;
    return () => { activeScope.current = null; };
  }, [scopeKey]);
  const query = useInfiniteQuery({
    queryKey: ["worker-native-trace", "native", scopeKey],
    enabled: pin !== null,
    retry: false,
    initialPageParam: 0,
    queryFn: ({ signal, pageParam }) => {
      if (pin === null) throw new NativeTraceUnavailableError();
      return readNativeTracePage(pin, { afterSequence: pageParam, signal });
    },
    getNextPageParam: (page, _pages, after) => page.has_more && page.next_sequence > after ? page.next_sequence : undefined,
  });
  const flattened = useMemo(() => {
    const events: NativeRunEvent[] = [];
    if (pin === null || !query.data) return { events, error: null };
    try {
      let cursor = 0;
      for (const [index, page] of query.data.pages.entries()) {
        if (query.data.pageParams[index] !== cursor) throw new NativeTraceUnavailableError();
        const validated = validateNativeTracePage(page, pin, cursor);
        events.push(...validated.events);
        cursor = validated.next_sequence;
        if (!validated.has_more && index < query.data.pages.length - 1) throw new NativeTraceUnavailableError();
      }
      return { events, error: null };
    } catch (error) {
      return { events: [], error: traceReadError(error) };
    }
  }, [pin, query.data]);
  const error = pin === null ? null : query.isError ? traceReadError(query.error) : flattened.error;
  const hasNext = pin !== null && !error && query.hasNextPage;
  const canReadNext = hasNext && !query.isFetching;
  const readNext = async () => {
    if (scopeKey === null || activeScope.current !== scopeKey || !canReadNext) return;
    await query.fetchNextPage();
  };
  const retry = async () => {
    if (scopeKey === null || activeScope.current !== scopeKey) return;
    await query.refetch();
  };
  return {
    pages: pin === null || error ? [] : query.data?.pages ?? [],
    events: error ? [] : flattened.events,
    loaded: pin !== null && !error && query.isSuccess,
    loading: pin !== null && query.isPending,
    isLoadingNext: query.isFetchingNextPage,
    hasNext,
    canReadNext,
    readNext,
    retry,
    error,
  };
}
