import { Send, Square } from "lucide-react";

export function ChatComposer({
  value,
  active,
  interruptAvailable,
  interruptReasonCode,
  disabled,
  errorMessage,
  onChange,
  onSend,
  onInterrupt,
}: {
  value: string;
  active: boolean;
  interruptAvailable: boolean;
  interruptReasonCode: string | null;
  disabled: boolean;
  errorMessage: string | null;
  onChange: (value: string) => void;
  onSend: () => void;
  onInterrupt: () => void;
}) {
  const inputDisabled = disabled || active;
  return (
    <section className="chat-composer" aria-label="发送消息">
      <textarea
        className="chat-composer__input"
        value={value}
        rows={3}
        disabled={inputDisabled}
        placeholder="输入要交给 Main Agent 的需求"
        aria-label="给 Main Agent 的消息"
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={(event) => {
          if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
            event.preventDefault();
            onSend();
          }
        }}
      />
      {errorMessage ? <div className="chat-composer__error" role="alert">{errorMessage}</div> : null}
      <div className="chat-composer__toolbar">
        <span className="mono-label">{active ? "TURN ACTIVE" : "READY"}</span>
        <div className="chat-composer__actions">
          {active ? (
            <button
              className="icon-button icon-button--danger"
              type="button"
              onClick={onInterrupt}
              disabled={disabled || !interruptAvailable}
              aria-label="中断当前回复"
              title={interruptAvailable ? "中断当前回复" : interruptReasonCode ?? "cancel_unconfirmed"}
            >
              <Square size={15} aria-hidden="true" />
            </button>
          ) : (
            <button
              className="primary-button chat-composer__send"
              type="button"
              onClick={onSend}
              disabled={inputDisabled || value.trim() === ""}
            >
              <Send size={15} aria-hidden="true" />发送消息
            </button>
          )}
        </div>
      </div>
    </section>
  );
}
