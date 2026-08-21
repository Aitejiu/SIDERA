import { QueryClientProvider } from "@tanstack/react-query";
import { createRoot } from "react-dom/client";
import { RouterProvider } from "react-router-dom";

import { queryClient } from "./app/queryClient";
import { router } from "./app/router";
import "./app/styles.css";

export function Main() {
  return (
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  );
}

export default Main;

const rootElement = document.getElementById("root");
if (rootElement === null) {
  throw new Error("SIDERA web root element is missing");
}

createRoot(rootElement).render(<Main />);
