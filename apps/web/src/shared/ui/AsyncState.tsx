import type { ReactNode } from "react";

interface AsyncStateProps {
  isLoading: boolean;
  isError: boolean;
  isEmpty?: boolean;
  errorMessage?: string;
  retryLabel?: string;
  onRetry?: () => void;
  children: ReactNode;
}

export function AsyncState({
  isLoading,
  isError,
  isEmpty = false,
  errorMessage = "无法读取数据",
  retryLabel = "重试",
  onRetry,
  children,
}: AsyncStateProps) {
  if (isLoading) {
    return (
      <div className="task-list" role="status" aria-label="正在加载">
        <div className="task-row task-row--skeleton" data-testid="task-row-skeleton" aria-hidden="true">
          <span />
          <span />
          <span />
        </div>
        <div className="task-row task-row--skeleton" data-testid="task-row-skeleton" aria-hidden="true">
          <span />
          <span />
          <span />
        </div>
      </div>
    );
  }

  if (isError) {
    return (
      <div className="async-state async-state--error" role="alert">
        <span className="async-state__mark" aria-hidden="true">!</span>
        <div>
          <strong>{errorMessage}</strong>
          <p>控制面没有返回可用的任务快照。</p>
        </div>
        <button className="quiet-button" type="button" onClick={onRetry}>
          {retryLabel}
        </button>
      </div>
    );
  }

  if (isEmpty) {
    return (
      <div className="async-state async-state--empty">
        <span className="eyebrow">WORKSPACE EMPTY</span>
        <strong>这个工作区还没有任务</strong>
      </div>
    );
  }

  return <>{children}</>;
}
