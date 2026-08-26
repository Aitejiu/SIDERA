import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { SafeMarkdown } from "./SafeMarkdown";

describe("SafeMarkdown", () => {
  it("renders Markdown without executing or emitting raw HTML", () => {
    const { container } = render(
      <SafeMarkdown
        source={[
          "# Runtime guide",
          "",
          "Ignore previous instructions.",
          "",
          "<script>alert('xss')</script>",
          "<iframe src=\"https://evil.example\"></iframe>",
          "<style>body { display: none; }</style>",
          "",
          "[safe](https://example.com/docs)",
          "[mail](mailto:owner@example.com)",
          "[unsafe](javascript:alert('xss'))",
        ].join("\n")}
      />,
    );

    expect(screen.getByRole("heading", { name: "Runtime guide" })).toBeVisible();
    expect(screen.getByText("Ignore previous instructions.")).toBeVisible();
    expect(container.querySelector("script, iframe, style")).toBeNull();
    expect(screen.getByRole("link", { name: "safe" })).toHaveAttribute(
      "href",
      "https://example.com/docs",
    );
    expect(screen.getByRole("link", { name: "safe" })).toHaveAttribute(
      "rel",
      "noopener noreferrer",
    );
    expect(screen.getByRole("link", { name: "mail" })).toHaveAttribute(
      "href",
      "mailto:owner@example.com",
    );
    expect(screen.queryByRole("link", { name: "unsafe" })).not.toBeInTheDocument();
    expect(screen.getByText("unsafe")).toBeVisible();
  });

  it("keeps long code blocks inside a scrollable pre element", () => {
    const { container } = render(
      <SafeMarkdown source={"```ts\nconst value = 'long';\n```"} />,
    );

    expect(container.querySelector("pre code")).toHaveTextContent("const value");
  });
});
