import { Code2 } from "lucide-react";
import type { ReactNode } from "react";

type ProviderExtensionRenderer = (value: unknown) => ReactNode;

const renderers = new Map<string, ProviderExtensionRenderer>([
  ["trace", (value) => <span className="extension-chip">{String(value)}</span>],
]);

export function ProviderExtensions({
  extensions,
}: {
  extensions: Record<string, unknown>;
}) {
  const entries = Object.entries(extensions);
  if (entries.length === 0) {
    return <span className="muted-copy">没有扩展字段</span>;
  }
  return (
    <div className="provider-extensions">
      {entries.map(([namespace, value]) => {
        const renderer = renderers.get(namespace.split(":", 1)[0] ?? namespace);
        return (
          <div className="provider-extension" key={namespace}>
            <span className="provider-extension__name"><Code2 size={13} aria-hidden="true" />{namespace}</span>
            {renderer?.(value) ?? <SafeJson value={value} />}
          </div>
        );
      })}
    </div>
  );
}

function SafeJson({ value }: { value: unknown }) {
  let output: string;
  try {
    output = JSON.stringify(value, null, 2) ?? "null";
  } catch {
    output = "unserializable";
  }
  return <pre className="safe-json">{output.slice(0, 8_192)}</pre>;
}
